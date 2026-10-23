"""Fail-safe for the exec guard: exec runs in mode "full" only because the exec-guard plugin is the gate.
If the running gateway did NOT load the plugin, fall back to native mode "ask" (every command asks the owner).

Run after the gateway starts (Start-LocalAI.ps1 does). Exit 0 = guard loaded or exec already strict, 1 = fell back to "ask".
usage: python guard_check.py [--wait SECONDS]
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import time

HOME = os.path.expanduser("~")
CONF = os.path.join(HOME, ".openclaw", "openclaw.json")
APPROVALS = os.path.join(HOME, ".openclaw", "exec-approvals.json")
GW_LOG = os.path.join(HOME, ".openclaw", "logs", "gateway-stdout.log")


def exec_mode():
    try:
        return json.load(open(CONF, encoding="utf-8")).get("tools", {}).get("exec", {}).get("mode")
    except Exception:
        return None


def guard_loaded():
    """True when the most recent 'http server listening' line of the gateway log lists exec-guard."""
    try:
        text = open(GW_LOG, encoding="utf-8", errors="replace").read()
    except OSError:
        return False
    lines = re.findall(r"http server listening \((\d+) plugins: ([^;)]*)", text)
    return bool(lines) and "exec-guard" in [p.strip() for p in lines[-1][1].split(",")]


def fall_back():
    patch = os.path.join(tempfile.gettempdir(), "exec-guard-fallback.json5")
    open(patch, "w").write('{ tools: { exec: { mode: "ask" } } }')
    subprocess.run(["openclaw.cmd", "config", "patch", "--file", patch], capture_output=True, timeout=120)
    try:
        c = json.load(open(APPROVALS, encoding="utf-8"))
        c["defaults"].update({"security": "allowlist", "ask": "on-miss", "askFallback": "deny"})
        json.dump(c, open(APPROVALS, "w", encoding="utf-8"), indent=2)
    except Exception as e:  # noqa: BLE001
        print("could not tighten host approvals:", e)


def main():
    wait = int(sys.argv[sys.argv.index("--wait") + 1]) if "--wait" in sys.argv else 0
    if exec_mode() != "full":
        print(f"exec mode is {exec_mode()!r}: native approvals already apply, nothing to check")
        return 0
    deadline = time.time() + wait
    while True:
        if guard_loaded():
            print("exec-guard loaded")
            return 0
        if time.time() >= deadline:
            break
        time.sleep(3)
    print("exec-guard NOT loaded: falling back to exec mode 'ask'")
    fall_back()
    return 1


if __name__ == "__main__":
    sys.exit(main())
