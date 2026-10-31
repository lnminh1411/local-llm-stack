# Setup

## Prerequisites

- Windows x64.
- NVIDIA GPU with CUDA support.
- Python on `PATH`.
- .NET 10 SDK (required to compile LocalAIHost).
- Node.js (required to run the plugin test suites).
- OpenClaw, configured in `%USERPROFILE%\.openclaw`.

## Configuration

1. Copy the example configuration:
   ```powershell
   Copy-Item config.env.example config.env
   ```
2. Edit `config.env` with your system values:
   - Run `nvidia-smi -L` to find your `GPU_UUID`.
   - Set `MODEL_DIRS` to your model storage locations.
   - Adjust `ECORE_MASK` if your CPU core layout differs from the Intel Core Ultra 7 265K.
   See [configuration.md](configuration.md) for all configuration keys.
3. Register the config path in your user environment:
   ```powershell
   setx LOCALAI_CONFIG "<path-to-repo>\config.env"
   ```

## Install llama.cpp

The stack uses the official Windows CUDA 12.4 build of llama.cpp:
1. Download `llama-bXXXXX-bin-win-cuda-12.4-x64.zip` and `cudart-llama-bin-win-cuda-12.4-x64.zip`.
2. Extract both archives into the same directory. The default path is `%LOCALAPPDATA%\Programs\llama.cpp`.
3. If using a different directory, set `LLAMA_DIR` in `config.env`.

## Install LocalAIHost service

The service boots the stack before user sign-in and keeps the model unloaded until the first request.

1. Set `SCRIPTS_DIR` in `config.env` to the repo `Launcher` directory.
2. Open an elevated PowerShell session.
3. Run the installation script:
   ```powershell
   .\Service\Install-Service.ps1
   ```
4. Enter your Windows account password when prompted (a Windows Hello PIN is rejected). The script grants `SeServiceLogonRight`, builds the executable, and registers a delayed-auto service.

Re-run this script after any Windows account password change. See [service.md](service.md) for service management and pipe commands.

## Register OpenClaw plugins

1. Edit `%USERPROFILE%\.openclaw\openclaw.json`.
2. Add the directory paths for `ExecGuard`, `ContentGuard`, and `StatusReporter` to `plugins.load.paths`.
3. Set `hooks.allowConversationAccess: true` for `ContentGuard` and `StatusReporter` in `plugins.entries`.
4. Restart the gateway by sending `restart gateway` to `\\.\pipe\LocalAIHost`, or run:
   ```powershell
   .\Launcher\Start-LocalAI.ps1 -Restart
   ```
See [openclaw-plugins.md](openclaw-plugins.md) for plugin hooks and configuration.

## Configure Local Agent Bridge

1. Create the harness Python virtual environment:
   ```powershell
   python -m venv Bridge\harness\venv
   .\Bridge\harness\venv\Scripts\pip install deepseek-harness-sdk
   ```
2. Install the bridge shim and register the `local-agent` skill for Claude Code and Antigravity:
   ```powershell
   python .\Bridge\sync.py
   ```
See [bridge.md](bridge.md) for CLI commands and harness modes.

## Moving the repository

When moving the repository checkout to another folder or drive:
1. Update `LOCALAI_HOME` and `SCRIPTS_DIR` in `config.env`.
2. Update the `LOCALAI_CONFIG` environment variable if the file moved.
3. Update `plugins.load.paths` in `%USERPROFILE%\.openclaw\openclaw.json`.
4. Re-run `python .\Bridge\sync.py` to refresh `~/.local-agent/la.py`.
5. Re-run `.\Service\Install-Service.ps1` from an elevated prompt to update service binary paths.

## Run tests

Run both test suites with the Node.js test runner:

```powershell
node --test "ExecGuard/test/*.test.js"
node --test "ContentGuard/test/*.js"
```
