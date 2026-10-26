namespace LocalAIHost;

/// <summary>The host's own log (LOG_DIR\localai-host.log). Children log to their own files.</summary>
static class FileLog
{
    static TextWriter? w;

    public static void Open(string path)
    {
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        w = TextWriter.Synchronized(OpenAppend(path));
    }

    public static StreamWriter OpenAppend(string path) =>
        new(new FileStream(path, FileMode.Append, FileAccess.Write, FileShare.ReadWrite | FileShare.Delete)) { AutoFlush = true };

    public static void Write(string msg)
    {
        var line = $"{DateTime.Now:yyyy-MM-dd HH:mm:ss} {msg}";
        w?.WriteLine(line);
        if (Environment.UserInteractive) Console.WriteLine(line);
    }
}
