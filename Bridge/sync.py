#!/usr/bin/env python3
"""Install the Local Agent Bridge for Claude Code and Antigravity.

  1. ~/.local-agent/la.py            shim that locates localagent.py on whatever drive this folder is on now
  2. ~/.claude/skills/local-agent/   SKILL.md for Claude Code
  3. ~/.gemini/config/skills/local-agent/  SKILL.md for Antigravity (agy)

Re-run after moving this folder or editing SKILL.md. Nothing is deleted.
"""
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
HOME = os.path.expanduser("~")

SHIM = r'''"""Finds the Local Agent Bridge on any drive (letters reshuffle on this PC) and runs it."""
import os, runpy, sys

candidates = [r"__HERE__"]
script = os.path.join(candidates[0], "localagent.py")
if not os.path.isfile(script):
    candidates = [f"{d}:\\Local LLM\\Bridge" for d in "EDFGHCIJKLMNOPQRSTUVWXYZ"]

for base in candidates:
    script = os.path.join(base, "localagent.py")
    if os.path.isfile(script):
        sys.argv[0] = script
        runpy.run_path(script, run_name="__main__")
        break
else:
    sys.exit("Local Agent Bridge not found; run sync.py from its folder again")
'''


def main():
    state = os.path.join(HOME, ".local-agent")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "la.py"), "w", encoding="utf-8") as f:
        f.write(SHIM.replace("__HERE__", HERE))
    print("shim   ->", os.path.join(state, "la.py"))

    src = os.path.join(HERE, "SKILL.md")
    for target in (os.path.join(HOME, ".claude", "skills", "local-agent"),
                   os.path.join(HOME, ".gemini", "config", "skills", "local-agent")):
        os.makedirs(target, exist_ok=True)
        shutil.copy2(src, os.path.join(target, "SKILL.md"))
        print("skill  ->", os.path.join(target, "SKILL.md"))

    if not os.environ.get("LOCALAI_CONFIG"):
        repo = os.path.dirname(HERE)
        cfg = os.path.join(repo, "config.env")
        print(f'reminder: setx LOCALAI_CONFIG "{cfg}"')


if __name__ == "__main__":
    main()
