using Microsoft.Win32;

namespace LocalAIHost;

/// <summary>The stack's central config.env (same lookup order as Launcher/LocalAI-Config.ps1).</summary>
public sealed class LocalAIConfig
{
    readonly Dictionary<string, string> values;
    public string Path { get; }

    LocalAIConfig(string path, Dictionary<string, string> values) { Path = path; this.values = values; }

    public static string Locate(string? explicitPath)
    {
        var cands = new List<string?>
        {
            explicitPath,
            Environment.GetEnvironmentVariable("LOCALAI_CONFIG"),
            Registry.CurrentUser.OpenSubKey("Environment")?.GetValue("LOCALAI_CONFIG") as string,
        };
        // publish folder is <repo>\Service\publish, so walk up to the repo root.
        for (var d = new DirectoryInfo(AppContext.BaseDirectory); d != null; d = d.Parent)
            cands.Add(System.IO.Path.Combine(d.FullName, "config.env"));
        foreach (var c in cands)
            if (!string.IsNullOrWhiteSpace(c) && File.Exists(c)) return System.IO.Path.GetFullPath(c);
        throw new FileNotFoundException("config.env not found. Pass --config <path> or set LOCALAI_CONFIG.");
    }

    public static LocalAIConfig Load(string path) => new(path, ReadEnvFile(path));

    /// <summary>KEY=VALUE lines, '#' comments, optional surrounding quotes (WebUI\.env uses them).</summary>
    public static Dictionary<string, string> ReadEnvFile(string path)
    {
        var h = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
        if (!File.Exists(path)) return h;
        foreach (var line in File.ReadAllLines(path))
        {
            var t = line.Trim();
            if (t.Length == 0 || t[0] == '#') continue;
            int i = t.IndexOf('=');
            if (i < 1) continue;
            var v = t[(i + 1)..].Trim();
            if (v.Length >= 2 && ((v[0] == '"' && v[^1] == '"') || (v[0] == '\'' && v[^1] == '\''))) v = v[1..^1];
            h[t[..i].Trim()] = v;
        }
        return h;
    }

    public string Get(string key, string fallback = "") =>
        values.TryGetValue(key, out var v) && v != "" ? v : fallback;

    public int GetInt(string key, int fallback) => int.TryParse(Get(key), out var v) ? v : fallback;

    public bool GetBool(string key, bool fallback)
    {
        var v = Get(key);
        return v == "" ? fallback : v.ToLowerInvariant() is not ("false" or "0" or "no" or "off");
    }

    public long GetMask(string key, long fallback)
    {
        var v = Get(key);
        if (v.StartsWith("0x", StringComparison.OrdinalIgnoreCase)) v = v[2..];
        return v != "" && long.TryParse(v, System.Globalization.NumberStyles.HexNumber, null, out var m) ? m : fallback;
    }

    static string Profile => Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
    public string Home => Get("LOCALAI_HOME", System.IO.Path.GetDirectoryName(Path)!);
    public string ScriptsDir => Get("SCRIPTS_DIR", System.IO.Path.Combine(Home, "Launcher"));
    public string LogDir => Get("LOG_DIR", System.IO.Path.Combine(Profile, ".openclaw", "logs"));
    public string HostAddr => Get("HOST", "127.0.0.1");
}
