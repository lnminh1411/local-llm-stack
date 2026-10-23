"""Inspect the exec guard's saved "always allow" commands and its audit log.

usage:
  python manage.py list                 saved commands
  python manage.py remove "<command>"   forget one saved command (exact text, as shown by list)
  python manage.py log [N]              last N audit lines (default 20)
"""
import json
import os
import re
import sys

D = os.path.join(os.path.expanduser("~"), ".openclaw", "exec-guard")
APPROVED = os.path.join(D, "approved.json")
AUDIT = os.path.join(D, "audit.jsonl")


def norm(c):
    return re.sub(r"[ \t]+", " ", c.replace("\r\n", "\n").replace("\r", "\n")).strip()


def load():
    try:
        return json.load(open(APPROVED, encoding="utf-8"))
    except OSError:
        return {}


def main(argv):
    cmd = argv[1] if len(argv) > 1 else "list"
    if cmd == "list":
        for k, v in load().items():
            print(f"{v.get('at', '?')}  {k}")
        return 0
    if cmd == "remove" and len(argv) > 2:
        d = load()
        if d.pop(norm(argv[2]), None) is None:
            print("not found")
            return 1
        json.dump(d, open(APPROVED, "w", encoding="utf-8"), indent=2)
        print("removed")
        return 0
    if cmd == "log":
        n = int(argv[2]) if len(argv) > 2 else 20
        try:
            lines = open(AUDIT, encoding="utf-8").read().splitlines()[-n:]
        except OSError:
            lines = []
        for ln in lines:
            e = json.loads(ln)
            print(f"{e['ts']}  {e.get('tier', '?'):<12} {e.get('decision', '?'):<16} {e.get('command', '')[:100]!r}")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
