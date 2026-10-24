#!/usr/bin/env python3
"""Local Agent Bridge: lets a cloud agent (Claude Code, Antigravity) hand work to the local model.

The local model is served by llama-server behind the wake proxy; settings come from config.env.
The same server answers the OpenClaw Discord bot.

Subcommands
  ask     one-shot question; files / stdin are inlined as context. No tools.
  run     worker agent loop with tools (read, grep, glob, list; write/exec/web opt-in).
  status  server, context size, GPU, jobs, 256K timer.
  wait    block (up to --timeout seconds) on a detached job, print its result.
  result  print a finished job's result.   jobs  list recent jobs.   cancel  kill a job.
  heavy   on|off|status: swap the server to 256K context now / back to 128K now.

Context policy: the server runs 128K (~2-3 tok/s on this card). A job that does not fit escalates to 256K by itself
(or `--heavy` forces it; `--no-escalate` forbids it): ~14 tok/s, model restart 20-100 s, and a background reaper
puts it back to 128K after `heavy_idle_minutes` of no jobs. The wake proxy unloads the model after 20 idle minutes;
any request wakes it. One job at a time (file lock); others queue.

Stdlib only. State lives in ~/.local-agent (jobs, lock, heavy timer).
"""
import argparse
import ctypes
import fnmatch
import glob as globlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

if sys.platform == "win32":
    for _s in (sys.stdout, sys.stderr):
        try:
            _s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)
import localai_config as LC

STATE = os.path.join(os.path.expanduser("~"), ".local-agent")
JOBS = os.path.join(STATE, "jobs")
LOCK = os.path.join(STATE, "gpu.lock")
HEAVY = os.path.join(STATE, "heavy.json")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

CFG = {
    "base_url": LC.base_url(),
    "model": LC.get("MODEL_ALIAS", "occamy-1.0-fit-quality"),
    "default_ctx": int(LC.get("CONTEXT", "131072")),
    "heavy_ctx": int(LC.get("HEAVY_CONTEXT", "262144")),
    "heavy_idle_minutes": 10,
    "launcher": os.path.join(LC.get("SCRIPTS_DIR", ""), "Start-LocalAI.ps1") if LC.get("SCRIPTS_DIR") else "",
    "max_tokens": 4096,
    "temperature": 0.4,
    "top_p": 0.9,
    "presence_penalty": 0.3,
    # llama.cpp sampler extras merged into every request. DRY stops the repetition loops a 0.3-temperature
    # review prompt fell into (2026-10-04: 1800 tokens of "Let's look at Line 159 again").
    "extra_body": {"dry_multiplier": 0.8},
    "request_timeout_s": 3600,
    "max_steps": 25,
    "tool_output_chars": 24000,
    "job_retention_days": 7,
    # `run` engine: "harness" = DeepSeek Harness (dsh) via harness_worker.py, "legacy" = the built-in tool loop below.
    "engine": "harness",
    "harness_python": os.path.join(HERE, "harness", "venv", "Scripts", "python.exe"),
    "harness_keep": [],      # dsh plugin ids to keep ON that harness_worker.ALWAYS_OFF would disable (e.g. "tool-subagent")
    "harness_disable": [],   # extra dsh plugin ids to switch off
}
try:
    with open(os.path.join(HERE, "config.json"), encoding="utf-8") as _f:
        CFG.update({k: v for k, v in json.load(_f).items() if not k.startswith("_")})
except FileNotFoundError:
    pass

EXIT_OK, EXIT_ERR, EXIT_RUNNING, EXIT_NEEDS_HEAVY, EXIT_TOO_BIG = 0, 1, 2, 3, 4


class BridgeError(Exception):
    def __init__(self, msg, code=EXIT_ERR):
        super().__init__(msg)
        self.code = code


# --------------------------------------------------------------------------- small utils
def now():
    return time.time()


def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def write_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def pid_alive(pid):
    if not pid:
        return False
    if sys.platform != "win32":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    code = ctypes.c_ulong()
    ok = k32.GetExitCodeProcess(h, ctypes.byref(code))
    k32.CloseHandle(h)
    return bool(ok) and code.value == 259  # STILL_ACTIVE


def spawn_detached(cmdline):
    """Start a hidden process that is NOT a child of the caller's job object (WMI Create), so it
    survives the agent tool that launched us. Returns the pid."""
    ps = (
        "$s = New-CimInstance -ClassName Win32_ProcessStartup -Property @{ShowWindow=[uint16]0} -ClientOnly; "
        "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
        "-Arguments @{CommandLine=$env:LA_CMD; ProcessStartupInformation=$s}; "
        "if ($r.ReturnValue -ne 0) { exit 1 }; $r.ProcessId"
    )
    env = dict(os.environ, LA_CMD=cmdline)
    out = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True,
                         env=env, creationflags=NO_WINDOW, timeout=60)
    if out.returncode != 0:
        raise BridgeError(f"could not spawn detached process: {out.stdout.strip()} {out.stderr.strip()}")
    return int(out.stdout.strip().split()[-1])


def human(n):
    return f"{n/1024:.0f}k" if n >= 2048 else str(n)


# --------------------------------------------------------------------------- llama-server access
def http(path, body=None, timeout=30):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(CFG["base_url"] + path, data=data,
                                 method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "null")


def hub_event(**ev):
    """Fire-and-forget status report to the wake proxy's hub (ESP32 display / dashboards). Never raises, never blocks long."""
    try:
        ev.setdefault("source", "bridge")
        req = urllib.request.Request(CFG["base_url"].rstrip("/") + "/_gate/event", data=json.dumps(ev).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=0.7).read()
    except Exception:
        pass


def gate_status():
    """Wake-proxy status. None = no proxy (llama-server answering directly); {'state': 'down'} = nothing listening."""
    try:
        return http("/_gate/status", timeout=5)
    except urllib.error.HTTPError:
        return None
    except Exception:
        return {"state": "down"}


def server_state():
    g = gate_status()
    if g is None:
        try:
            http("/health", timeout=3)
            return "ready"
        except Exception:
            return "down"
    return {"awake": "ready", "asleep": "asleep", "waking": "loading"}.get(g.get("state"), "down")


def server_ctx():
    g = gate_status()
    if g is not None:
        return g.get("ctx")
    try:
        return int(http("/props", timeout=5)["default_generation_settings"]["n_ctx"])
    except Exception:
        return None


def slot_busy():
    g = gate_status()
    return bool(g and g.get("inflight"))


def count_tokens(text):
    try:
        return len(http("/tokenize", {"content": text, "add_special": False}, timeout=900)["tokens"])
    except Exception:
        return int(len(text) / 3.2) + 1


def launcher_path():
    p = CFG.get("launcher", "")
    if p and os.path.exists(p):
        return p
    raise BridgeError("Start-LocalAI.ps1 not found (set SCRIPTS_DIR in config.env)")


def start_stack(ctx, log):
    """The wake proxy itself is down (e.g. before logon autostart): run the launcher, which brings up
    proxy + model but leaves the Discord gateway alone."""
    log("wake proxy is not running; starting the local AI stack ...")
    spawn_detached(f'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File "{launcher_path()}" '
                   f"-NoGateway -Context {ctx}")
    for _ in range(60):
        time.sleep(2)
        if (gate_status() or {}).get("state") != "down":
            return
    raise BridgeError("wake proxy did not come up (see ~/.openclaw/logs/wake-proxy*.log)")


def ensure_ctx(want, log, exact=False):
    """Make the model server run with at least `want` context (exactly `want` if exact). Wakes it if it was
    unloaded; the proxy drains in-flight requests before a resize. Returns the context size."""
    g = gate_status()
    if g is not None and g.get("state") == "down":
        start_stack(want, log)
        g = gate_status()
    if g is None:
        raise BridgeError("llama-server is running without the wake proxy; restart it with Start-LocalAI.ps1 -Restart")
    if g.get("state") == "asleep":
        log("model is unloaded (idle); waking it, ~20-100 s ...")
    elif g.get("ctx") and (g["ctx"] != want if exact else g["ctx"] < want):
        log(f"resizing context {human(g['ctx'])} -> {human(want)} (model restarts, ~20-100 s) ...")
    try:
        r = http("/_gate/ensure", {"ctx": want, "exact": exact}, timeout=1200)
    except urllib.error.HTTPError as e:
        raise BridgeError(f"could not bring the model up: {e.read().decode('utf-8', 'replace')[:300]}")
    except Exception as e:
        raise BridgeError(f"could not bring the model up: {e}")
    ctx = r.get("ctx") or want
    log(f"model ready, n_ctx={ctx}")
    if ctx > CFG["default_ctx"]:
        touch_heavy()
    return ctx


def touch_heavy():
    h = read_json(HEAVY, {}) or {}
    h.setdefault("since", now())
    h["last_used"] = now()
    write_json(HEAVY, h)
    if not pid_alive(h.get("reaper_pid")):
        pid = spawn_detached(f'"{sys.executable}" "{os.path.abspath(__file__)}" _reaper')
        h["reaper_pid"] = pid
        write_json(HEAVY, h)


# --------------------------------------------------------------------------- GPU lock (one job at a time)
def lock_info():
    info = read_json(LOCK)
    if info and pid_alive(info.get("pid")):
        return info
    return None


def acquire_lock(owner, log, wait_s=3600):
    os.makedirs(STATE, exist_ok=True)
    t0 = now()
    told = False
    while True:
        try:
            fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, json.dumps({"pid": os.getpid(), "owner": owner, "t": now()}).encode())
            os.close(fd)
            return
        except FileExistsError:
            if not lock_info():
                try:
                    os.remove(LOCK)
                except OSError:
                    pass
                continue
            if not told:
                log(f"queued: another local-agent job holds the model ({(read_json(LOCK) or {}).get('owner')})")
                told = True
            if now() - t0 > wait_s:
                raise BridgeError("timed out waiting for the local-agent lock")
            time.sleep(2)


def release_lock():
    info = read_json(LOCK)
    if info and info.get("pid") == os.getpid():
        try:
            os.remove(LOCK)
        except OSError:
            pass


# --------------------------------------------------------------------------- reaper (256K -> 128K)
def revert_to_default(log, warm=True):
    ensure_ctx(CFG["default_ctx"], log, exact=True)
    try:
        os.remove(HEAVY)
    except OSError:
        pass
    if warm:   # best effort: put the OpenClaw agent's ~24k-token prefix back into the prompt cache
        try:
            spawn_detached('cmd.exe /c openclaw agent --agent main -m "Reply with the single word: ready"')
        except BridgeError:
            pass


def reaper():
    me = os.getpid()
    idle_s = float(CFG["heavy_idle_minutes"]) * 60
    while True:
        time.sleep(15)
        h = read_json(HEAVY)
        if not h or h.get("reaper_pid") != me:
            return
        if server_state() != "ready" or (server_ctx() or 0) <= CFG["default_ctx"]:
            try:
                os.remove(HEAVY)
            except OSError:
                pass
            return
        if lock_info():
            h["last_used"] = now()
            write_json(HEAVY, h)
            continue
        if now() - h.get("last_used", now()) < idle_s:
            continue
        try:
            acquire_lock("reaper", lambda m: None, wait_s=30)
        except BridgeError:
            continue
        try:
            revert_to_default(lambda m: None)
        finally:
            release_lock()
        return


# --------------------------------------------------------------------------- jobs
def new_job_id():
    return time.strftime("%m%d-%H%M%S-") + uuid.uuid4().hex[:4]


def job_dir(jid):
    return os.path.join(JOBS, jid)


def prune_jobs():
    cutoff = now() - CFG["job_retention_days"] * 86400
    try:
        for d in os.listdir(JOBS):
            p = os.path.join(JOBS, d)
            if os.path.isdir(p) and os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
    except OSError:
        pass


class Job:
    def __init__(self, jid, echo):
        self.id = jid
        self.dir = job_dir(jid)
        self.echo = echo
        self.meta = read_json(os.path.join(self.dir, "job.json"), {})

    def log(self, msg):
        line = f"[{time.strftime('%H:%M:%S')}] {msg}"
        os.makedirs(self.dir, exist_ok=True)
        with open(os.path.join(self.dir, "log.txt"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
        if self.echo:
            print(f"[local-agent] {msg}", file=sys.stderr, flush=True)

    def set(self, **kw):
        self.meta.update(kw)
        write_json(os.path.join(self.dir, "job.json"), self.meta)


def create_job(req):
    jid = new_job_id()
    os.makedirs(job_dir(jid), exist_ok=True)
    j = Job(jid, echo=False)
    j.set(id=jid, status="queued", created=now(), request=req)
    return jid


# --------------------------------------------------------------------------- prompt assembly
def expand_files(specs, base):
    out = []
    for s in specs or []:
        if s == "-":
            continue
        p = os.path.expandvars(os.path.expanduser(s))
        if not os.path.isabs(p):
            p = os.path.join(base, p)
        hits = sorted(globlib.glob(p, recursive=True)) if any(c in p for c in "*?[") else [p]
        for h in hits:
            if os.path.isfile(h):
                out.append(os.path.abspath(h))
            elif not os.path.exists(h):
                raise BridgeError(f"file not found: {h}")
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def read_text(path):
    with open(path, "rb") as f:
        raw = f.read()
    if b"\x00" in raw[:4096]:
        return f"[binary file, {len(raw)} bytes, skipped]"
    return raw.decode("utf-8", errors="replace")


def build_context(req):
    parts = []
    for p in req.get("files", []):
        parts.append(f'<file path="{p}">\n{read_text(p)}\n</file>')
    if req.get("stdin"):
        parts.append(f'<file path="stdin">\n{req["stdin"]}\n</file>')
    return ("<files>\n" + "\n".join(parts) + "\n</files>\n\n") if parts else ""


ASK_SYSTEM = (
    "You are a local assistant working for a more capable cloud agent that delegated this question to you. "
    "Answer exactly what was asked, precisely and concisely. Quote file paths and line content verbatim. "
    "If the answer is not in the provided material, say so instead of guessing."
)


def run_system(req, tools):
    caps = ["read-only file access anywhere (read_file, list_dir, glob, grep)"]
    if "write_file" in tools:
        caps.append(f"write access ONLY inside the working directory (write_file, edit_file)")
    if "run_command" in tools:
        caps.append("run_command (Windows cmd.exe, working directory below)")
    if "web_search" in tools:
        caps.append("web_search, web_fetch")
    return (
        "You are a local worker agent on a Windows 11 PC, handed a task by a more capable cloud agent that "
        "will not see your tool calls, only your final message. Use the tools to inspect the real files; "
        "never guess contents. Be economical: read only what you need, prefer grep over reading whole files.\n"
        f"Working directory: {req['cwd']}\n"
        "Available: " + "; ".join(caps) + ".\n"
        "Paths may be absolute or relative to the working directory. If a tool errors, adjust and retry once, "
        "then report the problem rather than looping.\n"
        "When finished, reply WITHOUT calling a tool. Final message format: a short summary of what you did and "
        "found, the exact paths you changed (if any), and anything unresolved. No filler."
    )


# --------------------------------------------------------------------------- tools
def tool_schemas(req):
    def fn(name, desc, props, required):
        return {"type": "function", "function": {"name": name, "description": desc,
                "parameters": {"type": "object", "properties": props, "required": required}}}
    S, I = {"type": "string"}, {"type": "integer"}
    tools = [
        fn("read_file", "Read a text file. Returns up to `limit` lines starting at 1-based line `offset`.",
           {"path": S, "offset": I, "limit": I}, ["path"]),
        fn("list_dir", "List a directory (directories end with /, files show size in bytes).", {"path": S}, ["path"]),
        fn("glob", "Find files by glob pattern, e.g. src/**/*.py. Relative patterns resolve against the working directory.",
           {"pattern": S}, ["pattern"]),
        fn("grep", "Regex search (Python re, case-insensitive) in a file or directory tree. Returns path:line: text.",
           {"pattern": S, "path": S, "file_glob": S}, ["pattern"]),
    ]
    if req.get("write"):
        tools += [
            fn("write_file", "Create or overwrite a file inside the working directory with the given full content.",
               {"path": S, "content": S}, ["path", "content"]),
            fn("edit_file", "Replace one exact, unique occurrence of old_string with new_string in a file inside the working directory.",
               {"path": S, "old_string": S, "new_string": S}, ["path", "old_string", "new_string"]),
        ]
    if req.get("exec"):
        tools.append(fn("run_command", "Run a Windows cmd.exe command in the working directory (timeout 120 s). "
                        "Returns the exit code and the tail of the output.",
                        {"command": S, "timeout": I}, ["command"]))
    if req.get("web"):
        tools += [
            fn("web_search", "Search the web (DuckDuckGo). Returns title, url, snippet for the top results.", {"query": S}, ["query"]),
            fn("web_fetch", "Fetch a URL and return its text content (tags stripped).", {"url": S}, ["url"]),
        ]
    return tools


def _resolve(p, cwd):
    p = os.path.expandvars(os.path.expanduser(p or "."))
    return os.path.normpath(p if os.path.isabs(p) else os.path.join(cwd, p))


def _inside(p, root):
    rp, rr = os.path.normcase(os.path.realpath(p)), os.path.normcase(os.path.realpath(root))
    return rp == rr or rp.startswith(rr.rstrip("\\/") + os.sep)


def _cap(s):
    lim = CFG["tool_output_chars"]
    return s if len(s) <= lim else s[:lim] + f"\n[... truncated, {len(s) - lim} more chars; narrow the request]"


SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", "dist", "build"}


class Tools:
    def __init__(self, req, job):
        self.req, self.job, self.cwd = req, job, req["cwd"]
        self.changed = []

    def call(self, name, args):
        try:
            fn = getattr(self, "t_" + name, None)
            if fn is None or name not in {t["function"]["name"] for t in tool_schemas(self.req)}:
                return f"error: unknown or disabled tool '{name}'"
            return _cap(fn(**args))
        except TypeError as e:
            return f"error: bad arguments for {name}: {e}"
        except Exception as e:
            return f"error: {type(e).__name__}: {e}"

    def t_read_file(self, path, offset=1, limit=400):
        p = _resolve(path, self.cwd)
        if not os.path.isfile(p):
            return f"error: not a file: {p}"
        text = read_text(p)
        lines = text.splitlines()
        offset, limit = max(1, int(offset)), max(1, min(int(limit), 2000))
        chunk = lines[offset - 1: offset - 1 + limit]
        head = f"[{p}: lines {offset}-{offset + len(chunk) - 1} of {len(lines)}]\n"
        return head + "\n".join(chunk)

    def t_list_dir(self, path="."):
        p = _resolve(path, self.cwd)
        if not os.path.isdir(p):
            return f"error: not a directory: {p}"
        rows = []
        for n in sorted(os.listdir(p), key=str.lower)[:500]:
            fp = os.path.join(p, n)
            rows.append(n + "/" if os.path.isdir(fp) else f"{n}  ({os.path.getsize(fp)} bytes)")
        return "\n".join(rows) or "(empty)"

    def t_glob(self, pattern):
        p = _resolve(pattern, self.cwd)
        hits = sorted(h for h in globlib.glob(p, recursive=True) if not any(s in h.split(os.sep) for s in SKIP_DIRS))
        return "\n".join(hits[:300]) + (f"\n[{len(hits) - 300} more]" if len(hits) > 300 else "") if hits else "(no matches)"

    def t_grep(self, pattern, path=".", file_glob=None):
        rx = re.compile(pattern, re.I)
        root = _resolve(path, self.cwd)
        out, files = [], []
        if os.path.isfile(root):
            files = [root]
        else:
            for dp, dn, fns in os.walk(root):
                dn[:] = [d for d in dn if d not in SKIP_DIRS]
                for fn_ in fns:
                    if file_glob and not fnmatch.fnmatch(fn_, file_glob):
                        continue
                    files.append(os.path.join(dp, fn_))
        for fp in files:
            try:
                if os.path.getsize(fp) > 2_000_000:
                    continue
                with open(fp, "rb") as f:
                    raw = f.read()
                if b"\x00" in raw[:4096]:
                    continue
                for i, line in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
                    if rx.search(line):
                        out.append(f"{fp}:{i}: {line.strip()[:200]}")
                        if len(out) >= 150:
                            return "\n".join(out) + "\n[stopped at 150 matches]"
            except OSError:
                continue
        return "\n".join(out) or "(no matches)"

    def _writable(self, path):
        p = _resolve(path, self.cwd)
        if not _inside(p, self.cwd):
            raise PermissionError(f"writes are limited to the working directory ({self.cwd}); refused {p}")
        return p

    def t_write_file(self, path, content):
        p = self._writable(path)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        if p not in self.changed:
            self.changed.append(p)
        return f"wrote {len(content)} chars to {p}"

    def t_edit_file(self, path, old_string, new_string):
        p = self._writable(path)
        text = read_text(p)
        n = text.count(old_string)
        if n != 1:
            return f"error: old_string matched {n} times in {p}; it must match exactly once (include more context)"
        with open(p, "w", encoding="utf-8", newline="") as f:
            f.write(text.replace(old_string, new_string))
        if p not in self.changed:
            self.changed.append(p)
        return f"edited {p}"

    def t_run_command(self, command, timeout=120):
        r = subprocess.run(command, shell=True, cwd=self.cwd, capture_output=True, text=True, errors="replace",
                           timeout=min(int(timeout), 300), stdin=subprocess.DEVNULL, creationflags=NO_WINDOW)
        out = (r.stdout or "") + (("\n[stderr]\n" + r.stderr) if r.stderr else "")
        return f"exit code {r.returncode}\n" + out[-8000:]

    def t_web_search(self, query):
        url = "https://html.duckduckgo.com/html/?" + urllib.parse.urlencode({"q": query})
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            html = r.read().decode("utf-8", errors="replace")
        blocks = re.findall(r'class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?class="result__snippet"[^>]*>(.*?)</a>',
                            html, re.S)
        rows = []
        for link, title, snip in blocks[:6]:
            m = re.search(r"uddg=([^&]+)", link)
            link = urllib.parse.unquote(m.group(1)) if m else link
            strip = lambda s: re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", s)).strip()
            rows.append(f"{strip(title)}\n  {link}\n  {strip(snip)}")
        return "\n".join(rows) or "(no results)"

    def t_web_fetch(self, url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        with urllib.request.urlopen(req, timeout=15) as r:
            raw = r.read(1_500_000).decode("utf-8", errors="replace")
        raw = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", raw)
        text = re.sub(r"[ \t]+", " ", re.sub(r"<[^>]+>", " ", raw))
        return re.sub(r"\n\s*\n+", "\n\n", text).strip()


# --------------------------------------------------------------------------- model calls
class Stats:
    def __init__(self):
        self.t0 = now()
        self.prompt_n = self.pred_n = self.cache_n = self.calls = 0
        self.prompt_ms = self.pred_ms = 0.0

    def add(self, timings):
        t = timings or {}
        self.calls += 1
        self.prompt_n += t.get("prompt_n", 0)
        self.cache_n += t.get("cache_n", 0)
        self.pred_n += t.get("predicted_n", 0)
        self.prompt_ms += t.get("prompt_ms", 0.0)
        self.pred_ms += t.get("predicted_ms", 0.0)

    def as_dict(self, ctx):
        return {
            "seconds": round(now() - self.t0, 1),
            "prompt_tokens_processed": self.prompt_n,
            "prompt_tokens_cached": self.cache_n,
            "completion_tokens": self.pred_n,
            "prompt_tok_s": round(self.prompt_n / (self.prompt_ms / 1000), 1) if self.prompt_ms else None,
            "gen_tok_s": round(self.pred_n / (self.pred_ms / 1000), 1) if self.pred_ms else None,
            "ctx": ctx,
            "model_calls": self.calls,
        }


def chat(messages, req, tools=None):
    body = {
        "model": CFG["model"],
        "messages": messages,
        "max_tokens": req.get("max_tokens") or CFG["max_tokens"],
        "temperature": CFG["temperature"] if req.get("temp") is None else req["temp"],
        "top_p": CFG["top_p"],
        "presence_penalty": CFG["presence_penalty"],
        "stream": False,
    }
    body.update(CFG.get("extra_body") or {})
    if tools:
        body["tools"] = tools
        body["tool_choice"] = "auto"
    if req.get("think"):
        body["chat_template_kwargs"] = {"enable_thinking": True}
    try:
        r = http("/v1/chat/completions", body, timeout=CFG["request_timeout_s"])
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")[:600]
        raise BridgeError(f"llama-server HTTP {e.code}: {detail}")
    except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
        raise BridgeError(f"llama-server unreachable: {e}")
    ch = r["choices"][0]
    return ch["message"], ch.get("finish_reason"), r.get("timings"), r.get("usage", {})


# --------------------------------------------------------------------------- job execution
def preflight(req, job, system_text, ctx_have_hint=None):
    """Count tokens and decide whether the job fits the context it will get."""
    body = build_context(req)
    need = count_tokens(system_text + "\n" + body + req["prompt"])
    reserve = (req.get("max_tokens") or CFG["max_tokens"]) + (6000 if req["kind"] == "run" else 500)
    limit_default = CFG["default_ctx"]
    limit_heavy = CFG["heavy_ctx"]
    job.log(f"input ~{need} tokens (+{reserve} reserve)")
    if need + reserve > limit_heavy:
        raise BridgeError(f"input is ~{need} tokens; even 256K cannot hold it. Split the input.", EXIT_TOO_BIG)
    if need + reserve > limit_default and not req.get("heavy"):
        if req.get("no_escalate"):
            raise BridgeError(f"input is ~{need} tokens, over the 128K limit and --no-escalate was given. "
                              f"Drop the flag to run at 256K, or split the input.", EXIT_NEEDS_HEAVY)
        job.log(f"input exceeds 128K; escalating to 256K automatically (model restarts; cold prefill at 256K "
                f"runs ~350 tok/s, here ~{need // 350 // 60 + 1} min)")
        req["heavy"] = True
    return body, need


def do_ask(req, job):
    body, need = preflight(req, job, ASK_SYSTEM if not req.get("system") else req["system"])
    want = CFG["heavy_ctx"] if req.get("heavy") else CFG["default_ctx"]
    ctx = ensure_ctx(want, job.log)
    messages = [{"role": "system", "content": req.get("system") or ASK_SYSTEM},
                {"role": "user", "content": body + req["prompt"]}]
    st = Stats()
    job.log(f"asking ({human(ctx)} ctx{', thinking' if req.get('think') else ''}) ...")
    msg, finish, timings, _ = chat(messages, req)
    st.add(timings)
    return {"status": "done", "answer": (msg.get("content") or "").strip(),
            "reasoning": msg.get("reasoning_content") or None,
            "finish_reason": finish, "files_changed": [], "steps": 0, "stats": st.as_dict(ctx)}


def do_run_harness(req, job):
    """`run` through DeepSeek Harness: skills, todo, glob/grep/read/edit/write tools, optional pwsh and web_fetch."""
    py = CFG["harness_python"]
    if not os.path.isfile(py):
        raise BridgeError(f"harness venv missing ({py}); use --legacy or reinstall (see Project_Note.md)")
    body, need = preflight(req, job, "")
    want = CFG["heavy_ctx"] if req.get("heavy") else CFG["default_ctx"]
    ctx = ensure_ctx(want, job.log)
    wreq = {
        "cfg": {k: CFG[k] for k in ("model", "base_url", "max_steps", "request_timeout_s", "harness_keep", "harness_disable")},
        "prompt": body + req["prompt"], "cwd": req["cwd"], "write": req.get("write"), "exec": req.get("exec"),
        "web": req.get("web"), "think": req.get("think"), "ctx": ctx, "session_id": job.id,
        "max_tokens": req.get("max_tokens") or CFG["max_tokens"], "max_steps": req.get("max_steps"),
        "dsh_home": os.path.join(STATE, "dsh-home"), "job_dir": job.dir,
    }
    rpath = os.path.join(job.dir, "harness.request.json")
    write_json(rpath, wreq)
    job.log(f"harness: mode {'exec (UNSANDBOXED shell)' if req.get('exec') else 'write (workspace-confined)' if req.get('write') else 'read-only'}"
            f", ctx {human(ctx)}; the first model call carries ~9k tokens of harness prompt")
    proc = subprocess.Popen([py, "-I", "-X", "utf8", os.path.join(HERE, "harness_worker.py"), rpath],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            errors="replace", creationflags=NO_WINDOW)
    job.set(pid=os.getpid(), worker_pid=proc.pid)
    err_lines = []
    threading.Thread(target=lambda: err_lines.extend(proc.stderr.read().splitlines()[-30:]), daemon=True).start()
    result = None
    for line in proc.stdout:
        line = line.rstrip("\r\n")
        if line.startswith("LOG "):
            job.log(line[4:])
        elif line.startswith("RESULT "):
            result = json.loads(line[7:])
            break
    try:
        proc.wait(timeout=20)
    except subprocess.TimeoutExpired:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, creationflags=NO_WINDOW)
    if result is None:
        raise BridgeError("harness worker produced no result: " + " | ".join(err_lines[-5:])[:600])
    if result.get("status") == "error":
        raise BridgeError(result.get("error", "harness error"))
    return result


def do_run(req, job):
    if CFG.get("engine") == "harness" and not req.get("legacy"):
        return do_run_harness(req, job)
    tools_schema = tool_schemas(req)
    names = {t["function"]["name"] for t in tools_schema}
    system = run_system(req, names)
    body, need = preflight(req, job, system + json.dumps(tools_schema))
    want = CFG["heavy_ctx"] if req.get("heavy") else CFG["default_ctx"]
    ctx = ensure_ctx(want, job.log)
    tools = Tools(req, job)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": body + req["prompt"]}]
    st = Stats()
    max_steps = req.get("max_steps") or CFG["max_steps"]
    answer, status, steps = "", "done", 0
    for steps in range(1, max_steps + 1):
        job.log(f"step {steps}: thinking ...")
        hub_event(kind="job", id=job.id, state="running", step=steps, step_label="thinking")
        msg, finish, timings, usage = chat(messages, req, tools_schema)
        st.add(timings)
        calls = msg.get("tool_calls") or []
        messages.append({"role": "assistant", "content": msg.get("content") or "",
                         **({"tool_calls": calls} if calls else {})})
        if not calls:
            answer = (msg.get("content") or "").strip()
            if finish == "length":
                status = "truncated"
            break
        for c in calls:
            fn = c["function"]["name"]
            raw = c["function"].get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else raw
            except json.JSONDecodeError:
                args = None
            job.log(f"  tool {fn} {json.dumps(args, ensure_ascii=False)[:160] if args is not None else raw[:160]}")
            hub_event(kind="tool", state="start", name=fn, summary=json.dumps(args, ensure_ascii=False)[:100] if args is not None else "")
            res = tools.call(fn, args) if args is not None else "error: arguments were not valid JSON"
            hub_event(kind="tool", state="error" if str(res).startswith("error") else "end", name=fn)
            if str(res).startswith("error") and "outside" in str(res):
                hub_event(kind="guard", guard="sandbox", state="tripped", detail=f"{fn}: {str(res)[:100]}")
            messages.append({"role": "tool", "tool_call_id": c.get("id", ""), "content": res})
        used = (usage or {}).get("prompt_tokens", 0) + (usage or {}).get("completion_tokens", 0)
        if used > ctx - 2 * (req.get("max_tokens") or CFG["max_tokens"]) - 4000:
            if ctx <= CFG["default_ctx"] and not req.get("no_escalate"):
                job.log(f"context nearly full ({used} tokens); escalating to 256K and continuing")
                ctx = ensure_ctx(CFG["heavy_ctx"], job.log)
                req["heavy"] = True
                continue
            status = "context_exhausted"
            answer = (f"[stopped: the conversation reached {used} of {ctx} tokens after {steps} steps; "
                      f"split the task]")
            break
    else:
        status = "max_steps"
        answer = f"[stopped: hit the {max_steps}-step limit without a final answer]"
    return {"status": status, "answer": answer, "reasoning": None, "files_changed": tools.changed,
            "steps": steps, "stats": st.as_dict(ctx)}


def execute_job(jid, echo):
    job = Job(jid, echo)
    req = job.meta["request"]
    job.set(status="running", started=now(), pid=os.getpid())
    result = None
    snippet = (req.get("prompt") or "")[:80].replace(chr(10), " ")
    mode = "exec" if req.get("exec") else "write" if req.get("write") else "read-only"
    engine = "harness" if (req["kind"] == "run" and CFG.get("engine") == "harness" and not req.get("legacy")) else "direct"
    hub_event(kind="job", id=jid, state="queued", jtype=req["kind"], engine=engine, mode=mode, prompt=snippet, step=0,
              max_steps=int(req.get("max_steps") or CFG["max_steps"]) if req["kind"] == "run" else 1, step_label="queued")
    try:
        acquire_lock(f"{req['kind']} {jid}", job.log)
        try:
            job.log(f"started ({req['kind']})")
            hub_event(kind="job", id=jid, state="running", step_label="starting model" if req["kind"] == "run" else "asking")
            result = do_ask(req, job) if req["kind"] == "ask" else do_run(req, job)
            if (server_ctx() or 0) > CFG["default_ctx"]:
                touch_heavy()
        finally:
            release_lock()
        result["ok"] = result["status"] in ("done",)
    except BridgeError as e:
        result = {"ok": False, "status": "error", "error": str(e), "code": e.code}
        job.log(f"error: {e}")
    except Exception as e:  # noqa: BLE001
        result = {"ok": False, "status": "error", "error": f"{type(e).__name__}: {e}", "code": EXIT_ERR}
        job.log(f"error: {type(e).__name__}: {e}")
    result["job"] = jid
    hub_event(kind="job", id=jid, state=result.get("status", "error"))
    write_json(os.path.join(job.dir, "result.json"), result)
    job.set(status=result["status"], ended=now())
    return result


def print_result(res, as_json):
    if as_json:
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif res.get("status") == "error":
        print(f"ERROR: {res.get('error')}", file=sys.stderr)
    else:
        print(res.get("answer", ""))
        if res.get("files_changed"):
            print("\n[files changed]\n" + "\n".join(res["files_changed"]))
    st = res.get("stats")
    if st and not as_json:
        print(f"[local-agent] {res['status']} | {st['seconds']}s | prompt {st['prompt_tokens_processed']} tok"
              f"{' @ ' + str(st['prompt_tok_s']) + '/s' if st['prompt_tok_s'] else ''}"
              f" (+{st['prompt_tokens_cached']} cached) | gen {st['completion_tokens']} tok"
              f"{' @ ' + str(st['gen_tok_s']) + '/s' if st['gen_tok_s'] else ''} | ctx {human(st['ctx'])}"
              f" | steps {res.get('steps', 0)} | job {res['job']}", file=sys.stderr)


def exit_code(res):
    if res.get("status") == "error":
        return res.get("code", EXIT_ERR)
    return EXIT_OK if res.get("ok") else EXIT_ERR


# --------------------------------------------------------------------------- CLI commands
def make_request(a, kind):
    cwd = os.path.abspath(a.cwd or os.getcwd())
    stdin_text = None
    if a.file and "-" in a.file:
        stdin_text = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    prompt = a.prompt
    if prompt == "-":
        prompt = sys.stdin.buffer.read().decode("utf-8", errors="replace")
    return {"kind": kind, "prompt": prompt, "cwd": cwd, "files": expand_files(a.file, cwd), "stdin": stdin_text,
            "system": getattr(a, "system", None), "heavy": a.heavy, "no_escalate": a.no_escalate, "think": a.think, "max_tokens": a.max_tokens,
            "temp": a.temp, "write": getattr(a, "write", False), "exec": getattr(a, "exec", False),
            "web": getattr(a, "web", False), "max_steps": getattr(a, "max_steps", None),
            "legacy": getattr(a, "legacy", False)}


def cmd_job(a, kind):
    prune_jobs()
    req = make_request(a, kind)
    jid = create_job(req)
    if a.detach:
        pid = spawn_detached(f'"{sys.executable}" "{os.path.abspath(__file__)}" _job {jid}')
        print(json.dumps({"job": jid, "status": "running", "pid": pid,
                          "next": f'localagent wait {jid} --timeout 55   |   localagent result {jid}'}))
        return EXIT_OK
    res = execute_job(jid, echo=True)
    print_result(res, a.json)
    return exit_code(res)


def load_result(jid):
    res = read_json(os.path.join(job_dir(jid), "result.json"))
    meta = read_json(os.path.join(job_dir(jid), "job.json"))
    return res, meta


def cmd_wait(a):
    t0 = now()
    while True:
        res, meta = load_result(a.job)
        if meta is None:
            print(f"no such job: {a.job}", file=sys.stderr)
            return EXIT_ERR
        if res is not None:
            print_result(res, a.json)
            return exit_code(res)
        if meta.get("status") == "running" and not pid_alive(meta.get("pid")):
            print("job process died without a result; see log: " + os.path.join(job_dir(a.job), "log.txt"), file=sys.stderr)
            return EXIT_ERR
        if now() - t0 >= a.timeout:
            tail = ""
            try:
                with open(os.path.join(job_dir(a.job), "log.txt"), encoding="utf-8") as f:
                    tail = "".join(f.readlines()[-6:])
            except OSError:
                pass
            print(json.dumps({"job": a.job, "status": meta.get("status"), "log_tail": tail.strip().splitlines()}, indent=2))
            return EXIT_RUNNING
        time.sleep(2)


def cmd_result(a):
    a.timeout = 0
    return cmd_wait(a)


def cmd_jobs(a):
    rows = []
    try:
        ids = sorted(os.listdir(JOBS), reverse=True)[:a.n]
    except OSError:
        ids = []
    for jid in ids:
        meta = read_json(os.path.join(job_dir(jid), "job.json"), {})
        req = meta.get("request", {})
        rows.append(f"{jid}  {meta.get('status','?'):<17} {req.get('kind','?'):<4} {(req.get('prompt') or '')[:70]!r}")
    print("\n".join(rows) or "(no jobs)")
    return EXIT_OK


def cmd_cancel(a):
    meta = read_json(os.path.join(job_dir(a.job), "job.json"))
    if not meta:
        print(f"no such job: {a.job}", file=sys.stderr)
        return EXIT_ERR
    pid = meta.get("pid")
    if pid and pid_alive(pid):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, creationflags=NO_WINDOW)
    li = read_json(LOCK)
    if li and li.get("pid") == pid:
        try:
            os.remove(LOCK)
        except OSError:
            pass
    Job(a.job, False).set(status="cancelled", ended=now())
    write_json(os.path.join(job_dir(a.job), "result.json"), {"ok": False, "status": "cancelled", "job": a.job})
    print(f"cancelled {a.job}")
    return EXIT_OK


def gpu_line():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=10, creationflags=NO_WINDOW).stdout.strip()
        u, t = [x.strip() for x in out.splitlines()[0].split(",")]
        return f"{u}/{t} MiB"
    except Exception:
        return "n/a"


def cmd_status(a):
    g = gate_status() or {}
    st = server_state()
    ctx = g.get("ctx") if g else server_ctx()
    h = read_json(HEAVY)
    li = lock_info()
    info = {
        "server": st,          # ready | asleep (model unloaded, wakes on next request) | loading | down
        "ctx": ctx,
        "mode": None if not ctx else ("heavy-256k" if ctx > CFG["default_ctx"] else "default-128k"),
        "requests_in_flight": g.get("inflight"),
        "unload_in_s": g.get("unload_in_s"),
        "vram": gpu_line(),
        "busy_with": li.get("owner") if li else None,
        "revert_to_128k_in_s": (max(0, int(h["last_used"] + CFG["heavy_idle_minutes"] * 60 - now()))
                                if h and ctx and ctx > CFG["default_ctx"] and not li else None),
    }
    print(json.dumps(info, indent=2))
    return EXIT_OK if st in ("ready", "asleep") else EXIT_ERR


def cmd_heavy(a):
    log = lambda m: print(f"[local-agent] {m}", file=sys.stderr, flush=True)
    if a.action == "status":
        return cmd_status(a)
    acquire_lock(f"heavy {a.action}", log)
    try:
        if a.action == "on":
            ensure_ctx(CFG["heavy_ctx"], log)
            touch_heavy()
            log(f"256K active; reverts to 128K after {CFG['heavy_idle_minutes']} idle minutes")
        else:
            if (server_ctx() or 0) > CFG["default_ctx"]:
                revert_to_default(log)
            else:
                log("already at default context")
    finally:
        release_lock()
    return cmd_status(a)


def cmd_up(a):
    log = lambda m: print(f"[local-agent] {m}", file=sys.stderr, flush=True)
    acquire_lock("up", log)
    try:
        ensure_ctx(CFG["default_ctx"], log)
    finally:
        release_lock()
    return cmd_status(a)


def build_parser():
    p = argparse.ArgumentParser(prog="localagent", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("prompt", help="the question/task; '-' reads it from stdin")
        sp.add_argument("-f", "--file", action="append", default=[], metavar="PATH",
                        help="inline this file/glob as context (repeatable); '-f -' inlines stdin")
        sp.add_argument("--cwd", help="working directory (default: current)")
        sp.add_argument("--heavy", action="store_true", help="force 256K context from the start (otherwise it escalates only when the job needs it)")
        sp.add_argument("--no-escalate", action="store_true", help="never switch to 256K; fail with exit 3 if the job does not fit 128K")
        sp.add_argument("--think", action="store_true", help="enable the model's reasoning mode (slower, better on hard problems)")
        sp.add_argument("--max-tokens", type=int, help="max completion tokens per model call (default 4096)")
        sp.add_argument("--temp", type=float, help="sampling temperature (default 0.3)")
        sp.add_argument("--detach", action="store_true", help="return a job id immediately; use 'wait'/'result' later")
        sp.add_argument("--json", action="store_true", help="print the full result as JSON")

    a = sub.add_parser("ask", help="one-shot question over files/stdin, no tools")
    common(a)
    a.add_argument("--system", help="replace the default system prompt")
    r = sub.add_parser("run", help="worker agent with tools")
    common(r)
    r.add_argument("--write", action="store_true", help="allow write/edit inside --cwd (harness: workspace-write sandbox)")
    r.add_argument("--exec", action="store_true", help="allow a shell (harness: pwsh, UNSANDBOXED, implies full file access)")
    r.add_argument("--web", action="store_true", help="allow web fetch (harness: web_fetch; --legacy: also web_search)")
    r.add_argument("--legacy", action="store_true", help="use the built-in tool loop instead of DeepSeek Harness")
    r.add_argument("--max-steps", type=int, help="tool-loop step limit (default 25)")

    w = sub.add_parser("wait", help="wait for a detached job")
    w.add_argument("job")
    w.add_argument("--timeout", type=int, default=55)
    w.add_argument("--json", action="store_true")
    rs = sub.add_parser("result", help="print a job's result now")
    rs.add_argument("job")
    rs.add_argument("--json", action="store_true")
    j = sub.add_parser("jobs", help="list recent jobs")
    j.add_argument("-n", type=int, default=15)
    c = sub.add_parser("cancel", help="kill a job")
    c.add_argument("job")
    sub.add_parser("status", help="server / context / GPU / queue")
    sub.add_parser("up", help="make sure the model server is running")
    h = sub.add_parser("heavy", help="swap context size now")
    h.add_argument("action", choices=["on", "off", "status"])
    sub.add_parser("_reaper")
    jb = sub.add_parser("_job")
    jb.add_argument("job")
    return p


def main():
    a = build_parser().parse_args()
    try:
        if a.cmd in ("ask", "run"):
            return cmd_job(a, a.cmd)
        if a.cmd == "_job":
            execute_job(a.job, echo=False)
            return EXIT_OK
        if a.cmd == "_reaper":
            reaper()
            return EXIT_OK
        return {"wait": cmd_wait, "result": cmd_result, "jobs": cmd_jobs, "cancel": cmd_cancel,
                "status": cmd_status, "up": cmd_up, "heavy": cmd_heavy}[a.cmd](a)
    except BridgeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return e.code


if __name__ == "__main__":
    sys.exit(main())
