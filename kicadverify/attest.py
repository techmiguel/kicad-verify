"""Verification attestations: a self-contained, optionally signed statement of exactly what was
verified, what could not be, under which policy, tools and design intent, over which artifacts.

Format: an in-toto Statement v1 (https://in-toto.io/Statement/v1). `subject` lists every artifact
(design files, fabrication outputs, datasheets, config) with its SHA-256; `predicate` holds the
policy, tools, intent, gates and one entry per requirement (status, reason, source, verifiers,
coverage and its gaps, limits, evidence digests, deviations). Excluded requirements are listed with
their reasons, so nothing is silently out of scope.

Signing uses `ssh-keygen -Y sign` (the mechanism git uses for SSH-signed commits) with namespace
`kicad-verify`; verification uses an allowed-signers file. `check` re-verifies the signature and
recomputes every digest from the files on disk, so a reader can tell which parts of the claim still
hold for the files in front of them.
"""
import json
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config, evidence, provenance

STATEMENT_TYPE = "https://in-toto.io/Statement/v1"
PREDICATE_TYPE = "https://github.com/techmiguel/kicad-verify/verification/v1"
NAMESPACE = "kicad-verify"


def _req_entry(v):
    cov = v.get("coverage")
    return {
        "id": v["id"], "text": v.get("text"), "status": v["status"], "severity": v.get("severity"),
        "reason": v["reason"], "method": v["method"], "gate": v["gate"], "acceptance": v["acceptance"],
        "source": v.get("source"), "verified_by": v["verified_by"], "limits": v.get("limits"),
        "coverage": None if not cov else {"scope": cov["scope"], "total": cov["total"], "checked": cov["checked"],
                                          "unchecked": cov.get("unchecked", [])},
        "evidence": [{"check": e["check"], "outcome": e.get("outcome"), "status": e["status"],
                      "artifacts": [{k: a[k] for k in ("path", "sha256", "locator", "quote") if a.get(k)}
                                    for a in e["artifacts"] if a.get("sha256")]}
                     for e in v.get("evidence", [])],
        "findings": v.get("findings", []),
        "deviations": v.get("deviations", []),
    }


def statement(rep):
    """In-toto statement from a verification report (which must carry `provenance`)."""
    prov = rep["provenance"]
    ver = rep["verification"]
    subject = [{"name": a["path"], "digest": {"sha256": a["sha256"]}, "annotations": {"group": g}}
               for g, items in prov["artifacts"]["groups"].items() for a in items]
    by = {}
    for v in ver["requirements"]:
        by.setdefault(v["status"], []).append(v["id"])
    predicate = {
        "project": rep["project"], "mode": rep["mode"], "verified_at": rep["time"],
        "attested_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "design_hash": rep["design_hash"],
        "policy": prov["policy"], "tools": prov["tools"], "intent": prov["intent"],
        "artifacts_digest": prov["artifacts"]["digest"],
        "gates": {k: {"pass": g["pass"], "blocks_on": g.get("blocks_on"), "blockers": [b["id"] for b in g["blockers"]]}
                  for k, g in ver["gates"].items()},
        "summary": {**ver["summary"], "by_requirement": {k: sorted(v) for k, v in by.items()}},
        "requirements": [_req_entry(v) for v in ver["requirements"]],
        "excluded": ver.get("excluded", []),
        "orphan_checks": ver.get("orphan_checks", []),
        "lint": ver.get("lint", []),
        "review": rep.get("review_provenance"),
    }
    return {"_type": STATEMENT_TYPE, "subject": subject, "predicateType": PREDICATE_TYPE, "predicate": predicate}


def write(stmt, out_dir):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    p = stmt["predicate"]
    f = out_dir / f"{p['attested_at'][:19].replace(':', '')}-{p['design_hash'][:12]}.intoto.json"
    f.write_text(json.dumps(stmt, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    return f


def sign(path, key):
    """Detached SSH signature <path>.sig. Returns the signature path."""
    if not shutil.which("ssh-keygen"):
        raise RuntimeError("ssh-keygen not found (OpenSSH 8.1+ is needed to sign)")
    sig = Path(str(path) + ".sig")
    sig.unlink(missing_ok=True)
    p = subprocess.run(["ssh-keygen", "-Y", "sign", "-q", "-f", str(key), "-n", NAMESPACE, str(path)],
                       capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
    if p.returncode != 0 or not sig.exists():
        raise RuntimeError(f"ssh-keygen could not sign: {p.stderr.strip()[:300]}")
    return sig


def verify_signature(path, allowed_signers, identity=None):
    """(ok, signer or error). Without an identity, the signer is looked up from the allowed signers."""
    sig = Path(str(path) + ".sig")
    if not sig.exists():
        return False, "no signature file"
    if not shutil.which("ssh-keygen"):
        return False, "ssh-keygen not found"
    data = Path(path).read_bytes()
    if identity is None:
        p = subprocess.run(["ssh-keygen", "-Y", "find-principals", "-f", str(allowed_signers), "-s", str(sig)],
                           capture_output=True, text=True, timeout=60)
        if p.returncode != 0 or not p.stdout.strip():
            return False, "the signing key is not in the allowed signers"
        identity = p.stdout.split()[0]
    p = subprocess.run(["ssh-keygen", "-Y", "verify", "-f", str(allowed_signers), "-I", identity, "-n", NAMESPACE,
                        "-s", str(sig)], input=data, capture_output=True, timeout=60)
    return (True, identity) if p.returncode == 0 else (False, p.stderr.decode(errors="replace").strip()[:300])


def check(path, root, allowed_signers=None, identity=None):
    """Re-checks an attestation against the files on disk. Returns a dict of findings:
    signature, subject (per artifact: unchanged / changed / missing), new artifacts, design hash,
    policy, intent and tools digests compared with what they are now."""
    stmt = json.loads(Path(path).read_text(encoding="utf-8"))
    if stmt.get("_type") != STATEMENT_TYPE or stmt.get("predicateType") != PREDICATE_TYPE:
        raise ValueError("not a kicad-verify attestation")
    root = Path(root)
    pred = stmt["predicate"]
    out = {"signature": None, "subject": {"unchanged": [], "changed": [], "missing": []}, "new": [], "same": {}}
    if allowed_signers:
        ok, who = verify_signature(path, allowed_signers, identity)
        out["signature"] = {"valid": ok, "signer" if ok else "error": who}
    elif Path(str(path) + ".sig").exists():
        out["signature"] = {"valid": None, "error": "signature present but no allowed-signers file given"}
    for s in stmt["subject"]:
        f = root / s["name"]
        if not f.is_file():
            out["subject"]["missing"].append(s["name"])
        elif evidence.sha256(f) != s["digest"]["sha256"]:
            out["subject"]["changed"].append(s["name"])
        else:
            out["subject"]["unchanged"].append(s["name"])
    proj = config.load_project(root)
    now = provenance.collect(root, proj)
    known = {s["name"] for s in stmt["subject"]}
    out["new"] = sorted(a["path"] for g in ("design", "fabrication") for a in now["artifacts"]["groups"][g]
                        if a["path"] not in known)
    out["same"] = {
        "design_hash": now["design_hash"] == pred["design_hash"],
        "policy": now["policy"]["digest"] == pred["policy"]["digest"],
        "intent": now["intent"]["digest"] == pred["intent"]["digest"],
        "kicad-verify code": (now["tools"]["kicad-verify"]["code_digest"]
                              == pred["tools"]["kicad-verify"]["code_digest"]),
        "kicad-cli": now["tools"]["kicad-cli"]["version"] == pred["tools"]["kicad-cli"]["version"],
    }
    out["values"] = {
        "kicad-cli": (pred["tools"]["kicad-cli"]["version"], now["tools"]["kicad-cli"]["version"]),
        "kicad-verify code": (pred["tools"]["kicad-verify"]["code_digest"][:23],
                              now["tools"]["kicad-verify"]["code_digest"][:23]),
        "design_hash": (pred["design_hash"][:12], now["design_hash"][:12]),
        "policy": (pred["policy"]["digest"][:23], now["policy"]["digest"][:23]),
        "intent": (pred["intent"]["digest"][:23], now["intent"]["digest"][:23]),
    }
    if not out["same"]["policy"]:
        out["policy_components_changed"] = sorted(k for k, v in now["policy"]["components"].items()
                                                  if pred["policy"]["components"].get(k) != v)
    out["holds"] = (out["signature"] is None or out["signature"]["valid"] is True) and \
        not out["subject"]["changed"] and not out["subject"]["missing"] and not out["new"] and \
        out["same"]["design_hash"] and out["same"]["policy"] and out["same"]["intent"]
    out["predicate"] = pred
    return out
