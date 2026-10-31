# Local LLM stack

A private, always-available local AI on one Windows PC (RTX 3050 6 GB, Intel Core Ultra 7 265K), plus the glue around it: a Windows service that keeps it all running, a wake-on-request proxy, a Discord agent with guardrails, a delegation CLI for cloud coding agents, a chat UI, and a live status feed and control pipe for a small ESP32 display.

Companion project: **[ESP32-PC-Control-System](https://github.com/lnminh1411/ESP32-PC-Control-System)**, the ESP32-S3 carrier board with three ST7789 displays. Its host service streams PC stats to the board; the status feed in this repo (`/_gate/ai/flat`) shows on the same displays what the local AI is doing: phase, token speed, time taken, current tool, guardrail state. See [StatusHub.md](StatusHub.md). The control pipe (`\\.\pipe\LocalAIHost`) lets the board's host service load, unload and reload the model or restart a process; the board side of that relay is in progress.

## What is in this repo

| Folder | What it is |
|---|---|
| `Service/` | Local AI Host, a .NET 10 Windows service running as the user's account: starts the wake proxy, the OpenClaw gateway and Open WebUI at boot, restarts them when they die, and takes llama-server down with it. Control pipe `\\.\pipe\LocalAIHost` (load, unload, force unload, reload, context size, restart a process) for the ESP32 relay and scripts. `Install-Service.ps1` installs it; [Service/Project_Note.md](Service/Project_Note.md) has the command list. |
| `Launcher/` | The stack's scripts, run in place by the service: `Start-LocalAI.ps1` (llama-server flags, E-core pinning; with the service installed it only starts the service, `-BackendOnly` starts llama-server for the proxy), `llama-wake.py` (wake-on-request proxy, idle unload, keep-awake while loaded), `ai_hub.py` (live status hub), `LocalAI-Config.ps1` (config.env reader). |
| `Bridge/` | Local Agent Bridge: `localagent.py` (the `la` CLI: `ask`, `run`, `status`, `heavy`, `jobs`, `wait`, `cancel`), `harness_worker.py` (runs a `run` job inside DeepSeek Harness), `SKILL.md` (the skill installed for Claude Code and Antigravity), `sync.py` (installs it). |
| `ExecGuard/` | OpenClaw plugin: classifies every shell command. Read-only runs, writes ask the owner, dangerous and secret-touching commands need a red confirmation, catastrophic ones need two. Unknown commands are reviewed by the local model. 112 tests. |
| `ContentGuard/` | OpenClaw plugin, Discord only: blocks 18+, slurs, hate, harassment, self-harm and gore, inbound and outbound, for an uncensored model. |
| `StatusReporter/` | OpenClaw plugin: reports agent runs and tool calls to the status hub. Observer only. |
| `shared/` | `hub.js`, the fire-and-forget reporter the plugins use. |
| `config.env.example` | The one config file: folders, model files, GPU UUID, E-core mask, ports, context, idle time, what the service supervises. Copy to `config.env` (git-ignored). Read by the service, the launcher, the Bridge and the OpenClaw plugins. |
| `StatusHub.md` | The status feed: endpoints, every key, state codes, how to wire it to the ESP32. |
| `Project_Note.md` | Working notes: decisions, measurements, traps. |

Not in the repo (git-ignored): `WebUI/` (Open WebUI install, database, password env, 3 GB), `Bridge/harness/` (venv), `Bridge/legacy/` (old bridge).

## The pieces

**Local AI Host service.** One .NET 10 Windows service, running as your own account, starts everything at boot, before anyone signs in: the wake proxy, the OpenClaw gateway (re-applying the ExecGuard patch and running its fail-safe check), Open WebUI and its auth proxy. The model itself stays unloaded. A process that dies is restarted with a growing delay, and one whose port stays closed for a minute is treated as hung and restarted. If something else already serves a port, the service waits instead of starting a second copy. The service holds every child in a kill-on-close job object, so stopping or crashing it also frees the VRAM. Its named pipe takes one text command per line and answers with one JSON line:

| Command | Effect |
|---|---|
| `status` | each process's state, pid and restarts, plus the model state |
| `model load [ctx]`, `model reload [ctx]` | load (or force-unload, then load) the model |
| `model unload`, `model unload force` | unload when idle, or unload even in the middle of a request |
| `model ctx <n\|128k\|default\|heavy>` | restart the model at an exact context size |
| `model idle <minutes>`, `model keepawake <on\|off>` | auto-unload delay, keep Windows awake while loaded |
| `restart <name\|all>`, `stop <name>`, `start <name>` | wake-proxy, gateway, webui, authproxy |
| `gateway warm`, `guard check` | prime OpenClaw's prompt cache, re-run the ExecGuard check |

**Wake proxy and model.** `llama-server` runs Occamy-1.0 abliterated (IQ4_XS, 4-bit vision projector) with a 128K context, pinned to the 12 E-cores so the P-cores stay free. The proxy on :8080 is the only address clients use. It starts the model on the first real request, unloads it after 20 idle minutes, holds Windows awake while the model is loaded, and resizes the context on demand (256K for huge inputs). The Local AI Host service restarts the proxy if it dies. Measured on this card: about 26 tok/s generation, 250 to 600 tok/s prompt processing.

**Local Agent Bridge.** Lets a cloud agent hand bulk work to the local model for free and in private. `la ask` answers over files or stdin. `la run` is a real agent harness ([DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)): read, grep, glob, write, edit, todo, image read, and a `skill` tool that loads skills from `~/.agents/skills`. Permission follows the flag: none is read-only, `--write` confines writes to `--cwd`, `--exec` adds an unsandboxed PowerShell (dsh's Windows shell sandbox does not start in the SDK build), `--web` adds `web_fetch`. Sub-agents, goals and plan mode are switched off, DeepSeek telemetry and log upload are disabled. `--legacy` selects the older built-in tool loop. `--detach` returns a job id for long jobs.

**OpenClaw agent.** A Discord bot on the same model. Shell access goes through ExecGuard; every Discord message and reply goes through ContentGuard. Both fail safe.

**Status hub.** Everything that touches the model reports into `ai_hub.py` inside the proxy: inference phase (prompt processing, generating, tool, waiting approval), live and last token speed, time taken, current tool, Bridge job step, guard state (checking, tripped, waiting approval, approved, denied, timed out), GPU load and VRAM. Read it as JSON, as `key=value` lines for a microcontroller, or as server-sent events. Localhost only; reading it never wakes the model.

## Setup (this PC)

Windows only. Every machine-specific value (paths, GPU UUID, core mask, ports, model files) lives in `config.env`; treat the repo as a reference build, not a package.

1. `copy config.env.example config.env`, edit it (`nvidia-smi -L` gives `GPU_UUID`; `MODEL_DIRS` is where the Occamy GGUF + projector live), then `setx LOCALAI_CONFIG "<repo>\config.env"`. Install llama.cpp: extract the official Windows CUDA 12.4 release (`llama-bXXXXX-bin-win-cuda-12.4-x64.zip` + `cudart-llama-bin-win-cuda-12.4-x64.zip`) into one folder, `%LOCALAPPDATA%\Programs\llama.cpp` by default (`LLAMA_DIR`).
2. Set `SCRIPTS_DIR` to the repo's `Launcher` folder, then from an elevated PowerShell run `Service\Install-Service.ps1` (needs the .NET 10 SDK; asks for your Windows password, not the PIN, because the service runs as your account). At boot the proxy, the gateway and Open WebUI start with the model unloaded; it loads on the first real request (or `model load` on the pipe) and unloads after the idle time. Re-run the installer after a password change.
3. OpenClaw: add `ExecGuard`, `ContentGuard`, `StatusReporter` to `plugins.load.paths` in `~/.openclaw/openclaw.json`. Non-bundled plugins that read conversations need `plugins.entries.<id>.hooks.allowConversationAccess: true` (without it OpenClaw 2026.7 silently blocks the hook). Restart the gateway (`restart gateway` on the pipe, or `Launcher\Start-LocalAI.ps1 -Restart`).
4. Bridge: `python -m venv Bridge/harness/venv`, `Bridge/harness/venv/Scripts/pip install deepseek-harness-sdk`, then `python Bridge/sync.py` to install the `local-agent` skill for Claude Code and Antigravity. Use it as `python ~/.local-agent/la.py run "task" --cwd <dir>`.
5. Tests: `node --test "ExecGuard/test/*.test.js"` and `node --test "ContentGuard/test/*.js"`.

## Safety notes

- `la run --exec` is unsandboxed. Grant it only when the task needs a shell.
- The service runs as your account, never as SYSTEM, so the Discord agent's shell never gets SYSTEM rights. Windows keeps the account password as an LSA secret for the service. Only SYSTEM, Administrators and that account can open the control pipe.
- The DeepSeek harness is developer-preview software and has not been security audited.
- ContentGuard ships a base term list of slurs (used for masking and blocking); nothing else in the repo is sensitive. Secrets (Discord token, gateway token, WebUI password) live in `~/.openclaw/openclaw.json` and `WebUI/`, both outside version control.

## License

GNU Affero General Public License v3.0, see [LICENSE](LICENSE).
