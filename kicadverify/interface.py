"""Board interface for mechanical tools (cad-verify): a STEP of the current board plus a JSON with
outline, thickness, mounting holes and component positions, all in the STEP coordinate system.

With `--user-origin OXxOY`, kicad-cli writes x_step = x - OX and y_step = OY - y.
"""
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import config
from .checks import board as board_mod

SCHEMA = "kicad-verify/board-interface@1"


def _origin(spec):
    m = re.match(r"\s*([-\d.]+)\s*x\s*([-\d.]+)\s*(mm)?\s*$", str(spec or "0x0mm"))
    return (float(m.group(1)), float(m.group(2))) if m else (0.0, 0.0)


def design_hash(pcb):
    import hashlib
    return hashlib.sha1(Path(pcb).read_bytes()).hexdigest()


def export(kicad, params, out_dir, force=False):
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    pcb = kicad["pcb"]
    spec = (params.get("interface") or {}).get("step_user_origin", "0x0mm")
    jf = out_dir / f"{kicad['label']}.board_interface.json"
    step = out_dir / f"{kicad['label']}.step"
    h = design_hash(pcb)
    if not force and jf.exists() and step.exists():
        try:
            old = json.loads(jf.read_text(encoding="utf-8"))
            if old.get("pcb_sha1") == h and old.get("step_user_origin") == spec:
                return jf
        except Exception:
            pass
    subprocess.run([config.KICAD_CLI, "pcb", "export", "step", "--subst-models", "--force",
                    "--user-origin", spec, "-o", str(step), str(pcb)], capture_output=True, text=True, timeout=900)
    b = board_mod.load(pcb)
    ox, oy = _origin(spec)

    def t(x, y):
        return [round(x - ox, 4), round(oy - y, 4)]

    bb = b["outline_bbox"]
    data = {
        "schema": SCHEMA, "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "pcb": str(pcb), "pcb_sha1": h, "step": str(step) if step.exists() else None,
        "step_user_origin": spec, "units": "mm",
        "board": {"thickness": b["thickness"],
                  "outline_bbox": (t(bb[0], bb[3]) + t(bb[2], bb[1])) if bb else None,
                  "z_bottom": 0.0, "z_top": b["thickness"]},
        "mounting_holes": [{"ref": m["ref"], "xy": t(m["x"], m["y"]), "drill": m["drill"], "net": m["net"]}
                           for m in board_mod.mounting_holes(b, params)],
        "components": [{"ref": f["ref"], "value": f["value"], "footprint": f["name"], "xy": t(f["x"], f["y"]),
                        "side": "bottom" if f["layer"].startswith("B.") else "top",
                        "has_model": bool(f["models"]), "dnp": f["dnp"]}
                       for f in b["footprints"]],
    }
    jf.write_text(json.dumps(data, indent=1), encoding="utf-8")
    return jf
