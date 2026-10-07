"""ERC and DRC through kicad-cli (flags verified with --help on KiCad 10.0.5)."""
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from .. import config
from ..report import FAIL, NOT_VERIFIABLE, PASS, WARN, Result, count, not_verifiable
from ..waivers import vkey

PARITY_TYPES = {
    "missing_footprint", "extra_footprint", "net_conflict", "footprint_symbol_mismatch",
    "duplicate_footprints", "footprint_filters_mismatch", "schematic_parity",
}


# placement conflicts that do not exist on the assembled board when one of the footprints is DNP:
# an alternative part laid over another (a regulator footprint over a buck converter, two connector
# options). Drilled-hole conflicts stay errors: the holes are drilled whether the part is fitted or not
DNP_PLACEMENT_TYPES = {"courtyards_overlap", "pth_inside_courtyard", "npth_inside_courtyard"}
REF_RE = re.compile(r"(?:Footprint|of) ([^\s;()\[\]]+)")


def _dnp_downgrade(v, dnp):
    """A DNP-alternative placement conflict becomes a warning that names the DNP footprints."""
    if v.get("type") not in DNP_PLACEMENT_TYPES or v.get("severity") != "error" or not dnp:
        return v
    refs = sorted({m for i in v.get("items", []) for m in REF_RE.findall(i.get("description", ""))} & set(dnp))
    if not refs:
        return v
    return {**v, "severity": "warning", "description": f"{v.get('description')} [DNP: {', '.join(refs)}]"}


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


def _keep(src, evidence_dir, name):
    """Copies a raw tool report next to the verification report: it is the evidence."""
    if not evidence_dir:
        return None
    dst = Path(evidence_dir) / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dst)
    return dst


def _tool_failed(cid, label, what):
    return Result(cid, WARN, f"{label}: {what}", outcome=NOT_VERIFIABLE)


def erc(sch, label, evidence_dir=None):
    cid = "PCB-ERC-001"
    if not Path(sch).exists():
        return [not_verifiable(cid, f"{label}: no schematic")]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        out = Path(td) / "erc.json"
        try:
            _run([config.KICAD_CLI, "sch", "erc", "--format", "json", "--severity-all",
                  "-o", str(out), str(sch)])
        except Exception as e:
            return [_tool_failed(cid, label, f"kicad-cli ERC did not run: {e}")]
        if not out.exists():
            return [_tool_failed(cid, label, "ERC produced no report")]
        data = json.loads(out.read_text(encoding="utf-8"))
        kept = _keep(out, evidence_dir, f"{label}.erc.json")
    raw = [v for s in data.get("sheets", []) for v in s.get("violations", [])]
    vs = [_violation(v, f"erc:{label}") for v in raw]
    return [Result(cid, _status(raw), f"{label}: {count(len(raw), 'ERC violation')}", violations=vs,
                   evidence=[str(sch)] + ([kept] if kept else []))]


def drc(pcb, label, evidence_dir=None, dnp=()):
    """DRC (zones refilled in memory, file untouched) and unrouted connections. Parity lives in parity.py.
    `dnp`: references of DNP footprints, whose placement conflicts are reported as warnings."""
    if not Path(pcb).exists():
        return [not_verifiable("PCB-DRC-001", f"{label}: no PCB"), not_verifiable("PCB-CONN-001", f"{label}: no PCB")]
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
        out = Path(td) / "drc.json"
        try:
            _run([config.KICAD_CLI, "pcb", "drc", "--format", "json", "--severity-all",
                  "--all-track-errors", "--refill-zones", "-o", str(out), str(pcb)])
        except Exception as e:
            return [_tool_failed(c, label, f"kicad-cli DRC did not run: {e}") for c in ("PCB-DRC-001", "PCB-CONN-001")]
        if not out.exists():
            return [_tool_failed(c, label, "DRC produced no report") for c in ("PCB-DRC-001", "PCB-CONN-001")]
        data = json.loads(out.read_text(encoding="utf-8"))
        kept = _keep(out, evidence_dir, f"{label}.drc.json")
    drc_raw = [_dnp_downgrade(v, dnp) for v in data.get("violations", []) if v.get("type") not in PARITY_TYPES]
    unconn = data.get("unconnected_items", [])
    res = []
    vs = [_violation(v, f"drc:{label}") for v in drc_raw]
    res.append(Result("PCB-DRC-001", _status(drc_raw), f"{label}: {count(len(drc_raw), 'DRC violation')}",
                      violations=vs, evidence=[str(pcb)] + ([kept] if kept else [])))
    vs = [_violation(v, f"unconn:{label}") for v in unconn]
    res.append(Result("PCB-CONN-001", FAIL if unconn else PASS,
                      f"{label}: {count(len(unconn), 'unrouted connection')}", violations=vs,
                      evidence=[str(pcb)] + ([kept] if kept else [])))
    return res
