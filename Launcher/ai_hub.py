"""Live status hub for the local AI stack. Imported by llama-wake.py (the proxy every client goes through).

Everything that touches the model reports here, and anything can read one JSON document back:
  inference  - the proxy itself: request in flight, phase (prompt processing / generating), live token speed, timings
  OpenClaw   - StatusReporter plugin (agent start/end, tool calls) + ExecGuard / ContentGuard (guardrail state)
  Bridge     - localagent.py / harness_worker.py (job, step, tool, sandbox refusals)

Endpoints (served by llama-wake.py, localhost only, never wake the model):
  GET  /_gate/ai          full JSON snapshot
  GET  /_gate/ai/flat     `key=value` lines of numbers and short strings, for the ESP32 host service (codes below)
  GET  /_gate/ai/stream   server-sent events, one snapshot per second (and on change)
  POST /_gate/event       {"source","kind",...} from a reporter; see Hub.event

Phase codes   0 asleep  1 loading  2 idle  3 prompt  4 generating  5 tool  6 waiting_approval  7 error
Guard codes   0 clear   1 checking  2 tripped  3 waiting_approval  4 approved  5 denied  6 timed_out
Model codes   0 asleep  1 waking  2 awake
"""
import http.client
import json
import re
import subprocess
import threading
import time

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
PHASES = ["asleep", "loading", "idle", "prompt", "generating", "tool", "waiting_approval", "error"]
GUARDS = ["clear", "checking", "tripped", "waiting_approval", "approved", "denied", "timed_out"]
MODEL = ["asleep", "waking", "awake"]
RECENT_S = 90            # tripped / denied / timed_out stay visible this long, then read as clear (last_trip keeps them)
WAIT_MAX_S = 900         # a waiting_approval nobody resolved is dropped after this
TOOL_LINGER_S = 6        # a finished tool still shows for this long
ACTIVE_JOB_STALE_S = 1800
GUARD_NAMES = ("exec", "content", "sandbox")


def _now():
    return time.time()


class Hub:
    def __init__(self, backend, backend_port, alias):
        self.B, self.port, self.alias = backend, backend_port, alias
        self.lock = threading.RLock()
        self.t_start = _now()
        self.reqs = {}                                   # rid -> dict, in-flight proxied requests
        self.rid = 0
        self.last = None                                 # last finished inference
        self.totals = {"requests": 0, "errors": 0, "prompt_tokens": 0, "gen_tokens": 0}
        self.live = {"decoded": 0, "t_decode0": None, "tok_s": 0.0, "processing": False, "ts": 0.0}
        self.tool = {"name": "", "state": "", "source": "", "summary": "", "since": 0.0, "end": 0.0, "count": 0}
        self.last_tool = {"name": "", "ts": 0.0}
        self.agent = {"active": False, "source": "", "channel": "", "since": 0.0}
        self.job = None                                  # active Bridge job
        self.guard = {g: {"state": "clear", "detail": "", "tier": "", "ts": 0.0, "source": ""} for g in GUARD_NAMES}
        self.last_trip = None
        self.trips = 0
        self.gpu = {}
        self.events_n = 0
        self.changed = threading.Condition()
        self.version = 0
        threading.Thread(target=self._poll_slots, daemon=True).start()
        threading.Thread(target=self._poll_gpu, daemon=True).start()

    # ------------------------------------------------------------------ proxy request tracking
    def req_start(self, path, headers, body):
        stream = False
        try:
            stream = bool(json.loads(body or b"{}").get("stream"))
        except Exception:  # noqa: BLE001
            pass
        ua = (headers.get("X-Client") or headers.get("User-Agent") or "")[:40]
        with self.lock:
            self.rid += 1
            self.reqs[self.rid] = {"t0": _now(), "path": path.split("?")[0], "stream": stream, "ua": ua, "tail": b"", "t_first": None}
            self.live.update(decoded=0, t_decode0=None, tok_s=0.0)
            self.totals["requests"] += 1
            if self.tool["state"] == "running":      # a new model call means the previous tool call has returned (some hosts never report the end)
                self._tool_end("end", _now())
            r = self.rid
        self._bump()
        return r

    def req_chunk(self, rid, chunk):
        with self.lock:
            r = self.reqs.get(rid)
            if r is None:
                return
            if r["t_first"] is None:
                r["t_first"] = _now()
            r["tail"] = (r["tail"] + chunk)[-12000:]

    def req_end(self, rid, status, ok):
        with self.lock:
            r = self.reqs.pop(rid, None)
            if r is None:
                return
            tm = self._timings(r["tail"])
            dt = _now() - r["t0"]
            self.last = {
                "ok": bool(ok) and status < 400, "status": status, "total_s": round(dt, 1), "path": r["path"], "ts": _now(),
                "prompt_n": tm.get("prompt_n"), "cached_n": tm.get("cache_n"), "gen_n": tm.get("predicted_n"),
                "prompt_tok_s": round(tm["prompt_per_second"], 1) if tm.get("prompt_per_second") else None,
                "gen_tok_s": round(tm["predicted_per_second"], 1) if tm.get("predicted_per_second") else None,
                "prompt_s": round(tm["prompt_ms"] / 1000, 1) if tm.get("prompt_ms") else None,
                "gen_s": round(tm["predicted_ms"] / 1000, 1) if tm.get("predicted_ms") else None,
            }
            if not self.last["ok"]:
                self.totals["errors"] += 1
            self.totals["prompt_tokens"] += tm.get("prompt_n") or 0
            self.totals["gen_tokens"] += tm.get("predicted_n") or 0
            if not self.reqs:
                self.live.update(decoded=0, t_decode0=None, processing=False)
        self._bump()

    @staticmethod
    def _timings(tail):
        # non-stream: one JSON object; stream: the last data chunk carries "timings":{...}
        try:
            txt = tail.decode("utf-8", "replace")
            i = txt.rfind('"timings"')
            if i < 0:
                return {}
            j = txt.index("{", i)
            obj, _ = json.JSONDecoder().raw_decode(txt[j:])
            return obj
        except Exception:  # noqa: BLE001
            return {}

    # ------------------------------------------------------------------ events from reporters
    def event(self, ev):
        kind, src = ev.get("kind"), str(ev.get("source") or "")[:20]
        now = _now()
        with self.lock:
            self.events_n += 1
            if kind == "agent":
                on = ev.get("state") == "start"
                self.agent = {"active": on, "source": src, "channel": str(ev.get("channel") or "")[:20], "since": now if on else self.agent["since"]}
                if not on and self.tool["state"] == "running" and self.tool["source"] == src:
                    self._tool_end("end", now)
            elif kind == "tool":
                st = ev.get("state")
                if st == "start":
                    self.tool = {"name": str(ev.get("name") or "")[:40], "state": "running", "source": src,
                                 "summary": str(ev.get("summary") or "")[:120], "since": now, "end": 0.0, "count": self.tool["count"] + 1}
                    self.last_tool = {"name": self.tool["name"], "ts": now}
                else:
                    self._tool_end(st or "end", now)
            elif kind == "guard":
                g = str(ev.get("guard") or "")
                if g in self.guard:
                    st = ev.get("state") if ev.get("state") in GUARDS else "clear"
                    self.guard[g] = {"state": st, "detail": str(ev.get("detail") or "")[:140], "tier": str(ev.get("tier") or "")[:20],
                                     "ts": now, "source": src}
                    if st in ("tripped", "denied", "timed_out"):
                        self.trips += 1
                        self.last_trip = {"guard": g, "state": st, "detail": self.guard[g]["detail"], "ts": now, "source": src}
            elif kind == "job":
                st = ev.get("state")
                if st in ("done", "error", "cancelled", "truncated", "max_steps", "timeout", "context_exhausted"):
                    if self.job and self.job.get("id") == ev.get("id"):
                        self.job.update(state=st, ended=now)
                        self.job["step_label"] = ""
                else:
                    j = self.job if (self.job and self.job.get("id") == ev.get("id")) else {"id": ev.get("id"), "t0": now}
                    for k in ("engine", "mode", "ctx", "step", "max_steps", "step_label", "prompt"):
                        if k in ev:
                            j[k] = ev[k]
                    if "jtype" in ev:
                        j["kind"] = ev["jtype"]
                    j.update(state=st or j.get("state", "running"), ts=now, source=src)
                    self.job = j
        self._bump()

    def _tool_end(self, st, now):
        self.tool.update(state="error" if st == "error" else "done", end=now)

    # ------------------------------------------------------------------ pollers
    def _poll_slots(self):
        while True:
            busy = bool(self.reqs)
            time.sleep(0.5 if busy else 2.0)
            if not busy:
                continue
            try:
                c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
                c.request("GET", "/slots")
                slots = json.loads(c.getresponse().read())
                c.close()
            except Exception:  # noqa: BLE001
                continue
            if isinstance(slots, dict):
                slots = slots.get("slots") or [slots]
            s = slots[0] if slots else {}
            nt = s.get("next_token")
            if isinstance(nt, list):
                nt = nt[0] if nt else {}
            nt = nt or {}
            decoded = int(nt.get("n_decoded") or 0)
            now = _now()
            with self.lock:
                self.live["processing"] = bool(s.get("is_processing"))
                if decoded > 0:
                    if self.live["t_decode0"] is None or decoded < self.live["decoded"]:
                        self.live.update(t_decode0=now, decoded=decoded, base=decoded)
                    else:
                        dt = now - self.live["t_decode0"]
                        base = self.live.get("base", 0)
                        if dt > 1.0 and decoded > base:
                            self.live["tok_s"] = round((decoded - base) / dt, 1)
                        self.live["decoded"] = decoded
                else:
                    self.live.update(decoded=0, t_decode0=None, tok_s=0.0)
                self.live["ts"] = now
            self._bump()

    def _poll_gpu(self):
        while True:
            try:
                out = subprocess.run(
                    ["nvidia-smi", "--query-gpu=utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw", "--format=csv,noheader,nounits"],
                    capture_output=True, text=True, timeout=4, creationflags=NO_WINDOW).stdout.strip().splitlines()[0]
                u, mu, mt, t, p = [x.strip() for x in out.split(",")]
                with self.lock:
                    self.gpu = {"util": int(u), "vram_used_mb": int(mu), "vram_total_mb": int(mt), "temp_c": int(t), "power_w": float(p)}
            except Exception:  # noqa: BLE001
                pass
            time.sleep(3)

    def _bump(self):
        with self.changed:
            self.version += 1
            self.changed.notify_all()

    # ------------------------------------------------------------------ views
    def _guard_view(self, now):
        order = {"clear": 0, "approved": 0, "denied": 1, "timed_out": 2, "checking": 3, "tripped": 4, "waiting_approval": 5}
        view = {}
        for g, d in self.guard.items():
            st, age = d["state"], now - d["ts"]
            if st == "waiting_approval" and age > WAIT_MAX_S:
                st = "timed_out"
            elif st in ("tripped", "denied", "timed_out", "approved") and age > RECENT_S:
                st = "clear"
            elif st == "checking" and age > 120:
                st = "clear"
            view[g] = {"state": st, "code": GUARDS.index(st), "detail": d["detail"] if st != "clear" else "", "tier": d["tier"],
                       "age_s": int(age) if d["ts"] else None, "source": d["source"]}
        top = max((v["state"] for v in view.values()), key=lambda s: order[s])
        return view, top

    def snapshot(self):
        now = _now()
        bs = self.B.status()
        with self.lock:
            model_state = bs["state"]
            guards, gtop = self._guard_view(now)
            busy = bool(self.reqs)
            tool_running = self.tool["state"] == "running" and now - self.tool["since"] < 900
            tool_recent = self.tool["state"] in ("done", "error") and now - self.tool["end"] < TOOL_LINGER_S
            job = self.job
            job_live = bool(job and job.get("state") in ("queued", "running") and now - job.get("ts", 0) < ACTIVE_JOB_STALE_S)
            if model_state == "waking":
                phase = "loading"
            elif gtop == "waiting_approval":
                phase = "waiting_approval"
            elif busy:
                phase = "generating" if self.live["decoded"] > 0 else "prompt"
            elif tool_running:
                phase = "tool"
            elif model_state == "asleep":
                phase = "asleep"
            elif self.last and not self.last["ok"] and now - self.last["ts"] < 30:
                phase = "error"
            else:
                phase = "idle"
            first = min(self.reqs.values(), key=lambda r: r["t0"]) if self.reqs else None
            inf = {
                "active": busy, "in_flight": len(self.reqs), "phase": phase, "phase_code": PHASES.index(phase),
                "elapsed_s": round(now - first["t0"], 1) if first else 0.0,
                "gen_tokens": self.live["decoded"] if busy else 0,
                "gen_tok_s_live": self.live["tok_s"] if busy and self.live["decoded"] > 0 else 0.0,
                "client": first["ua"] if first else "",
                "last": self.last,
            }
            step = {"label": "", "n": 0, "max": 0}
            if job_live:
                step = {"label": job.get("step_label", ""), "n": int(job.get("step") or 0), "max": int(job.get("max_steps") or 0)}
            snap = {
                "ts": round(now, 3), "uptime_s": int(now - self.t_start), "alias": self.alias,
                "model": {"state": model_state, "code": MODEL.index(model_state), "ctx": bs.get("ctx"), "unload_in_s": bs.get("unload_in_s"),
                          "idle_s": bs.get("idle_s"), "sleep_blocked": bs.get("sleep_blocked")},
                "inference": inf,
                "step": step,
                "tool": {"name": self.tool["name"] if (tool_running or tool_recent) else "", "state": self.tool["state"] if (tool_running or tool_recent) else "",
                         "source": self.tool["source"], "summary": self.tool["summary"] if (tool_running or tool_recent) else "",
                         "running_s": round(now - self.tool["since"], 1) if tool_running else 0.0, "calls_total": self.tool["count"],
                         "last": self.last_tool},
                "guard": {"state": gtop, "code": GUARDS.index(gtop), "trips_total": self.trips, "last_trip": self.last_trip, **guards},
                "agent": self.agent,
                "job": ({"id": job.get("id"), "state": job.get("state"), "kind": job.get("kind"), "engine": job.get("engine"), "mode": job.get("mode"),
                         "elapsed_s": round((job.get("ended") or now) - job["t0"], 1), "prompt": job.get("prompt", "")} if job else None),
                "gpu": self.gpu,
                "totals": self.totals,
            }
        return snap

    def flat(self):
        s = self.snapshot()
        i, last, g, t, st, m = s["inference"], s["inference"]["last"] or {}, s["guard"], s["tool"], s["step"], s["model"]
        gen = i["gen_tok_s_live"] if i["gen_tok_s_live"] else (last.get("gen_tok_s") or 0)
        done = (last.get("total_s") if not i["active"] else i["elapsed_s"]) or 0
        job = s["job"] or {}
        rows = [
            ("ai.model", m["code"]), ("ai.model_name", m["state"]), ("ai.phase", i["phase_code"]), ("ai.phase_name", i["phase"]),
            ("ai.active", int(i["active"])), ("ai.elapsed_s", done), ("ai.gen_tok_s", gen), ("ai.prompt_tok_s", last.get("prompt_tok_s") or 0),
            ("ai.gen_tokens", i["gen_tokens"] if i["active"] else (last.get("gen_n") or 0)), ("ai.prompt_tokens", last.get("prompt_n") or 0),
            ("ai.tool", t["name"]), ("ai.tool_state", t["state"]), ("ai.tool_s", t["running_s"]),
            ("ai.step", st["n"]), ("ai.step_max", st["max"]), ("ai.job", job.get("id") or ""), ("ai.job_state", job.get("state") or ""),
            ("ai.guard", g["code"]), ("ai.guard_name", g["state"]), ("ai.guard_detail", (g.get("exec", {}).get("detail") if g["exec"]["state"] != "clear" else
                                                                                           g["content"]["detail"] or g["sandbox"]["detail"]) or ""),
            ("ai.guard_trips", g["trips_total"]), ("ai.agent", int(s["agent"]["active"])), ("ai.client", i["client"]),
            ("ai.unload_in_s", m["unload_in_s"] or 0), ("ai.ctx", m["ctx"] or 0), ("ai.sleep_blocked", int(bool(m["sleep_blocked"]))),
            ("ai.gpu_util", s["gpu"].get("util", 0)), ("ai.vram_mb", s["gpu"].get("vram_used_mb", 0)), ("ai.gpu_temp", s["gpu"].get("temp_c", 0)),
            ("ai.gpu_power_w", s["gpu"].get("power_w", 0)), ("ai.requests", s["totals"]["requests"]), ("ai.errors", s["totals"]["errors"]),
        ]
        return "".join(f"{k}={str(v).replace(chr(10), ' ')}\n" for k, v in rows)
