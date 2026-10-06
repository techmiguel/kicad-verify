"""Human sign-off bound to the design hash: any change to the KiCad design files invalidates it.

A sign-off records who checked what, when, for which exact design, and the outcome: `pass` (the
requirement is VERIFIED) or `fail` (FAILED: the person found a defect). A sign-off for another
design hash does not count; the requirement shows NOT_RUN with the stale entry as context.
"""
from datetime import date

import yaml

from . import config
from .report import FAIL, NOT_RUN, PASS, SKIP, Result


def design_hash(root):
    return config.design_hash(root)


def load(root):
    data = config.load_yaml(root / config.DIRNAME / "signoff.yaml", {}) or {}
    return data.get("signoffs") or []


def add(root, rid, by, note="", result="pass"):
    f = root / config.DIRNAME / "signoff.yaml"
    entries = [e for e in load(root) if e.get("id") != rid]
    entries.append({"id": rid, "by": by, "date": date.today().isoformat(), "design_hash": design_hash(root),
                    "result": result, "note": note})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Written by `kicadverify signoff`. Valid only for the recorded design hash.\n"
                 + yaml.safe_dump({"signoffs": entries}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def pending(root, requirements):
    h = design_hash(root)
    valid = {e["id"] for e in load(root) if e.get("design_hash") == h and e.get("result", "pass") == "pass"}
    return [r["id"] for r in requirements if r.get("method") == "human" and r["id"] not in valid]


def results(root, requirements, h=None):
    """One Result per human requirement that has a sign-off entry (current or stale)."""
    h = h or design_hash(root)
    f = root / config.DIRNAME / "signoff.yaml"
    entries = {e.get("id"): e for e in load(root)}
    out = []
    for r in requirements:
        if r.get("method") != "human" or r["id"] not in entries:
            continue
        e = entries[r["id"]]
        who = f"{e.get('by')} on {e.get('date')}"
        note = f" ({e['note']})" if e.get("note") else ""
        if e.get("design_hash") != h:
            out.append(Result(r["id"], SKIP, f"sign-off by {who}{note} is for design {str(e.get('design_hash'))[:12]}, "
                              f"not the current {h[:12]}", outcome=NOT_RUN, evidence=[f]))
        elif str(e.get("result", "pass")).lower() == "fail":
            out.append(Result(r["id"], FAIL, f"checked by {who} and found NOT compliant{note}", evidence=[f],
                              violations=[{"key": f"signoff-{r['id']}", "text": e.get("note") or "failed sign-off"}]))
        else:
            out.append(Result(r["id"], PASS, f"signed by {who} for design {h[:12]}{note}", evidence=[f]))
    return out
