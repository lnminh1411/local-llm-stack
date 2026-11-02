# Configuration reference

All machine-specific settings live in `config.env` in the repository root. Copy `config.env.example` to `config.env` and set values appropriate for the local machine.

## Configuration discovery

The stack contains four configuration readers. Each reader locates `config.env` using a specific lookup sequence.

### PowerShell loader

[Launcher/LocalAI-Config.ps1](../Launcher/LocalAI-Config.ps1) resolves the file path through the following search order:
1. Process environment variable: `$env:LOCALAI_CONFIG`
2. User registry environment variable: `[Environment]::GetEnvironmentVariable('LOCALAI_CONFIG', 'User')`
3. File beside script: `Launcher\config.env`
4. File one directory up: `config.env` (repository root)

If no file exists at these locations, the function throws an error.

### Python loader

[Bridge/localai_config.py](../Bridge/localai_config.py) checks:
1. Environment variable: `os.environ.get("LOCALAI_CONFIG")`
2. File beside module: `Bridge/config.env`
3. File one directory up: `config.env` (repository root)

If missing, the module returns empty defaults without throwing.

### JavaScript loader

[shared/config.js](../shared/config.js) checks:
1. Environment variable: `process.env.LOCALAI_CONFIG`
2. File beside module: `shared/config.env`
3. File one directory up: `config.env` (repository root)

If missing, the module returns an empty configuration object.

### .NET host service loader

[Service/LocalAIConfig.cs](../Service/LocalAIConfig.cs) checks:
1. Command-line argument: `--config <path>` or `-c <path>`
2. Process environment variable: `Environment.GetEnvironmentVariable("LOCALAI_CONFIG")`
3. User registry key: `HKCU\Environment\LOCALAI_CONFIG`
4. Directory traversal: walks up parent directories starting from `AppContext.BaseDirectory` checking for `config.env`

If none resolve to an existing file, it throws `FileNotFoundException`.

## Configuration keys

The table below lists every setting defined in [config.env.example](../config.env.example). "Built-in default" is what the code uses when the value is blank or the key is missing; the template ships its own values for several keys (for example `MODEL_ALIAS=occamy-1.0-fit-quality`, `ECORE_MASK=0x3FC3C`, `MODEL_DIRS=C:\ModelCache`).

| Key | Built-in default | Description | Readers |
|---|---|---|---|
| `LOCALAI_HOME` | Folder of `config.env` (service). The launcher skips the ExecGuard helpers when blank | Absolute path to repository checkout directory | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Service/Supervisor.cs` |
| `SCRIPTS_DIR` | `LOCALAI_HOME\Launcher` (service). The Bridge needs it set to find `Start-LocalAI.ps1` | Directory containing launcher scripts (`Start-LocalAI.ps1`, `llama-wake.py`) | `Service/LocalAIConfig.cs`, `Service/Supervisor.cs`, `Bridge/localagent.py` |
| `MODEL_DIRS` | `<every drive>:\ModelCache` | Semicolon-separated directories searched for the GGUF model, vision projector and chat template | `Launcher/Start-LocalAI.ps1` |
| `LLAMA_DIR` | `%LOCALAPPDATA%\Programs\llama.cpp` | Directory containing official llama.cpp CUDA binaries (`llama-server.exe`) | `Launcher/Start-LocalAI.ps1` |
| `LOG_DIR` | `%USERPROFILE%\.openclaw\logs` | Directory for host, proxy, gateway and llama-server logs | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Service/Supervisor.cs`, `Service/FileLog.cs`, `Service/ControlServer.cs` |
| `PYTHON` | First `python.exe` on PATH (the service skips the `WindowsApps` Store stub) | Python executable for the wake proxy and the ExecGuard helper scripts. Set it: a service may not see the user PATH | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `MODEL_FILE` | None (required) | Filename of primary GGUF model | `Launcher/Start-LocalAI.ps1` |
| `MMPROJ_FILE` | Empty (text only) | Filename of vision projector GGUF, or empty for text-only | `Launcher/Start-LocalAI.ps1` |
| `MODEL_ALIAS` | `local-model` (launcher, service); `occamy-1.0-fit-quality` (Bridge) | Model identifier served by llama-server and the wake proxy and sent in API calls. The proxy's own `--alias` default (`qwen3.6-35b-a3b-apex`) only applies when it is started without `--alias`; the service and launcher always pass it. The guards use their own `reviewer.model` | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Bridge/localagent.py` |
| `CHAT_TEMPLATE_FILE` | Empty | Filename of custom Jinja template, or empty to use template embedded in GGUF | `Launcher/Start-LocalAI.ps1` |
| `CONTEXT` | `131072` | Standard context size in tokens (128K); also the pipe's `default` | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Service/ControlServer.cs`, `Bridge/localagent.py` |
| `HEAVY_CONTEXT` | `262144` | Expanded context size in tokens (256K) for the pipe's `heavy` and Bridge escalation | `Service/ControlServer.cs`, `Bridge/localagent.py` |
| `IDLE_UNLOAD_MINUTES` | `20` | Minutes of inactivity before wake proxy unloads model (0 disables auto-unload) | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `SAMPLING` | `occamy` | Model sampling parameters profile: `occamy` or `swift` | `Launcher/Start-LocalAI.ps1` |
| `MTP_DRAFT` | `0` | Number of draft tokens for speculative decoding (0 disables) | `Launcher/Start-LocalAI.ps1` |
| `FIT_TARGET` | `100` | Target unallocated VRAM margin in MiB for dynamic layer offloading | `Launcher/Start-LocalAI.ps1` |
| `IMAGE_MAX_TOKENS` | `768` | Maximum tokens per input image (0 uses model default) | `Launcher/Start-LocalAI.ps1` |
| `MMPROJ_GPU` | `false` | Vision projector on the GPU when true; on the CPU when false (the model keeps that VRAM: 27-31 vs 25.3 tok/s generation on the RTX 3050; an image takes ~10 s to encode) | `Launcher/Start-LocalAI.ps1` |
| `KEEP_AWAKE` | `true` | Prevents Windows idle sleep while model is loaded or loading | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `GPU_UUID` | None | Device UUID assigned to `CUDA_VISIBLE_DEVICES` (`nvidia-smi -L`) | `Launcher/Start-LocalAI.ps1` |
| `ECORE_MASK` | None: set it when `PIN_ECORES` is true | Hex CPU affinity mask for `llama-server.exe`. The template value `0x3FC3C` is the E-cores (CPUs 2-5, 10-17) of an Intel Core Ultra 7 265K | `Launcher/Start-LocalAI.ps1` |
| `PIN_ECORES` | `true` | Pins `llama-server.exe` process affinity to `ECORE_MASK` | `Launcher/Start-LocalAI.ps1` |
| `HOST` | `127.0.0.1` | Address clients use to reach the proxy, llama-server and gateway; llama-server's `--host`. The wake proxy itself always binds `127.0.0.1` | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Bridge/localai_config.py`, `shared/config.js` |
| `PROXY_PORT` | `8080` | TCP port for wake-on-request proxy | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Service/ControlServer.cs`, `Bridge/localai_config.py`, `shared/config.js` |
| `LLAMA_PORT` | `8081` | TCP port for backend `llama-server.exe` | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `GATEWAY_PORT` | `18789` | TCP port for OpenClaw gateway | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `SUPERVISE_GATEWAY` | `true` | Enables service supervision of OpenClaw gateway | `Service/Supervisor.cs` |
| `SUPERVISE_WEBUI` | `true` | Enables service supervision of Open WebUI and auth proxy | `Service/Supervisor.cs` |
| `WEBUI_DIR` | `LOCALAI_HOME\WebUI` | Directory containing Open WebUI installation and virtual environment | `Service/Supervisor.cs` |
| `PCORE_MASK` | `0xC03C3` | CPU affinity mask for P-cores applied to Open WebUI and auth proxy | `Service/Supervisor.cs` |

Booleans read as false for `false`, `0`, `no` or `off`. The .NET reader also strips surrounding quotes and accepts masks with or without `0x`.

The template header also lists `ExecGuard/*.py` as readers, but `guard_check.py` and `patch_discord_card.py` use fixed `~/.openclaw` paths and do not read `config.env`.

## Other configuration files

- `WebUI\.env` (git-ignored): `WEBUI_PORT` (auth proxy, default 8337), `WEBUI_BACKEND_PORT` (Open WebUI, default 8338), `WEBUI_PASSWORD` (LAN login), optional `WEBUI_PROXY_PYTHON`. The service reads the two ports from here, not from `config.env`.
- `WebUI\openwebui.env` (git-ignored): Open WebUI settings passed to the `webui` child.
- [Bridge/config.json](../Bridge/config.json): Bridge defaults, see [bridge.md](bridge.md#configuration-keys-in-bridgeconfigjson).
- `~/.openclaw/openclaw.json`: plugin paths and plugin config, see [openclaw-plugins.md](openclaw-plugins.md).

## Unread keys

All 30 keys in `config.env.example` are read by at least one component in the repository. No unused keys exist in the configuration template.
