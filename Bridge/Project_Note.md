# Local Agent Bridge: project note

Lets Claude Code and Antigravity hand work to the local Occamy-1.0 (35B-A3B MoE, vision-capable). Swift 1.5 27B was tried 2026-10-09 and dropped (2-3 tok/s); its files are still in C:\ModelCache on this PC (RTX 3050 + 12 E-cores). Rebuilt
2026-10-04; it replaces the old Ollama/Gemma bridge, which is gone (Recycle Bin).

## Files
| File | Purpose |
|---|---|
| `harness_worker.py` | Runs one `run` job through DeepSeek Harness (venv `harnessenv`, SDK `deepseek-harness-sdk` 0.1.5rc1): builds the dsh config patch, denies approvals, counts steps/tokens, enforces step and time limits. Talks to localagent.py via `LOG`/`RESULT` lines on stdout. |
| `localagent.py` | The CLI (stdlib only). `ask`, `run`, `status`, `up`, `heavy`, `wait`, `result`, `jobs`, `cancel`. |
| `config.json` | Endpoint, model alias, 128K/256K sizes, idle minutes, launcher path, sampling. Missing keys fall back to defaults in the script. |
| `SKILL.md` | The `local-agent` skill text for both agents. Edit here, then run `sync.py`. |
| `sync.py` | Installs the skill to `~/.claude/skills/local-agent/` and `~/.gemini/config/skills/local-agent/`, and writes the shim `~/.local-agent/la.py` that finds this folder on whatever drive letter it has. Re-run after moving the folder or editing `SKILL.md`. |

Outside this folder (same stack):
- The `LocalAIHost` service (`..\Service`) starts the wake proxy and the OpenClaw gateway; `..\Launcher\Start-LocalAI.ps1` starts the service (and, with `-BackendOnly`, llama-server for the proxy).
- `..\Launcher\llama-wake.py` is the wake-on-request proxy on :8080 (real llama-server on :8081).
- State: `~/.local-agent/` (jobs, `gpu.lock`, `heavy.json`). Jobs older than 7 days are pruned.
- Global rule: "Local Agent Offloading" section in `~/.claude/CLAUDE.md` and `~/.gemini/GEMINI.md`.
- The OpenClaw agent has its own copy of the `antigravity` skill in `~/.openclaw/workspace/skills/antigravity/`.

## How it behaves
- Calls go through the wake proxy `/_gate/*` endpoints; the bridge never starts llama-server itself unless the proxy is down
  (then it runs the launcher with `-NoGateway`).
- 128K default. A job that does not fit escalates to 256K by itself (`--heavy` forces, `--no-escalate` forbids). A detached
  reaper reverts to 128K after `heavy_idle_minutes` (10). `run` also escalates mid-loop when its context fills.
- After 20 idle minutes the proxy unloads the model (frees ~16 GB RAM, ~5.5 GB VRAM); any request wakes it (20-100 s).
- One job at a time (`gpu.lock`); the rest queue. Exit codes: 0 ok, 1 error, 2 running, 3 needs 256K but `--no-escalate`,
  4 too big even for 256K.
- `run` tools: read-only by default; `--write` confined to `--cwd`; `--exec` and `--web` opt-in.

## Measured facts (2026-10-04)
- Prefill ~520-670 tok/s at 128K, ~350 tok/s at 256K; generation ~29 tok/s at 128K, ~14 at 256K.
- 178k-token needle query at 256K: 513-519 s, answer correct. Swap 128K<->256K ~25 s with the model in page cache.
- KV and prompt cache do not survive an unload or restart. Built-in `--sleep-idle-seconds` drops them and crashed with a CUDA
  error on wake; a slot restored from disk is accepted, then ignored by the server (hybrid/recurrent model, "forcing full prompt
  re-processing"). Conversation history lives in OpenClaw's state, so only compute is lost, not memory.
- The OpenClaw agent's prompt was trimmed from ~24k to 12.7k tokens (tools.deny + skills allowlist in `openclaw.json`).
- Core parking shows E-cores as parked at idle, but all 12 run at 97-100% under load; not a problem.

## Traps
- Sampling: temperature 0.4, presence penalty 0.3, DRY 0.8 (`config.json` `extra_body`). At 0.3 / 0.0 a review prompt fell into a 1800-token repetition loop. The local agent is still a poor security reviewer (rambling, wrong line numbers): use it for extraction and bulk reads, audit anything that matters yourself.
- Run the launcher from Python without pipes: llama-server inherits them and `subprocess.run` never sees EOF.
- Backslashes get mangled when writing files through some tools; use forward slashes in paths inside scripts.
- `--detach` for anything that escalates or follows an unload; foreground calls block until done.
- Antigravity verified end to end through `agy -p ... --dangerously-skip-permissions`.

## Open
- Not git-tracked.
- Logon autostart with the wake proxy and a Discord message waking an unloaded model are untested end to end.


## Harness engine (2026-10-09)
- `run` now goes through dsh (`engine: harness` in config; `--legacy` = old loop). Profile `sdk` in an isolated `DSH_HOME` = `~/.local-agent/dsh-home` (sessions accumulate there, delete freely). Provider `local` (llm-pi-ai, openai-completions, `http://127.0.0.1:8080/v1`, compat `supportsDeveloperRole:false`, `max_tokens`), model `input:[text,image]`.
- Switched off in the patch: DeepSeek session-log upload, OTel telemetry, package inventory, DeepSeek web search, goal/ralph/workflow/sub-agent tools, plan mode, ask-user. `harness_keep` / `harness_disable` in config.json override the list.
- Permissions: DSH_PERMISSION_MODE = read-only / workspace-write / danger-full-access from the flags. The SDK has no approval channel, so any sandbox escalation fails closed (verified: write outside cwd refused, escalation refused). `--exec` is unsandboxed because the Windows pwsh sandbox errors with `--profile <name> is required`.
- Skills: dsh discovers `~/.agents/skills` (+ project skill dirs). The skill list adds ~3k tokens, the tool schemas ~7k; whole harness prompt ~9-11k tokens, so a cold job pays ~20-30 s before step 1 on Occamy (~70 s on the slow Swift model). Warm steps reuse the llama prompt cache.
- Not ported: `web_search` (dsh's is DeepSeek-only; `--legacy` still has DuckDuckGo). Idea: MCP stdio server for it.
- Verified end to end: read-only grep/read, `--write` confinement, skill load (`grilling`), `--exec --detach`.

## Status reporting (2026-10-23)
`localagent.py` (`hub_event`) and `harness_worker.py` (`hub`) post job, step, tool and sandbox-refusal events to the wake proxy's status hub (`E:\Local LLM\StatusHub.md`), so the ESP32 display can show what a Bridge job is doing. Fire-and-forget, 0.7 s timeout, never affects the job.
