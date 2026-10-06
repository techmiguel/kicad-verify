"""Human sign-off bound to the design hash: any change to the KiCad files invalidates it."""
from datetime import date

import yaml

from . import config


def design_hash(root):
    return config.design_hash(root)


def load(root):
    data = config.load_yaml(root / config.DIRNAME / "signoff.yaml", {}) or {}
    return data.get("signoffs") or []


def add(root, rid, by, note=""):
    f = root / config.DIRNAME / "signoff.yaml"
    entries = [e for e in load(root) if e.get("id") != rid]
    entries.append({"id": rid, "by": by, "date": date.today().isoformat(), "design_hash": design_hash(root),
                    "note": note})
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text("# Written by `kicadverify signoff`. Valid only for the recorded design hash.\n"
                 + yaml.safe_dump({"signoffs": entries}, sort_keys=False, allow_unicode=True), encoding="utf-8")


def pending(root, requirements):
    h = design_hash(root)
    valid = {e["id"] for e in load(root) if e.get("design_hash") == h}
    return [r["id"] for r in requirements if r.get("method") == "human" and r["id"] not in valid]
