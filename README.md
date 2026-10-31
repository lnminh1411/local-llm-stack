# Local LLM stack

Always-available local AI stack on Windows, running Occamy-1.0 35B-A3B on an RTX 3050 and CPU E-cores. A Windows service keeps the stack running and the model loads on demand. Guard plugins filter the Discord agent, and a CLI lets cloud coding agents hand work to the local model.
The companion [ESP32-PC-Control-System](https://github.com/lnminh1411/ESP32-PC-Control-System) board shows the stack's status on its displays.

## Architecture

| Piece | What it does | Docs |
|---|---|---|
| Service | Runs LocalAIHost at boot, manages processes, and serves a control pipe. | [docs/service.md](docs/service.md) |
| Launcher and wake proxy | Starts llama-server on demand, unloads on idle, and keeps Windows awake while loaded. | [docs/launcher.md](docs/launcher.md) |
| Bridge | Exposes the `la` CLI and DeepSeek Harness to offload coding tasks. | [docs/bridge.md](docs/bridge.md) |
| OpenClaw plugins | ExecGuard gates shell commands, ContentGuard filters Discord text, StatusReporter reports agent activity. | [docs/openclaw-plugins.md](docs/openclaw-plugins.md) |
| Status hub | Serves JSON, SSE, and flat feeds for telemetry and microcontrollers. | [docs/status-hub.md](docs/status-hub.md) |
| Configuration | Central environment variables for hardware, ports, and models. | [docs/configuration.md](docs/configuration.md) |

Not in the repo: `WebUI/`, `Bridge/harness/`, `Bridge/legacy/`.

## Quick setup

1. Copy `config.env.example` to `config.env`, set values, and set `LOCALAI_CONFIG`.
2. Extract official llama.cpp CUDA 12.4 binaries into `%LOCALAPPDATA%\Programs\llama.cpp`.
3. Run `.\Service\Install-Service.ps1` from an elevated PowerShell terminal.
4. Register plugins in `~/.openclaw/openclaw.json` and run `python .\Bridge\sync.py`.

Full setup instructions: [docs/setup.md](docs/setup.md).

## Documentation

- [docs/setup.md](docs/setup.md): install steps, end to end
- [docs/configuration.md](docs/configuration.md): every `config.env` key
- [docs/service.md](docs/service.md): LocalAIHost service and control pipe
- [docs/launcher.md](docs/launcher.md): Start-LocalAI.ps1, llama-wake.py, model flags, idle unload
- [docs/bridge.md](docs/bridge.md): Local Agent Bridge and `la` CLI
- [docs/openclaw-plugins.md](docs/openclaw-plugins.md): ExecGuard, ContentGuard, StatusReporter, shared/
- [docs/status-hub.md](docs/status-hub.md): status feed and ESP32 link

## Safety notes

- `la run --exec` is unsandboxed. Grant it only when the task needs a shell.
- The service runs as your user account, never SYSTEM. Windows keeps the account password as an LSA secret. Only SYSTEM, Administrators, and that account can open the control pipe.
- The DeepSeek harness is developer-preview software and has not been security audited.
- ContentGuard ships a base term list of slurs for masking and blocking. Secrets live in `~/.openclaw/openclaw.json` and `WebUI/`, outside version control.

## License

GNU Affero General Public License v3.0, see [LICENSE](LICENSE).
