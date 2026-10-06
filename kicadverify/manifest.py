"""Release manifest: the record of what was verified, bound to the exact bytes.

`kicadverify release` writes verification/pcb/release/manifest.json (plus an archived copy per
design) with the SHA-256 of every design file, every fabrication output, the verification config,
the review report and the verification report, and the verdict of every requirement.
`kicadverify audit` recomputes the hashes: any difference means the files about to be sent to the
fab are not the ones that were verified.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

from . import config, evidence
from .checks import fab

SCHEMA = "kicad-verify/release-manifest@1"
CONFIG_FILES = ("project.yaml", "requirements.yaml", "waivers.yaml", "signoff.yaml", "pins.yaml",
                "approved_footprints.txt", "design_intent.md")


def _files(root, proj):
    root = Path(root)
    g, d, b, c = fab.find_outputs(root, proj["params"])
    groups = {
        "design": config.design_files(root),
        "fabrication": sorted(set(g) | set(d) | set(b) | set(c)),
        "config": [proj["dir"] / n for n in CONFIG_FILES if (proj["dir"] / n).exists()],
    }
    return {k: {evidence.rel(f, root): evidence.sha256(f) for f in v if Path(f).is_file()} for k, v in groups.items()}


def build(root, proj, rep, review_report=None, attestation=None):
    root = Path(root)
    reports = {}
    for name, f in (("verification_report", proj["dir"] / "reports" / "verify_report.json"),
                    ("review_report", review_report)):
        if f and Path(f).is_file():
            reports[name] = {"path": evidence.rel(f, root), "sha256": evidence.sha256(f)}
    if attestation and Path(attestation).is_file():
        reports["attestation"] = {"path": evidence.rel(attestation, root), "sha256": evidence.sha256(attestation)}
        sig = Path(str(attestation) + ".sig")
        if sig.is_file():
            reports["attestation_signature"] = {"path": evidence.rel(sig, root), "sha256": evidence.sha256(sig)}
    ver = rep["verification"]
    prov = rep.get("provenance") or {}
    return {
        "schema": SCHEMA, "project": proj["project"]["name"],
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "design_hash": config.design_hash(root), "tools": prov.get("tools") or rep.get("tools"),
        "policy_digest": (prov.get("policy") or {}).get("digest"),
        "intent_digest": (prov.get("intent") or {}).get("digest"),
        "artifacts_digest": (prov.get("artifacts") or {}).get("digest"),
        "gates": {k: v["pass"] for k, v in ver["gates"].items()},
        "summary": ver["summary"],
        "requirements": [{"id": v["id"], "status": v["status"], "method": v["method"], "gate": v["gate"],
                          "source": (v.get("source") or {}).get("ref"),
                          "deviations": [{"key": d["key"], "reason": d["reason"], "by": d.get("by")}
                                         for d in v.get("deviations", [])]}
                         for v in ver["requirements"]],
        "excluded": ver.get("excluded", []),
        "files": _files(root, proj), "reports": reports,
    }


def write(root, proj, man):
    d = proj["dir"] / "release"
    d.mkdir(parents=True, exist_ok=True)
    text = json.dumps(man, indent=2, ensure_ascii=False)
    (d / "manifest.json").write_text(text, encoding="utf-8")
    arch = d / f"{man['time'][:10]}-{man['design_hash'][:12]}"
    arch.mkdir(exist_ok=True)
    (arch / "manifest.json").write_text(text, encoding="utf-8")
    md = proj["dir"] / "reports" / "verification_report.md"
    if md.exists():
        (arch / "verification_report.md").write_text(md.read_text(encoding="utf-8"), encoding="utf-8")
    return d / "manifest.json"


def audit(root, proj, path=None):
    """[(kind, path, detail)] differences between the manifest and the files on disk now."""
    f = Path(path) if path else proj["dir"] / "release" / "manifest.json"
    if not f.exists():
        return None, f"no release manifest at {f}"
    man = json.loads(f.read_text(encoding="utf-8"))
    now = _files(root, proj)
    diffs = []
    if man.get("design_hash") != config.design_hash(root):
        diffs.append(("design", "-", "design hash differs from the released design"))
    for group, files in man.get("files", {}).items():
        cur = now.get(group, {})
        for p, h in files.items():
            if p not in cur:
                diffs.append((group, p, "missing"))
            elif cur[p] != h:
                diffs.append((group, p, "changed"))
        for p in set(cur) - set(files):
            if group in ("design", "fabrication"):
                diffs.append((group, p, "not in the manifest"))
    return diffs, man
