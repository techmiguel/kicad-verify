"""Installs kicad-verify hooks and skills into ~/.claude (merging, never overwriting other hooks)."""
import json
import shutil
import sys
from pathlib import Path

HOME = Path.home() / ".claude"
SKILLS = Path(__file__).resolve().parent / "data" / "claude" / "skills"
MARK = "kicadverify.hooks"


def _cmd(event):
    return f'"{sys.executable}" -m {MARK} {event}'


def install(hooks=True):
    HOME.mkdir(exist_ok=True)
    for s in SKILLS.iterdir():
        dst = HOME / "skills" / s.name
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copy(s / "SKILL.md", dst / "SKILL.md")
    print(f"Skills installed: {', '.join(p.name for p in SKILLS.iterdir())}")
    if not hooks:
        return 0
    f = HOME / "settings.json"
    cfg = json.loads(f.read_text(encoding="utf-8")) if f.exists() else {}
    bak = HOME / "settings.json.kicad-verify.bak"
    if f.exists() and not bak.exists():
        shutil.copy(f, bak)
    h = cfg.setdefault("hooks", {})
    wanted = {
        "SessionStart": ("startup|resume", "session_start", 60),
        "PostToolUse": ("mcp__.*(kicad|konnect).*|Write|Edit", "post_tool", 120),
        "Stop": (None, "stop", 2400),
    }
    for event, (matcher, name, timeout) in wanted.items():
        groups = [g for g in h.get(event, []) if not any(MARK in x.get("command", "") for x in g.get("hooks", []))]
        g = {"hooks": [{"type": "command", "command": _cmd(name), "timeout": timeout}]}
        if matcher:
            g["matcher"] = matcher
        groups.append(g)
        h[event] = groups
    f.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Hooks installed in {f} (first backup: {bak.name})")
    return 0


def uninstall():
    f = HOME / "settings.json"
    if f.exists():
        cfg = json.loads(f.read_text(encoding="utf-8"))
        for event, groups in list(cfg.get("hooks", {}).items()):
            groups[:] = [g for g in groups if not any(MARK in x.get("command", "") for x in g.get("hooks", []))]
            if not groups:
                del cfg["hooks"][event]
        f.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    for s in SKILLS.iterdir():
        shutil.rmtree(HOME / "skills" / s.name, ignore_errors=True)
    print("kicad-verify hooks and skills removed.")
    return 0
