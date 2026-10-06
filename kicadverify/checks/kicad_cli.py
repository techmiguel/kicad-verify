"""ERC and DRC through kicad-cli (flags verified with --help on KiCad 10.0.5)."""
import json
import subprocess
import tempfile
from pathlib import Path

from .. import config
from ..report import FAIL, PASS, SKIP, WARN, Result
from ..waivers import vkey

PARITY_TYPES = {
    "missing_footprint", "extra_footprint", "net_conflict", "footprint_symbol_mismatch",
    "duplicate_footprints", "footprint_filters_mismatch", "schematic_parity",
}


def _run(args, timeout=900):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def _items_text(v):
    items = v.get("items", [])
    return "; ".join(i.get("description", "") for i in items)


def _violation(v, label):
    text = f"[{v.get('severity')}] {v.get('type')}: {v.get('description')} ({_items_text(v)})"
    return {"key": vkey(label, v.get("type"), _items_text(v)), "text": text}


def _status(vs_raw):
    sev = {v.get("severity") for v in vs_raw}
    if "error" in sev:
        return FAIL
    if sev:
        return WARN
    return PASS


def erc(sch, label):
    cid = "PCB-ERC-001"
    if not Path(sch).exists():
        return [Result(cid, SKIP, f"{label}: no schematic")]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "erc.json"
        try:
            _run([config.KICAD_CLI, "sch", "erc", "--format", "json", "--severity-all",
                  "-o", str(out), str(sch)])
        except Exception as e:
            return [Result(cid, FAIL, f"{label}: kicad-cli ERC did not run: {e}")]
        if not out.exists():
            return [Result(cid, FAIL, f"{label}: ERC produced no report")]
        data = json.loads(out.read_text(encoding="utf-8"))
    raw = [v for s in data.get("sheets", []) for v in s.get("violations", [])]
    vs = [_violation(v, f"erc:{label}") for v in raw]
    return [Result(cid, _status(raw), f"{label}: {len(raw)} ERC violations", violations=vs,
                   evidence=[str(sch)])]


def drc(pcb, label):
    """DRC (zones refilled in memory, file untouched) and unrouted connections. Parity lives in parity.py."""
    if not Path(pcb).exists():
        return [Result("PCB-DRC-001", SKIP, f"{label}: no PCB")]
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "drc.json"
        try:
            _run([config.KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all",
                  "--all-track-errors", "--refill-zones", "-o", str(out), str(pcb)])
        except Exception as e:
            return [Result("PCB-DRC-001", FAIL, f"{label}: kicad-cli DRC did not run: {e}")]
        if not out.exists():
            return [Result("PCB-DRC-001", FAIL, f"{label}: DRC produced no report")]
        data = json.loads(out.read_text(encoding="utf-8"))
    groups = {"violations": [], "unconnected_items": [], "schematic_parity": []}
    for k in groups:
        groups[k] = data.get(k, [])
    parity = list(groups["schematic_parity"]) + [
        v for v in groups["violations"] if v.get("type") in PARITY_TYPES]
    drc_raw = [v for v in groups["violations"] if v.get("type") not in PARITY_TYPES]
    unconn = groups["unconnected_items"]
    res = []
    vs = [_violation(v, f"drc:{label}") for v in drc_raw]
    res.append(Result("PCB-DRC-001", _status(drc_raw), f"{label}: {len(drc_raw)} DRC violations",
                      violations=vs, evidence=[str(pcb)]))
    vs = [_violation(v, f"unconn:{label}") for v in unconn]
    res.append(Result("PCB-CONN-001", FAIL if unconn else PASS,
                      f"{label}: {len(unconn)} unrouted connections", violations=vs,
                      evidence=[str(pcb)]))
    return res
