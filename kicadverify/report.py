import json
import platform
import subprocess
from datetime import datetime, timezone

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
ORDER = {FAIL: 0, WARN: 1, PASS: 2, SKIP: 3}


class Result:
    def __init__(self, check_id, status, detail="", evidence=None, violations=None):
        self.check_id = check_id
        self.status = status
        self.detail = detail
        self.evidence = evidence or []
        # individual findings: [{"key", "text"}]; each one can be waived on its own
        self.violations = violations or []

    def as_dict(self):
        return {"check": self.check_id, "status": self.status, "detail": self.detail,
                "evidence": [str(e) for e in self.evidence], "violations": self.violations}

    @classmethod
    def from_dict(cls, d):
        return cls(d["check"], d["status"], d["detail"], d.get("evidence"), d.get("violations"))


def overall(results):
    st = {r.status for r in results}
    return FAIL if FAIL in st else WARN if WARN in st else PASS


def tool_versions(kicad_cli):
    v = {"python": platform.python_version()}
    try:
        v["kicad-cli"] = subprocess.run([kicad_cli, "--version"], capture_output=True, text=True,
                                        timeout=30).stdout.strip()
    except Exception:
        v["kicad-cli"] = "unavailable"
    try:
        from importlib.metadata import version
        v["kicad-verify"] = version("kicad-verify")
    except Exception:
        v["kicad-verify"] = "dev"
    return v


def build(project, mode, results, versions, extra=None):
    rep = {"tool": "kicad-verify", "project": project, "mode": mode,
           "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "overall": overall(results), "tools": versions,
           "results": [r.as_dict() for r in sorted(results, key=lambda r: ORDER[r.status])]}
    rep.update(extra or {})
    return rep


def write(path, rep):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")


def text(rep, only_problems=True, limit=8):
    lines = [f"kicad-verify [{rep['mode']}] {rep['project']}: {rep['overall']}"]
    for r in rep["results"]:
        if only_problems and r["status"] in (PASS, SKIP):
            continue
        lines.append(f"  {r['status']:<4} {r['check']}: {r['detail']}")
        for v in r["violations"][:limit]:
            lines.append(f"       - {v['text']}  [key {v['key']}]")
        if len(r["violations"]) > limit:
            lines.append(f"       ... and {len(r['violations']) - limit} more")
    pend = rep.get("human_pending") or []
    if pend:
        lines.append(f"  Human sign-off pending: {', '.join(pend)}")
    return "\n".join(lines)
