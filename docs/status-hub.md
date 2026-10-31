# Status hub

The status hub aggregates state from inference requests, OpenClaw plugins, and the Local Agent Bridge. It runs inside the wake proxy ([ai_hub.py](../Launcher/ai_hub.py), imported by [llama-wake.py](../Launcher/llama-wake.py)) on `127.0.0.1:8080`.

All status endpoints bind to localhost only. Reading status endpoints never wakes the model.

## Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/_gate/ai` | Full JSON state snapshot. |
| `GET` | `/_gate/ai/flat` | Line-delimited `key=value` feed for microcontrollers. |
| `GET` | `/_gate/ai/stream` | Server-sent events (`text/event-stream`), sent on update and at 1 Hz. |
| `POST` | `/_gate/event` | Ingest endpoint for reporters. Returns `{"ok": true}`. |

## Flat feed keys

`GET /_gate/ai/flat` returns newline-separated `key=value` pairs.

| Key | Type | Description |
|---|---|---|
| `ai.model` | integer | Model status code: `0` (asleep), `1` (waking), `2` (awake). |
| `ai.model_name` | string | Model status name: `asleep`, `waking`, or `awake`. |
| `ai.phase` | integer | Phase code (`0` to `7`). |
| `ai.phase_name` | string | Current phase name. |
| `ai.active` | integer | `1` when an inference request is in flight, otherwise `0`. |
| `ai.elapsed_s` | float | Elapsed seconds of the active request, or total duration of the last request. |
| `ai.gen_tok_s` | float | Generation tokens per second (live during generation, else last request). |
| `ai.prompt_tok_s` | float | Prompt processing tokens per second of the last request. |
| `ai.gen_tokens` | integer | Tokens generated (live count while active, else last request total). |
| `ai.prompt_tokens` | integer | Prompt tokens evaluated during the last request. |
| `ai.tool` | string | Current or recent tool name. Empty when idle. |
| `ai.tool_state` | string | Tool execution state (`running`, `done`, `error`). |
| `ai.tool_s` | float | Tool run time in seconds while active, otherwise `0.0`. |
| `ai.step` | integer | Current step index of an active Bridge job. |
| `ai.step_max` | integer | Maximum step count configured for the Bridge job. |
| `ai.job` | string | Active Bridge job identifier. |
| `ai.job_state` | string | Bridge job state: `queued`, `running`, or a final state such as `done`, `error`, `cancelled`, `truncated`, `max_steps`, `timeout`, `context_exhausted`. |
| `ai.guard` | integer | Code for the highest-severity guard state (`0` to `6`). |
| `ai.guard_name` | string | Highest-severity guard state name. |
| `ai.guard_detail` | string | Detail string from the highest-priority non-clear guard. |
| `ai.guard_trips` | integer | Total count of guard trip events since startup. |
| `ai.agent` | integer | `1` when an OpenClaw agent run is active, otherwise `0`. |
| `ai.client` | string | Client user agent or identifier from the active request header. |
| `ai.unload_in_s` | integer | Seconds remaining before idle model unload, or `0`. |
| `ai.ctx` | integer | Active context size in tokens, or `0` if unloaded. |
| `ai.sleep_blocked` | integer | `1` if system sleep prevention is held, otherwise `0`. |
| `ai.gpu_util` | integer | GPU utilization percentage from nvidia-smi. |
| `ai.vram_mb` | integer | Used GPU VRAM in megabytes. |
| `ai.gpu_temp` | integer | GPU temperature in degrees Celsius. |
| `ai.gpu_power_w` | float | GPU power draw in watts. |
| `ai.requests` | integer | Cumulative count of proxied requests. |
| `ai.errors` | integer | Cumulative count of failed requests. |

## JSON snapshot schema

`GET /_gate/ai` returns an object containing these top-level keys:

- `ts`: Current Unix timestamp.
- `uptime_s`: Seconds elapsed since the status hub initialized.
- `alias`: Configured model alias.
- `model`: Object with `state`, `code`, `ctx`, `unload_in_s`, `idle_s`, and `sleep_blocked`.
- `inference`: Object with `active`, `in_flight`, `phase`, `phase_code`, `elapsed_s`, `gen_tokens`, `gen_tok_s_live`, `client`, and `last` metrics (`ok`, `status`, `total_s`, `path`, `ts`, `prompt_n`, `cached_n`, `gen_n`, `prompt_tok_s`, `gen_tok_s`, `prompt_s`, `gen_s`).
- `step`: Object with `label`, `n`, and `max`.
- `tool`: Object with `name`, `state`, `source`, `summary`, `running_s`, `calls_total`, and `last` (`name`, `ts`). Finished tools linger for 6 seconds.
- `guard`: Object with `state`, `code`, `trips_total`, `last_trip` record, and per-guard objects for `exec`, `content`, and `sandbox` (`state`, `code`, `detail`, `tier`, `age_s`, `source`).
- `agent`: Object with `active`, `source`, `channel`, and `since`.
- `job`: Active Bridge job object or `null` (`id`, `state`, `kind`, `engine`, `mode`, `elapsed_s`, `prompt`).
- `gpu`: Object with `util`, `vram_used_mb`, `vram_total_mb`, `temp_c`, and `power_w`. Polled every 3 seconds.
- `totals`: Cumulative counters for `requests`, `errors`, `prompt_tokens`, and `gen_tokens`.

## State codes

### Phase codes

| Code | State | Condition |
|---|---|---|
| `0` | `asleep` | Model backend process is not running. |
| `1` | `loading` | Model backend process is starting. |
| `2` | `idle` | Model is awake with no active requests or tools. |
| `3` | `prompt` | Model is processing prompt tokens. |
| `4` | `generating` | Model is generating response tokens (`decoded > 0`). |
| `5` | `tool` | Tool execution is active (within 900 seconds of start). |
| `6` | `waiting_approval` | A guard is waiting for user confirmation. |
| `7` | `error` | Last inference request returned an error within the past 30 seconds. |

### Guard codes

| Code | State |
|---|---|
| `0` | `clear` |
| `1` | `checking` |
| `2` | `tripped` |
| `3` | `waiting_approval` |
| `4` | `approved` |
| `5` | `denied` |
| `6` | `timed_out` |

`ai.guard` shows the highest-ranked state across all guards. Rank from lowest to highest: `clear` and `approved`, `denied`, `timed_out`, `checking`, `tripped`, `waiting_approval`.

Guard states expire automatically:
- `tripped`, `denied`, `timed_out`, and `approved` reset to `clear` after 90 seconds. `guard.last_trip` retains the event.
- `checking` resets to `clear` after 120 seconds.
- `waiting_approval` transitions to `timed_out` after 900 seconds (15 minutes).

### Model codes

| Code | State |
|---|---|
| `0` | `asleep` |
| `1` | `waking` |
| `2` | `awake` |

## Event reporters

Reporters post JSON to `POST /_gate/event`.

- OpenClaw plugins (`StatusReporter`, `ExecGuard`, `ContentGuard`) report through [hub.js](../shared/hub.js). Requests send `source: "openclaw"` with an 800 ms timeout.
- Bridge workers (`localagent.py`, `harness_worker.py`) send POST requests with a 0.7 second timeout and `source: "bridge"`.

Failures are swallowed silently. A stopped wake proxy never blocks agent execution.

### Payload shapes

Agent lifecycle:

```json
{"kind": "agent", "state": "start", "channel": "discord"}
```

Tool execution:

```json
{"kind": "tool", "state": "start", "name": "exec", "summary": "git status"}
```

Guard state:

```json
{"kind": "guard", "guard": "exec", "state": "waiting_approval", "tier": "danger", "detail": "Remove-Item"}
```

Bridge jobs:

```json
{"kind": "job", "id": "job-101", "state": "running", "jtype": "run", "engine": "harness", "mode": "write", "step": 3, "max_steps": 25, "step_label": "editing"}
```

## ESP32 integration

The companion project [ESP32-PC-Control-System](https://github.com/lnminh1411/ESP32-PC-Control-System) displays PC and AI stack statistics on three ST7789 screens.

The host service (`host/pc_stats`, written in C#) polls `http://127.0.0.1:8080/_gate/ai/flat` once per second. Numeric values map directly to telemetry dictionaries, such as `/localai/0/phase` and `/localai/0/gen_tok_s`. String fields (`ai.tool`, `ai.guard_detail`, `ai.phase_name`) require custom text handling (such as `@t key text`) in `pcstats.cpp`.

Because `/_gate/` requests are marked passive, polling never wakes the model or resets the idle unload timer.
