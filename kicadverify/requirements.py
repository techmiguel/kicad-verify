"""Requirements model: every requirement has a source, a verification method, the verifiers that
produce its evidence, an acceptance criterion and the gate from which it must be VERIFIED.

    VERIFIED        every verifier ran, found nothing outside the acceptance criterion and covered
                    its whole scope (or each unchecked item was waived by a person, with a reason)
    FAILED          a verifier found a defect that is not waived
    NOT_VERIFIABLE  a verifier ran but could not decide: missing input (datasheet, pins.yaml, fab
                    profile, outputs), partial scope, or a reviewer claim whose evidence did not hold
    NOT_RUN         no verifier produced a result for the current design (fast mode, review not run,
                    no valid sign-off, verifier missing)

Precedence when a requirement has several verifier results: FAILED > NOT_VERIFIABLE > NOT_RUN >
VERIFIED. A requirement is never VERIFIED by default.
"""
import fnmatch
import hashlib
import json
from functools import lru_cache

from . import config, evidence
from .report import (FAIL, FAILED, NOT_RUN, NOT_VERIFIABLE, PASS, SKIP, VERDICTS, VERIFIED, WARN)

METHODS = {"auto": "auto", "analysis": "auto", "check": "auto",
           "model": "model", "review": "model",
           "human": "human", "inspection": "human", "test": "human", "demonstration": "human"}
METHOD_LABEL = {"auto": "analysis (deterministic check)", "model": "review (independent model, evidence re-checked)",
                "human": "inspection/test (human sign-off)"}
GATES = ("dev", "fab", "release")
GATE_RANK = {"dev": 0, "fab": 1, "release": 2, "none": 99}
DEFAULT_GATE = {"auto": "fab", "model": "release", "human": "release"}
ACCEPTANCE = ("no_fail", "no_findings")
SOURCE_KINDS = ("standard", "regulatory", "datasheet", "fab_capability", "design_intent", "customer",
                "lesson_learned", "best_practice", "tool")
DEFAULT_POLICY = {"dev": [FAILED], "fab": [FAILED, NOT_VERIFIABLE, NOT_RUN],
                  "release": [FAILED, NOT_VERIFIABLE, NOT_RUN]}
PRECEDENCE = {FAILED: 0, NOT_VERIFIABLE: 1, NOT_RUN: 2, VERIFIED: 3}
ORPHANS = "@orphans"
# internal results that are not verifiers of their own (their effect shows up in the checks they feed)
INTERNAL = {"DISCOVER", "WAIVERS"}


@lru_cache(maxsize=1)
def registry():
    return (config.load_yaml(config.DATA / "checks.yaml", {}) or {}).get("checks", {})


def registry_entry(check_id):
    reg = registry()
    if check_id in reg:
        return reg[check_id]
    for k, v in reg.items():
        if k.endswith("*") and check_id.startswith(k[:-1]):
            return v
    return None


# ------------------------------------------------------------------ normalisation
def normalize(r, origin="project"):
    """Fills the defaults of one requirement dict (the input is not modified)."""
    r = dict(r)
    method = METHODS.get(str(r.get("method", "auto")).lower())
    r["method_raw"] = r.get("method", "auto")
    r["method"] = method or "invalid"
    if r.get("check") and method == "auto":
        r.setdefault("verified_by", [r["id"]])
        r["verifier"] = "ASSERT"
    elif method == "model":
        r["verified_by"] = [r["id"]]
        r["verifier"] = "REVIEW"
    elif method == "human":
        r["verified_by"] = [r["id"]]
        r["verifier"] = "SIGNOFF"
    else:
        vb = r.get("verified_by") or [r["id"]]
        r["verified_by"] = [vb] if isinstance(vb, str) else list(vb)
        r["verifier"] = None
    r["acceptance"] = r.get("acceptance") or "no_fail"
    r["gate"] = str(r.get("gate") or DEFAULT_GATE.get(method, "fab"))
    src = r.get("source")
    r["source"] = {"kind": "unspecified", "ref": src} if isinstance(src, str) else (dict(src) if src else None)
    r["origin"] = origin
    return r


def matches(pattern, check_id):
    if pattern == check_id:
        return True
    return "*" in pattern and fnmatch.fnmatchcase(check_id, pattern)


def requirements_hash(reqs):
    """Identity of the requirement set given to the reviewer: a review is reused only while it holds."""
    key = [(r["id"], r.get("text", "")) for r in sorted(reqs, key=lambda r: r["id"]) if r["method"] == "model"]
    return hashlib.sha256(json.dumps(key, ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------ lint
def lint(reqs, excluded=(), assertion_types=()):
    """Configuration problems: errors make the requirement set unusable, warnings weaken it."""
    out = []
    seen = set()

    def add(level, rid, text):
        out.append({"level": level, "id": rid, "text": text})

    for r in reqs:
        rid = r.get("id") or "?"
        if rid in seen:
            add("error", rid, "duplicate requirement id")
        seen.add(rid)
        if not r.get("text"):
            add("error", rid, "requirement has no text")
        if r["method"] == "invalid":
            add("error", rid, f"unknown method {r.get('method_raw')!r} (auto | model | human)")
        if r["acceptance"] not in ACCEPTANCE:
            add("error", rid, f"unknown acceptance {r['acceptance']!r} ({' | '.join(ACCEPTANCE)})")
        if r["gate"] not in GATE_RANK:
            add("error", rid, f"unknown gate {r['gate']!r} (dev | fab | release | none)")
        if r["method"] == "auto" and not r.get("check"):
            for p in r["verified_by"]:
                if p != ORPHANS and not registry_entry(p) and not any(matches(p, k) for k in registry()):
                    add("error", rid, f"verifier {p} is not a known check (see `kicadverify checks`)")
        if r.get("check"):
            t = (r["check"] or {}).get("type")
            if assertion_types and t not in assertion_types:
                add("error", rid, f"unknown assertion type {t!r} ({', '.join(assertion_types)})")
        src = r.get("source")
        if not src or not src.get("ref"):
            add("warning", rid, "no source: where does this requirement come from? (source: {kind, ref})")
        else:
            if src.get("kind") not in SOURCE_KINDS:
                add("warning", rid, f"source kind {src.get('kind')!r} not in {', '.join(SOURCE_KINDS)}")
            if src.get("confirmed") is False:
                add("warning", rid, f"source not confirmed: {src.get('ref')} ({src.get('note', 'confirm it')})")
    for e in excluded:
        if not e.get("reason"):
            add("warning", e["id"], "excluded without a reason (disable: [{id, reason}])")
    return out


# ------------------------------------------------------------------ evaluation
def _outcome(res, acceptance):
    """Requirement-level outcome of one check result, before coverage."""
    if res.outcome:
        return res.outcome, res.detail
    if res.status == PASS:
        return VERIFIED, res.detail
    if res.status == FAIL:
        return FAILED, res.detail
    if res.status == SKIP:
        return NOT_VERIFIABLE, res.detail
    # WARN: findings below the gating severity
    if acceptance == "no_findings":
        return FAILED, res.detail
    return VERIFIED, res.detail + " (warnings accepted by the requirement's criterion)"


def _merge_cov(covs):
    covs = [c for c in covs if c]
    if not covs:
        return None
    scopes = list(dict.fromkeys(c["scope"] for c in covs))
    return {"scope": ", ".join(scopes), "total": sum(c["total"] for c in covs),
            "checked": sum(c["checked"] for c in covs),
            "unchecked": [u for c in covs for u in c.get("unchecked", [])]}


def _not_run_reason(req, mode):
    if req["method"] == "model":
        return "independent review not run for this design and requirement set (`kicadverify review`)"
    if req["method"] == "human":
        return f"no sign-off for the current design (`kicadverify signoff {req['id']} --by <name>`)"
    entry = registry_entry(req["verified_by"][0]) if req["verified_by"] else None
    if entry and mode not in entry.get("modes", []) and mode in ("fast", "full"):
        return f"verifier runs only in {'/'.join(entry.get('modes', []))} mode"
    return "no verifier produced a result (no KiCad design, or the verifier is not available)"


def evaluate(reqs, results, root, mode="full", limit=25):
    """One verdict per requirement, with its evidence, coverage, deviations and known limits."""
    out = []
    used = set()
    for req in reqs:
        if req["verified_by"] == [ORPHANS]:
            continue
        matched = [r for r in results if any(matches(p, r.check_id) for p in req["verified_by"])]
        used |= {id(r) for r in matched}
        out.append(_verdict(req, matched, root, mode, limit))
    orphan = [r for r in results if id(r) not in used and r.check_id not in INTERNAL
              and not (registry_entry(r.check_id) or {}).get("informational")]
    for req in reqs:
        if req["verified_by"] == [ORPHANS]:
            out.append(_orphan_verdict(req, orphan, root, limit))
    return out, orphan


def _verdict(req, matched, root, mode, limit):
    base = {"id": req["id"], "text": req.get("text", ""), "method": req["method"], "source": req["source"],
            "verified_by": req["verified_by"], "acceptance": req["acceptance"], "gate": req["gate"]}
    entry = registry_entry(req.get("verifier") or (req["verified_by"][0] if req["verified_by"] else ""))
    base["limits"] = (entry or {}).get("limits")
    if not matched:
        return {**base, "status": NOT_RUN, "severity": None, "reason": _not_run_reason(req, mode), "coverage": None,
                "evidence": [], "findings": [], "deviations": []}
    parts = []
    for res in matched:
        st, why = _outcome(res, req["acceptance"])
        cov = res.coverage
        if st == VERIFIED and cov and cov.get("unchecked"):
            st = NOT_VERIFIABLE
            why = (f"partial coverage: {cov['checked']}/{cov['total']} {cov['scope']} verified, "
                   f"{len(cov['unchecked'])} not checkable")
        parts.append((st, why, res))
    status = min((p[0] for p in parts), key=PRECEDENCE.get)
    reasons = list(dict.fromkeys(why for st, why, _ in parts if st == status))
    findings = [{"check": res.check_id, **v} for st, _, res in parts if st in (FAILED, NOT_VERIFIABLE)
                for v in res.violations][:limit]
    cov = _merge_cov([res.coverage for res in matched])
    if cov:
        cov["unchecked"] = cov["unchecked"][:limit]
    ev = [{"check": res.check_id, "status": res.status, "outcome": st, "detail": res.detail,
           "artifacts": evidence.from_result_evidence(res.evidence, root)} for st, _, res in parts]
    deviations = [{"check": res.check_id, **w} for res in matched for w in res.waived]
    # error: a verifier reported a defect at gating severity; warning: FAILED only through the
    # requirement's no_findings criterion applied to lower-severity findings
    severity = None
    if status == FAILED:
        severity = "error" if any(st == FAILED and res.status == FAIL for st, _, res in parts) else "warning"
    return {**base, "status": status, "severity": severity, "reason": "; ".join(reasons)[:600], "coverage": cov,
            "evidence": ev, "findings": findings, "deviations": deviations}


def _orphan_verdict(req, orphan, root, limit):
    base = {"id": req["id"], "text": req.get("text", ""), "method": req["method"], "source": req["source"],
            "verified_by": req["verified_by"], "acceptance": req["acceptance"], "gate": req["gate"],
            "limits": "Only findings reported by the verifiers that ran", "coverage": None, "deviations": []}
    bad = [r for r in orphan if r.status == FAIL or (req["acceptance"] == "no_findings" and r.status == WARN)]
    findings = [{"check": r.check_id, "key": v["key"], "text": v["text"]} for r in bad for v in r.violations]
    findings += [{"check": r.check_id, "key": "-", "text": r.detail} for r in bad if not r.violations]
    ev = [{"check": r.check_id, "status": r.status, "outcome": None, "detail": r.detail,
           "artifacts": evidence.from_result_evidence(r.evidence, root)} for r in orphan]
    if bad:
        return {**base, "status": FAILED, "severity": "error" if any(r.status == FAIL for r in bad) else "warning",
                "evidence": ev, "findings": findings[:limit],
                "reason": f"{len(bad)} verifier results outside every requirement report defects: "
                          + ", ".join(sorted({r.check_id for r in bad}))}
    return {**base, "status": VERIFIED, "severity": None, "evidence": ev, "findings": [],
            "reason": f"{len(orphan)} untraced verifier results, none failing"}


# ------------------------------------------------------------------ gates and summary
def _action(v):
    st = v["status"]
    if st == FAILED:
        return "fix the findings, or waive each one in waivers.yaml with a reason"
    if st == NOT_RUN:
        if v["method"] == "model":
            return "run `kicadverify review`"
        if v["method"] == "human":
            return f"a person checks it and runs `kicadverify signoff {v['id']} --by <name>`"
        return "run the full gate (`kicadverify verify`)"
    if v.get("coverage") and v["coverage"].get("unchecked"):
        return "check the unchecked items by hand and waive each one, or extend the inputs"
    return "provide the missing input named in the reason, or exclude the requirement with a reason"


def gates(verdicts, policy=None, allow=None):
    """{gate: {"pass", "blockers": [{id, status, reason, action}]}}.

    A FAILED requirement whose verifier reported an error-severity defect blocks every gate: a
    confirmed defect is never deferred. Any other blocking status (including FAILED reached only
    through a no_findings criterion on warnings) blocks only from the requirement's own gate up.
    Requirements with gate `none` are advisory and never block."""
    policy = {**DEFAULT_POLICY, **(policy or {})}
    allow = allow or {}
    out = {}
    for g in GATES:
        block = set(policy.get(g) or [])
        blockers = []
        for v in verdicts:
            if v["gate"] == "none":
                continue
            applies = GATE_RANK.get(v["gate"], 1) <= GATE_RANK[g]
            hard = v["status"] == FAILED and v.get("severity") == "error"
            if hard or (applies and v["status"] in block
                        and v["id"] not in (allow.get(g, {}).get(v["status"]) or ())):
                blockers.append({"id": v["id"], "status": v["status"], "reason": v["reason"], "action": _action(v)})
        out[g] = {"pass": not blockers, "blockers": blockers, "blocks_on": sorted(block)}
    return out


def summary(verdicts, excluded):
    by_status = {s: 0 for s in VERDICTS}
    by_method = {}
    for v in verdicts:
        by_status[v["status"]] += 1
        m = by_method.setdefault(v["method"], {s: 0 for s in VERDICTS})
        m[v["status"]] += 1
    total = len(verdicts)
    items = [v["coverage"] for v in verdicts if v.get("coverage")]
    return {"total": total, "by_status": by_status, "by_method": by_method,
            "verified_pct": round(100 * by_status[VERIFIED] / total, 1) if total else 0.0,
            "with_source": sum(1 for v in verdicts if (v.get("source") or {}).get("ref")),
            "unconfirmed_sources": sorted(v["id"] for v in verdicts
                                          if (v.get("source") or {}).get("confirmed") is False),
            "scope_items": {"total": sum(c["total"] for c in items), "checked": sum(c["checked"] for c in items)},
            "deviations": sum(len(v.get("deviations") or []) for v in verdicts),
            "excluded": len(excluded)}


def verification(reqs, excluded, results, root, mode, params, lint_items=()):
    verdicts, orphan = evaluate(reqs, results, root, mode)
    allow = {}
    rv = (params.get("review") or {}).get("unverifiable_at_release")
    if rv == "warn":  # legacy switch: reviewer NOT_VERIFIABLE does not block release
        allow["release"] = {NOT_VERIFIABLE: [r["id"] for r in reqs if r["method"] == "model"]}
    g = gates(verdicts, params.get("gates"), allow)
    return {"requirements": verdicts, "summary": summary(verdicts, excluded), "gates": g,
            "excluded": list(excluded), "orphan_checks": sorted({r.check_id for r in orphan}),
            "lint": list(lint_items)}
