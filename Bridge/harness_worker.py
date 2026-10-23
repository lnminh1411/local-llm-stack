#!/usr/bin/env python3
"""Runs ONE Local Agent Bridge job through DeepSeek Harness (dsh). Started by localagent.py with the venv python:

    harness\\venv\\Scripts\\python.exe -I -X utf8 harness_worker.py <request.json>

Protocol on stdout (one item per line): `LOG <text>` progress, last line `RESULT <json>`.
The harness runtime is a bundled native binary; this script only generates its config patch, answers its
approval channel (always "no"), counts steps/tokens, and enforces the step and wall-clock limits.

Permission model (maps the CLI flags to dsh sandbox presets):
  no flag   read-only      file tools read anywhere, nothing can be written, no shell
  --write   workspace-write  write/edit confined to --cwd (+ temp roots) by dsh's file sandbox
  --exec    danger-full-access  the shell tool is NOT sandboxed (dsh's Windows shell sandbox does not start in
            the SDK build: `--profile <name> is required`), so the shell and file tools can touch anything,
            exactly like the old run_command. Opt-in only.
Anything the sandbox refuses would need an approval; the SDK has no approval channel, so it is refused for good.
"""
import json
import os
import sys
import threading
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "harness", "venv", "Lib", "site-packages"))

from deepseek_harness import DeepSeekHarness  # noqa: E402

# Plugins that never belong in a local, offline-by-default worker: DeepSeek log/telemetry upload, DeepSeek-only web
# search, and everything that needs a human (questions, plan approval) or that fans out parallel model calls on a
# one-slot GPU (sub-agents, workflows, goal/ralph loops).
ALWAYS_OFF = [
    "session-log-deepseek", "session-telemetry-otel", "plugin-package-inventory-deepseek", "web-search-deepseek",
    "tool-goal", "tool-ralph", "tool-workflow", "tool-subagent", "tool-subagent-fork", "tool-subagent-control",
    "tool-subagent-list-agents", "plan-mode", "user-questions",
]


def emit(kind, payload):
    sys.stdout.write(f"{kind} {payload}\n")
    sys.stdout.flush()


HUB = {"url": None}


def hub(**ev):
    """Fire-and-forget status to the wake proxy hub (see ai_hub.py next to llama-wake.py). Never raises."""
    if not HUB["url"]:
        return
    try:
        ev.setdefault("source", "bridge")
        r = urllib.request.Request(HUB["url"], data=json.dumps(ev).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(r, timeout=0.7).read()
    except Exception:  # noqa: BLE001
        pass


def build_patch(req):
    cfg = req["cfg"]
    keep = set(cfg.get("harness_keep") or [])
    off = [i for i in ALWAYS_OFF if i not in keep]
    if not req.get("exec"):
        off += ["tool-pwsh", "tool-jobs"]
    if not req.get("web"):
        off += ["tool-web"]
    off += [i for i in (cfg.get("harness_disable") or []) if i not in off]
    rows = [f"- id: {i}\n  disabled: true\n" for i in off]
    model = cfg["model"]
    rows.append(
        "- id: llm-pi-ai\n  config:\n    providers:\n      local:\n"
        "        apiKeyEnv: LOCAL_LLM_KEY\n        api: openai-completions\n"
        f"        baseURL: {cfg['base_url'].rstrip('/')}/v1\n"
        "        compat:\n          supportsDeveloperRole: false\n          maxTokensField: max_tokens\n"
        "        models:\n"
        f"          - id: {model}\n            name: {model}\n            input: [text, image]\n"
        f"            contextWindow: {int(req['ctx'])}\n            maxTokens: {int(req['max_tokens'])}\n"
        "            reasoningEfforts:\n              off:\n              high: high\n"
    )
    rows.append(f"- id: agent-default-model\n  config:\n    provider: local\n    model: {model}\n")
    suffix = (
        "Your working directory is {{cwd}}. You are a local worker agent on a Windows 11 PC, handed one task by a more "
        "capable cloud agent that sees only your final message, not your tool calls. Inspect the real files with the "
        "tools; never guess contents. Be economical: grep before reading whole files. If a tool is refused or errors, "
        "adjust once, then report the problem instead of looping. When finished, reply WITHOUT calling a tool: a short "
        "summary of what you did and found, the exact paths you changed, and anything unresolved. No filler."
    )
    rows.append(
        "- id: system-prompt\n  config:\n    personaPrefix: You are a coding agent powered by the {{model}} model.\n"
        f"    personaSuffix: {json.dumps(suffix)}\n"
    )
    return "".join(rows)


def main():
    req = json.load(open(sys.argv[1], encoding="utf-8"))
    cfg = req["cfg"]
    HUB["url"] = cfg["base_url"].rstrip("/") + "/_gate/event"
    home = req["dsh_home"]
    os.makedirs(home, exist_ok=True)
    patch = os.path.join(req["job_dir"], "harness.patch.yml")
    with open(patch, "w", encoding="utf-8") as f:
        f.write(build_patch(req))
    mode = "danger-full-access" if req.get("exec") else ("workspace-write" if req.get("write") else "read-only")
    env = {"LOCAL_LLM_KEY": "local", "DSH_PERMISSION_MODE": mode, "DSH_TELEMETRY_MODE": "OFF"}
    max_steps = int(req.get("max_steps") or cfg["max_steps"])
    timeout_s = int(req.get("timeout_s") or cfg["request_timeout_s"])

    state = {"steps": 0, "in": 0, "cached": 0, "out": 0, "text": "", "changed": [], "t0": time.time(), "calls": {}}
    done = threading.Event()

    def finish(status, answer, reason=None):
        res = {
            "status": status, "answer": answer, "reasoning": None, "finish_reason": reason,
            "files_changed": sorted(set(state["changed"])), "steps": state["steps"], "mode": mode,
            "stats": {
                "seconds": round(time.time() - state["t0"], 1), "prompt_tokens_processed": state["in"],
                "prompt_tokens_cached": state["cached"], "completion_tokens": state["out"],
                "prompt_tok_s": None, "gen_tok_s": None, "ctx": req["ctx"], "model_calls": state["steps"],
            },
        }
        emit("RESULT", json.dumps(res, ensure_ascii=False))
        done.set()
        os._exit(0)  # the dsh runtime child is reaped by the job-tree kill / its own stdin close

    def on_note(n):
        if n.method != "session.event":
            return
        ev = n.payload.get("event") or {}
        t, d = ev.get("type"), ev.get("data") or {}
        if n.payload.get("sessionId") != req["session_id"]:
            return
        if t == "step/start":
            state["steps"] = d.get("step", state["steps"] + 1)
            emit("LOG", f"step {state['steps']}: thinking ...")
            hub(kind="job", id=req["session_id"], state="running", step=state["steps"], max_steps=max_steps, step_label="thinking")
            if state["steps"] > max_steps:
                finish("max_steps", state["text"] or f"[stopped: hit the {max_steps}-step limit without a final answer]")
        elif t == "assistant/message":
            u = d.get("usage") or {}
            state["in"] += u.get("inputTokens", 0)
            state["cached"] += u.get("cacheReadTokens", 0)
            state["out"] += u.get("outputTokens", 0)
            for blk in (d.get("message") or {}).get("content") or []:
                if blk.get("type") == "text" and blk.get("text"):
                    state["text"] = blk["text"]
        elif t == "tool/call":
            name, args = d.get("name"), d.get("arguments")
            state["calls"][d.get("callId")] = (name, args)
            emit("LOG", f"  tool {name} {str(args)[:160]}")
            hub(kind="tool", state="start", name=name, summary=str(args)[:100])
        elif t == "tool/result":
            msg = d.get("message") or {}
            for blk in msg.get("content") or []:
                if blk.get("type") != "tool-result":
                    continue
                name, args = state["calls"].get(blk.get("toolCallId"), (None, None))
                text = " ".join(c.get("text", "") for c in (blk.get("content") or []) if isinstance(c, dict))
                refused = "sandbox" in text.lower() and ("denied" in text.lower() or "requires approval" in text.lower())
                hub(kind="tool", state="error" if blk.get("isError") else "end", name=name)
                if refused:
                    hub(kind="guard", guard="sandbox", state="tripped", detail=f"{name}: {text[:110]}")
                if blk.get("isError"):
                    continue
                if name in ("write", "edit"):
                    try:
                        state["changed"].append(json.loads(args).get("file_path"))
                    except Exception:  # noqa: BLE001
                        pass

    threading.Timer(timeout_s, lambda: finish("timeout", state["text"] or f"[stopped: {timeout_s}s limit]")).start()

    try:
        with DeepSeekHarness(
            provider="local", model=cfg["model"], max_tokens=int(req["max_tokens"]),
            reasoning_effort="high" if req.get("think") else None,
            cwd=req["cwd"], dsh_home=home, profile="sdk", patches=(patch,), env=env,
            initialize_timeout_seconds=180, request_timeout_seconds=None,
        ) as h:
            h.start()
            client = h.client

            def deny_requests():
                while not done.is_set():
                    try:
                        rq = client.next_request()
                    except Exception:  # noqa: BLE001
                        return
                    emit("LOG", f"  refused runtime request {rq.method}")
                    hub(kind="guard", guard="sandbox", state="tripped", detail=f"approval request refused: {rq.method}")
                    try:
                        client.respond_error(rq.id, -32000, "denied: non-interactive worker")
                    except Exception:  # noqa: BLE001
                        return

            threading.Thread(target=deny_requests, daemon=True).start()
            sess = h.start_session(req["session_id"])
            r = sess.run(req["prompt"], on_notification=on_note)
        status = {"completed": "done", "max-tokens": "truncated"}.get(r.finish_reason or "", "error" if r.finish_reason == "error" else "done")
        finish(status, (r.final_response or state["text"] or "").strip(), r.finish_reason)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        emit("RESULT", json.dumps({"status": "error", "error": f"harness: {type(e).__name__}: {e}", "code": 1}))
        os._exit(0)


if __name__ == "__main__":
    main()
