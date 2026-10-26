# Live status hub (ESP32 / dashboards)

One place that knows what the local AI is doing. It lives in the wake proxy (`Launcher\ai_hub.py`, served by `llama-wake.py` on `127.0.0.1:8080`), because every client (OpenClaw, the Local Agent Bridge, Open WebUI) already goes through it. Localhost only. Status endpoints never wake the model.

## Read
| Endpoint | Returns |
|---|---|
| `GET /_gate/ai/flat` | `key=value` lines, numbers and short strings. Use this from the ESP32 host service. |
| `GET /_gate/ai` | Full JSON snapshot. |
| `GET /_gate/ai/stream` | Server-sent events, one snapshot per second and on change. |

### Flat keys
`ai.model` 0 asleep / 1 waking / 2 awake (`ai.model_name`) · `ai.phase` (`ai.phase_name`) 0 asleep, 1 loading, 2 idle, 3 prompt (processing the prompt), 4 generating, 5 tool, 6 waiting_approval, 7 error · `ai.active` request in flight · `ai.elapsed_s` time taken (live while active, else the last request) · `ai.gen_tok_s` token speed (live while generating, else last) · `ai.prompt_tok_s` last prompt-processing speed · `ai.gen_tokens` · `ai.prompt_tokens` · `ai.tool` / `ai.tool_state` (running|done|error) / `ai.tool_s` · `ai.step` / `ai.step_max` (Bridge job) · `ai.job` / `ai.job_state` · `ai.guard` (`ai.guard_name`) 0 clear, 1 checking, 2 tripped, 3 waiting_approval, 4 approved, 5 denied, 6 timed_out · `ai.guard_detail` · `ai.guard_trips` · `ai.agent` OpenClaw agent run active · `ai.client` · `ai.unload_in_s` · `ai.ctx` · `ai.sleep_blocked` · `ai.gpu_util` · `ai.vram_mb` · `ai.gpu_temp` · `ai.gpu_power_w` · `ai.requests` · `ai.errors`.

Guard state is the worst of three guards (`exec`, `content`, `sandbox`, each with its own state and detail in the JSON). `tripped`, `denied` and `timed_out` stay visible for 90 s, then read `clear`; `guard.last_trip` keeps the last one, `trips_total` counts them. `waiting_approval` holds until resolved (dropped as `timed_out` after 15 min).

Limits: there is no prompt-processing percentage (llama-server does not expose it); `elapsed_s` is the only progress signal while `phase` is 3. Live speed needs about a second of generation before it shows.

## Write (reporters)
`POST /_gate/event` with JSON, `source` plus `kind`:
- `{"kind":"agent","state":"start|end","channel":"discord"}`
- `{"kind":"tool","state":"start|end|error","name":"exec","summary":"..."}`
- `{"kind":"guard","guard":"exec|content|sandbox","state":"checking|clear|tripped|waiting_approval|approved|denied|timed_out","tier":"...","detail":"..."}`
- `{"kind":"job","id":"...","state":"queued|running|done|error|...","jtype":"ask|run","engine":"harness","mode":"read-only|write|exec","step":3,"max_steps":25,"step_label":"thinking"}`

Reporters: `E:\Local LLM\StatusReporter` (OpenClaw agent runs and every tool call), ExecGuard (shell guard: checking, waiting for approval, approved, denied, timed out), ContentGuard (inbound/outbound checks, tripped), the Bridge `localagent.py` and `harness_worker.py` (job, step, tool, sandbox refusals). All use `E:\Local LLM\shared\hub.js` (OpenClaw) or a 0.7 s fire-and-forget POST (Bridge); a dead proxy never slows or breaks them.

## Plugging into the ESP32 PC Controller
The host service (`host/pc_stats`, C#) already streams LibreHardwareMonitor sensors to the carrier as `@d/@v/@e` lines. It also polls `http://127.0.0.1:8080/_gate/ai/flat` once a second and adds the numeric keys as extra dictionary entries with ids like `/localai/0/phase`, `/localai/0/gen_tok_s`. Strings (`ai.tool`, `ai.guard_detail`, `ai.phase_name`) need a new line type (for example `@t key text`) in `pcstats.cpp`.
