"""Claude Code hooks: `python -m kicadverify.hooks session_start|post_tool|stop`.

- session_start: in a KiCad project, creates verification/pcb/ the first time and tells Claude.
- post_tool: after a KiCad MCP tool or a Write/Edit of a KiCad file, runs the fast checks; FAILs go
  back to Claude (exit 2).
- stop: when the design changed, runs the full gate. FAIL blocks the stop (exit 2) up to
  MAX_RETRIES times for the same design state, then lets it stop and reports. In `release`
  status, a changed design also re-runs the independent reviewer.
Every hook exits silently outside KiCad projects and inside reviewer sessions.
"""
import json
import os
import sys

from . import config, report, signoff
from .review import GUARD_ENV

MAX_RETRIES = 3
KICAD_EXT = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru")


def _payload():
    try:
        return json.loads(sys.stdin.read() or "{}")
    except Exception:
        return {}


def _state(root):
    try:
        return json.loads((root / config.DIRNAME / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(root, st):
    try:
        (root / config.DIRNAME / "state.json").write_text(json.dumps(st, indent=2), encoding="utf-8")
    except OSError:
        pass


def session_start(p):
    root, init = config.find_root(p.get("cwd") or os.getcwd())
    if root is None:
        return 0
    if not init:
        config.init_project(root)
        msg = (f"kicad-verify: KiCad project detected at {root}; created verification/pcb/. Checks now run "
               "automatically after design changes. Fill pins.yaml and requirements.yaml from the datasheets.")
    else:
        msg = ("kicad-verify active: fast checks after each KiCad change, full gate when you finish. "
               "On demand: `/pcb-verify`, `/pcb-review`.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": msg}}))
    return 0


def _relevant(p):
    name = p.get("tool_name", "")
    if name.startswith("mcp__"):
        return any(k in name.lower() for k in ("kicad", "konnect"))
    return str((p.get("tool_input") or {}).get("file_path", "")).lower().endswith(KICAD_EXT)


def post_tool(p):
    if not _relevant(p):
        return 0
    root, init = config.find_root(p.get("cwd") or os.getcwd())
    if root is None or not init:
        return 0
    h = config.artifact_hash(root)
    st = _state(root)
    if st.get("fast_hash") == h:
        return 0
    from .cli import analyse
    rep, _ = analyse(root, "fast")
    st["fast_hash"] = h
    _save(root, st)
    if rep["overall"] == report.FAIL:
        print(report.text(rep), file=sys.stderr)
        return 2
    return 0


def stop(p):
    root, init = config.find_root(p.get("cwd") or os.getcwd())
    if root is None or not init:
        return 0
    h = config.artifact_hash(root)
    st = _state(root)
    if st.get("verified_hash") == h or st.get("blocked_hash") == h:
        return 0
    from .cli import analyse, do_review
    rep, ctx = analyse(root, "full")
    text = report.text(rep)
    failed = rep["overall"] == report.FAIL
    proj = ctx["proj"]
    review_note = ""
    dh = config.design_hash(root)
    if not failed and proj["project"].get("status") == "release" and proj["project"].get("release_design_hash") != dh \
            and st.get("reviewed_design") != dh:
        results, rr = do_review(root, release=True, rep=rep, ctx=ctx)
        st["reviewed_design"] = dh
        bad = [r for r in results if r.status == report.FAIL]
        if bad:
            failed = True
            text += "\nIndependent review FAIL:\n" + "\n".join(f"  {r.check_id}: {r.detail}" for r in bad)
        review_note = ("\nDesign changed after release: run `kicadverify release` again "
                       f"(human sign-off pending: {', '.join(signoff.pending(root, proj['requirements']))}).")
    if not failed:
        st.update(verified_hash=h, retries=0, blocked_hash=None)
        _save(root, st)
        if rep["overall"] == report.WARN or review_note:
            print(json.dumps({"systemMessage": text + review_note}))
        return 0
    retries = int(st.get("retries", 0)) + 1
    if retries > MAX_RETRIES:
        st.update(retries=0, blocked_hash=h)
        _save(root, st)
        print(json.dumps({"systemMessage": f"kicad-verify: FAIL persists after {MAX_RETRIES} attempts; "
                                           f"stopping the loop.\n{text}"}))
        return 0
    st["retries"] = retries
    _save(root, st)
    print(f"[attempt {retries}/{MAX_RETRIES}] Fix the FAIL items before finishing, or add a justified "
          f"waiver to verification/pcb/waivers.yaml:\n{text}", file=sys.stderr)
    return 2


def main():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8")
        except Exception:
            pass
    if os.environ.get(GUARD_ENV):
        return 0
    event = sys.argv[1] if len(sys.argv) > 1 else ""
    p = _payload()
    fn = {"session_start": session_start, "post_tool": post_tool, "stop": stop}.get(event)
    if fn is None:
        return 0
    try:
        return fn(p)
    except Exception as e:  # a crashing hook must never wedge the session
        print(json.dumps({"systemMessage": f"kicad-verify hook {event} error: {e}"}))
        return 0


if __name__ == "__main__":
    sys.exit(main())
