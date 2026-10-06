"""Human sign-off bound to the design hash AND the verification policy: any change to the KiCad
design files, or to the policy the requirement was signed under (requirements, thresholds, waivers,
exclusions, fab profile...), invalidates it.

A sign-off records who checked what, when, for which exact design, and the outcome: `pass` (the
requirement is VERIFIED) or `fail` (FAILED: the person found a defect). A sign-off for another
design hash or another policy digest does not count; the requirement shows NOT_RUN with the stale
entry as context, naming the policy components that changed. Entries without a policy digest
(written before 0.3) do not count either.
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


def _policy(root, policy):
    if policy is None:
        from . import provenance
        policy = provenance.policy(config.load_project(root))
    return policy


def add(root, rid, by, note="", result="pass", policy=None):
    policy = _policy(root, policy)
    f = root / config.DIRNAME / "signoff.yaml"
    entries = [e for e in load(root) if e.get("id") != rid]
    entries.append({"id": rid, "by": by, "date": date.today().isoformat(), "design_hash": design_hash(root),
                    "policy_digest": policy["digest"], "policy_components": policy["components"],
                    "result": result, "note": note})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Written by `kicadverify signoff`. Valid only for the recorded design hash and policy digest.\n"
                 + yaml.safe_dump({"signoffs": entries}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def _stale(e, h, policy):
    """Why a sign-off entry does not apply to the current design and policy (None when it does)."""
    if e.get("design_hash") != h:
        return f"is for design {str(e.get('design_hash'))[:12]}, not the current {h[:12]}"
    if not e.get("policy_digest"):
        return "has no policy binding (written before kicad-verify 0.3); sign again"
    if e["policy_digest"] != policy["digest"]:
        old = e.get("policy_components") or {}
        changed = sorted(k for k, v in policy["components"].items() if old.get(k) != v)
        return (f"was signed under policy {e['policy_digest'][7:19]}, the current one is {policy['digest'][7:19]}"
                + (f" (changed: {', '.join(changed)})" if changed else ""))
    return None


def pending(root, requirements, policy=None):
    policy = _policy(root, policy)
    h = design_hash(root)
    valid = {e["id"] for e in load(root) if not _stale(e, h, policy) and e.get("result", "pass") == "pass"}
    return [r["id"] for r in requirements if r.get("method") == "human" and r["id"] not in valid]


def results(root, requirements, h=None, policy=None):
    """One Result per human requirement that has a sign-off entry (current or stale)."""
    h = h or design_hash(root)
    policy = _policy(root, policy)
    f = root / config.DIRNAME / "signoff.yaml"
    entries = {e.get("id"): e for e in load(root)}
    out = []
    for r in requirements:
        if r.get("method") != "human" or r["id"] not in entries:
            continue
        e = entries[r["id"]]
        who = f"{e.get('by')} on {e.get('date')}"
        note = f" ({e['note']})" if e.get("note") else ""
        why = _stale(e, h, policy)
        if why:
            out.append(Result(r["id"], SKIP, f"sign-off by {who}{note} {why}", outcome=NOT_RUN, evidence=[f]))
        elif str(e.get("result", "pass")).lower() == "fail":
            out.append(Result(r["id"], FAIL, f"checked by {who} and found NOT compliant{note}", evidence=[f],
                              violations=[{"key": f"signoff-{r['id']}", "text": e.get("note") or "failed sign-off"}]))
        else:
            out.append(Result(r["id"], PASS, f"signed by {who} for design {h[:12]} under policy "
                              f"{policy['digest'][7:19]}{note}", evidence=[f]))
    return out
