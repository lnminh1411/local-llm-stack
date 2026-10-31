# Launcher and wake proxy

The launcher scripts and wake proxy start, configure, monitor, and unload the local model.

## Start-LocalAI.ps1

[Start-LocalAI.ps1](../Launcher/Start-LocalAI.ps1) starts the stack components and sets model flags for `llama-server`.

### Parameters

All parameters read defaults from `config.env` unless specified on the command line:

| Parameter | Type | Default source | Description |
|---|---|---|---|
| `-ModelFile` | string | `MODEL_FILE` | GGUF model file name |
| `-MmprojFile` | string | `MMPROJ_FILE` | Vision projector GGUF file name, or empty for text only |
| `-ModelAlias` | string | `MODEL_ALIAS` | Model alias name exposed by server (default `local-model`) |
| `-ChatTemplateFile` | string | `CHAT_TEMPLATE_FILE` | Custom Jinja template file name, or empty to use GGUF template |
| `-PinECores` | bool | `PIN_ECORES` | Pins `llama-server` to E-cores when true (default true) |
| `-MtpDraft` | int | `MTP_DRAFT` | Draft token count for MTP speculative decoding (0 disables) |
| `-MmprojGpu` | bool | `MMPROJ_GPU` | Keeps vision projector on GPU when true, CPU when false (default true) |
| `-ImageMaxTokens` | int | `IMAGE_MAX_TOKENS` | Maximum image token limit (default 768) |
| `-Sampling` | string | `SAMPLING` | Sampling profile: `occamy` or `swift` (default `occamy`) |
| `-FitTarget` | int | `FIT_TARGET` | Target free VRAM margin in MiB (default 100) |
| `-Context` | int | `CONTEXT` | Model context window in tokens (default 131072) |
| `-IdleUnloadMinutes` | double | `IDLE_UNLOAD_MINUTES` | Idle minutes before auto-unload (default 20) |
| `-KeepAwake` | bool | `KEEP_AWAKE` | Blocks Windows idle sleep while model is loaded (default true) |
| `-Restart` | switch | None | Forces restart of managed stack processes |
| `-NoGateway` | switch | None | Skips starting or restarting OpenClaw gateway |
| `-Warm` | switch | None | Runs a warmup query to populate prompt cache |
| `-NoModel` | switch | None | Starts proxy and gateway without loading model |
| `-ProxyOnly` | switch | None | Starts wake proxy only if port is closed |
| `-BackendOnly` | switch | None | Starts `llama-server.exe` directly on backend port and returns |

### Service and standalone modes

When the `LocalAIHost` Windows service is installed:
- If the service is stopped, `Start-LocalAI.ps1` starts it.
- If `-Restart` is specified, it stops `llama-server`, sends `restart wake-proxy` to `\\.\pipe\LocalAIHost`, and sends `restart gateway` unless `-NoGateway` is present.
- It waits up to 60 seconds for proxy port `PROXY_PORT` to open.
- If `-ProxyOnly` is present, it exits immediately after the proxy port is open.
- Unless `-NoModel` is specified, it sends an HTTP POST request to `http://127.0.0.1:8080/_gate/ensure` with context size `Context` and blocks until the model is loaded.
- If `-Warm` is set (and neither `-NoGateway` nor `-NoModel` is active), it waits for gateway port `GATEWAY_PORT` and runs `openclaw agent --agent main -m 'Reply with the single word: ready'`.

When running without the `LocalAIHost` service (standalone mode):
- If `-Restart` is specified, it kills processes matching `llama-server`, `llama-wake.py`, and the gateway entry point.
- If the proxy port is closed, it starts `llama-wake.py` directly using Python and logs output to `LOG_DIR\wake-proxy.log` and `LOG_DIR\wake-proxy-err.log`.
- Unless `-NoModel` is specified, it issues an HTTP POST request to `/_gate/ensure` to load the model.
- Unless `-NoGateway` is specified, it runs `ExecGuard\patch_discord_card.py`, starts `cmd.exe /c openclaw gateway run`, and runs `ExecGuard\guard_check.py --wait 45`.
- If `-Warm` is set, it executes the prompt cache warmup query.

### Backend-only mode

When invoked with `-BackendOnly`, `Start-LocalAI.ps1` bypasses all service, proxy, and gateway checks. It launches `llama-server.exe` on `LLAMA_PORT` (:8081) directly and waits for `http://127.0.0.1:8081/health` to return HTTP 200. The wake proxy invokes `-BackendOnly` when demand requires loading the model.

### Llama-server flags

`Start-LocalAI.ps1` sets the following command-line flags on `llama-server.exe`:

| Flag | Value or setting | Purpose |
|---|---|---|
| `--model` | Path to resolved GGUF file | Specifies model weights file |
| `--alias` | Value of `$ModelAlias` | Names model alias exposed to clients |
| `--host` | Value of `$HostAddr` (`127.0.0.1`) | Sets HTTP listener bind address |
| `--port` | Value of `$LlamaPort` (`8081`) | Sets HTTP listener backend port |
| `--no-webui` | Present | Disables built-in web frontend |
| `-np` | `1` | Sets parallel processing slots to 1 |
| `-c` | Value of `$Context` | Sets context window length |
| `-t` | `12` | Sets compute threads to 12 |
| `-tb` | `12` | Sets batch compute threads to 12 |
| `-b` | `4096` | Sets logical batch size |
| `-ub` | `4096` | Sets physical micro-batch size |
| `--flash-attn` | `on` | Enables flash attention kernel |
| `-ctk` | `q8_0` or `q4_0` | Sets key cache quantization (`q4_0` if alias starts with `swift` and context > 65536, else `q8_0`) |
| `-ctv` | `q8_0` or `q4_0` | Sets value cache quantization (`q4_0` if alias starts with `swift` and context > 65536, else `q8_0`) |
| `--fit` | `on` | Enables automatic VRAM layer allocation |
| `--fit-target` | Value of `$FitTarget` (default 100) | Sets target unallocated VRAM margin in MiB |
| `--load-mode` | `none` | Disables memory mapping to load weights into RAM |
| `--jinja` | Present | Enables Jinja chat template rendering |
| `--reasoning` | `off` | Disables internal thinking output |
| `--cache-ram` | `6144` | Allocates 6144 MiB for RAM caching |
| `--temp` | `0.7` (occamy) or `1.0` (swift) | Sets generation temperature |
| `--top-p` | `0.8` (occamy) or `0.95` (swift) | Sets top-p nucleus sampling |
| `--top-k` | `20` | Sets top-k filtering limit |
| `--min-p` | `0` | Sets min-p sampling threshold |
| `--presence-penalty` | `1.5` | Sets presence penalty (applied when sampling profile is `occamy`) |
| `--chat-template-file` | Path to template file | Specifies custom Jinja template (applied when `$ChatTemplateFile` is set) |
| `--spec-type` | `draft-mtp` | Sets speculative decoding method (applied when `$MtpDraft` > 0) |
| `--spec-draft-n-max` | Value of `$MtpDraft` | Sets draft token limit (applied when `$MtpDraft` > 0) |
| `--mmproj` | Path to projector file | Specifies vision projector file (applied when `$MmprojFile` is set) |
| `--no-mmproj-offload` | Present | Keeps projector on CPU (applied when `$MmprojGpu` is false) |
| `--image-max-tokens` | Value of `$ImageMaxTokens` | Caps image token output (applied when `$ImageMaxTokens` > 0) |

### E-core pinning and environment

Before launching `llama-server.exe`, `Start-LocalAI.ps1`:
- Sets `$env:CUDA_VISIBLE_DEVICES = $GpuUuid` to restrict execution to the RTX 3050.
- Prepends `$LlamaDir` to `$env:PATH` so DLLs resolve beside the executable.
- Removes `GGML_BACKEND_PATH` to allow internal CUDA backend discovery.
- Sets process priority to `Normal`.
- If `$PinECores` is true, sets `ProcessorAffinity` to `$ECoreMask` (default `0x3FC3C`, corresponding to Intel Core Ultra 7 265K E-core CPUs 2 to 5 and 10 to 17).

## llama-wake.py

[llama-wake.py](../Launcher/llama-wake.py) is a reverse proxy running on `127.0.0.1:8080`. It intercepts inference requests, starts `llama-server.exe` on demand, and unloads it during idle periods.

### Command-line arguments

| Argument | Type | Default | Description |
|---|---|---|---|
| `--listen-port` | int | `8080` | Port the proxy listens on |
| `--backend-port` | int | `8081` | Port of the backend `llama-server` process |
| `--ctx` | int | `131072` | Default context window size |
| `--idle-minutes` | float | `20` | Idle timeout before unloading model (0 disables) |
| `--alias` | string | `qwen3.6-35b-a3b-apex` | Model name returned in API responses |
| `--launcher` | string | Required | Path to `Start-LocalAI.ps1` |
| `--no-keep-awake` | flag | Present disables | Disables Windows idle sleep blocking while loaded |

### Request waking

Incoming HTTP requests follow two paths:
1. Passive endpoints (`/health`, `/props`, `/slots`, `/v1/models`, `/models`, `/_gate/*`): return canned responses immediately if the backend is asleep. Passive requests do not wake the model or update the activity timestamp.
2. Active requests (such as `/v1/chat/completions`): update the activity timestamp and call `ensure()`.

When `ensure()` runs while the model is asleep:
- It launches `powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File <launcher> -BackendOnly -Context <ctx>`.
- The proxy state changes to `waking`.
- It polls `http://127.0.0.1:8081/health` every 2 seconds for up to 480 seconds until it receives HTTP 200.
- Once ready, the state changes to `awake`.
- The proxy forwards the pending request to port 8081 and streams the backend response back to the client.

### Idle unload

A background watchdog thread checks activity every 30 seconds. If:
- `idle_minutes` is greater than 0,
- No requests are in flight (`inflight == 0`),
- The proxy is not currently waking a backend,
- Elapsed time since last active request exceeds `idle_minutes * 60`,
- The backend is currently ready,

the watchdog triggers `sleep_now()`. The proxy runs `taskkill /F /T /IM llama-server.exe`, waits for the port to close, pauses 1.5 seconds for the GPU driver to release VRAM, and resets the target context size to the default `--ctx` value.

### Keep-awake mechanism

When enabled, a background thread calls the Windows kernel function `SetThreadExecutionState` every 10 seconds.
- While the backend is waking or ready, it sets `ES_CONTINUOUS | ES_SYSTEM_REQUIRED` (flags `0x80000000 | 0x00000001`). This prevents Windows from entering idle sleep while model weights occupy VRAM.
- When the backend unloads, it calls `SetThreadExecutionState(ES_CONTINUOUS)` to clear the system sleep block.

### Context resize and the 256K path

Clients can request context adjustments using `POST /_gate/ensure {"ctx": N, "exact": bool}` or control pipe commands (`model ctx heavy`).
- If `exact` is true, a restart occurs if current context does not equal target context.
- If `exact` is false, a restart occurs if current context is smaller than target context.
- Before restarting, the proxy waits up to 180 seconds for in-flight requests to complete.
- It then stops the existing backend and calls `Start-LocalAI.ps1 -BackendOnly -Context 262144`.
- On subsequent idle unload, desired context resets to the default 128K (`131072`). Context enlargement to 256K does not persist past an unload.

### Admin endpoints

The proxy provides administrative endpoints under `/_gate/*`:

| Endpoint | Method | Payload | Description |
|---|---|---|---|
| `/_gate/status` | GET | None | Returns JSON status: `state`, `ctx`, `desired_ctx`, `inflight`, `idle_s`, `idle_minutes`, `keep_awake`, `sleep_blocked`, and `unload_in_s`. |
| `/_gate/ensure` | POST | `{"ctx": <int>, "exact": <bool>}` | Starts or resizes the backend to the requested context. Blocks until ready. |
| `/_gate/sleep` | POST | `{"force": <bool>}` | Unloads the backend. Refused if requests are in flight unless `force` is true. |
| `/_gate/config` | POST | `{"idle_minutes": <float>, "keep_awake": <bool>}` | Updates idle unload delay and sleep prevention settings dynamically. |

For status hub endpoints (`/_gate/ai`, `/_gate/ai/flat`, `/_gate/ai/stream`, `/_gate/event`), see [Status hub](status-hub.md).

## LocalAI-Config.ps1

[LocalAI-Config.ps1](../Launcher/LocalAI-Config.ps1) loads configuration values from `config.env`.

See [configuration.md](configuration.md) for the lookup order. `Get-LocalAIConfig` parses lines matching `KEY=VALUE`, ignoring empty lines and comments beginning with `#`. `Get-Cfg` retrieves keys with a fallback default.

## Upgrading llama.cpp

The stack uses official Windows CUDA 12.4 release binaries from the llama.cpp project.

Upgrade steps:
1. Download matching release archives for the target build:
   - `llama-b<BUILD>-bin-win-cuda-12.4-x64.zip`
   - `cudart-llama-bin-win-cuda-12.4-x64.zip`
2. Extract all contents of both archives flat into `LLAMA_DIR` (`%LOCALAPPDATA%\Programs\llama.cpp` by default), overwriting existing executables and DLLs.
3. Send `model reload` on the [control pipe](service.md#control-pipe), or run `.\Launcher\Start-LocalAI.ps1 -Restart`.

## Measured performance

Performance benchmarked on llama.cpp release b12005 with Occamy-1.0 35B-A3B FIT-QUALITY IQ4_XS and 4-bit vision projector on NVIDIA GeForce RTX 3050 6 GB and Intel Core Ultra 7 265K:

| Metric | Measurement |
|---|---|
| GPU VRAM allocation | 5.4 GB |
| Prompt processing (prefill) at 18.7k context | 665 tok/s |
| Generation speed on short prompt | 22 tok/s |
| Generation speed at 18.7k context | 18 tok/s |
