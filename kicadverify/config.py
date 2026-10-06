import hashlib
import os
import shutil
import sys
from pathlib import Path

import yaml

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
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in SKIP_DIRS and not d.startswith(".")]
        if len(Path(dp).parts) - base >= max_depth:
            dns[:] = []
        for f in fns:
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


def design_hash(root):
    """Content hash of the KiCad design files only (not outputs, not verification config)."""
    h = hashlib.sha1()
    for f in sorted(walk(root)):
        if f.suffix.lower() in (".kicad_pcb", ".kicad_sch"):
            h.update(str(f.relative_to(root)).replace("\\", "/").encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def load_project(root):
    root = Path(root)
    vd = root / DIRNAME
    proj = load_yaml(vd / "project.yaml", {}) or {}
    proj.setdefault("name", root.name)
    proj.setdefault("status", "dev")
    proj.setdefault("reviewer_model", "opus")
    base = load_yaml(DATA / "requirements_base.yaml", {}) or {}
    own = load_yaml(vd / "requirements.yaml", {}) or {}
    params = _merge(base.get("params", {}), own.get("params") or {})
    reqs = {r["id"]: r for r in base.get("requirements", [])}
    for r in own.get("requirements") or []:
        reqs[r["id"]] = {**reqs.get(r["id"], {}), **r}
    for rid in own.get("disable") or []:
        reqs.pop(rid, None)
    return {"root": root, "dir": vd, "project": proj, "params": params,
            "requirements": list(reqs.values()), "disabled": set(own.get("disable") or []),
            "waivers": (load_yaml(vd / "waivers.yaml", {}) or {}).get("waivers", []),
            "pins": load_yaml(vd / "pins.yaml", {}) or {}}


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def init_project(root):
    root = Path(root)
    vd = root / DIRNAME
    vd.mkdir(parents=True, exist_ok=True)
    created = []
    for t in (DATA / "templates").iterdir():
        dst = vd / (".gitignore" if t.name == "gitignore" else t.name)
        if not dst.exists():
            shutil.copy(t, dst)
            created.append(dst)
    pj = vd / "project.yaml"
    pj.write_text(pj.read_text(encoding="utf-8").replace("{{name}}", root.name), encoding="utf-8")
    return created
