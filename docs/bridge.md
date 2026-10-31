# Local Agent Bridge

The Local Agent Bridge lets cloud agents like Claude Code and Antigravity hand work to the local Occamy-1.0 model (35B-A3B MoE). It keeps data on the local PC, costs no cloud tokens, and runs requests through the wake proxy at http://127.0.0.1:8080.

The Bridge provides two execution modes:
- ask: A single-turn query that inlines files or standard input directly into the prompt without tool execution.
- run: A multi-step autonomous agent that inspects files, runs regex searches, modifies code, or runs commands through tool calling.

The Bridge enforces a single GPU execution lock at a time, manages context resizing between 128K and 256K, coordinates idle unloading through the wake proxy, and publishes status updates to the status hub.

## Installation

The Bridge uses an isolated virtual environment for DeepSeek Harness and a sync script that configures agent skills.

### Virtual environment and packages

Create the virtual environment at Bridge/harness/venv and install the harness SDK:

```powershell
python -m venv Bridge\harness\venv
.\Bridge\harness\venv\Scripts\pip install deepseek-harness-sdk
```

The worker script [../Bridge/harness_worker.py](../Bridge/harness_worker.py) invokes `Bridge\harness\venv\Scripts\python.exe` directly.

### Running sync.py

Register the Bridge with Claude Code and Antigravity by running [../Bridge/sync.py](../Bridge/sync.py):

```powershell
python Bridge\sync.py
```

sync.py performs the following actions:
1. Writes the shim script `~/.local-agent/la.py`. The shim runs `localagent.py` from the folder `sync.py` was run in. If that path is gone, it tries `<drive>:\Local LLM\Bridge` on each drive letter, so a changed drive letter does not break it.
2. Copies `Bridge/SKILL.md` to `~/.claude/skills/local-agent/SKILL.md` for Claude Code.
3. Copies `Bridge/SKILL.md` to `~/.gemini/config/skills/local-agent/SKILL.md` for Antigravity.
4. Prints a reminder to set `LOCALAI_CONFIG` if the environment variable is not defined.

Re-run `python Bridge\sync.py` whenever:
- The repository moves to another path or drive letter.
- You edit `Bridge/SKILL.md`.

## Command-line interface

The CLI entry point is `python ~/.local-agent/la.py` (abbreviated here as `la`). It exposes subcommands defined in [../Bridge/localagent.py](../Bridge/localagent.py). Standard help flags `-h` and `--help` print usage information for any subcommand.

### Subcommand: ask

Runs a one-shot query over files or piped input without tool calls.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `prompt` | Positional string | Required | Task or question. Use `-` to read the prompt from standard input. |
| `-f`, `--file` | String (repeatable) | `[]` | Path or glob pattern to inline as context. Use `-f -` to inline standard input. |
| `--cwd` | String | Current directory | Working directory used to resolve relative paths. |
| `--heavy` | Flag | `False` | Force 256K context from the start instead of auto-escalating. |
| `--no-escalate` | Flag | `False` | Forbid switching to 256K context. Exits with code 3 if context exceeds 128K. |
| `--think` | Flag | `False` | Enable model reasoning mode (`enable_thinking: True`). |
| `--max-tokens` | Integer | `4096` | Maximum completion tokens per model call. Falls back to `max_tokens` in `config.json`. |
| `--temp` | Float | `0.4` | Sampling temperature. Runtime falls back to `temperature` in `config.json`. |
| `--detach` | Flag | `False` | Run in background and return a job identifier immediately. |
| `--json` | Flag | `False` | Output result formatted as JSON. |
| `--system` | String | `None` | Custom system prompt text to replace default assistant prompt. |

### Subcommand: run

Runs an autonomous tool loop through DeepSeek Harness (or legacy runner).

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `prompt` | Positional string | Required | Task or instructions. Use `-` to read from standard input. |
| `-f`, `--file` | String (repeatable) | `[]` | Path or glob pattern to inline as context. Use `-f -` to inline standard input. |
| `--cwd` | String | Current directory | Working directory for tools and path resolution. |
| `--heavy` | Flag | `False` | Force 256K context from the start. |
| `--no-escalate` | Flag | `False` | Forbid switching to 256K context. Exits with code 3 if context exceeds 128K. |
| `--think` | Flag | `False` | Enable model reasoning mode. |
| `--max-tokens` | Integer | `4096` | Maximum completion tokens per model call. |
| `--temp` | Float | `0.4` | Sampling temperature. Runtime falls back to `temperature` in `config.json`. |
| `--detach` | Flag | `False` | Run in background and return a job identifier immediately. |
| `--json` | Flag | `False` | Output result formatted as JSON. |
| `--write` | Flag | `False` | Permit file creation and modifications confined to `--cwd`. |
| `--exec` | Flag | `False` | Permit PowerShell shell execution. Unsandboxed. Implies full system file access. |
| `--web` | Flag | `False` | Permit web page fetching (`web_fetch`). |
| `--legacy` | Flag | `False` | Use built-in Python tool loop instead of DeepSeek Harness. |
| `--max-steps` | Integer | `25` | Maximum tool execution steps before stopping. Falls back to `max_steps` in `config.json`. |

### Subcommand: wait

Blocks on a detached background job and prints the result once complete.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `job` | Positional string | Required | Job identifier. |
| `--timeout` | Integer | `55` | Maximum seconds to wait before returning status. |
| `--json` | Flag | `False` | Output result formatted as JSON. |

Exit codes for `wait`:
- `0`: Job completed successfully.
- `1`: Job failed or process died.
- `2`: Job still running when timeout expired (outputs recent log lines).

### Subcommand: result

Fetches and prints the final result of a completed job without waiting.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `job` | Positional string | Required | Job identifier. |
| `--json` | Flag | `False` | Output result formatted as JSON. |

### Subcommand: jobs

Lists recent Bridge jobs from `~/.local-agent/jobs/`.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `-n` | Integer | `15` | Maximum count of recent jobs to display. |

### Subcommand: cancel

Kills a running job process tree and releases lock files.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `job` | Positional string | Required | Job identifier to cancel. |

### Subcommand: status

Prints a JSON object with model state, context mode, VRAM usage, queue status, and timers. Takes no arguments.

### Subcommand: up

Wakes the model server through the proxy (`/health`, `/_gate/ensure`) with the default 128K context. Takes no arguments.

### Subcommand: heavy

Controls manual swapping of context size between 128K and 256K.

| Argument / Flag | Type | Default | Description |
|---|---|---|---|
| `action` | Positional choice (`on`, `off`, `status`) | Required | `on` switches to 256K context. `off` reverts to 128K context. `status` prints current status. |

### Internal subcommands

The CLI defines two internal subcommands used by detached background processes:

| Subcommand | Arguments | Description |
|---|---|---|
| `_job` | `job` (Job identifier) | Entry point executed by detached worker process spawned via WMI. |
| `_reaper` | None | Background loop monitoring context idle timeout to revert 256K to 128K. |

## Permission model

The permission flags dictate what capabilities the local agent receives during `la run`.
In DeepSeek Harness mode, permissions map to `DSH_PERMISSION_MODE` in [../Bridge/harness_worker.py](../Bridge/harness_worker.py), while the generated patch sets tool access.

| Flag | Harness permission mode | Granted tools | Sandbox boundary |
|---|---|---|---|
| None (default) | `read-only` | `read`, `glob`, `grep`, `read_image`, `todo_write`, `skill` | Read anywhere on filesystem. Writes blocked. Shell and web disabled. |
| `--write` | `workspace-write` | All default tools plus `write`, `edit` | Write and edit confined to `--cwd` and system temp roots. Out-of-bounds writes refused. Shell and web disabled. |
| `--exec` | `danger-full-access` | All write tools plus `tool-pwsh`, `tool-jobs` | Unsandboxed PowerShell. Can touch any file or command accessible by the user. |
| `--web` | Mode set by other flags | Enables `tool-web` (`web_fetch`) | Allows HTTP fetching of web pages. |
| `--legacy` | Built-in Python loop | Handled in `localagent.py` | `read_file`, `list_dir`, `glob`, `grep`; `--write` adds `write_file`, `edit_file`; `--exec` adds `cmd.exe` `run_command`; `--web` adds `web_search` and `web_fetch`. |

### Approval handling in harness_worker.py

The DeepSeek Harness SDK lacks an interactive user approval channel. In [../Bridge/harness_worker.py](../Bridge/harness_worker.py), a daemon thread monitors incoming client approval requests:
- Every runtime permission request triggers `client.respond_error(rq.id, -32000, "denied: non-interactive worker")`.
- If an operation requires escalation outside the granted sandbox level, it fails closed immediately.
- Refusals emit a log line and notify the status hub with `kind="guard", guard="sandbox", state="tripped"`.

## Detached jobs

Long-running jobs can run disconnected from the calling cloud agent.

### State storage layout

Bridge state is stored in `~/.local-agent/`:
- `~/.local-agent/jobs/<job-id>/`: Contains individual job data:
  - `job.json`: Job metadata, arguments, status (`queued`, `running`, `done`, `cancelled`, `error`), PID, timestamps.
  - `log.txt`: Complete execution log with timestamps.
  - `result.json`: Final output, status, changed files list, token counts, timings.
  - `harness.request.json`: Input parameters passed to `harness_worker.py`.
  - `harness.patch.yml`: Generated YAML configuration patch for DeepSeek Harness.
- `~/.local-agent/gpu.lock`: Concurrency file lock containing PID, owner name, and timestamp.
- `~/.local-agent/heavy.json`: State file tracking 256K context usage timestamps and reaper process PID.
- `~/.local-agent/dsh-home/`: DeepSeek Harness session store and cache.

Job retention is controlled by `job_retention_days` (default 7). `prune_jobs()` deletes job folders older than 7 days on every new request.

### Detached workflow

1. Launch with `--detach`:
   ```powershell
   la run "Refactor logging in src/" --cwd "K:/scratch/my-project" --write --detach
   ```
   This returns JSON with the job ID and process PID:
   ```json
   {"job": "1031-110000-a1b2", "status": "running", "pid": 12345, "next": "localagent wait 1031-110000-a1b2 --timeout 55 | localagent result 1031-110000-a1b2"}
   ```
2. Check recent jobs:
   ```powershell
   la jobs
   ```
3. Wait on progress:
   ```powershell
   la wait 1031-110000-a1b2 --timeout 55
   ```
   If still running after 55 seconds, `wait` exits with code 2 and prints recent log lines.
4. Retrieve results:
   ```powershell
   la result 1031-110000-a1b2
   ```
5. Cancel if needed:
   ```powershell
   la cancel 1031-110000-a1b2
   ```
   `cancel` terminates the process tree using Windows `taskkill /F /T /PID`, clears the GPU lock if held, and marks the job cancelled.

## Context sizes and escalation

Occamy-1.0 runs in one of two context configurations:
- Default context: 128K (`131072` tokens), configured via `CONTEXT` in `config.env`.
- Heavy context: 256K (`262144` tokens), configured via `HEAVY_CONTEXT` in `config.env`.

### Context rules and performance

- Prefill and generation speed: At 128K context, llama.cpp b12005 delivers 665 tok/s prefill (at 18.7k context) and 18 to 22 tok/s generation. At 256K context, generation slows to ~14 tok/s and prefill slows to ~350 tok/s.
- Explicit selection: Passing `--heavy` forces 256K context from start.
- Auto-escalation: When calculating prompt tokens plus context files and token reserves (max completion tokens plus 6000 for `run` or 500 for `ask`), if the total exceeds 128K, `localagent.py` automatically escalates to 256K via proxy `POST /_gate/ensure {"ctx": 262144}`.
- Mid-loop escalation: During a `run` session, if conversation tokens exceed `ctx - 2 * max_tokens - 4000`, the loop restarts the server at 256K context and continues.
- Forbidding escalation: Passing `--no-escalate` prevents switching to 256K. If prompt size exceeds 128K, it exits with exit code 3 (`EXIT_NEEDS_HEAVY`).
- Hard limit: If prompt plus reserve exceeds 256K tokens, the bridge rejects the job with exit code 4 (`EXIT_TOO_BIG`).
- Automatic reaper: When 256K is active, `touch_heavy()` spawns `la _reaper`. After `heavy_idle_minutes` (default 10) without active jobs, the reaper reverts context to 128K and primes the OpenClaw prompt cache.

## Configuration keys in Bridge/config.json

[../Bridge/config.json](../Bridge/config.json) contains runtime defaults for the Bridge. Missing keys fall back to values in `CFG` in [../Bridge/localagent.py](../Bridge/localagent.py).

| Key | Type | Default | Description |
|---|---|---|---|
| `_comment` | String | Note | Informational header string explaining fallback behavior. |
| `heavy_idle_minutes` | Integer | `10` | Minutes of inactivity before 256K context automatically reverts to 128K. |
| `max_tokens` | Integer | `4096` | Maximum generation completion tokens per model call. |
| `temperature` | Float | `0.4` | Default sampling temperature. |
| `top_p` | Float | `0.9` | Top-p nucleus sampling threshold. |
| `max_steps` | Integer | `25` | Maximum tool execution iterations for `run`. |
| `extra_body` | Object | `{"dry_multiplier": 0.8}` | Additional fields merged into request body for `/v1/chat/completions`. |
| `dry_multiplier` | Float | `0.8` | DRY (Don't Repeat Yourself) repetition penalty multiplier set inside `extra_body`. |
| `presence_penalty` | Float | `0.3` | Presence penalty added to token generation logits. |

Other settings in `CFG` in `localagent.py` read from `config.env` or script defaults:
- `base_url`: Base URL for the wake proxy, default `http://127.0.0.1:8080`.
- `model`: Model alias name, default `occamy-1.0-fit-quality`.
- `default_ctx`: Default context size, default `131072`.
- `heavy_ctx`: High context size, default `262144`.
- `launcher`: Path to `Start-LocalAI.ps1`.
- `request_timeout_s`: Request timeout in seconds, default `3600`.
- `tool_output_chars`: Maximum character length of tool output before truncation, default `24000`.
- `job_retention_days`: Number of days to preserve finished job logs, default `7`.
- `engine`: Engine for `run`, default `"harness"`.
- `harness_python`: Path to python executable in `Bridge/harness/venv`.
- `harness_keep`: List of plugin identifiers from `ALWAYS_OFF` to leave enabled.
- `harness_disable`: List of plugin identifiers to disable in addition to `ALWAYS_OFF`.

## Disabled harness plugins

DeepSeek Harness includes features suited for cloud environments or interactive users that do not fit a local single-GPU offline worker. [../Bridge/harness_worker.py](../Bridge/harness_worker.py) disables them in `ALWAYS_OFF`:

| Disabled item | Identifier | Reason |
|---|---|---|
| Log upload | `session-log-deepseek` | Prevents uploading session transcripts to external DeepSeek servers. |
| Telemetry | `session-telemetry-otel` | Disables OpenTelemetry metrics collection and external transport. |
| Package inventory | `plugin-package-inventory-deepseek` | Disables scanning and reporting host package installations. |
| Web search | `web-search-deepseek` | Disables proprietary cloud search service. |
| Goals | `tool-goal` | Disables autonomous goal management loops. |
| Ralph loop | `tool-ralph` | Disables autonomous task looping engine. |
| Workflows | `tool-workflow` | Disables multi-agent workflow orchestration. |
| Sub-agents | `tool-subagent`, `tool-subagent-fork`, `tool-subagent-control`, `tool-subagent-list-agents` | Prevents spawning parallel model sessions that would contend for single-slot GPU. |
| Plan mode | `plan-mode` | Disables interactive human-in-the-loop plan confirmation. |
| User questions | `user-questions` | Disables interactive user prompt questions. |

In addition:
- `tool-pwsh` and `tool-jobs` are disabled unless `--exec` is set.
- `tool-web` is disabled unless `--web` is set.

## Status hub reporting

The Bridge reports execution state to the wake proxy status hub documented in [status-hub.md](status-hub.md).

Both `localagent.py` (`hub_event`) and `harness_worker.py` (`hub`) send fire-and-forget JSON payloads to `POST /_gate/event` with a 0.7 second timeout. Network or proxy errors are ignored so monitoring never halts task progress.

Reported event types:
- Job lifecycle:
  `{"kind": "job", "id": "<job_id>", "state": "queued"|"running"|"done"|"error", "jtype": "ask"|"run", "engine": "harness"|"direct", "mode": "read-only"|"write"|"exec", "step": <n>, "max_steps": <max>, "step_label": "thinking"|"starting model"|"queued"}`
- Tool execution:
  `{"kind": "tool", "state": "start"|"end"|"error", "name": "<tool_name>", "summary": "<arguments_prefix>"}`
- Guard trips and sandbox refusals:
  `{"kind": "guard", "guard": "sandbox", "state": "tripped", "detail": "<reason>"}`

The status hub publishes these events to `GET /_gate/ai/flat`, which the ESP32 display board reads once per second.

## Safety notes

- Unsandboxed shell with `--exec`: The DeepSeek Harness Windows shell sandbox does not initialize in the SDK release without a named profile. Therefore, `--exec` runs commands directly in `pwsh` without isolation. Grant `--exec` only for tasks that strictly require shell execution.
- File system isolation with `--write`: When `--write` is specified without `--exec`, file write and edit operations are strictly confined to `--cwd` and system temp roots. Out-of-bounds writes fail closed.
- Automatic refusal of elevation: The worker runs non-interactively. Any attempt by a tool to prompt for interactive permissions is automatically rejected.
- Single GPU concurrency: The file lock `~/.local-agent/gpu.lock` limits execution to one job at a time. Simultaneous requests queue until the active job releases the lock.
- Local model privacy: All inference runs on the local model behind the proxy, and harness telemetry and log upload are off. `--web` and `--exec` can still reach the network.
