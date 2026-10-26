using System.Diagnostics;
using System.IO.Pipes;
using System.Net.Http.Json;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text;
using System.Text.Json;

namespace LocalAIHost;

/// <summary>
/// Named pipe \\.\pipe\LocalAIHost: one text command per line in, one JSON line out ({"ok":true,"data":...} or
/// {"ok":false,"error":"..."}), the same framing as PcHwCtl (E:\PC Hardware Controller\docs\ipc.md), so
/// ESP32-Helper relays the board's `!ai` lines the way it relays `!hw`. Commands: see Help below.
/// Access: SYSTEM (ESP32PcStats), Administrators, and the account the host runs as.
/// </summary>
public sealed class ControlServer(LocalAIConfig cfg, IReadOnlyList<ManagedChild> children, IReadOnlyDictionary<string, string> env, string python)
{
    public const string PipeName = "LocalAIHost";
    const int MaxLine = 4096;

    static readonly string[] Help =
    [
        "ping", "status", "help",
        "model load [ctx]", "model unload", "model unload force", "model reload [ctx]",
        "model ctx <n|default|heavy>", "model idle <minutes, 0 = never>", "model keepawake <on|off>",
        "restart <name|all>", "stop <name>", "start <name>",
        "gateway warm", "guard check",
    ];

    readonly HttpClient http = new() { BaseAddress = new Uri($"http://{cfg.HostAddr}:{cfg.GetInt("PROXY_PORT", 8080)}"), Timeout = Timeout.InfiniteTimeSpan };
    readonly object lastLock = new();
    object? last;   // outcome of the last background action, shown in `status`
    int busy;       // one background model action at a time

    public async Task RunAsync(CancellationToken ct)
    {
        var sec = new PipeSecurity();
        foreach (var sid in new[] { new SecurityIdentifier(WellKnownSidType.LocalSystemSid, null),
                                    new SecurityIdentifier(WellKnownSidType.BuiltinAdministratorsSid, null),
                                    WindowsIdentity.GetCurrent().User! })
            sec.AddAccessRule(new PipeAccessRule(sid, PipeAccessRights.FullControl, AccessControlType.Allow));
        FileLog.Write($"[control] listening on \\\\.\\pipe\\{PipeName}");
        while (!ct.IsCancellationRequested)
        {
            NamedPipeServerStream pipe;
            try
            {
                pipe = NamedPipeServerStreamAcl.Create(PipeName, PipeDirection.InOut, NamedPipeServerStream.MaxAllowedServerInstances,
                    PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 0, 0, sec);
            }
            catch (Exception e)
            {
                FileLog.Write($"[control] cannot create the pipe: {e.Message}");
                try { await Task.Delay(5000, ct); } catch (OperationCanceledException) { }
                continue;
            }
            try { await pipe.WaitForConnectionAsync(ct); }
            catch (OperationCanceledException) { pipe.Dispose(); break; }
            _ = ServeAsync(pipe, ct);
        }
    }

    async Task ServeAsync(NamedPipeServerStream pipe, CancellationToken ct)
    {
        await using var _ = pipe;
        var enc = new UTF8Encoding(false);
        var r = new StreamReader(pipe, enc);
        var w = new StreamWriter(pipe, enc) { AutoFlush = true, NewLine = "\n" };
        try
        {
            while (!ct.IsCancellationRequested)
            {
                var line = await r.ReadLineAsync(ct);
                if (line == null) break;
                if (line.Length > MaxLine) { await w.WriteLineAsync(Reply(false, "line too long")); break; }
                string answer;
                try { answer = await HandleAsync(line.Trim(), ct); }
                catch (Exception e) when (e is not OperationCanceledException) { answer = Reply(false, e.Message); }
                await w.WriteLineAsync(answer);
            }
        }
        catch (Exception e) when (e is IOException or OperationCanceledException or ObjectDisposedException) { }
    }

    static string Reply(bool ok, object? dataOrError) =>
        ok ? JsonSerializer.Serialize(new { ok = true, data = dataOrError }) : JsonSerializer.Serialize(new { ok = false, error = dataOrError });

    async Task<string> HandleAsync(string line, CancellationToken ct)
    {
        var a = line.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries).Select(s => s.ToLowerInvariant()).ToArray();
        if (a.Length == 0) return Reply(false, "empty command");
        FileLog.Write($"[control] {line}");
        switch (a[0])
        {
            case "ping": return Reply(true, new { pong = true });
            case "help": return Reply(true, Help);
            case "status": return Reply(true, await StatusAsync(ct));
            case "model" when a.Length >= 2: return await ModelAsync(a, ct);
            case "restart" or "stop" or "start" when a.Length == 2: return Control(a[0], a[1]);
            case "gateway" when a is [_, "warm"]:
                return Background("gateway warm", ct => Tool(Path.Combine(Environment.SystemDirectory, "cmd.exe"),
                    ["/c", "openclaw agent --agent main -m \"Reply with the single word: ready\""], TimeSpan.FromMinutes(15), ct));
            case "guard" when a is [_, "check"]:
                return Background("guard check", ct => Tool(python, [Path.Combine(cfg.Home, "ExecGuard", "guard_check.py"), "--wait", "45"], TimeSpan.FromMinutes(2), ct));
        }
        return Reply(false, "unknown command (try help)");
    }

    async Task<object> StatusAsync(CancellationToken ct)
    {
        object? model;
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(6000);   // ~2.1 s while asleep: the proxy's health probe waits out Windows' refused-connect retries
            model =await http.GetFromJsonAsync<JsonElement>("/_gate/status", cts.Token);
        }
        catch (Exception e) { model = new { state = "down", error = e.GetType().Name }; }
        lock (lastLock)
            return new { children = children.ToDictionary(c => c.Name, c => c.Snapshot()), model, last, busy = busy != 0 };
    }

    string Control(string verb, string name)
    {
        var targets = name == "all" ? children : children.Where(c => c.Name == name).ToList();
        if (targets.Count == 0) return Reply(false, $"no process '{name}' ({string.Join(", ", children.Select(c => c.Name))})");
        if (name == "all" && verb != "restart") return Reply(false, $"{verb} all is not allowed; stop the service instead");
        foreach (var c in targets)
        {
            if (verb == "restart") c.Restart(); else if (verb == "stop") c.Stop(); else c.Start();
        }
        return Reply(true, new { action = verb, targets = targets.Select(c => c.Name) });
    }

    int ParseCtx(string s) => s switch
    {
        "default" => cfg.GetInt("CONTEXT", 131072),
        "heavy" => cfg.GetInt("HEAVY_CONTEXT", 262144),
        _ when s.EndsWith('k') && int.TryParse(s[..^1], out var k) => k * 1024,
        _ when int.TryParse(s, out var n) => n,
        _ => throw new ArgumentException($"bad context size '{s}' (number, 128k, default or heavy)"),
    };

    async Task<string> ModelAsync(string[] a, CancellationToken ct)
    {
        switch (a[1])
        {
            case "load":
            {
                int? ctx = a.Length > 2 ? ParseCtx(a[2]) : null;
                return Background("model load", ct => Ensure(ctx, false, ct));
            }
            case "ctx" when a.Length == 3:
            {
                int ctx = ParseCtx(a[2]);
                if (ctx < 4096 || ctx > 1048576) return Reply(false, "context must be 4096..1048576");
                return Background($"model ctx {ctx}", ct => Ensure(ctx, true, ct));
            }
            case "unload":
            {
                bool force = a.Length > 2 && a[2] == "force";
                var res = await Unload(force, ct);
                return res == null ? Reply(true, new { unloaded = true }) : Reply(false, res);
            }
            case "reload":
            {
                int? ctx = a.Length > 2 ? ParseCtx(a[2]) : null;
                return Background("model reload", async ct =>
                {
                    var res = await Unload(true, ct);
                    if (res != null) return res;
                    return await Ensure(ctx, ctx != null, ct);
                });
            }
            case "idle" when a.Length == 3 && double.TryParse(a[2], System.Globalization.CultureInfo.InvariantCulture, out var mins) && mins >= 0:
                return await Config(new { idle_minutes = mins }, ct);
            case "keepawake" when a.Length == 3 && a[2] is "on" or "off":
                return await Config(new { keep_awake = a[2] == "on" }, ct);
        }
        return Reply(false, "unknown model command (try help)");
    }

    async Task<string> Config(object body, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(5000);
        var resp = await http.PostAsJsonAsync("/_gate/config", body, cts.Token);
        return resp.IsSuccessStatusCode ? Reply(true, await resp.Content.ReadFromJsonAsync<JsonElement>(cts.Token)) : Reply(false, $"wake proxy answered {(int)resp.StatusCode}");
    }

    /// <summary>null = done, else the error.</summary>
    async Task<string?> Ensure(int? ctx, bool exact, CancellationToken ct)
    {
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(TimeSpan.FromMinutes(20));
        var resp = await http.PostAsJsonAsync("/_gate/ensure", new { ctx, exact }, cts.Token);
        return resp.IsSuccessStatusCode ? null : $"wake proxy answered {(int)resp.StatusCode}: {Trim(await resp.Content.ReadAsStringAsync(cts.Token))}";
    }

    /// <summary>
    /// Polite unload is refused while a request runs. Force asks the proxy to unload anyway; if the proxy is
    /// stuck (e.g. mid-load, holding its lock) it kills llama-server directly.
    /// </summary>
    async Task<string?> Unload(bool force, CancellationToken ct)
    {
        try
        {
            using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
            cts.CancelAfter(force ? 8000 : 15000);
            var resp = await http.PostAsJsonAsync("/_gate/sleep", new { force }, cts.Token);
            var j = await resp.Content.ReadFromJsonAsync<JsonElement>(cts.Token);
            if (j.TryGetProperty("ok", out var ok) && ok.ValueKind == JsonValueKind.True) return null;
            if (!force) return "busy: a request is running (use: model unload force)";
        }
        catch (Exception e) when (e is not OperationCanceledException || !ct.IsCancellationRequested)
        {
            if (!force) return $"wake proxy unreachable ({e.GetType().Name})";
        }
        int n = 0;
        foreach (var p in Process.GetProcessesByName("llama-server"))
            using (p) try { p.Kill(true); n++; } catch { }
        FileLog.Write($"[control] force unload: killed {n} llama-server process(es) directly");
        return null;
    }

    async Task<string?> Tool(string exe, string[] args, TimeSpan timeout, CancellationToken ct)
    {
        await ManagedChild.RunToolAsync(exe, args, cfg.Home, Path.Combine(cfg.LogDir, "localai-host-actions.log"), env, timeout, ct);
        return null;
    }

    /// <summary>Long actions (model load takes ~25-100 s) answer at once; the outcome shows as `last` in status.</summary>
    string Background(string what, Func<CancellationToken, Task<string?>> work)
    {
        if (Interlocked.CompareExchange(ref busy, 1, 0) != 0) return Reply(false, "busy with another action");
        lock (lastLock) last = new { action = what, result = "running", at = DateTime.Now.ToString("HH:mm:ss") };
        _ = Task.Run(async () =>
        {
            string result;
            try { result = await work(CancellationToken.None) ?? "ok"; }
            catch (Exception e) { result = $"err {e.Message}"; }
            finally { Interlocked.Exchange(ref busy, 0); }
            lock (lastLock) last = new { action = what, result, at = DateTime.Now.ToString("HH:mm:ss") };
            FileLog.Write($"[control] {what} -> {result}");
        });
        return Reply(true, new { started = what });
    }

    static string Trim(string s) => s.Length > 200 ? s[..200] : s;
}
