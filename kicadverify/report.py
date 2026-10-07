"""Check results (findings level) and the verdict vocabulary of the requirements level.

Two levels, on purpose:
- A check result (`Result`) is what one verifier observed: PASS / WARN / FAIL / SKIP plus individual
  findings (`violations`), the scope it examined (`coverage`) and the files it read (`evidence`).
- A requirement verdict is derived from one or more check results by `requirements.evaluate` and is
  always one of VERIFIED / FAILED / NOT_VERIFIABLE / NOT_RUN.

A check sets `outcome` explicitly when its status alone would be ambiguous, e.g. a SKIP because an
input is missing (NOT_VERIFIABLE) versus a SKIP because the mode does not run it (NOT_RUN).
"""
import json
import platform
import subprocess
from datetime import datetime, timezone

PASS, WARN, FAIL, SKIP = "PASS", "WARN", "FAIL", "SKIP"
ORDER = {FAIL: 0, WARN: 1, PASS: 2, SKIP: 3}

VERIFIED, FAILED, NOT_VERIFIABLE, NOT_RUN = "VERIFIED", "FAILED", "NOT_VERIFIABLE", "NOT_RUN"
VERDICTS = (VERIFIED, FAILED, NOT_VERIFIABLE, NOT_RUN)
REPORT_SCHEMA = "kicad-verify/verification-report@2"


class Result:
    def __init__(self, check_id, status, detail="", evidence=None, violations=None, coverage=None,
                 outcome=None, waived=None):
        self.check_id = check_id
        self.status = status
        self.detail = detail
        self.evidence = evidence or []
        # individual findings: [{"key", "text"}]; each one can be waived on its own
        self.violations = violations or []
        # scope actually examined: {"scope", "total", "checked", "unchecked": [{"key", "text"}]}
        self.coverage = coverage
        # explicit requirement-level outcome when the status is ambiguous (see module doc)
        self.outcome = outcome
        # findings removed by a waiver, kept for the record: [{"key", "text", "reason", ...}]
        self.waived = waived or []

    def as_dict(self):
        d = {"check": self.check_id, "status": self.status, "detail": self.detail,
             "evidence": [e if isinstance(e, dict) else str(e) for e in self.evidence], "violations": self.violations}
        if self.coverage is not None:
            d["coverage"] = self.coverage
        if self.outcome:
            d["outcome"] = self.outcome
        if self.waived:
            d["waived"] = self.waived
        return d

    @classmethod
    def from_dict(cls, d):
        return cls(d["check"], d["status"], d["detail"], d.get("evidence"), d.get("violations"),
                   d.get("coverage"), d.get("outcome"), d.get("waived"))


def count(n, noun):
    """'1 finding', '3 findings', '2 mismatches'."""
    if n == 1:
        return f"1 {noun}"
    return f"{n} {noun}{'es' if noun.endswith(('ch', 'sh', 's', 'x')) else 's'}"


def coverage(scope, total, unchecked=None, checked=None):
    """Scope record for a check. `unchecked` items are in scope but were not verified (with the
    reason in their text); they carry keys so a person can waive them after checking by hand."""
    unchecked = unchecked or []
    return {"scope": scope, "total": total,
            "checked": total - len(unchecked) if checked is None else checked,
            "unchecked": unchecked}


def not_verifiable(check_id, detail, missing=None):
    """A check that could not run because an input is missing (the requirement cannot be decided)."""
    return Result(check_id, SKIP, detail, outcome=NOT_VERIFIABLE,
                  violations=[{"key": "missing-input", "text": missing}] if missing else None)


def overall(results):
    st = {r.status for r in results}
    return FAIL if FAIL in st else WARN if WARN in st else PASS


def tool_versions(kicad_cli):
    v = {"python": platform.python_version()}
    try:
        v["kicad-cli"] = subprocess.run([kicad_cli, "--version"], capture_output=True, text=True,
                                        timeout=30).stdout.strip() or "unavailable"
    except Exception:
        v["kicad-cli"] = "unavailable"
    try:
        from importlib.metadata import version
        v["kicad-verify"] = version("kicad-verify")
    except Exception:
        from . import __version__
        v["kicad-verify"] = __version__
    return v


def build(project, mode, results, versions, extra=None):
    rep = {"schema": REPORT_SCHEMA, "tool": "kicad-verify", "project": project, "mode": mode,
           "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "overall": overall(results), "tools": versions,
           "results": [r.as_dict() for r in sorted(results, key=lambda r: (ORDER[r.status], r.check_id))]}
    rep.update(extra or {})
    return rep


def write(path, rep):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")


def _cov(c):
    return f"{c['checked']}/{c['total']} {c['scope']}" if c else ""


def text(rep, only_problems=True, limit=8, gate=None):
    """Terminal view: requirement verdicts first, then the findings behind them."""
    ver = rep.get("verification") or {}
    lines = [f"kicad-verify [{rep['mode']}] {rep['project']}  design {str(rep.get('design_hash', ''))[:12]}"]
    if ver:
        s = ver["summary"]
        counts = "  ".join(f"{k} {s['by_status'].get(k, 0)}" for k in VERDICTS)
        lines.append(f"Requirements: {s['total']} applicable  {counts}  "
                     f"(verified {s['verified_pct']}%, excluded {len(ver.get('excluded', []))})")
        for r in ver["requirements"]:
            if only_problems and r["status"] == VERIFIED and not r.get("deviations"):
                continue
            cov = f" [{_cov(r['coverage'])}]" if r.get("coverage") else ""
            dev = f" ({len(r['deviations'])} waived)" if r.get("deviations") else ""
            lines.append(f"  {r['status']:<14} {r['id']:<16} {r['reason']}{cov}{dev}")
    lines.append(f"Findings: {rep['overall']}")
    for r in rep["results"]:
        if only_problems and r["status"] in (PASS, SKIP):
            continue
        lines.append(f"  {r['status']:<4} {r['check']}: {r['detail']}")
        for v in r["violations"][:limit]:
            lines.append(f"       - {v['text']}  [key {v['key']}]")
        if len(r["violations"]) > limit:
            lines.append(f"       ... and {len(r['violations']) - limit} more")
    for name, g in (ver.get("gates") or {}).items():
        if gate and name != gate:
            continue
        verdict = "PASS" if g["pass"] else f"BLOCKED by {len(g['blockers'])}"
        lines.append(f"Gate {name}: {verdict}")
        if gate and not g["pass"]:
            for b in g["blockers"]:
                lines.append(f"  {b['id']:<16} {b['status']:<14} {b['action']}")
    pend = rep.get("human_pending") or []
    if pend:
        lines.append(f"Human sign-off pending: {', '.join(pend)}")
    return "\n".join(lines)
