---
name: local-agent
description: >-
  Hand a job to the local agent on the user's PC (Occamy-1.0 35B-A3B MoE, FIT-QUALITY IQ4_XS, vision, on the RTX 3050 + E-cores, 128K context,
  256K on demand) running inside the DeepSeek Harness agent runtime through the Local Agent Bridge CLI. Use when the user says "ask the local agent/model",
  "use the bridge", "/local-agent", or when a task is bulk read-only digestion (summarize or search huge
  logs/files/codebases, extract data, draft boilerplate or tests) that would otherwise burn cloud context.
  Free, private, nothing leaves the machine. Not for architecture decisions, subtle bugs, or anything where
  being wrong is expensive.
---

# Local Agent Bridge

The local agent is Occamy-1.0, a 35B-parameter MoE model (3B active, with vision) running on this PC. It is **slower and less capable than you**
(~26 tok/s generation, ~250-600 tok/s prompt processing, a cold `run` costs ~20-30 s of harness prompt; no knowledge past its training) but costs no cloud tokens and sees files directly. You are the
orchestrator: give it bounded, checkable work, then verify what comes back.

One CLI, same for Claude Code and Antigravity:

```
python "$HOME/.local-agent/la.py" <command> ...
```

(`$HOME/.local-agent/la.py` is a shim that finds the real script even when drive letters reshuffle. Re-run
`sync.py` in the bridge folder if it ever goes missing.)

## Pick the mode

| Need | Command |
|---|---|
| Question over files or piped text, no actions | `ask` |
| Task that needs to look around, grep, edit, run | `run` |

```bash
la ask "Which functions never get called?" -f src/**/*.py
git diff | la ask "Review this diff for bugs. List file:line + problem." -f -
la ask "Summarize the errors, grouped by cause" -f build.log --max-tokens 1500
la run "Add a --verbose flag to cli.py and update README usage" --cwd E:/proj --write
la run "Why does test_parse fail? Find the cause, do not change files" --cwd E:/proj --exec
```
(`la` = the full `python "$HOME/.local-agent/la.py"` command.)

`run` is a real agent harness (DeepSeek Harness, `dsh`), not a bare loop: tools `read glob grep` (always), `write edit`,
`todo_write`, `read_image` (vision), a `skill` tool that loads skills on demand, and with `--exec` a `pwsh` shell and
background jobs, with `--web` `web_fetch`. Permission is the CLI flag, enforced by dsh's sandbox:
- no flag: **read-only** (reads anywhere, nothing writes);
- `--write`: **workspace-write**, write/edit confined to `--cwd`; an attempt outside is refused and cannot be approved;
- `--exec`: **full access, UNSANDBOXED** shell and files (dsh's Windows shell sandbox does not start in the SDK build). Same trust as the old `run_command`; grant it only when the task needs a shell.
Skills come from `~/.agents/skills` and the project's own `.agents/skills`/`.claude/skills` (the same SKILL.md format you use), so the local agent can use any of them. Sub-agents, goals, workflows and plan mode are switched off (one GPU slot, no human to answer).
`--legacy` falls back to the old built-in loop (adds `web_search`). Grant only what the task needs. `--think` turns on reasoning for hard questions (slower).

Useful flags: `-f PATH|GLOB` (repeatable, inlined as context; `-f -` inlines stdin), `--cwd DIR`, `--max-tokens N`,
`--temp T`, `--max-steps N` (default 25 model calls), `--json` (structured result), `--detach` (see below).

## Context: 128K by default, 256K escalates by itself

- The model server runs 128K at full speed. If a job does not fit, the bridge **escalates to 256K automatically**: it
  restarts the server (20-100 s), runs the job, and a background timer puts it back to 128K after 10 idle minutes. For
  `run`, escalation also happens mid-loop when the conversation fills the window; the work so far is kept and re-read.
- At 256K generation drops to ~14 tok/s and prefill to ~350 tok/s: a 178k-token prompt took 513 s (measured). **Use
  `--detach` for anything that will escalate.** `--heavy` forces 256K from the start; `--no-escalate` forbids it (the job
  then exits with code 3 if it does not fit).
- The same server answers the **OpenClaw Discord bot**. A 256K swap interrupts it for the restart and slows it until the
  revert, so prefer splitting an input that only just exceeds 128K.
- Exit codes: 0 ok, 1 error, 2 still running (`wait` timed out), 3 does not fit 128K and `--no-escalate` was set, 4 too big
  even for 256K (split it).

## Idle unload

After 20 idle minutes a wake proxy kills the model server to free ~16 GB RAM and ~5.5 GB VRAM. The next request (yours,
or a Discord message) starts it again and waits, 20-100 s. The KV cache and prompt cache do **not** survive an unload, so
the first job after one is also a cold prefill. Not a fault; `status` shows `server: asleep`.

## Long jobs: detach, then wait

Foreground calls block until done. Anything that may exceed your tool timeout (every job that escalates to 256K, big `run` tasks, a first call after an unload):

```bash
la run "..." --cwd E:/proj --write --detach     # prints {"job": "<id>", ...} immediately
la wait <id> --timeout 55                        # prints result, or exits 2 with a log tail if still running
la result <id>      la jobs      la cancel <id>
```
Claude Code: you can instead run the foreground command with `run_in_background` and be notified on completion.
Antigravity: poll `wait <id> --timeout 55` (the 60-second timer pattern); do not spin faster.
Jobs queue: only one job runs at a time, others wait for the model.

## Checking the machine

`la status` shows server state (`ready` / `asleep` / `loading` / `down`), context mode (`default-128k` / `heavy-256k`), VRAM, current
job, seconds until the idle unload and until a 256K revert. `la up` wakes or starts the model server (it does not start the Discord gateway). `la heavy on|off` swaps context
by hand.

## Trust and verification

- Treat the local agent's output as a competent junior's draft: **good at reading and extracting, unreliable at judgment**.
  Spot-check anything you will act on; for `--write` runs, audit the changes (`git diff`, run the tests) before staging, exactly
  as with any other delegated code.
- Its final message is all you get back; tool calls are in the job log (`~/.local-agent/jobs/<id>/log.txt`).
- Give it self-contained prompts: it has no conversation history and no access to yours. Name files, state the expected output
  format, and cap length (`--max-tokens`).
- It reads any file on the PC but writes only inside `--cwd`. Do not point it at secrets you do not need it to read.
- When you used it, tell the user, and include its answer plus the stats footer line (`[local-agent] done | 12.1s | prompt ... | gen ...`)
  so they can see what it did.

## Good fits / poor fits

Good: log and diff triage, "find every place that does X", summarizing big files, mechanical edits across many files,
boilerplate and test scaffolding against an existing pattern, extracting tables from text, first-pass code review.

Poor: designing anything, debugging subtle logic, security-sensitive review, anything needing current web facts you could
simply look up, tasks where verifying the result costs more than doing the work.
