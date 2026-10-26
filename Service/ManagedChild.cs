using System.Diagnostics;
using System.Net.Sockets;
using System.Text;

namespace LocalAIHost;

/// <summary>What to run and how to tell it is healthy: a TCP port that must accept connections.</summary>
public sealed record ChildSpec(string Name, string Exe, IReadOnlyList<string> Args, string WorkDir,
    string PortHost, int Port, string OutLog, string ErrLog)
{
    public IReadOnlyDictionary<string, string> Env { get; init; } = new Dictionary<string, string>();
    public long Affinity { get; init; }                                  // 0 = any core
    public Func<CancellationToken, Task>? BeforeStart { get; init; }     // e.g. re-apply a patch; failures are logged, not fatal
    public Func<CancellationToken, Task>? AfterReady { get; init; }      // runs once per start, after the port opens
    public TimeSpan ReadyTimeout { get; init; } = TimeSpan.FromMinutes(2);
}

/// <summary>
/// Runs one child forever: start, wait for its port, watch it, restart with backoff when it exits or its
/// port stays closed. If something else already serves the port (a copy started by hand), it waits instead
/// of starting a second one: racing duplicates is what broke the gateway in 2026-08.
/// </summary>
public sealed class ManagedChild(ChildSpec spec, IReadOnlyDictionary<string, string> baseEnv)
{
    static readonly TimeSpan Poll = TimeSpan.FromSeconds(15);
    const int DeadPortChecks = 4;   // 4 x 15 s with the port closed after it was up = hung, restart

    readonly SemaphoreSlim wake = new(0);
    volatile bool stopped, restartNow;
    volatile Process? current;
    volatile string state = "starting";
    int restarts;
    DateTime? since;

    public string Name => spec.Name;
    public int Port => spec.Port;

    public object Snapshot()
    {
        int? pid = null;
        try { pid = current?.Id; } catch { }
        return new { state, pid, port = spec.Port, restarts, up_s = since is { } t && state == "running" ? (int)(DateTime.UtcNow - t).TotalSeconds : (int?)null };
    }

    /// <summary>Kill it now and start it again at once (no backoff).</summary>
    public void Restart() { restartNow = true; stopped = false; KillCurrent(); Wake(); }
    /// <summary>Kill it and keep it down until Start().</summary>
    public void Stop() { stopped = true; KillCurrent(); Wake(); }
    public void Start() { stopped = false; restartNow = true; Wake(); }

    void KillCurrent() { var p = current; if (p != null) Kill(p); }
    void Wake() { if (wake.CurrentCount == 0) wake.Release(); }
    async Task Sleep(TimeSpan t, CancellationToken ct)
    {
        try { await wake.WaitAsync(t, ct); } catch (OperationCanceledException) { }
    }

    public async Task RunAsync(CancellationToken ct)
    {
        var backoff = TimeSpan.FromSeconds(2);
        while (!ct.IsCancellationRequested)
        {
            if (stopped)
            {
                state = "stopped";
                await Sleep(TimeSpan.FromHours(1), ct);
                continue;
            }
            if (await PortOpen(spec.PortHost, spec.Port))
            {
                state = "external";
                FileLog.Write($"[{spec.Name}] :{spec.Port} is already served by another process; waiting for it to go away");
                while (!ct.IsCancellationRequested && !stopped && await PortOpen(spec.PortHost, spec.Port)) await Sleep(Poll, ct);
                continue;
            }

            var started = DateTime.UtcNow;
            try { await RunOnceAsync(ct); }
            catch (OperationCanceledException) when (ct.IsCancellationRequested) { break; }
            catch (Exception e) { FileLog.Write($"[{spec.Name}] {e.GetType().Name}: {e.Message}"); }
            finally { current = null; }
            if (ct.IsCancellationRequested) break;
            if (stopped) { FileLog.Write($"[{spec.Name}] stopped on request"); continue; }
            restarts++;
            if (restartNow)
            {
                restartNow = false;
                backoff = TimeSpan.FromSeconds(2);
                FileLog.Write($"[{spec.Name}] restarting on request");
                await Task.Delay(1000, ct).ContinueWith(_ => { });   // let the port go
                continue;
            }

            if (DateTime.UtcNow - started > TimeSpan.FromMinutes(10)) backoff = TimeSpan.FromSeconds(2);
            state = "backoff";
            FileLog.Write($"[{spec.Name}] restarting in {backoff.TotalSeconds:0} s");
            await Sleep(backoff, ct);
            backoff = TimeSpan.FromSeconds(Math.Min(backoff.TotalSeconds * 2, 300));
        }
    }

    async Task RunOnceAsync(CancellationToken ct)
    {
        if (spec.BeforeStart != null)
        {
            try { await spec.BeforeStart(ct); }
            catch (Exception e) when (e is not OperationCanceledException) { FileLog.Write($"[{spec.Name}] pre-start step failed: {e.Message}"); }
        }

        var psi = new ProcessStartInfo(spec.Exe)
        {
            WorkingDirectory = spec.WorkDir, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in spec.Args) psi.ArgumentList.Add(a);
        psi.Environment.Clear();
        foreach (var (k, v) in baseEnv) psi.Environment[k] = v;
        foreach (var (k, v) in spec.Env) psi.Environment[k] = v;

        Directory.CreateDirectory(Path.GetDirectoryName(spec.OutLog)!);
        using var outW = TextWriter.Synchronized(FileLog.OpenAppend(spec.OutLog));
        using var errW = TextWriter.Synchronized(FileLog.OpenAppend(spec.ErrLog));
        using var p = new Process { StartInfo = psi };
        p.OutputDataReceived += (_, e) => { if (e.Data != null) outW.WriteLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) errW.WriteLine(e.Data); };
        state = "starting";
        p.Start();
        current = p;
        p.StandardInput.Close();
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        if (spec.Affinity != 0) try { p.ProcessorAffinity = (IntPtr)spec.Affinity; } catch { }
        FileLog.Write($"[{spec.Name}] started pid {p.Id}");
        outW.WriteLine($"=== {DateTime.Now:yyyy-MM-dd HH:mm:ss} started by Local AI Host, pid {p.Id}");

        var exited = p.WaitForExitAsync(CancellationToken.None);
        try
        {
            bool ready = false;
            var deadline = DateTime.UtcNow + spec.ReadyTimeout;
            while (!exited.IsCompleted && !ready && DateTime.UtcNow < deadline)
            {
                ready = await PortOpen(spec.PortHost, spec.Port);
                if (!ready) await Task.WhenAny(exited, Task.Delay(1000, ct));
                ct.ThrowIfCancellationRequested();
            }
            if (ready)
            {
                state = "running";
                since = DateTime.UtcNow;
                FileLog.Write($"[{spec.Name}] ready on :{spec.Port}");
                if (spec.AfterReady != null) _ = RunAfterReady(ct);
            }
            else if (!exited.IsCompleted)
                FileLog.Write($"[{spec.Name}] :{spec.Port} not open after {spec.ReadyTimeout.TotalSeconds:0} s; still watching");

            int misses = 0;
            while (!exited.IsCompleted)
            {
                await Task.WhenAny(exited, Task.Delay(Poll, ct));
                ct.ThrowIfCancellationRequested();
                if (exited.IsCompleted) break;
                bool open = await PortOpen(spec.PortHost, spec.Port);
                if (open)
                {
                    if (!ready) { state = "running"; since = DateTime.UtcNow; }
                    ready = true; misses = 0; continue;
                }
                if (ready && ++misses >= DeadPortChecks)
                {
                    FileLog.Write($"[{spec.Name}] :{spec.Port} closed for {misses * Poll.TotalSeconds:0} s; killing pid {p.Id}");
                    Kill(p);
                    break;
                }
            }
            await exited;
            FileLog.Write($"[{spec.Name}] pid {p.Id} exited, code {p.ExitCode}");
        }
        catch (OperationCanceledException)
        {
            FileLog.Write($"[{spec.Name}] stopping pid {p.Id}");
            Kill(p);
            throw;
        }
    }

    async Task RunAfterReady(CancellationToken ct)
    {
        try { await spec.AfterReady!(ct); }
        catch (Exception e) when (e is not OperationCanceledException) { FileLog.Write($"[{spec.Name}] post-start step failed: {e.Message}"); }
    }

    static void Kill(Process p)
    {
        try { p.Kill(entireProcessTree: true); p.WaitForExit(10_000); } catch { }
    }

    static async Task Delay(TimeSpan t, CancellationToken ct)
    {
        try { await Task.Delay(t, ct); } catch (OperationCanceledException) { }
    }

    /// <summary>Raw connect, like the PowerShell launchers (Get-NetTCPConnection intermittently misses live listeners).</summary>
    public static async Task<bool> PortOpen(string host, int port)
    {
        using var c = new TcpClient();
        using var cts = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try { await c.ConnectAsync(host, port, cts.Token); return true; }
        catch { return false; }
    }

    /// <summary>Run a one-shot helper (patch / check script) to completion, output appended to a log.</summary>
    public static async Task RunToolAsync(string exe, IEnumerable<string> args, string workDir, string log,
        IReadOnlyDictionary<string, string> env, TimeSpan timeout, CancellationToken ct)
    {
        var psi = new ProcessStartInfo(exe)
        {
            WorkingDirectory = workDir, UseShellExecute = false, CreateNoWindow = true,
            RedirectStandardOutput = true, RedirectStandardError = true, RedirectStandardInput = true,
            StandardOutputEncoding = Encoding.UTF8, StandardErrorEncoding = Encoding.UTF8,
        };
        foreach (var a in args) psi.ArgumentList.Add(a);
        psi.Environment.Clear();
        foreach (var (k, v) in env) psi.Environment[k] = v;
        Directory.CreateDirectory(Path.GetDirectoryName(log)!);
        using var w = TextWriter.Synchronized(FileLog.OpenAppend(log));
        w.WriteLine($"=== {DateTime.Now:yyyy-MM-dd HH:mm:ss} {Path.GetFileName(exe)} {string.Join(' ', args)}");
        using var p = new Process { StartInfo = psi };
        p.OutputDataReceived += (_, e) => { if (e.Data != null) w.WriteLine(e.Data); };
        p.ErrorDataReceived += (_, e) => { if (e.Data != null) w.WriteLine(e.Data); };
        p.Start();
        p.StandardInput.Close();
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        using var cts = CancellationTokenSource.CreateLinkedTokenSource(ct);
        cts.CancelAfter(timeout);
        try { await p.WaitForExitAsync(cts.Token); }
        catch (OperationCanceledException) { Kill(p); if (ct.IsCancellationRequested) throw; w.WriteLine("(timed out, killed)"); return; }
        w.WriteLine($"(exit {p.ExitCode})");
    }
}
