# Documentation summary

The cross-repo system design (ESP32 board and helper, PcHwCtl, this stack, and the interfaces between them) is in `E:\System Architecture Documentation\ESP32 - Local LLM - PCHwCtl\ESP32-PcHwCtl-LocalLLM.md`, with a condensed copy for agents in `ESP32-PcHwCtl-LocalLLM.agents.md` in the same folder.

## Overview

- [README.md](../README.md): What the stack is (Occamy-1.0 35B-A3B on an RTX 3050 plus CPU E-cores, served by llama-server behind a wake proxy and supervised by a Windows service), a table of its pieces with links to each doc, a four-step quick setup, safety notes (unsandboxed `la run --exec`, service account and pipe access, where secrets live) and the license. It also names what is not in git (`WebUI/`, `Bridge/harness/`, `Bridge/legacy/`) and the ESP32 board that displays and controls the stack.
- [docs/setup.md](setup.md): End-to-end install: prerequisites, creating `config.env` and setting `LOCALAI_CONFIG`, extracting the official llama.cpp CUDA 12.4 binaries, installing the LocalAIHost service, registering the OpenClaw plugins (load paths, hook permissions, `tools.exec.mode`), setting up the Bridge harness venv and `sync.py`, what to update when the repo moves, and how to run the plugin test suites.

## Configuration

- [docs/configuration.md](configuration.md): How each of the four readers (PowerShell, Python, JavaScript, .NET service) finds `config.env`, and every key in `config.env.example` with its built-in default when blank, what it controls and which files read it. Also lists the other configuration files (`WebUI\.env` for the Open WebUI ports and LAN password, `WebUI\openwebui.env`, `Bridge/config.json`, `~/.openclaw/openclaw.json`) and notes that the ExecGuard Python helpers do not read `config.env`.

## Service

- [docs/service.md](service.md): The LocalAIHost .NET 10 Windows service: the account it runs as, the four supervised children (`wake-proxy`, `gateway`, `webui`, `authproxy`) with their command lines, ports, pre/post steps and environment, child states, restart backoff, hung-port detection, the `external` (port already served) state, the kill-on-close job object, log files, build and `Install-Service.ps1`. Its control pipe section documents `\\.\pipe\LocalAIHost`: access rights, wire format and errors, every command with its reply, sample `status`, how the ESP32 helper relays the board's `!ai` commands (trust check, allowed verbs, condensed status line), and a PowerShell client snippet.

## Launcher, wake proxy and status hub

- [docs/launcher.md](launcher.md): `Start-LocalAI.ps1` parameters and its service, standalone and `-BackendOnly` modes, every `llama-server` flag it sets, E-core pinning and the CUDA environment, and where llama-server logs go. Then `llama-wake.py`: arguments, passive versus waking requests, how a wake runs the launcher and waits, idle unload, the keep-awake thread, context resize and the 256K path, the `/_gate/*` admin endpoints, the `LocalAI-Config.ps1` loader, upgrading llama.cpp, and measured performance.
- [docs/status-hub.md](status-hub.md): The status hub inside the wake proxy (`ai_hub.py`): its endpoints (`/_gate/ai`, `/_gate/ai/flat`, `/_gate/ai/stream`, `/_gate/event`), every flat-feed key, the JSON snapshot schema, phase, guard and model codes with their expiry rules, which reporters post events and the payload shapes. Ends with how the ESP32 helper polls the flat feed and forwards the `ai.*` keys to the board.

## Local Agent Bridge

- [docs/bridge.md](bridge.md): The `la` CLI that lets cloud coding agents hand work to the local model: installing the harness venv and running `sync.py`, every subcommand and flag, the permission model (read-only, `--write`, `--exec`, `--web`, `--legacy`) and how the harness worker refuses approvals, detached jobs and the `~/.local-agent` state layout, 128K/256K context rules (preflight token count, automatic escalation, legacy-only mid-loop escalation, the reaper), `Bridge/config.json` keys, the harness plugins it always disables, status hub events and safety notes.
- [Bridge/SKILL.md](../Bridge/SKILL.md): The `local-agent` skill text that `sync.py` copies into Claude Code and Antigravity: when to offload work to the local model, how to call `la ask` / `la run`, context and timeout guidance, exit codes, and how to check its output.

## Guards and OpenClaw plugins

- [docs/openclaw-plugins.md](openclaw-plugins.md): How OpenClaw loads the three plugins and the `openclaw.json` entries they need. ExecGuard: its two stages, command tiers, decision order, AI review of unknown commands, saved approvals and audit log, the support scripts (`patch_discord_card.py`, `guard_check.py`, `manage.py`), its config keys and fail-safe behaviour. ContentGuard: its three Discord-only hooks, the moderation pipeline (hard minor rule, lexicon, model classifier, fail mode), categories, masking, config keys, term lists and audit file. StatusReporter's observer hooks, the `shared/` modules (`hub.js`, `config.js`) and the test commands.

## Open WebUI

Open WebUI and `authproxy.py` live in the git-ignored `WebUI/` folder and have no doc of their own. How the service starts them, their ports (from `WebUI\.env`), interpreters, CPU affinity, frontend patch and logs are in [docs/service.md](service.md); their configuration files are listed in [docs/configuration.md](configuration.md#other-configuration-files).
