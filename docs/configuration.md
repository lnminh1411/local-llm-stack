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

The table below lists every setting defined in [config.env.example](../config.env.example):

| Key | Default value | Description | Readers |
|---|---|---|---|
| `LOCALAI_HOME` | `C:\Local LLM` | Absolute path to repository checkout directory | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Service/Supervisor.cs` |
| `SCRIPTS_DIR` | `C:\Local LLM\Launcher` | Directory containing launcher scripts (`Start-LocalAI.ps1`, `llama-wake.py`) | `Service/LocalAIConfig.cs`, `Service/Supervisor.cs`, `Bridge/localagent.py` |
| `MODEL_DIRS` | `C:\ModelCache` | Semicolon-separated directories searched for GGUF model and vision projector weights | `Launcher/Start-LocalAI.ps1` |
| `LLAMA_DIR` | `%LOCALAPPDATA%\Programs\llama.cpp` | Directory containing official llama.cpp CUDA binaries (`llama-server.exe`) | `Launcher/Start-LocalAI.ps1` |
| `LOG_DIR` | `%USERPROFILE%\.openclaw\logs` | Directory for process logs, stdout, stderr, and action logs | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Service/Supervisor.cs`, `Service/FileLog.cs`, `Service/ControlServer.cs` |
| `PYTHON` | First `python.exe` on PATH | Path to Python executable for proxy and helper scripts | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Bridge/localagent.py` |
| `MODEL_FILE` | `occamy-1.0-abliterated-FIT-QUALITY-17G-IQ4_XS.gguf` | Filename of primary GGUF model | `Launcher/Start-LocalAI.ps1` |
| `MMPROJ_FILE` | `mmproj-occamy-1.0-abliterated-Q4_0.gguf` | Filename of vision projector GGUF, or empty for text-only | `Launcher/Start-LocalAI.ps1` |
| `MODEL_ALIAS` | `occamy-1.0-fit-quality` | Model identifier returned by OpenAI-compatible API endpoints | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Bridge/localagent.py` |
| `CHAT_TEMPLATE_FILE` | Empty | Filename of custom Jinja template, or empty to use template embedded in GGUF | `Launcher/Start-LocalAI.ps1` |
| `CONTEXT` | `131072` | Standard context size in tokens (128K) | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Service/ControlServer.cs`, `Bridge/localagent.py` |
| `HEAVY_CONTEXT` | `262144` | Expanded context size in tokens (256K) used for heavy agent workloads | `Service/ControlServer.cs`, `Bridge/localagent.py` |
| `IDLE_UNLOAD_MINUTES` | `20` | Minutes of inactivity before wake proxy unloads model (0 disables auto-unload) | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `SAMPLING` | `occamy` | Model sampling parameters profile: `occamy` or `swift` | `Launcher/Start-LocalAI.ps1`, `Bridge/localagent.py` |
| `MTP_DRAFT` | `0` | Number of draft tokens for speculative decoding (0 disables) | `Launcher/Start-LocalAI.ps1` |
| `FIT_TARGET` | `100` | Target unallocated VRAM margin in MiB for dynamic layer offloading | `Launcher/Start-LocalAI.ps1` |
| `IMAGE_MAX_TOKENS` | `768` | Maximum tokens generated per input image (0 uses model default) | `Launcher/Start-LocalAI.ps1` |
| `MMPROJ_GPU` | `true` | Offloads vision projector to GPU when true; keeps on CPU when false | `Launcher/Start-LocalAI.ps1` |
| `KEEP_AWAKE` | `true` | Prevents Windows idle sleep while model is resident in VRAM | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `GPU_UUID` | `GPU-00000000-0000-0000-0000-000000000000` | Device UUID assigned to `CUDA_VISIBLE_DEVICES` | `Launcher/Start-LocalAI.ps1` |
| `ECORE_MASK` | `0x3FC3C` | CPU affinity mask for E-cores on Intel Core Ultra 7 265K | `Launcher/Start-LocalAI.ps1` |
| `PIN_ECORES` | `true` | Pins `llama-server.exe` process affinity to `ECORE_MASK` | `Launcher/Start-LocalAI.ps1` |
| `HOST` | `127.0.0.1` | Local IPv4 address for network bindings | `Launcher/Start-LocalAI.ps1`, `Service/LocalAIConfig.cs`, `Bridge/localai_config.py`, `shared/config.js` |
| `PROXY_PORT` | `8080` | TCP port for wake-on-request proxy | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs`, `Service/ControlServer.cs`, `Bridge/localai_config.py`, `shared/config.js` |
| `LLAMA_PORT` | `8081` | TCP port for backend `llama-server.exe` | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `GATEWAY_PORT` | `18789` | TCP port for OpenClaw gateway | `Launcher/Start-LocalAI.ps1`, `Service/Supervisor.cs` |
| `SUPERVISE_GATEWAY` | `true` | Enables service supervision of OpenClaw gateway | `Service/Supervisor.cs` |
| `SUPERVISE_WEBUI` | `true` | Enables service supervision of Open WebUI and auth proxy | `Service/Supervisor.cs` |
| `WEBUI_DIR` | `LOCALAI_HOME\WebUI` | Directory containing Open WebUI installation and virtual environment | `Service/Supervisor.cs` |
| `PCORE_MASK` | `0xC03C3` | CPU affinity mask for P-cores applied to Open WebUI and auth proxy | `Service/Supervisor.cs` |

## Unread keys

All 30 keys in `config.env.example` are read by at least one component in the repository. No unused keys exist in the configuration template.
