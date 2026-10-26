# Local AI Host (Windows service)

`LocalAIHost.exe` (.NET 10 Worker Service, service name `LocalAIHost`) runs as the user's own account and keeps the stack's long-running processes up. It is the only thing that starts the stack: the logon tasks "Local AI Startup", "Local AI Proxy Watchdog" and "Local WebUI Startup" and `Register-Tasks.ps1` are gone. At boot the model stays unloaded until a real request or `model load` on the pipe.

| Child | Command | Health port | Logs |
|---|---|---|---|
| `wake-proxy` | `python SCRIPTS_DIR\llama-wake.py --listen-port --backend-port --ctx --idle-minutes --alias --launcher` (it starts llama-server itself via `Start-LocalAI.ps1 -BackendOnly`) | `PROXY_PORT` | `LOG_DIR\wake-proxy*.log` |
| `gateway` | `cmd /c openclaw gateway run`; `ExecGuard\patch_discord_card.py` before, `guard_check.py --wait 45` after it is up | `GATEWAY_PORT` | `LOG_DIR\gateway-*.log` |
| `webui` | `WebUI\venv\Scripts\python.exe -c "from open_webui import app; app()" serve`; `patch_frontend.py` before; P-cores | `WEBUI_BACKEND_PORT` (WebUI\.env) | `WebUI\logs\webui-*.log` |
| `authproxy` | Python 3.12 `WebUI\authproxy.py` (that exe has the firewall rule); P-cores | `WEBUI_PORT` | `WebUI\logs\authproxy*.log` |

Host log: `LOG_DIR\localai-host.log`; background actions: `LOG_DIR\localai-host-actions.log`.

## Behaviour
- Restart with backoff 2 s doubling to 5 min (reset after 10 min up). A port closed for 60 s after it was up = hung: killed and restarted.
- A port already served by another process (a copy started by hand) = state `external`: the host waits, never starts a duplicate.
- The host puts itself in a kill-on-close job object: every child and grandchild (including llama-server) dies with it, so VRAM is freed on stop or crash.
- Environment for children = process env + machine and user registry variables, PATH = machine;user (npm and Python live in the user PATH).
- Config: `config.env` via `--config` (installer passes it), else `LOCALAI_CONFIG`, else walking up from the exe. New keys: `SUPERVISE_GATEWAY`, `SUPERVISE_WEBUI` (default true), `PCORE_MASK` (default 0xC03C3), `WEBUI_DIR` (default `LOCALAI_HOME\WebUI`).
- `Start-LocalAI.ps1` is service-aware: when the service exists it starts it instead of spawning copies, and `-Restart` goes through the pipe. `-BackendOnly` is unchanged.

## Control pipe `\\.\pipe\LocalAIHost`
Same framing as PcHwCtl: one UTF-8 command per line, one JSON line back, `{"ok":true,"data":...}` or `{"ok":false,"error":"..."}`. Access: SYSTEM, Administrators, the service account.

| Command | Effect |
|---|---|
| `ping`, `help`, `status` | status = per-child `state` (starting/running/backoff/stopped/external), pid, restarts, up_s; `model` = wake proxy `/_gate/status`; `last` = last background action |
| `model load [ctx]` | background: `/_gate/ensure` |
| `model unload` | polite: refused while a request runs |
| `model unload force` | proxy unloads even mid-request; if the proxy is stuck, llama-server is killed directly |
| `model reload [ctx]` | background: force unload, then load |
| `model ctx <n\|128k\|default\|heavy>` | background: exact resize |
| `model idle <minutes>` | auto-unload delay, 0 = never (until proxy restart) |
| `model keepawake <on\|off>` | hold Windows awake while loaded |
| `restart <name\|all>`, `stop <name>`, `start <name>` | names: wake-proxy, gateway, webui, authproxy. `stop` holds it down until `start` |
| `gateway warm` | background: one throwaway OpenClaw turn to fill the prompt cache |
| `guard check` | background: `ExecGuard\guard_check.py` |

Background commands answer `{"started":...}` at once; only one runs at a time (`busy with another action`).

## Install / update / remove
Elevated PowerShell: `Service\Install-Service.ps1` (asks for the Windows password, not the PIN; grants "Log on as a service"; publishes to `Service\publish`; deletes the old logon tasks if present; stops old copies; delayed auto-start; SCM restarts after 5/30/60 s). Re-run after a password change or a change to the service code. `-Uninstall` deletes the service; nothing then starts the stack at boot.
`Launcher\*` runs in place (`SCRIPTS_DIR` = the repo's Launcher folder): after editing `llama-wake.py`/`ai_hub.py` send `restart wake-proxy`; `Start-LocalAI.ps1` changes apply on the next model load.

## Not verified
- A reboot with the service as the only starter.
- The gateway and Open WebUI under the service, before the install.
- llama-server CUDA load in session 0 (expected to work; the proxy launches it the same way).
