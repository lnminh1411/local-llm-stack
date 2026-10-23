# Local LLM: project note

Everything around the local model (Occamy-1.0 35B-A3B on the RTX 3050 + E-cores) lives here. Combined 2026-10-05 from four folders. One git repo at this root (nothing committed yet).

| Folder | What it is | Entry points |
|---|---|---|
| `Bridge\` | Lets Claude Code and Antigravity hand work to the local model (`local-agent` skill). | `localagent.py`, `sync.py` (re-run after editing `SKILL.md` or moving the folder) |
| `WebUI\` | Open WebUI behind a password gate for phone/LAN, open on localhost, started at logon. | `start-webui.ps1`, `authproxy.py`, `patch_frontend.py` + `greetings.txt` (opening greeting, always-visible Edit pencil), scheduled task "Local WebUI Startup" |
| `ExecGuard\` | OpenClaw plugin: gates the Discord agent's shell (double confirm for catastrophes, writes need you, read-only runs). | `index.js`, `classify.js`, `patch_discord_card.py`, `guard_check.py`, `manage.py` |
| `ContentGuard\` | OpenClaw plugin: Discord-only guardrails (18+, slurs, hate, harassment, self-harm, gore). | `index.js`, `moderate.js`, `custom-terms.txt` |

Each folder has its own `Project_Note.md` (ContentGuard's is below in this file's "Content Guard" section). Outside this folder:
- `F:\!PC Scripts\Start-LocalAI.ps1` starts the wake proxy (`llama-wake.py`, :8080), llama-server (:8081) and the OpenClaw gateway (:18789). It also re-applies ExecGuard's Discord card patch and runs its fail-safe check.
- `~\.openclaw\openclaw.json` loads both plugins from `plugins.load.paths` (`E:\Local LLM\ExecGuard`, `E:\Local LLM\ContentGuard`).
- `~\.local-agent\la.py` (shim, written by `Bridge\sync.py`) and `~\.local-agent\start-webui-hidden.vbs` search every drive for `Local LLM\Bridge` / `Local LLM\WebUI`, so drive-letter shuffles are fine. Moving the folder again means: re-run `Bridge\sync.py`, update `plugins.load.paths`, and fix the two `Start-LocalAI.ps1` lines that name `Local LLM\ExecGuard`.

## How the pieces connect
`Claude/Antigravity -> Bridge -> :8080 wake proxy -> llama-server`, `Open WebUI -> :8080`, `Discord <-> OpenClaw gateway -> :8080`.
The two guards only run inside the OpenClaw gateway. ExecGuard also calls the model (:8080) to review unknown commands; ContentGuard calls it to classify every Discord message and reply (about 2 s each; measured no slowdown of the following agent turn).

## Content Guard (Discord only)
- Hooks: `before_agent_run` (inbound request -> refused before the model runs), `reply_payload_sending` and `message_sending` (outbound reply -> replaced by a notice). Only when the channel is Discord; the Web UI, the bridge and the CLI are untouched.
- Per text: (1) hard rule: sexual wording near anything minor-like is blocked with no model call; (2) a lexicon (`terms-base.txt`, 916 entries from the better-profanity wordlist, plus your `custom-terms.txt`) only *triggers* attention; (3) the local model classifies: sexual_explicit, sexual_minors, hate_slur, hate_discrimination, harassment, self_harm, graphic_violence, slur_mention, none.
- `slur_mention` (a slur quoted for information, e.g. the name of Lovecraft's cat) is allowed but the lexicon words are **masked** (`n*****`). Anything else in a block category is held back: replies become "🚫 Content guard: ...", requests get a refusal.
- Fail mode: model unreachable + lexicon hit = blocked; model unreachable + clean text = allowed.
- Config: `plugins.entries.content-guard.config` (`enabled`, `mode` always|triggered, `ownerExempt` false, `blockCategories`). Audit: `~\.openclaw\content-guard\audit.jsonl` (first 120 chars of what was held back).
- Soft layer: a "Discord content rules" section in `~\.openclaw\workspace\AGENTS.md`.
- Verified: 11 unit tests, live classifier on 16 sample texts (see results in the session notes: explicit/hate/self-harm/gore/threat flagged, code/help/history/mild swearing allowed). NOT verified end to end in Discord yet.

## Traps (all folders)
- Do not match processes by a command-line pattern from inside a command that contains that pattern (the shell kills itself).
- The Bash tool mangles some heredocs and refuses `rm -rf` style removals: write files with the editor tools, use plain `mv` for moves.
- pip console-script `.exe` launchers embed the venv's absolute path and break when a folder moves: `WebUI\start-webui.ps1` starts Open WebUI through `venv\Scripts\python.exe -c "from open_webui import app; app()"` for that reason.
- A gateway config "restart" is deferred while an agent run is active: kill the `openclaw gateway run` processes and run `Start-LocalAI.ps1`.

## Live status hub (2026-10-23)
`StatusHub.md` documents it. `F:\!PC Scripts\ai_hub.py` (in the wake proxy) exposes `GET /_gate/ai`, `/_gate/ai/flat` (ESP32-friendly `key=value`), `/_gate/ai/stream` (SSE) and takes `POST /_gate/event`: inference phase (prompt / generating / tool / waiting approval), live and last token speed, time taken, current tool, Bridge job step, guard state (exec / content / sandbox: checking, tripped, waiting approval, approved, denied, timed out), GPU. New folders: `StatusReporter\` (OpenClaw plugin: agent runs and tool calls; registered in `plugins.load.paths` with `hooks.allowConversationAccess`), `shared\hub.js` (fire-and-forget reporter used by the plugins). ExecGuard and ContentGuard now report their state.
- Found while wiring this: ContentGuard's `before_agent_run` (inbound Discord check) was being BLOCKED by OpenClaw 2026.7.x ("non-bundled plugins must set plugins.entries.content-guard.hooks.allowConversationAccess=true"). Set to true on 2026-10-23, so inbound filtering works again; check the gateway log for `blocked because` after any OpenClaw upgrade.
- OpenClaw build here has no `agent_start` hook (ignored); the reporter uses `before_agent_run`. `after_tool_call` did not report a tool end for `exec`, so the hub also closes a running tool when the next model call starts.
