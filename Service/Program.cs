// Local AI Host: one Windows service that keeps the stack's long-running processes up (see Project_Note.md).
//   LocalAIHost.exe [--config <path to config.env>]
// Runs as a console app too (Ctrl+C stops it and everything it started).
using LocalAIHost;

string? explicitConfig = null;
for (int i = 0; i < args.Length - 1; i++)
    if (args[i] is "--config" or "-c") explicitConfig = args[i + 1];

var cfg = LocalAIConfig.Load(LocalAIConfig.Locate(explicitConfig));
FileLog.Open(Path.Combine(cfg.LogDir, "localai-host.log"));
FileLog.Write($"=== Local AI Host {Environment.ProcessId} starting, config {cfg.Path}, user {Environment.UserName}");

// Every child (and grandchild, e.g. the llama-server the wake proxy launches) inherits this job:
// when the host exits, crashes or is killed, Windows kills them all and the VRAM is freed.
JobObject.KillChildrenWithThisProcess();

var builder = Host.CreateApplicationBuilder(args);
builder.Services.AddWindowsService(o => o.ServiceName = "LocalAIHost");
builder.Services.Configure<HostOptions>(o => o.ShutdownTimeout = TimeSpan.FromSeconds(25));
builder.Services.AddSingleton(cfg);
builder.Services.AddHostedService<Supervisor>();
builder.Build().Run();
FileLog.Write("=== Local AI Host stopped");
