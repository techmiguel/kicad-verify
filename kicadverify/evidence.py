"""Evidence records: files identified by path and SHA-256 at the moment they were used, so a report
or a release manifest says exactly which bytes were verified."""
import hashlib
from pathlib import Path

_CACHE = {}


def sha256(path):
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p.resolve()), st.st_size, st.st_mtime_ns)
    if key not in _CACHE:
        h = hashlib.sha256()
        with open(p, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        _CACHE[key] = h.hexdigest()
    return _CACHE[key]


def rel(path, root):
    try:
        return str(Path(path).resolve().relative_to(Path(root).resolve())).replace("\\", "/")
    except ValueError:
        return str(path).replace("\\", "/")


def artifact(path, root):
    """{"path", "sha256"} for an existing file, None otherwise."""
    p = Path(path)
    if not p.is_file():
        return None
    return {"path": rel(p, root), "sha256": sha256(p)}


def from_result_evidence(items, root):
    """Result.evidence holds file paths and free-text labels; hash the files, keep the labels."""
    out = []
    for e in items or []:
        if isinstance(e, dict):  # a cited quote: {"path", "locator", "quote"}
            a = artifact(e.get("path", ""), root) if e.get("path") else None
            out.append({**e, **(a or {})})
            continue
        a = artifact(e, root) if isinstance(e, (str, Path)) and str(e) and Path(str(e)).is_file() else None
        out.append(a if a else {"note": str(e)})
    return out
