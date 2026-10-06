"""Waivers accept ONE finding or ONE coverage gap (check + key). Each carries a reason and a date,
may name who approved it and may expire. Waived items are not deleted: they move to `Result.waived`
and appear as deviations next to the requirement verdict. A waiver that matches nothing is reported
as stale, so accepted deviations cannot outlive the finding they were written for unnoticed."""
import hashlib
from datetime import date

from .report import FAIL, PASS, WARN, Result


def vkey(*parts):
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:10]


def _record(item, w, kind):
    return {"key": item["key"], "text": item["text"], "kind": kind, "reason": w["reason"], "date": str(w["date"]),
            "by": w.get("by") or w.get("approved_by"), "expires": str(w.get("expires") or "") or None}


def apply(results, waivers, today=None):
    today = today or date.today().isoformat()
    notes, stale = [], []
    for w in waivers or []:
        if not (w.get("check") and w.get("key") and w.get("reason") and w.get("date")):
            notes.append(f"incomplete waiver ignored (needs check, key, reason, date): {w}")
            continue
        exp = str(w.get("expires") or "")
        if exp and exp < today:
            notes.append(f"expired waiver ({exp}) ignored: {w['check']} {w['key']}")
            continue
        hit = False
        for r in results:
            if r.check_id != w["check"]:
                continue
            keep = [v for v in r.violations if v["key"] != w["key"]]
            if len(keep) != len(r.violations):
                hit = True
                r.waived += [_record(v, w, "finding") for v in r.violations if v["key"] == w["key"]]
                r.violations = keep
                if not keep and r.status in (FAIL, WARN):
                    r.status = PASS
                    r.detail += f" (all {len(r.waived)} findings waived)"
            cov = r.coverage
            if cov and cov.get("unchecked"):
                gap = [u for u in cov["unchecked"] if u["key"] == w["key"]]
                if gap:
                    hit = True
                    r.waived += [_record(u, w, "coverage") for u in gap]
                    cov["unchecked"] = [u for u in cov["unchecked"] if u["key"] != w["key"]]
                    cov["checked"] += len(gap)
        if not hit and any(r.check_id == w["check"] for r in results):  # checks that did not run are not judged
            stale.append(f"{w['check']} {w['key']}")
    if notes:
        results.append(Result("WAIVERS", WARN, "; ".join(notes)))
    if stale:
        results.append(Result("WAIVERS", WARN, f"{len(stale)} waivers match no current finding (stale): "
                              + ", ".join(stale[:10]),
                              violations=[{"key": f"stale-{i}", "text": s} for i, s in enumerate(stale)]))
    return results
