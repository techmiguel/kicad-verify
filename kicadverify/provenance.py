"""Provenance: the exact policy, tools, artifacts and design intent behind a verification.

Every verification report carries a `provenance` block, so a verdict can always be traced to:
- policy:    a digest of the effective verification policy (normalised requirements, parameters,
             gate policy, waivers, exclusions, fab profile, verifier registry, reviewer prompt),
             with one digest per component and the SHA-256 of every policy source file;
- tools:     kicad-verify (version, digest of its own code and data, git commit when run from a
             checkout), kicad-cli, the kicad-happy engine (pinned tag and installed commit),
             Python and platform;
- artifacts: every design file, fabrication output, datasheet and config file, with SHA-256;
- intent:    the design intent the board was judged against (fields declared, fields missing, the
             context files given to the reviewer), with its digest.

Digests are SHA-256 over canonical JSON (sorted keys, no whitespace), so two runs with the same
policy, tools and inputs produce the same digests.
"""
import hashlib
import json
import os
import platform
import re
import subprocess
from functools import lru_cache
from pathlib import Path

from . import __version__, config, evidence

PKG = Path(__file__).resolve().parent
INTENT_FILE = "design_intent.md"
INTENT_FIELD = re.compile(r"^\s*[-*]\s*([^:\n]{3,80}):[ \t]*(.*)$")


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(obj):
    return "sha256:" + hashlib.sha256(canonical(obj).encode("utf-8")).hexdigest()


def _files(paths, root):
    out = []
    for p in paths:
        a = evidence.artifact(p, root)
        if a:
            out.append(a)
    return sorted(out, key=lambda a: a["path"])


# ------------------------------------------------------------------ policy
POLICY_REQ_KEYS = ("id", "text", "method", "verified_by", "acceptance", "gate", "check", "source")


def policy(proj):
    """The effective verification policy and its digest."""
    from . import requirements as rq
    from .checks import dfm
    params = proj["params"]
    prof, _ = dfm.resolve_profile(params)
    reqs = sorted(({k: r.get(k) for k in POLICY_REQ_KEYS} for r in proj["requirements"]), key=lambda r: r["id"])
    components = {
        "requirements": reqs,
        "params": {k: v for k, v in params.items() if k != "review"} | {
            "review": {k: v for k, v in (params.get("review") or {}).items() if k != "timeout_s"}},
        "gates": {**rq.DEFAULT_POLICY, **(params.get("gates") or {})},
        "waivers": sorted(proj["waivers"], key=lambda w: (str(w.get("check")), str(w.get("key")))),
        "excluded": sorted(proj["excluded"], key=lambda e: str(e["id"])),
        "fab_profile": prof,
        "verifier_registry": rq.registry(),
        "reviewer_prompt": (config.DATA / "reviewer_prompt.md").read_text(encoding="utf-8"),
        "pins": proj["pins"],
    }
    files = _files([config.DATA / "requirements_base.yaml", config.DATA / "checks.yaml",
                    config.DATA / "fab_profiles.yaml", config.DATA / "reviewer_prompt.md"]
                   + [proj["dir"] / n for n in ("requirements.yaml", "waivers.yaml", "pins.yaml",
                                                "approved_footprints.txt")], proj["root"])
    comp_digests = {k: digest(v) for k, v in components.items()}
    return {"digest": digest(comp_digests), "components": comp_digests, "files": files,
            "requirements": len(reqs), "waivers": len(proj["waivers"]), "excluded": len(proj["excluded"]),
            "fab_profile": (prof or {}).get("name")}


# ------------------------------------------------------------------ tools
@lru_cache(maxsize=1)
def code_digest():
    """Digest of kicad-verify's own Python code and data: identifies the exact build that ran."""
    h = hashlib.sha256()
    for f in sorted(PKG.rglob("*")):
        if f.is_file() and f.suffix in (".py", ".yaml", ".yml", ".md", ".txt") and "__pycache__" not in f.parts:
            h.update(str(f.relative_to(PKG)).replace("\\", "/").encode())
            h.update(b"\0")
            h.update(f.read_bytes())
    return "sha256:" + h.hexdigest()


def _git(cwd, *args):
    try:
        p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=20)
        return p.stdout.strip() if p.returncode == 0 else None
    except Exception:
        return None


@lru_cache(maxsize=1)
def _self_git():
    top = _git(PKG, "rev-parse", "--show-toplevel")
    if not top or not (Path(top) / "pyproject.toml").exists() or Path(top) != PKG.parent:
        return None
    return {"commit": _git(PKG, "rev-parse", "HEAD"),
            "dirty": bool(_git(PKG, "status", "--porcelain", "--", str(PKG)))}


@lru_cache(maxsize=4)
def _kicad_cli_version(exe):
    try:
        return subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=120).stdout.strip() or None
    except Exception:
        return None


def tools(params, kicad_happy_used=None):
    from .checks import happy
    kh_dir = happy.locate(params)
    kh_root = kh_dir.parents[2] if kh_dir else None
    out = {
        "kicad-verify": {"version": __version__, "code_digest": code_digest(), "git": _self_git()},
        "kicad-cli": {"version": _kicad_cli_version(config.KICAD_CLI), "path": config.KICAD_CLI},
        "kicad-happy": {"pinned": happy.PINNED, "installed": str(kh_root) if kh_root else None,
                        "commit": _git(kh_root, "rev-parse", "HEAD") if kh_root else None,
                        "ran": kicad_happy_used},
        "python": platform.python_version(), "platform": platform.platform(),
    }
    env = {k: os.environ[k] for k in ("KICAD_IMAGE", "KICAD_CLI", "GITHUB_SHA", "GITHUB_RUN_ID", "CI")
           if os.environ.get(k)}
    if env:
        out["environment"] = env
    return out


# ------------------------------------------------------------------ design intent
def intent(root, proj):
    """The design intent the board is judged against: declared fields, empty fields, context files."""
    f = proj["dir"] / INTENT_FILE
    declared, missing = {}, []
    if f.exists():
        for line in f.read_text(encoding="utf-8").splitlines():
            m = INTENT_FIELD.match(line)
            if m:
                key, val = m.group(1).strip(), m.group(2).strip()
                (declared.__setitem__(key, val) if val else missing.append(key))
    pats = (proj["params"].get("review") or {}).get("context_files") or []
    ctx = []
    for pat in pats:
        ctx += [p for p in Path(root).glob(pat) if p.is_file()]
    context = _files(list(dict.fromkeys(ctx)), root)
    body = {"file": evidence.artifact(f, root), "declared": declared, "missing": missing,
            "context_files": context}
    return {**body, "digest": digest(body), "present": f.exists(),
            "complete": f.exists() and not missing and bool(declared)}


# ------------------------------------------------------------------ artifacts
def artifacts(root, proj):
    """Every input with its SHA-256, grouped: design, fabrication, datasheets, config."""
    from .checks import fab
    root = Path(root)
    g, d, b, c = fab.find_outputs(root, proj["params"])
    ds = []
    for sub in (proj["params"].get("review") or {}).get("datasheet_dirs") or []:
        p = root / sub
        if p.is_dir():
            ds += list(p.rglob("*.pdf"))
    groups = {
        "design": config.design_files(root),
        "fabrication": sorted(set(g) | set(d) | set(b) | set(c)),
        "datasheets": sorted(set(ds)),
        # project.yaml is left out: `release` writes the status into it after verifying
        "config": [proj["dir"] / n for n in ("requirements.yaml", "waivers.yaml", "signoff.yaml",
                                              "pins.yaml", "approved_footprints.txt", INTENT_FILE)],
    }
    out = {k: _files(v, root) for k, v in groups.items()}
    return {"groups": out, "digest": digest(out)}


def collect(root, proj, kicad_happy_used=None):
    pol, intn, art = policy(proj), intent(root, proj), artifacts(root, proj)
    return {"policy": pol, "tools": tools(proj["params"], kicad_happy_used), "intent": intn, "artifacts": art,
            "design_hash": config.design_hash(root)}
