"""Waivers silence ONE finding (check + key) and must carry a reason, a date and may expire."""
import hashlib
from datetime import date

from .report import FAIL, PASS, WARN, Result


def vkey(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def apply(results, waivers):
    today = date.today().isoformat()
    notes = []
    for w in waivers or []:
        if not (w.get("check") and w.get("key") and w.get("reason") and w.get("date")):
            notes.append(f"incomplete waiver ignored: {w}")
            continue
        exp = str(w.get("expires") or "")
        if exp and exp < today:
            notes.append(f"expired waiver ({exp}) ignored: {w['check']} {w['key']}")
            continue
        for r in results:
            if r.check_id != w["check"]:
                continue
            keep = [v for v in r.violations if v["key"] != w["key"]]
            if len(keep) != len(r.violations):
                r.violations = keep
                if not keep and r.status in (FAIL, WARN):
                    r.status = PASS
                    r.detail += f" (waived: {w['reason']})"
    if notes:
        results.append(Result("WAIVERS", WARN, "; ".join(notes)))
    return results
