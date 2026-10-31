# Local AI Host service

LocalAIHost is a Windows background service built on .NET 10. It supervises the stack processes and exposes a local named pipe for administrative control.

## Account and privileges

LocalAIHost runs under the user Windows account rather than `NT AUTHORITY\SYSTEM`. This prevents child processes such as the OpenClaw Discord gateway from running with SYSTEM privileges. Windows stores the account password as an LSA secret for the service. The service installer grants the account the `SeServiceLogonRight` user right.

## Supervised processes

LocalAIHost supervises up to four child processes. The model weights remain unloaded at boot until a client sends an inference request or an operator sends a load command.

| Name | Command | Health port | Supervision condition |
|---|---|---|---|
| `wake-proxy` | `python SCRIPTS_DIR\llama-wake.py --listen-port <PROXY_PORT> --backend-port <LLAMA_PORT> --ctx <CONTEXT> --idle-minutes <IDLE_UNLOAD_MINUTES> --alias <MODEL_ALIAS> --launcher <SCRIPTS_DIR\Start-LocalAI.ps1>` | `PROXY_PORT` (default 8080) | Always supervised |
| `gateway` | `cmd.exe /c openclaw gateway run` | `GATEWAY_PORT` (default 18789) | `SUPERVISE_GATEWAY=true` |
| `webui` | `venv\Scripts\python.exe -c "from open_webui import app; app()" serve --host 127.0.0.1 --port <WEBUI_BACKEND_PORT>` | `WEBUI_BACKEND_PORT` (default 8338) | `SUPERVISE_WEBUI=true` |
| `authproxy` | `python WebUI\authproxy.py --listen-port <WEBUI_PORT> --backend-port <WEBUI_BACKEND_PORT> --env-file WebUI\.env` | `WEBUI_PORT` (default 8337) | `SUPERVISE_WEBUI=true` |

If `KEEP_AWAKE=false`, `wake-proxy` receives `--no-keep-awake`.

The `gateway` process runs two helpers:
- Pre-start: `python ExecGuard\patch_discord_card.py` runs before start with a 1 minute timeout.
- Post-ready: `python ExecGuard\guard_check.py --wait 45` runs after the port opens with a 2 minute timeout.

The `webui` process runs `python patch_frontend.py` before start with a 2 minute timeout. It sets processor affinity to `PCORE_MASK` (default `0xC03C3`) and waits up to 5 minutes for port readiness.

The `authproxy` process runs on the Python 3.12 interpreter set by `WEBUI_PROXY_PYTHON` in `WebUI\.env` or found at `%LOCALAPPDATA%\Programs\Python\Python312\python.exe`. It sets processor affinity to `PCORE_MASK`.

## Restart policy

When a process exits unexpectedly:
- Initial backoff delay is 2 seconds.
- Each consecutive restart doubles the delay: 4 seconds, 8 seconds, 16 seconds, up to a maximum cap of 300 seconds (5 minutes).
- If the process runs continuously for more than 10 minutes, the backoff resets to 2 seconds.
- An explicit restart request resets the backoff to 2 seconds and applies a 1 second delay before launching.

## Hung-port detection

LocalAIHost polls the TCP health port of each running child every 15 seconds using a direct socket connection. If a child opened its port previously but fails connection checks for 4 consecutive intervals (60 seconds total), LocalAIHost marks the child hung, kills its process tree, and restarts it.

## Port already served

Before launching a child, LocalAIHost checks if its health port already accepts connections. If open, LocalAIHost sets the child state to `external`. It logs that another process serves the port and polls every 15 seconds until the port closes. It never spawns a duplicate instance.

## Job object

LocalAIHost creates an anonymous Windows Job Object configured with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and assigns its own process to it. All child and grandchild processes, including `llama-server.exe` started by the wake proxy, inherit this job. If LocalAIHost exits or crashes, Windows terminates all processes in the job tree and frees GPU VRAM.

## Logs

LocalAIHost writes log output to the following locations:

| Log file | Source |
|---|---|
| `LOG_DIR\localai-host.log` | Host service operational log |
| `LOG_DIR\localai-host-actions.log` | Background actions from control pipe (`gateway warm`, `guard check`) |
| `LOG_DIR\wake-proxy.log` | Standard output of `wake-proxy` |
| `LOG_DIR\wake-proxy-err.log` | Standard error of `wake-proxy` |
| `LOG_DIR\gateway-stdout.log` | Standard output of OpenClaw gateway |
| `LOG_DIR\gateway-stderr.log` | Standard error of OpenClaw gateway |
| `LOG_DIR\gateway-prestart.log` | Standard output and error of ExecGuard patch and check scripts |
| `WebUI\logs\webui-out.log` | Standard output of Open WebUI |
| `WebUI\logs\webui-err.log` | Standard error of Open WebUI |
| `WebUI\logs\patch-frontend.log` | Standard output and error of Open WebUI frontend patch script |
| `WebUI\logs\authproxy.log` | Standard output of Open WebUI auth proxy |
| `WebUI\logs\authproxy-err.log` | Standard error of Open WebUI auth proxy |

## Build

Build the service with the .NET 10 SDK:

```powershell
dotnet publish Service/LocalAIHost.csproj -c Release -o Service/publish --nologo
```

## Installation and removal

Install and manage the service with [Install-Service.ps1](../Service/Install-Service.ps1) from an elevated PowerShell prompt.

Requirements:
- Administrator rights.
- .NET 10 SDK.
- Windows user account password (not a Windows Hello PIN).

Install command:

```powershell
.\Service\Install-Service.ps1
```

Parameters:
- `-Uninstall`: stops the running service, removes the service registration from Windows Service Control Manager, and leaves the stack unmanaged.
- `-Credential`: passes a pre-populated `[pscredential]` object containing the Windows username and password. If omitted, the script prompts for credentials interactively.

What the installer does:
1. Validates account credentials against local machine security.
2. Exports user rights policy via `secedit`, adds `*<SID>` to `SeServiceLogonRight`, and reapplies the policy.
3. Stops any running `LocalAIHost` service instance.
4. Compiles the project using `dotnet publish` into `Service\publish`.
5. Removes legacy scheduled tasks: "Local AI Startup", "Local AI Proxy Watchdog", "Local WebUI Startup".
6. Terminates old running processes matching `llama-wake.py`, `gateway run`, `open-webui`, or `authproxy`.
7. Registers the service with startup type `delayed-auto`.
8. Configures service recovery via `sc.exe failure LocalAIHost reset= 86400 actions= restart/5000/restart/30000/restart/60000` (restarts after 5 seconds, 30 seconds, and 60 seconds).
9. Starts the service and verifies pipe connectivity with a `status` probe.

## Control pipe

LocalAIHost exposes a named pipe server at:

```
\\.\pipe\LocalAIHost
```

### Access rights

The named pipe security descriptor allows `PipeAccessRights.FullControl` to:
- `NT AUTHORITY\SYSTEM`
- `BUILTIN\Administrators`
- The user account running LocalAIHost (`WindowsIdentity.GetCurrent().User`)

### Wire format

Clients connect to `\\.\pipe\LocalAIHost` and communicate via UTF-8 text streams:
- Requests: single text line up to 4096 bytes, terminated by a newline (`\n` or `\r\n`).
- Responses: single JSON line terminated by a newline (`\n`).

Response structures:
- Success: `{"ok":true,"data":...}`
- Error: `{"ok":false,"error":"..."}`

### Command reference

`help` returns the command list. Source: [ControlServer.cs](../Service/ControlServer.cs).

| Command | Execution mode | Description | Success data format |
|---|---|---|---|
| `ping` | Synchronous | Tests pipe responsiveness. | `{"pong":true}` |
| `status` | Synchronous | Returns supervisor state, child states, model status, and background task outcome. | `{"children":{...},"model":{...},"last":{...},"busy":false}` |
| `help` | Synchronous | Returns array of supported command strings. | Array of command syntax strings |
| `model load [ctx]` | Background | Loads model via proxy `/_gate/ensure`. Optional `ctx` accepts numbers, `128k`, `default`, or `heavy`. | `{"started":"model load"}` |
| `model unload` | Synchronous | Requests polite model unload via proxy `/_gate/sleep`. Returns error if requests are active. | `{"unloaded":true}` |
| `model unload force` | Synchronous | Forces unload via proxy `/_gate/sleep {"force":true}`. Kills `llama-server.exe` directly if proxy is unresponsive. | `{"unloaded":true}` |
| `model reload [ctx]` | Background | Forces model unload, then reloads with optional context size. | `{"started":"model reload"}` |
| `model ctx <n\|default\|heavy>` | Background | Resizes model context to exact value. Range: 4096 to 1048576. Accepts numbers, `128k`, `default`, or `heavy`. | `{"started":"model ctx <n>"}` |
| `model idle <minutes, 0 = never>` | Synchronous | Sets idle unload timeout in minutes via proxy `/_gate/config`. Accepts numbers, where 0 disables auto-unload. | Current proxy status object |
| `model keepawake <on\|off>` | Synchronous | Enables or disables Windows idle sleep blocking while model is loaded via proxy `/_gate/config`. | Current proxy status object |
| `restart <name\|all>` | Synchronous | Restarts specified child or `all` children immediately without backoff delay. Allowed names: `wake-proxy`, `gateway`, `webui`, `authproxy`, `all`. | `{"action":"restart","targets":[...]}` |
| `stop <name>` | Synchronous | Terminates child and holds it in `stopped` state. Name must be single child; `all` returns an error. | `{"action":"stop","targets":["<name>"]}` |
| `start <name>` | Synchronous | Clears `stopped` flag and starts child immediately. Name must be single child; `all` returns an error. | `{"action":"start","targets":["<name>"]}` |
| `gateway warm` | Background | Executes `cmd.exe /c openclaw agent --agent main -m "Reply with the single word: ready"` with a 15 minute timeout. | `{"started":"gateway warm"}` |
| `guard check` | Background | Executes `python ExecGuard\guard_check.py --wait 45` with a 2 minute timeout. | `{"started":"guard check"}` |

Background commands return `{"ok":true,"data":{"started":"<action>"}}` immediately. Only one background action runs at a time; if an action is in progress, background commands return `{"ok":false,"error":"busy with another action"}`. The outcome of the background action updates the `last` property in `status`.

### Sample request and response

Request:

```
ping
```

Response:

```json
{"ok":true,"data":{"pong":true}}
```

Request:

```
status
```

Response:

```json
{
  "ok": true,
  "data": {
    "children": {
      "wake-proxy": { "state": "running", "pid": 4812, "port": 8080, "restarts": 0, "up_s": 3600 },
      "gateway": { "state": "running", "pid": 5120, "port": 18789, "restarts": 0, "up_s": 3595 }
    },
    "model": {
      "state": "asleep",
      "ctx": null,
      "desired_ctx": 131072,
      "inflight": 0,
      "idle_s": 1200,
      "idle_minutes": 20.0,
      "keep_awake": true,
      "sleep_blocked": false,
      "unload_in_s": null
    },
    "last": {
      "action": "model load",
      "result": "ok",
      "at": "10:15:30"
    },
    "busy": false
  }
}
```

### PowerShell command invocation

Send a command through the control pipe in PowerShell:

```powershell
$pipe = New-Object System.IO.Pipes.NamedPipeClientStream('.', 'LocalAIHost', [System.IO.Pipes.PipeDirection]::InOut)
try {
    $pipe.Connect(5000)
    $writer = New-Object System.IO.StreamWriter($pipe)
    $writer.NewLine = "`n"
    $writer.AutoFlush = $true
    $reader = New-Object System.IO.StreamReader($pipe)

    $writer.WriteLine('status')
    $response = $reader.ReadLine()
    $response | ConvertFrom-Json
} finally {
    $pipe.Dispose()
}
```
