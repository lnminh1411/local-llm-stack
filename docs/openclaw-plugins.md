# OpenClaw plugins

## Loading plugins

OpenClaw loads plugins from directory paths listed in `plugins.load.paths` inside `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "load": {
      "paths": [
        "E:/Local LLM/ExecGuard",
        "E:/Local LLM/ContentGuard",
        "E:/Local LLM/StatusReporter"
      ]
    }
  }
}
```

Plugin ids declared in each `openclaw.plugin.json`:
- `exec-guard`: [ExecGuard](../ExecGuard/openclaw.plugin.json)
- `content-guard`: [ContentGuard](../ContentGuard/openclaw.plugin.json)
- `status-reporter`: [StatusReporter](../StatusReporter/openclaw.plugin.json)

Plugins that read conversation content require explicit access permission in OpenClaw. Set `plugins.entries.<id>.hooks.allowConversationAccess` to `true` in `~/.openclaw/openclaw.json`:

```json
{
  "plugins": {
    "entries": {
      "exec-guard": {
        "hooks": {
          "timeoutMs": 200000
        }
      },
      "content-guard": {
        "hooks": {
          "allowConversationAccess": true
        }
      },
      "status-reporter": {
        "hooks": {
          "allowConversationAccess": true
        }
      }
    }
  }
}
```

Without `allowConversationAccess: true`, OpenClaw blocks the `before_agent_run` hook for non-bundled plugins. `hooks.timeoutMs` on `exec-guard` gives the AI review time to wait for a model wake.

## ExecGuard

[ExecGuard](../ExecGuard/index.js) gates shell execution for the OpenClaw agent. It inspects tools `exec`, `bash`, and interactive actions on `process`.

### Registered hooks and policies

ExecGuard registers two safety layers:
1. `registerTrustedToolPolicy`: registers trusted tool policy `exec-guard-gate`. Runs at stage 1 for every `exec`, `bash`, and `process` call. It fails closed: any unhandled exception blocks the tool call.
2. `before_tool_call`: registered with `{ priority: 100 }`. Runs stage 2 checks. It handles the second confirmation for `catastrophic` commands.

For tool `process`, stage 1 intercepts actions `send-keys`, `submit`, `paste`, and `write`. It requests owner approval with severity `warning` and title `Type into a running shell`. It never offers `allow-always`.

### Command classification tiers

The pure classifier in [classify.js](../ExecGuard/classify.js) evaluates commands against six tiers:

| Tier | Confirmation count | Severity | Allowed decisions | Description |
|---|---|---|---|---|
| `catastrophic` | 2 | critical | `allow-once`, `deny` | Formats disks, edits boot configs, wipes system or root paths, deletes shadow copies, clears event logs, modifies user accounts, or disables firewalls. |
| `danger` | 1 | critical | `allow-once`, `deny` | Recursive deletes, kills processes, stops services, writes registry keys, modifies scheduled tasks, downloads and executes code, or runs uninstalls. |
| `sensitive` | 1 | warning | `allow-once`, `deny` | Accesses `.env` files, credentials, SSH keys, certificates, browser logins, passwords, tokens, or wallets. |
| `write` | 1 | warning | `allow-once`, `allow-always`, `deny` | Modifies files, invokes modifying PowerShell verbs, uses file redirection, or runs state-changing executables. Selecting `allow-always` saves the command. |
| `readonly` | 0 | none | none | Runs immediately without prompts. Includes safe PowerShell inspection cmdlets (`Get-*`, `Select-Object`, `Format-Table`), safe git subcommands (`status`, `log`, `diff`), and informational utilities (`whoami`, `hostname`, `nvidia-smi`). |
| `unknown` | 0 or 1 | none, critical, or warning | depends on AI | Not on allowlists and lacks write indicators. Dispatched to the local AI reviewer. |

### Model review for unknown commands

When a command falls into tier `unknown`, [reviewer.js](../ExecGuard/reviewer.js) queries the local model through `POST /v1/chat/completions`.

Review request parameters:
- Endpoint: `reviewer.baseUrl` (default `http://127.0.0.1:8080/v1` via `proxyBase()`)
- Model: `reviewer.model` (default `occamy-1.0-fit-quality`)
- Timeout: `reviewer.timeoutMs` (default `90000` ms)
- Temperature: `0`
- Maximum tokens: `80`
- Response format: structured JSON schema requiring `verdict` and `reason`

Possible verdicts:
- `read_only`: the command only inspects state. ExecGuard executes the command immediately without prompting.
- `writes`: the command modifies state. ExecGuard requests approval with severity `warning` and permits `allow-always`.
- `dangerous`: the command presents system risk. ExecGuard requests approval with severity `critical` and forbids `allow-always`.
- `unavailable`: reviewer disabled (`reviewer.enabled: false`), timed out, or returned an error. ExecGuard requests approval with severity `warning` and permits `allow-always`.

### Persistence

[store.js](../ExecGuard/store.js) manages persistent files in `dataDir` (default `~/.openclaw/exec-guard`):
- `approved.json`: maps normalized command strings to approval timestamps `{ "at": "<ISO-8601>" }`. Saved atomically by writing to `approved.json.tmp` and renaming.
- `audit.jsonl`: append-only JSON Lines record of every evaluation. Stores `ts`, `tool`, `tier`, `decision`, `reasons`, and truncated `command` (up to 2000 characters). I/O errors are caught to avoid interrupting tool execution.

### Support scripts

ExecGuard includes three CLI scripts:

#### patch_discord_card.py

[patch_discord_card.py](../ExecGuard/patch_discord_card.py) patches `@openclaw/discord/dist/approval-handler.runtime-*.js` to format Discord approval cards for `exec-guard`.

Usage:
```powershell
python ExecGuard/patch_discord_card.py [--check | --revert]
```

Arguments:
- No arguments: applies the card patch. Backs up the original file to `*.execguard-orig`.
- `--check`: exits 0 and prints `patched` if the patch marker exists; exits 1 and prints `NOT patched` otherwise.
- `--revert`: restores `*.execguard-orig` if present.

The patch applies accent colors based on severity (`#ED4245` critical, `#FAA61A` warning, `#57F287` approved, `#99AAB5` expired), displays reasons in bold, formats commands inside PowerShell code blocks, and sets custom button labels. For `catastrophic` commands, confirmation 2 edits card 1 in place rather than posting a new message.

#### guard_check.py

[guard_check.py](../ExecGuard/guard_check.py) verifies that the gateway loaded `exec-guard` when execution mode is set to `full`.

Usage:
```powershell
python ExecGuard/guard_check.py [--wait SECONDS]
```

Arguments:
- `--wait SECONDS`: polling timeout in seconds (default `0`).

If `tools.exec.mode` in `~/.openclaw/openclaw.json` is not `full`, the script exits 0. If `full`, it inspects `~/.openclaw/logs/gateway-stdout.log` for the loaded plugin list. If `exec-guard` is missing after the wait period, it sets `tools.exec.mode` to `ask` via `openclaw.cmd config patch` and tightens `~/.openclaw/exec-approvals.json` (`security: "allowlist"`, `ask: "on-miss"`, `askFallback: "deny"`).

#### manage.py

[manage.py](../ExecGuard/manage.py) inspects and edits stored approvals and audit records.

Usage:
```powershell
python ExecGuard/manage.py list
python ExecGuard/manage.py remove "<command>"
python ExecGuard/manage.py log [N]
```

Arguments:
- `list`: lists all commands in `approved.json` with approval timestamps.
- `remove "<command>"`: deletes a specific normalized command from `approved.json`. Exits 0 on removal, 1 if missing.
- `log [N]`: prints the last `N` lines from `audit.jsonl` (default `20`).

### Configuration keys

Configured under `plugins.entries.exec-guard.config` in `openclaw.json`:

| Key | Type | Default | Description |
|---|---|---|---|
| `reviewer` | object | `{}` | AI reviewer configuration object. |
| `reviewer.enabled` | boolean | `true` | Enables AI review for unknown commands. |
| `reviewer.baseUrl` | string | `http://127.0.0.1:8080/v1` | URL for the OpenAI-compatible completions endpoint. |
| `reviewer.model` | string | `occamy-1.0-fit-quality` | Model identifier sent in completion requests. |
| `reviewer.timeoutMs` | integer | `90000` | Review request timeout in milliseconds (1000 to 300000). |
| `approvalTimeoutMs` | integer | `300000` | Discord card approval timeout in milliseconds (10000 to 600000). |
| `dataDir` | string | `~/.openclaw/exec-guard` | Directory storing `approved.json` and `audit.jsonl`. |

### Fail-safe behaviour

ExecGuard enforces fail-safe operation:
- Trusted policy evaluation fails closed. An unhandled exception aborts command execution.
- Reviewer failures (timeouts, network drops, model unreachability) fall back to owner approval instead of executing.
- `guard_check.py` automatically downgrades OpenClaw to `ask` mode if the plugin fails to load.

## ContentGuard

[ContentGuard](../ContentGuard/index.js) enforces content boundaries on Discord conversations. Non-Discord channels (Web UI, CLI, Bridge) pass through uninspected.

### Registered hooks

ContentGuard registers three hooks:
- `before_agent_run`: priority 90. Checks inbound user requests in `event.prompt`. Blocks prohibited requests before model execution starts.
- `reply_payload_sending`: priority 90. Checks outbound assistant replies in `event.payload.text`. Blocks prohibited content or masks quoted slurs. Removes media attachments (`mediaUrl`, `mediaUrls`) on block.
- `message_sending`: priority 90. Checks outbound text generated via the message tool in `event.content`. Cancels delivery on block or masks slurs.

### Discord scoping

All hooks check channel identity through helper `isDiscord()`. A channel qualifies only when its identifier string begins with `discord` (case-insensitive).

Inbound checks skip evaluation if `event.senderIsOwner` is true and `ownerExempt` is set to `true`. All checks exit immediately when `enabled` is set to `false`.

### Processing pipeline

[moderate.js](../ContentGuard/moderate.js) processes text in five stages:
1. Length validation: text shorter than 3 trimmed characters passes immediately (`allow`, source `short`).
2. Hard minor protection: checks `sexual_minors` pattern regex (proximity of minor and sexual terms within 0 to 6 words). Matches block immediately (`block`, source `hard-rule`) without calling the model.
3. Lexicon matching: normalizes text (NFKD, accent removal, leetspeak conversion `@4->a, 3->e, 1->i, 0->o, $5->s, 7->t`) and scans against compiled terms. If `mode` is `triggered` and zero terms match, text passes immediately (`allow`, source `no-trigger`).
4. Model classification: calls `POST /v1/chat/completions` on the local model with structured JSON output schema returning `category` and `reason`.
5. Decision resolution:
   - If model is unavailable: fails closed (`block`, category `hate_slur`, source `fail-closed`) if lexicon matches exist; fails open (`allow`, source `unavailable-open`) if lexicon is clean.
   - If model returns `slur_mention`: masks text if lexicon matches exist; allows text without modification if lexicon is clean.
   - If model returns a category present in `blockCategories`: blocks delivery (`block`).
   - If model returns `none`: allows delivery (`allow`).

### Moderation categories

Categories recognized by the classifier:
- `sexual_explicit`: sexually explicit content or erotica (18+).
- `sexual_minors`: sexual content involving minors or childlike characters.
- `hate_slur`: slurs used as insults or attacks.
- `hate_discrimination`: discrimination, dehumanizing stereotypes, or hate propaganda.
- `harassment`: targeted harassment, bullying, or threats against a person.
- `self_harm`: instructions or encouragement for self-harm or suicide.
- `graphic_violence`: gratuitous gore or violence.
- `slur_mention`: slurs referenced neutrally or quoted for informational context.
- `none`: text without violations.

### Masking

When `slur_mention` is allowed, `maskTerms()` replaces characters of lexicon matches from index 1 onward with asterisks (`*`), preserving the first letter. If NFKD normalization changes the overall character length, masking aborts to prevent misaligned string replacement.

### Fail mode

When the local model is unreachable:
- Texts containing lexicon matches fail closed and get blocked under `hate_slur`.
- Texts with zero lexicon matches fail open and proceed without blocking.

### Configuration keys

Configured under `plugins.entries.content-guard.config` in `openclaw.json`:

| Key | Type | Default | Description |
|---|---|---|---|
| `enabled` | boolean | `true` | Enables content filtering. |
| `mode` | string | `always` | Evaluation mode: `always` checks all texts; `triggered` checks only texts matching the lexicon. |
| `ownerExempt` | boolean | `false` | Bypasses inbound inspection for the channel owner when `true`. |
| `blockCategories` | array | All 7 categories | List of categories that trigger blocking. |
| `reviewer` | object | `{}` | Local model reviewer configuration. |
| `reviewer.baseUrl` | string | `http://127.0.0.1:8080/v1` | URL for the OpenAI-compatible completions endpoint. |
| `reviewer.model` | string | `occamy-1.0-fit-quality` | Model identifier sent in classification requests. |
| `reviewer.timeoutMs` | integer | `60000` | Classifier timeout in milliseconds (1000 to 300000). |
| `dataDir` | string | `~/.openclaw/content-guard` | Directory storing `audit.jsonl`. |

### Term list files

Lexicon definitions reside in two plain text files:
- `terms-base.txt`: contains 916 base entries loaded from disk.
- `custom-terms.txt`: contains 0 entries by default for site-specific additions.

`loadLexicon()` reads both files, strips comments starting with `#`, trims whitespace, converts to lowercase, and compiles an alternation regular expression sorted by descending string length.

### Audit file

Held-back content and masked outputs log to `audit.jsonl` in `dataDir` (default `~/.openclaw/content-guard`):
- Direction `dir`: `in` (user request), `out` (assistant reply), or `out-tool` (message tool).
- Action `action`: `block` or `mask`.
- Category `category`: matched category name.
- Snippet `text`: first 120 characters of the inspected text.
- Timestamp `ts`: ISO-8601 string.

## StatusReporter

[StatusReporter](../StatusReporter/index.js) relays gateway activity to the status hub. It operates strictly as an observer: hook handlers return no values, modify no payloads, and never block agent actions.

Detailed status hub endpoints and data formats are documented in [status-hub.md](status-hub.md).

### Registered hooks and events

StatusReporter registers four hooks:
- `before_agent_run`: sends `{ kind: "agent", state: "start", channel: "<channelId>" }`.
- `agent_end`: sends `{ kind: "agent", state: "end", channel: "<channelId>" }`.
- `before_tool_call`: registered with `{ priority: 1 }`. Extracts command, path, query, or url parameters and sends `{ kind: "tool", state: "start", name: "<toolName>", summary: "<clippedSummary>" }`.
- `after_tool_call`: sends `{ kind: "tool", state: event.error ? "error" : "end", name: "<toolName>" }`.

`StatusReporter` declares no configuration keys; `configSchema.properties` in [openclaw.plugin.json](../StatusReporter/openclaw.plugin.json) is `{}`.

## Shared modules

The `shared/` directory provides utilities used across all plugins.

### hub.js

[hub.js](../shared/hub.js) handles event reporting to the live status hub.

Exported functions:
- `report(ev)`: POSTs `{ source: "openclaw", ...ev }` to `process.env.AI_HUB_URL` or `${proxyBase()}/_gate/event`. Sets an abort timeout of 800 milliseconds via `AbortSignal.timeout(800)`.
- `clip(v, n = 100)`: serializes and truncates strings or objects to `n` characters, appending `…` when truncated.

Failure behaviour: `report` wraps network requests in `try/catch` and attaches `.catch(() => {})` to fetch promises. Network rejections, HTTP errors, and timeouts fail silently without throwing exceptions or delaying callers.

### config.js

[config.js](../shared/config.js) loads central environment variables from disk.

Exported functions:
- `load()`: reads and parses key-value pairs from `config.env`. Checks file candidates in order: `process.env.LOCALAI_CONFIG`, `shared/config.env`, and the repo-root `config.env`. Caches parsed properties in memory.
- `get(key, def = "")`: retrieves a configuration value from `load()`, returning `def` if absent or empty.
- `proxyBase()`: constructs `http://${get("HOST", "127.0.0.1")}:${get("PROXY_PORT", "8080")}`.

Failure behaviour: file reads are wrapped in `try/catch` blocks. Missing files or syntax issues fail silently, returning an empty cache object `{}` and falling back to default parameter values.

## Tests

Test suites use Node.js built-in test runner.

Commands:
- ExecGuard: `node --test "ExecGuard/test/*.test.js"` (from repository root) or `node --test test/` (from `ExecGuard/`).
- ContentGuard: `node --test "ContentGuard/test/*.js"` (from repository root) or `node --test test/` (from `ContentGuard/`).

Test case counts:
- ExecGuard: [classify.test.js](../ExecGuard/test/classify.test.js) runs 112 test cases.
- ContentGuard: [moderate.test.js](../ContentGuard/test/moderate.test.js) runs 11 test cases.
