using Microsoft.Win32;

namespace LocalAIHost;

/// <summary>
/// The four long-running processes of the stack, the same commands Start-LocalAI.ps1 and WebUI\start-webui.ps1 used:
///   wake-proxy  llama-wake.py on PROXY_PORT (it starts/stops llama-server itself via Start-LocalAI.ps1 -BackendOnly)
///   gateway     openclaw gateway run on GATEWAY_PORT (ExecGuard card patch before, guard_check.py after)
///   webui       Open WebUI on WEBUI_BACKEND_PORT (frontend patch before), P-cores
///   authproxy   WebUI\authproxy.py on WEBUI_PORT, P-cores
/// SUPERVISE_GATEWAY=false / SUPERVISE_WEBUI=false in config.env leave those out.
/// ControlServer (pipe \\.\pipe\LocalAIHost) lets ESP32-Helper and scripts drive the model and these processes.
/// </summary>
public sealed class Supervisor(LocalAIConfig cfg) : BackgroundService
{
    protected override async Task ExecuteAsync(CancellationToken ct)
    {
        var env = BuildEnvironment();
        var specs = new List<ChildSpec>();
        try
        {
            specs.Add(WakeProxy(env));
            if (cfg.GetBool("SUPERVISE_GATEWAY", true)) specs.Add(Gateway(env));
            if (cfg.GetBool("SUPERVISE_WEBUI", true)) specs.AddRange(WebUI(env));
        }
        catch (Exception e)
        {
            FileLog.Write($"config error: {e.Message}");
            throw;
        }
        FileLog.Write($"supervising: {string.Join(", ", specs.Select(s => $"{s.Name} :{s.Port}"))}");
        var children = specs.Select(s => new ManagedChild(s, env)).ToList();
        var control = new ControlServer(cfg, children, env, Python(env));
        await Task.WhenAll(children.Select(c => c.RunAsync(ct)).Append(control.RunAsync(ct)));
    }

    string Log(string file) => Path.Combine(cfg.LogDir, file);

    ChildSpec WakeProxy(IReadOnlyDictionary<string, string> env)
    {
        var py = Python(env);
        var args = new List<string>
        {
            Path.Combine(cfg.ScriptsDir, "llama-wake.py"),
            "--listen-port", cfg.GetInt("PROXY_PORT", 8080).ToString(),
            "--backend-port", cfg.GetInt("LLAMA_PORT", 8081).ToString(),
            "--ctx", cfg.GetInt("CONTEXT", 131072).ToString(),
            "--idle-minutes", cfg.Get("IDLE_UNLOAD_MINUTES", "20"),
            "--alias", cfg.Get("MODEL_ALIAS", "local-model"),
            "--launcher", Path.Combine(cfg.ScriptsDir, "Start-LocalAI.ps1"),
        };
        if (!cfg.GetBool("KEEP_AWAKE", true)) args.Add("--no-keep-awake");
        return new ChildSpec("wake-proxy", py, args, cfg.ScriptsDir, cfg.HostAddr, cfg.GetInt("PROXY_PORT", 8080),
            Log("wake-proxy.log"), Log("wake-proxy-err.log"));
    }

    ChildSpec Gateway(IReadOnlyDictionary<string, string> env)
    {
        var py = Python(env);
        var guard = Path.Combine(cfg.Home, "ExecGuard");
        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return new ChildSpec("gateway", Path.Combine(Environment.SystemDirectory, "cmd.exe"), ["/c", "openclaw gateway run"],
            profile, cfg.HostAddr, cfg.GetInt("GATEWAY_PORT", 18789), Log("gateway-stdout.log"), Log("gateway-stderr.log"))
        {
            // The Discord approval card patch lives in an npm package (an update wipes it); must run before the gateway starts.
            BeforeStart = ct => File.Exists(Path.Combine(guard, "patch_discord_card.py"))
                ? ManagedChild.RunToolAsync(py, [Path.Combine(guard, "patch_discord_card.py")], guard, Log("gateway-prestart.log"), env, TimeSpan.FromMinutes(1), ct)
                : Task.CompletedTask,
            // Fail-safe: exec mode "full" without exec-guard loaded falls back to "ask".
            AfterReady = ct => File.Exists(Path.Combine(guard, "guard_check.py"))
                ? ManagedChild.RunToolAsync(py, [Path.Combine(guard, "guard_check.py"), "--wait", "45"], guard, Log("gateway-prestart.log"), env, TimeSpan.FromMinutes(2), ct)
                : Task.CompletedTask,
        };
    }

    IEnumerable<ChildSpec> WebUI(IReadOnlyDictionary<string, string> env)
    {
        var root = cfg.Get("WEBUI_DIR", Path.Combine(cfg.Home, "WebUI"));
        var venvPy = Path.Combine(root, "venv", "Scripts", "python.exe");
        if (!File.Exists(venvPy)) { FileLog.Write($"[webui] {venvPy} not found; Open WebUI not supervised"); yield break; }
        var web = LocalAIConfig.ReadEnvFile(Path.Combine(root, ".env"));
        int pub = int.TryParse(web.GetValueOrDefault("WEBUI_PORT"), out var a) ? a : 8337;
        int back = int.TryParse(web.GetValueOrDefault("WEBUI_BACKEND_PORT"), out var b) ? b : 8338;
        // Intel Core Ultra 7 265K P-cores (CPUs 0,1,6-9,18,19): the model owns the E-cores.
        long pcores = cfg.GetMask("PCORE_MASK", 0xC03C3);
        var data = Path.Combine(root, "data");
        var logs = Path.Combine(root, "logs");
        Directory.CreateDirectory(data);

        var webEnv = new Dictionary<string, string>(LocalAIConfig.ReadEnvFile(Path.Combine(root, "openwebui.env")), StringComparer.OrdinalIgnoreCase)
        {
            ["DATA_DIR"] = data, ["HF_HOME"] = Path.Combine(data, "hf"), ["PYTHONUTF8"] = "1",
        };
        // pip's open-webui.exe embeds the venv's old absolute path and breaks when the folder moves, so start it through python.
        yield return new ChildSpec("webui", venvPy, ["-c", "from open_webui import app; app()", "serve", "--host", "127.0.0.1", "--port", back.ToString()],
            data, "127.0.0.1", back, Path.Combine(logs, "webui-out.log"), Path.Combine(logs, "webui-err.log"))
        {
            Env = webEnv, Affinity = pcores, ReadyTimeout = TimeSpan.FromMinutes(5),
            // Opening greeting + always-visible Edit pencil; idempotent, re-applies after an Open WebUI upgrade.
            BeforeStart = ct => ManagedChild.RunToolAsync(venvPy, [Path.Combine(root, "patch_frontend.py")], root,
                Path.Combine(logs, "patch-frontend.log"), env, TimeSpan.FromMinutes(2), ct),
        };

        // authproxy.py is stdlib-only and runs on the system Python 3.12: that exe has the firewall allow rule the phone needs.
        var proxyPy = web.GetValueOrDefault("WEBUI_PROXY_PYTHON") ?? "";
        if (proxyPy == "") proxyPy = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Programs", "Python", "Python312", "python.exe");
        if (!File.Exists(proxyPy)) proxyPy = venvPy;
        yield return new ChildSpec("authproxy", proxyPy,
            [Path.Combine(root, "authproxy.py"), "--listen-port", pub.ToString(), "--backend-port", back.ToString(), "--env-file", Path.Combine(root, ".env")],
            root, "127.0.0.1", pub, Path.Combine(logs, "authproxy.log"), Path.Combine(logs, "authproxy-err.log"))
        { Affinity = pcores };
    }

    string Python(IReadOnlyDictionary<string, string> env)
    {
        var py = cfg.Get("PYTHON");
        if (py != "") return py;
        // First python.exe on PATH, skipping the Microsoft Store stub.
        foreach (var dir in env.GetValueOrDefault("PATH", "").Split(';', StringSplitOptions.RemoveEmptyEntries))
        {
            if (dir.Contains("WindowsApps", StringComparison.OrdinalIgnoreCase)) continue;
            var c = Path.Combine(dir.Trim(), "python.exe");
            if (File.Exists(c)) return c;
        }
        throw new FileNotFoundException("python.exe not found: set PYTHON in config.env");
    }

    /// <summary>
    /// The process environment plus the machine and user variables from the registry, with PATH = machine;user.
    /// A service does not necessarily see the user's PATH (npm, Python), which openclaw and the scripts need.
    /// </summary>
    static Dictionary<string, string> BuildEnvironment()
    {
        var env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        foreach (System.Collections.DictionaryEntry e in Environment.GetEnvironmentVariables())
            env[(string)e.Key] = (string?)e.Value ?? "";
        string machinePath = "", userPath = "";
        void Overlay(RegistryKey? k, ref string path)
        {
            if (k == null) return;
            foreach (var name in k.GetValueNames())
            {
                var v = k.GetValue(name)?.ToString() ?? "";   // REG_EXPAND_SZ comes back expanded
                if (name.Equals("Path", StringComparison.OrdinalIgnoreCase)) path = v; else env[name] = v;
            }
        }
        using (var m = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")) Overlay(m, ref machinePath);
        // By SID: in a service HKCU can resolve to .DEFAULT instead of the account's own hive.
        var sid = System.Security.Principal.WindowsIdentity.GetCurrent().User?.Value;
        using (var u = (sid != null ? Registry.Users.OpenSubKey($@"{sid}\Environment") : null) ?? Registry.CurrentUser.OpenSubKey("Environment"))
            Overlay(u, ref userPath);
        env.Remove("PATH");   // case-insensitive: drops "Path" too, so the new key is spelled PATH
        env["PATH"] = string.Join(';', new[] { machinePath, userPath }.Where(s => s != ""));
        env["LOCALAI_HOST_SERVICE"] = "1";
        return env;
    }
}
