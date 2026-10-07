import hashlib
import os
import shutil
import sys
from pathlib import Path

import yaml

from . import __version__

PKG = Path(__file__).resolve().parent
DATA = PKG / "data"
DIRNAME = os.path.join("verification", "pcb")


def _default_kicad_cli():
    env = os.environ.get("KICAD_CLI")
    if env:
        return env
    found = shutil.which("kicad-cli")
    if found:
        return found
    if sys.platform == "win32":
        for v in ("10.0", "9.0", "8.0"):
            p = Path(rf"C:\Program Files\KiCad\{v}\bin\kicad-cli.exe")
            if p.exists():
                return str(p)
    if sys.platform == "darwin":
        p = Path("/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli")
        if p.exists():
            return str(p)
    return "kicad-cli"


KICAD_CLI = _default_kicad_cli()


def kicad_cli_found():
    return bool(shutil.which(KICAD_CLI)) or Path(KICAD_CLI).is_file()
SKIP_DIRS = {".history", "verification", "backups", ".git", "node_modules", ".venv", "venv",
             "__pycache__", "hwverify"}
KICAD_SUFFIXES = {".kicad_pro", ".kicad_pcb", ".kicad_sch"}
OUTPUT_SUFFIXES = {".csv", ".drl", ".gbr", ".gbrjob"}


def load_yaml(path, default=None):
    try:
        data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
        return default if data is None else data
    except FileNotFoundError:
        return default


def walk(root, max_depth=4):
    root = Path(root)
    base = len(root.parts)
    for dp, dns, fns in os.walk(root):  # sorted: the order must not depend on the file system
        dns[:] = sorted(d for d in dns if d not in SKIP_DIRS and not d.startswith("."))
        if len(Path(dp).parts) - base >= max_depth:
            dns[:] = []
        for f in sorted(fns):
            yield Path(dp) / f


def find_root(start):
    """Walks up from `start`. Returns (root, initialised) or (None, False)."""
    p = Path(start).resolve()
    chain = [p, *p.parents][:6]
    for d in chain:
        if (d / DIRNAME / "project.yaml").exists():
            return d, True
    for d in chain:
        if any(d.glob("*.kicad_pro")) or any((d / "hardware").glob("*.kicad_pro")):
            return d, False
    return None, False


def discover(root):
    out = []
    for f in walk(root):
        if f.suffix.lower() == ".kicad_pro":
            out.append({"pro": f, "pcb": f.with_suffix(".kicad_pcb"), "sch": f.with_suffix(".kicad_sch"),
                        "label": f.stem})
    return sorted(out, key=lambda k: str(k["pro"]))


def artifact_hash(root, include_outputs=True):
    h = hashlib.sha1()
    suffixes = KICAD_SUFFIXES | (OUTPUT_SUFFIXES if include_outputs else set())
    for f in sorted(walk(root)):
        s = f.suffix.lower()
        if s in suffixes or (s.startswith(".g") and len(s) == 4):
            try:
                st = f.stat()
                h.update(f"{f}|{st.st_size}|{int(st.st_mtime)}".encode())
            except OSError:
                pass
    vd = Path(root) / DIRNAME
    for name in ("requirements.yaml", "waivers.yaml", "pins.yaml", "approved_footprints.txt"):
        f = vd / name
        if f.exists():
            h.update(f.read_bytes())
    return h.hexdigest()


DESIGN_SUFFIXES = (".kicad_pcb", ".kicad_sch", ".kicad_pro", ".kicad_dru")


def design_files(root):
    """KiCad files that define the design. .kicad_pro and .kicad_dru hold the design rules and net
    classes the DRC is judged against, so they are part of the design."""
    return [f for f in sorted(walk(root)) if f.suffix.lower() in DESIGN_SUFFIXES]


def design_hash(root):
    """SHA-256 over the relative path and content of every design file (not outputs, not config)."""
    h = hashlib.sha256()
    for f in design_files(root):
        h.update(str(f.relative_to(root)).replace("\\", "/").encode())
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()


def load_project(root):
    from . import requirements as rq
    root = Path(root)
    vd = root / DIRNAME
    proj = load_yaml(vd / "project.yaml", {}) or {}
    proj.setdefault("name", root.name)
    proj.setdefault("status", "dev")
    proj.setdefault("reviewer_model", "opus")
    base = load_yaml(DATA / "requirements_base.yaml", {}) or {}
    own = load_yaml(vd / "requirements.yaml", {}) or {}
    params = _merge(base.get("params", {}), own.get("params") or {})
    raw = {r["id"]: (dict(r), "base") for r in base.get("requirements", [])}
    for r in own.get("requirements") or []:
        prev = raw.get(r["id"])
        raw[r["id"]] = ({**prev[0], **r}, "base+project") if prev else (dict(r), "project")
    reqs = {rid: rq.normalize(r, origin) for rid, (r, origin) in raw.items()}
    excluded = []
    for d in own.get("disable") or []:
        e = {"id": d, "reason": None} if isinstance(d, str) else {"id": d.get("id"), "reason": d.get("reason"),
                                                                    "by": d.get("by"), "date": d.get("date")}
        excluded.append(e)
        reqs.pop(e["id"], None)
    if (params.get("fab") or {}).get("assembly", True) is False:
        for rid in ("FAB-BOM-001", "FAB-CPL-001", "HUM-CPL-001"):
            if reqs.pop(rid, None):
                excluded.append({"id": rid, "reason": "params.fab.assembly is false (bare boards, no assembly)"})
    for e in excluded:
        e["text"] = next((r.get("text") for r in base.get("requirements", []) if r["id"] == e["id"]), None)
    return {"root": root, "dir": vd, "project": proj, "params": params,
            "requirements": list(reqs.values()), "excluded": excluded,
            "disabled": {e["id"] for e in excluded},
            "waivers": (load_yaml(vd / "waivers.yaml", {}) or {}).get("waivers", []),
            "pins": load_yaml(vd / "pins.yaml", {}) or {}}


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _write_detected_outputs(root, req_file):
    """Pre-fills params.fab with fabrication outputs found outside the project folder."""
    from .checks import fab
    found = fab.detect_outside(root)
    if not found:
        return
    lines = "".join(f"    {k}: {v}\n" for k, v in found.items())
    text = req_file.read_text(encoding="utf-8")
    marker = "    profile: null"
    i = text.find(marker)
    if i < 0:
        return
    j = text.index("\n", i) + 1
    req_file.write_text(text[:j] + "    # found by `kicadverify init` outside the project folder: check them\n" + lines
                        + text[j:], encoding="utf-8")


def init_project(root, ci=None):
    root = Path(root)
    vd = root / DIRNAME
    vd.mkdir(parents=True, exist_ok=True)
    created = []
    for t in (DATA / "templates").iterdir():
        dst = vd / (".gitignore" if t.name == "gitignore" else t.name)
        if not dst.exists():
            shutil.copy(t, dst)
            created.append(dst)
            if dst.name == "project.yaml":
                dst.write_text(dst.read_text(encoding="utf-8").replace("{{name}}", root.name), encoding="utf-8")
            if dst.name == "requirements.yaml":
                _write_detected_outputs(root, dst)
    if ci == "github":
        repo = next((d for d in [root, *root.parents] if (d / ".git").exists()), root)
        wf = repo / ".github" / "workflows" / "hw-verify.yml"
        if not wf.exists():
            wf.parent.mkdir(parents=True, exist_ok=True)
            rel = os.path.relpath(root, repo).replace("\\", "/")
            wf.write_text((DATA / "ci" / "github-workflow.yml").read_text(encoding="utf-8").replace("{{path}}", rel)
                          .replace("{{version}}", __version__),
                          encoding="utf-8")
            created.append(wf)
    return created
