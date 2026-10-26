#!/usr/bin/env python3
"""Wake-on-request proxy in front of llama-server.

Listens on 127.0.0.1:8080 (what OpenClaw and the Local Agent Bridge talk to) and forwards to the real
llama-server on 127.0.0.1:8081. After --idle-minutes without a real request it kills llama-server, which frees
the ~16 GB of RAM and ~5.5 GB of VRAM the weights occupy. The next request starts it again (Start-LocalAI.ps1
-BackendOnly) and is held until the model is ready (~25-100 s), then forwarded.

What does NOT survive an unload: the KV cache and prompt cache. They live in the llama-server process and
cannot be reused after a restart on this hybrid (recurrent) model: a slot restored from disk is accepted in
0.3 s and then discarded ("forcing full prompt re-processing"), measured 2026-10-04. So the first request after
an unload re-processes the whole prompt (~36 s for OpenClaw's 24k-token system prompt).

Admin endpoints (not forwarded):
  GET  /_gate/status
  POST /_gate/ensure  {"ctx": N, "exact": false}   start / resize the backend, block until ready
  POST /_gate/sleep  {"force": false}              unload the model now (refused while a request runs unless force)
  POST /_gate/config  {"idle_minutes": N}          0 disables idle unload
  POST /_gate/config  {"keep_awake": false}        stop holding Windows awake while the model is loaded (default on)
  GET  /_gate/ai | /_gate/ai/flat | /_gate/ai/stream   live inference / tool / guardrail status (ESP32 display); POST /_gate/event feeds it. See ai_hub.py

Stdlib only. Started by Start-LocalAI.ps1; log at ~/.openclaw/logs/wake-proxy.log.
"""
import argparse
import http.client
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ai_hub  # noqa: E402  live status hub (see its docstring)

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailers",
       "transfer-encoding", "upgrade", "host", "content-length"}
PASSIVE = ("/health", "/props", "/slots", "/v1/models", "/models", "/_gate/")   # never counts as activity, never wakes the model
# (Open WebUI and other UIs poll /v1/models; waking the model for that would keep it loaded forever)

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

A = None  # parsed args


def log(msg):
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}", flush=True)


class Backend:
    def __init__(self):
        self.life = threading.RLock()          # serialises start / stop / resize
        self.cv = threading.Condition()
        self.inflight = 0
        self.last_activity = time.time()
        self.ctx = None                        # context size of the running backend
        self.desired_ctx = A.ctx               # what a wake-up starts with
        self.waking = False
        self.idle_minutes = A.idle_minutes
        self.keep_awake = A.keep_awake         # block Windows idle sleep while the model is loaded
        self.awake_held = False                # the block is in force right now

    # --- state
    def ready(self):
        try:
            c = http.client.HTTPConnection("127.0.0.1", A.backend_port, timeout=2)
            c.request("GET", "/health")
            ok = c.getresponse().status == 200
            c.close()
            return ok
        except Exception:
            return False

    def state(self):
        if self.waking:
            return "waking"
        return "awake" if self.ready() else "asleep"

    def status(self):
        st = self.state()
        idle = time.time() - self.last_activity
        return {
            "state": st, "ctx": self.ctx if st == "awake" else None, "desired_ctx": self.desired_ctx,
            "inflight": self.inflight, "idle_s": int(idle), "idle_minutes": self.idle_minutes,
            "keep_awake": self.keep_awake, "sleep_blocked": self.awake_held,
            "unload_in_s": (max(0, int(self.idle_minutes * 60 - idle)) if st == "awake" and self.idle_minutes and not self.inflight else None),
        }

    def touch(self):
        self.last_activity = time.time()

    def probe_ctx(self):
        try:
            c = http.client.HTTPConnection("127.0.0.1", A.backend_port, timeout=3)
            c.request("GET", "/props")
            n = json.loads(c.getresponse().read())["default_generation_settings"]["n_ctx"]
            c.close()
            return int(n)
        except Exception:
            return None

    # --- lifecycle
    def _launcher(self, ctx):
        cmd = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", A.launcher,
               "-BackendOnly", "-Context", str(ctx)]
        # No pipes: the llama-server the launcher spawns would inherit them and keep them open forever,
        # so run() would never see EOF (this hung the first test for 500 s).
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           creationflags=NO_WINDOW, timeout=600)
        if r.returncode != 0:
            log(f"launcher failed rc={r.returncode} (see ~/.openclaw/logs/llama-stderr.log)")

    def stop(self):
        subprocess.run(["taskkill", "/F", "/T", "/IM", "llama-server.exe"], capture_output=True, creationflags=NO_WINDOW)
        for _ in range(20):
            if not self.ready():
                break
            time.sleep(0.5)
        time.sleep(1.5)   # let the driver release VRAM
        self.ctx = None

    def ensure(self, ctx=None, exact=False, drain_s=180):
        """Block until the backend is up with `ctx` context (or at least `ctx` unless exact)."""
        with self.life:
            want = ctx or self.desired_ctx
            up = self.ready()
            if up and self.ctx is None:
                self.ctx = self.probe_ctx()
            need_restart = up and (self.ctx is not None) and (self.ctx != want if exact else self.ctx < want)
            if up and not need_restart:
                return
            self.waking = True
            try:
                if need_restart:
                    t0 = time.time()
                    with self.cv:
                        while self.inflight and time.time() - t0 < drain_s:
                            self.cv.wait(2)
                    log(f"resizing backend {self.ctx} -> {want}")
                    self.stop()
                else:
                    log(f"waking backend (ctx {want})")
                t0 = time.time()
                self._launcher(want)
                for _ in range(240):
                    if self.ready():
                        break
                    time.sleep(2)
                else:
                    raise RuntimeError("backend did not become ready")
                self.ctx = self.desired_ctx = want
                log(f"backend ready in {time.time() - t0:.0f}s (ctx {want})")
            finally:
                self.waking = False
            self.touch()

    def sleep_now(self, force=False):
        with self.life:
            with self.cv:
                if self.inflight and not force:
                    return False
            if not self.ready() and not force:
                return True
            log("unloading backend (forced)" if force else "unloading backend (idle)")
            self.stop()
            self.desired_ctx = A.ctx           # a 256K swap does not outlive an unload
            return True

    def keep_awake_loop(self):
        """Hold Windows awake (no idle sleep) while the model is loaded or loading; release the moment it is unloaded.
        SetThreadExecutionState is per-thread and ES_CONTINUOUS stays in force until that same thread clears it,
        so one long-lived thread owns it. Display may still turn off; manual sleep, lid and power button still work."""
        if os.name != "nt":
            return
        import ctypes
        f = ctypes.windll.kernel32.SetThreadExecutionState
        f.argtypes = [ctypes.c_uint32]
        f.restype = ctypes.c_uint32
        while True:
            try:
                want = bool(self.keep_awake and (self.waking or self.ready()))
                if want != self.awake_held:
                    f(ES_CONTINUOUS | (ES_SYSTEM_REQUIRED if want else 0))
                    self.awake_held = want
                    log("keeping Windows awake (model loaded)" if want else "released sleep block (model unloaded)")
            except Exception as e:  # noqa: BLE001
                log(f"keep-awake error: {e}")
            time.sleep(10)

    def watchdog(self):
        while True:
            time.sleep(30)
            try:
                if (self.idle_minutes and not self.waking and not self.inflight
                        and time.time() - self.last_activity > self.idle_minutes * 60 and self.ready()):
                    self.sleep_now()
            except Exception as e:  # noqa: BLE001
                log(f"watchdog error: {e}")


B = None
H = None   # ai_hub.Hub


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _json(self, code, obj):
        data = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(data)
        self.close_connection = True

    def _body(self):
        # .NET's PostAsJsonAsync (LocalAIHost) sends chunked bodies without a
        # Content-Length; reading 0 bytes there dropped every admin argument
        if "chunked" in (self.headers.get("Transfer-Encoding") or "").lower():
            out = b""
            while True:
                size = int(self.rfile.readline().split(b";")[0].strip() or b"0", 16)
                if size == 0:
                    while self.rfile.readline() not in (b"\r\n", b"\n", b""):
                        pass   # trailers
                    return out
                out += self.rfile.read(size)
                self.rfile.readline()   # CRLF after each chunk
        n = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(n) if n else b""

    def _admin(self, body):
        p = self.path.split("?")[0]
        if p == "/_gate/status":
            return self._json(200, B.status())
        if p == "/_gate/ai":
            return self._json(200, H.snapshot())
        if p == "/_gate/ai/flat":
            data = H.flat().encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(data)
            self.close_connection = True
            return
        if p == "/_gate/ai/stream":
            return self._sse()
        try:
            args = json.loads(body or b"{}")
        except json.JSONDecodeError:
            return self._json(400, {"error": "bad json"})
        if p == "/_gate/event":
            H.event(args if isinstance(args, dict) else {})
            return self._json(200, {"ok": True})
        if p == "/_gate/ensure":
            try:
                B.ensure(args.get("ctx"), bool(args.get("exact")))
            except Exception as e:  # noqa: BLE001
                return self._json(500, {"error": str(e)})
            return self._json(200, B.status())
        if p == "/_gate/sleep":
            return self._json(200, {"ok": B.sleep_now(bool(args.get("force")))})
        if p == "/_gate/config":
            if "idle_minutes" in args:
                B.idle_minutes = float(args["idle_minutes"])
            if "keep_awake" in args:
                B.keep_awake = bool(args["keep_awake"])
            return self._json(200, B.status())
        return self._json(404, {"error": "unknown admin path"})

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        seen = -1
        try:
            while True:
                with H.changed:
                    if H.version == seen:
                        H.changed.wait(1.0)
                    seen = H.version
                self.wfile.write(b"data: " + json.dumps(H.snapshot()).encode() + b"\n\n")
                self.wfile.flush()
                time.sleep(0.25)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def _proxy(self):
        body = self._body()
        if self.path.startswith("/_gate/"):
            return self._admin(body)
        passive = self.path.startswith(PASSIVE)
        if passive and not B.ready():
            # answer cheaply without waking the model
            if self.path.startswith("/health"):
                return self._json(503, {"status": "asleep"})
            if self.path.startswith("/props"):
                return self._json(200, {"asleep": True, "total_slots": 1,
                                        "default_generation_settings": {"n_ctx": B.desired_ctx}})
            if self.path.startswith(("/v1/models", "/models")):
                m = {"id": A.alias, "object": "model", "created": 0, "owned_by": "llamacpp"}
                return self._json(200, {"object": "list", "data": [m], "models": [m]})
            return self._json(200, [{"id": 0, "n_ctx": B.desired_ctx, "is_processing": False}])
        if not passive:
            B.touch()
            try:
                B.ensure()
            except Exception as e:  # noqa: BLE001
                return self._json(503, {"error": {"message": f"model failed to start: {e}"}})
        with B.cv:
            B.inflight += 1
        conn = None
        rid = H.req_start(self.path, self.headers, body) if not passive else None
        ok = False
        status = 0
        try:
            conn = http.client.HTTPConnection("127.0.0.1", A.backend_port, timeout=3600)
            hdrs = {k: v for k, v in self.headers.items() if k.lower() not in HOP}
            conn.request(self.command, self.path, body=body or None, headers=hdrs)
            resp = conn.getresponse()
            status = resp.status
            self.send_response(resp.status)
            clen = resp.getheader("Content-Length")
            for k, v in resp.getheaders():
                if k.lower() not in HOP:
                    self.send_header(k, v)
            if clen:
                self.send_header("Content-Length", clen)
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            while True:
                chunk = resp.read1(65536)
                if not chunk:
                    ok = True
                    break
                if rid:
                    H.req_chunk(rid, chunk)
                self.wfile.write(chunk)
                self.wfile.flush()
                if not passive:
                    B.touch()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass    # client went away; closing the upstream socket aborts generation
        except Exception as e:  # noqa: BLE001
            try:
                self._json(502, {"error": {"message": f"proxy error: {e}"}})
            except Exception:
                pass
        finally:
            if rid:
                H.req_end(rid, status or 502, ok)
            if conn:
                conn.close()
            with B.cv:
                B.inflight -= 1
                B.cv.notify_all()
            if not passive:
                B.touch()

    do_GET = do_POST = do_PUT = do_DELETE = do_OPTIONS = _proxy


def main():
    global A, B
    p = argparse.ArgumentParser()
    p.add_argument("--listen-port", type=int, default=8080)
    p.add_argument("--backend-port", type=int, default=8081)
    p.add_argument("--ctx", type=int, default=131072)
    p.add_argument("--idle-minutes", type=float, default=20)
    p.add_argument("--alias", default="qwen3.6-35b-a3b-apex")
    p.add_argument("--launcher", required=True)
    p.add_argument("--no-keep-awake", dest="keep_awake", action="store_false",
                   help="do not hold Windows awake while the model is loaded")
    A = p.parse_args()
    B = Backend()
    if B.ready():
        B.ctx = B.probe_ctx()
    global H
    H = ai_hub.Hub(B, A.backend_port, A.alias)
    threading.Thread(target=B.watchdog, daemon=True).start()
    threading.Thread(target=B.keep_awake_loop, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", A.listen_port), Handler)
    srv.daemon_threads = True
    log(f"wake proxy on :{A.listen_port} -> :{A.backend_port}, ctx {A.ctx}, idle unload {A.idle_minutes} min")
    srv.serve_forever()


if __name__ == "__main__":
    main()
