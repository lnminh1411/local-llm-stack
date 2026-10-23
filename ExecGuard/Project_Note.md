# OpenClaw Exec Guard: project note

Plugin for the OpenClaw gateway (Discord). Sits in front of the agent's shell (`exec` / `bash`, and `process` input actions) and decides per
command. Built 2026-10-05. Linked install: `openclaw plugins install --link "E:\Local LLM\ExecGuard"` (also listed in `plugins.load.paths`); enabled under `plugins.entries.exec-guard`.

## Tiers (classify.js, pure and unit-tested)
| Tier | Examples | What happens |
|---|---|---|
| catastrophic | `format C:`, `Format-Volume`, `diskpart`, `shutdown`, rm/Remove-Item on a drive root / Windows / Users / home, `vssadmin delete`, `Set-ExecutionPolicy`, `net user` | **Two red confirmations in a row** ("1/2" from the trusted policy, "FINAL 2/2" from an ordinary hook). Allow once / Deny only. Not blocked outright. |
| danger | recursive or wildcard delete, Stop-Process / taskkill / Stop-Service, registry writes, schtasks, `iex`, encoded PowerShell, uninstalls, `git reset --hard`, `openclaw gateway ...` | One red confirmation. Allow once / Deny. |
| sensitive | mentions `.env`, `openclaw.json`, keys, tokens, passwords, wallets, browser cookie stores | Yellow confirmation. Allow once / Deny. |
| write | any redirect, Set-/New-/Remove-/Copy-... cmdlets, mkdir/del/copy..., python/node/pip/git commit, curl... | Yellow confirmation. **Allow always** saves this exact command. |
| readonly | `Get-*`, `dir`, `git status/log/diff`, `whoami`, `nvidia-smi --query...`, `--version`, safe pipes | Runs, no prompt (audited). |
| unknown | an executable not on any list (`ffprobe`, `jq`, `7z l`) | Local model reviews it (JSON verdict, ~2 s). Only `read_only` runs. `writes` / `dangerous` / model down -> confirmation. |

Hard rules: deterministic checks always run first and the AI can only ever *allow* a command that already has no write indicator, so a model
hallucination cannot turn a write into a pass. Global vetoes (never auto-run): redirection, backticks, `$()`, `&` call operator, dot-sourcing, `::`,
.NET types, New-Object, Invoke-*, interpreters with arguments, any write cmdlet or native writer anywhere in the line (even inside `{}`).

## Pieces
- `index.js`: `registerTrustedToolPolicy("exec-guard-gate")` (stage 1; **fails closed** on any error) + `api.on("before_tool_call")` at priority 100 (stage 2, catastrophic only).
  Only the first `requireApproval` of one hook layer is kept, so two layers are what make the double confirmation work.
- `classify.js`, `reviewer.js` (local model at 127.0.0.1:8080, json_schema, temp 0), `store.js` (approved list + audit log).
- `manage.py`: `list`, `remove "<cmd>"`, `log [N]`. Data in `~/.openclaw/exec-guard/` (`approved.json`, `audit.jsonl`).
- `guard_check.py`: fail-safe. Run by `Start-LocalAI.ps1` after the gateway starts: if exec is mode `full` but the gateway did not list exec-guard, it sets exec mode `ask` (and tightens `exec-approvals.json`).
- `test/classify.test.js`: 112 tests, `node --test test/classify.test.js`.

## Discord card (patch_discord_card.py)
- Approvals reach Discord through the **native** client: `channels.discord.execApprovals {enabled:true, approvers:[owner id], target:"dm", cleanupAfterResolve:true}`.
  Do NOT use `approvals.plugin` forwarding: its buttons are generic chat components, a click only sends "✓" to the model and never resolves the request (the run waits out the 5 min timeout).
- The stock card says "Plugin Approval Required / Command / Shell Preview". `patch_discord_card.py` edits `@openclaw/discord/dist/approval-handler.runtime-*.js` for `pluginId == "exec-guard"` only:
  coloured card, bold reason line, command in a code block, buttons "Allow once / Allow always (save this command) / Deny". The plugin sends `description = reason + "
" + command`.
- Catastrophic: card 1/2 is **edited in place** into the FINAL 2/2 card (no second message); after the last decision the card is deleted (`cleanupAfterResolve`). Audit log keeps the record.
- The patch lives in an npm package, so an update wipes it: `Start-LocalAI.ps1` re-runs it before the gateway starts. `--check` / `--revert` available; the original is kept as `*.execguard-orig`.
- The gateway only restarts itself *after* the active run finishes (config changes are "deferred"); to restart for real kill the `openclaw gateway run` processes and run `Start-LocalAI.ps1`.

## Required config (already applied)
- `tools.exec.mode: "full"` AND `exec-approvals.json` defaults `full/off/deny`, so this plugin is the only prompt. Do not combine `mode` with `security`/`ask`.
- `plugins.entries.exec-guard.hooks.timeoutMs: 200000` (the AI review can wait on a model wake), reviewer timeout 90 s, approval timeout 5 min -> deny.
- Still in force: `tools.fs.workspaceOnly`, `tools.elevated.enabled=false`, `tools.deny` (gateway, nodes, ...), workspace `AGENTS.md` safety rules.

## Verified (2026-10-05, through the real gateway with `openclaw agent`)
- Plugin loads (gateway lists 10 plugins incl. exec-guard); readonly `Get-Date` runs unprompted; a sensitive command and a catastrophic one (`shutdown /a`) each
  stopped at the guard (no approval surface from the CLI, so they were cancelled and nothing ran); AI reviewer: ffprobe/7z l/jq read_only, 7z x/7z a/magick writes, `--delete-all` dangerous.
- NOT verified end to end: the Discord DM rendering (red embed) and the "Allow always" button saving a command, and the second confirmation after the first is approved. These need a real tap in Discord.

## Traps
- Windows exec shell is PowerShell 7 / 5.1; the rules are PowerShell-first. A quoted `>` or a word like "token" in a harmless command only costs a confirmation, never a pass.
- "Allow always" is the exact normalised command text (whitespace collapsed); changing a flag asks again. Danger / sensitive / catastrophic are never offered "always".
- If the plugin is ever removed or fails to load while exec is `full`, commands are unguarded until `guard_check.py` runs (next gateway start). After changes, restart the gateway (kill the :18789 listener and run `Start-LocalAI.ps1`).
- The tool layer here mangles some heredocs: write files with the editor tools, not `cat <<EOF`.
