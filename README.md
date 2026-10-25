# Local LLM stack

A private, always-available local AI on one Windows PC (RTX 3050 6 GB, Intel Core Ultra 7 265K), plus the glue around it: a wake-on-request proxy, a Discord agent with guardrails, a delegation CLI for cloud coding agents, a chat UI, and a live status feed for a small ESP32 display.

Companion project: **[ESP32-PC-Control-System](https://github.com/lnminh1411/ESP32-PC-Control-System)**, the ESP32-S3 carrier board with three ST7789 displays. Its host service streams PC stats to the board; the status feed in this repo (`/_gate/ai/flat`) shows on the same displays what the local AI is doing: phase, token speed, time taken, current tool, guardrail state. See [StatusHub.md](StatusHub.md).

## What is in this repo

| Folder | What it is |
|---|---|
| `Launcher/` | Snapshot of the stack's start-up scripts (the live copies run from `F:\!PC Scripts`): `Start-LocalAI.ps1` (llama-server flags, E-core pinning, proxy, gateway), `llama-wake.py` (wake-on-request proxy, idle unload, keep-awake while loaded), `ai_hub.py` (live status hub), `LocalAI-Config.ps1` (config.env reader), `Register-Tasks.ps1` (logon-start and watchdog scheduled tasks, native PowerShell, no VBScript). |
| `Bridge/` | Local Agent Bridge: `localagent.py` (the `la` CLI: `ask`, `run`, `status`, `heavy`, `jobs`, `wait`, `cancel`), `harness_worker.py` (runs a `run` job inside DeepSeek Harness), `SKILL.md` (the skill installed for Claude Code and Antigravity), `sync.py` (installs it). |
| `ExecGuard/` | OpenClaw plugin: classifies every shell command. Read-only runs, writes ask the owner, dangerous and secret-touching commands need a red confirmation, catastrophic ones need two. Unknown commands are reviewed by the local model. 112 tests. |
| `ContentGuard/` | OpenClaw plugin, Discord only: blocks 18+, slurs, hate, harassment, self-harm and gore, inbound and outbound, for an uncensored model. |
| `StatusReporter/` | OpenClaw plugin: reports agent runs and tool calls to the status hub. Observer only. |
| `shared/` | `hub.js`, the fire-and-forget reporter the plugins use. |
| `config.env.example` | The one config file: folders, model files, GPU UUID, E-core mask, ports, context, idle time. Copy to `config.env` (git-ignored). Read by the launcher, the Bridge and the OpenClaw plugins. |
| `StatusHub.md` | The status feed: endpoints, every key, state codes, how to wire it to the ESP32. |
| `Project_Note.md` | Working notes: decisions, measurements, traps. |

Not in the repo (git-ignored): `WebUI/` (Open WebUI install, database, password env, 3 GB), `Bridge/harness/` (venv), `Bridge/legacy/` (old bridge).

## The pieces

**Wake proxy and model.** `llama-server` runs Occamy-1.0 abliterated (IQ4_XS, 4-bit vision projector) with a 128K context, pinned to the 12 E-cores so the P-cores stay free. The proxy on :8080 is the only address clients use. It starts the model on the first real request, unloads it after 20 idle minutes, holds Windows awake while the model is loaded, and resizes the context on demand (256K for huge inputs). A scheduled task restarts the proxy if it dies. Measured on this card: about 26 tok/s generation, 250 to 600 tok/s prompt processing.

**Local Agent Bridge.** Lets a cloud agent hand bulk work to the local model for free and in private. `la ask` answers over files or stdin. `la run` is a real agent harness ([DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)): read, grep, glob, write, edit, todo, image read, and a `skill` tool that loads skills from `~/.agents/skills`. Permission follows the flag: none is read-only, `--write` confines writes to `--cwd`, `--exec` adds an unsandboxed PowerShell (dsh's Windows shell sandbox does not start in the SDK build), `--web` adds `web_fetch`. Sub-agents, goals and plan mode are switched off, DeepSeek telemetry and log upload are disabled. `--legacy` selects the older built-in tool loop. `--detach` returns a job id for long jobs.

**OpenClaw agent.** A Discord bot on the same model. Shell access goes through ExecGuard; every Discord message and reply goes through ContentGuard. Both fail safe.

**Status hub.** Everything that touches the model reports into `ai_hub.py` inside the proxy: inference phase (prompt processing, generating, tool, waiting approval), live and last token speed, time taken, current tool, Bridge job step, guard state (checking, tripped, waiting approval, approved, denied, timed out), GPU load and VRAM. Read it as JSON, as `key=value` lines for a microcontroller, or as server-sent events. Localhost only; reading it never wakes the model.

## Setup (this PC)

Windows only. Every machine-specific value (paths, GPU UUID, core mask, ports, model files) lives in `config.env`; treat the repo as a reference build, not a package.

1. `copy config.env.example config.env`, edit it (`nvidia-smi -L` gives `GPU_UUID`; `MODEL_DIRS` is where the Occamy GGUF + projector live), then `setx LOCALAI_CONFIG "<repo>\config.env"`. `llama-server.exe` is taken from Ollama's `lib\ollama` folder.
2. Copy `Launcher/*` to `SCRIPTS_DIR`, run `Start-LocalAI.ps1`, then `Register-Tasks.ps1` once. At logon only the wake proxy and the OpenClaw gateway start (`-NoModel`); the model loads on the first real request and unloads after the idle time. The tasks run `conhost.exe --headless powershell.exe ...`, so nothing ever opens a window (Task Scheduler's Last Run Result always reads 0 that way; check the logs).
3. OpenClaw: add `ExecGuard`, `ContentGuard`, `StatusReporter` to `plugins.load.paths` in `~/.openclaw/openclaw.json`. Non-bundled plugins that read conversations need `plugins.entries.<id>.hooks.allowConversationAccess: true` (without it OpenClaw 2026.7 silently blocks the hook). Restart the gateway.
4. Bridge: `python -m venv Bridge/harness/venv`, `Bridge/harness/venv/Scripts/pip install deepseek-harness-sdk`, then `python Bridge/sync.py` to install the `local-agent` skill for Claude Code and Antigravity. Use it as `python ~/.local-agent/la.py run "task" --cwd <dir>`.
5. Tests: `node --test "ExecGuard/test/*.test.js"` and `node --test "ContentGuard/test/*.js"`.

## Safety notes

- `la run --exec` is unsandboxed. Grant it only when the task needs a shell.
- The DeepSeek harness is developer-preview software and has not been security audited.
- ContentGuard ships a base term list of slurs (used for masking and blocking); nothing else in the repo is sensitive. Secrets (Discord token, gateway token, WebUI password) live in `~/.openclaw/openclaw.json` and `WebUI/`, both outside version control.

## License

GNU Affero General Public License v3.0, see [LICENSE](LICENSE).
