"""Adapter for kicad-happy (https://github.com/aklofas/kicad-happy, MIT): 60+ deterministic circuit,
layout and fabrication detectors. kicad-verify runs its analyzers, turns each finding into a
waivable violation under check id `KH-<rule_id>`, and gates on the mapped severity.

Default mapping: error -> FAIL, warning -> WARN, info -> kept only for the reviewer bundle.
`params.kicad_happy.severity` overrides it per rule (values: fail, warn, info, off). The defaults
below were set after running kicad-happy v2.3.1 on boards that were fabricated and work.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

from ..report import FAIL, NOT_VERIFIABLE, PASS, WARN, Result, count

PINNED = "v2.3.1"
REPO = "https://github.com/aklofas/kicad-happy.git"
DEFAULT_OVERRIDES = {
    "SS-001": "warn",   # MPN coverage: JLCPCB flows use LCSC codes; FAB-BOM-001 checks supplier codes
    "DS-001": "warn",   # no local datasheets: the review is weaker, the board is not wrong
    "PM-002": "warn",   # courtyard close to the edge: often intentional (connectors, modules)
    "KO-001": "warn",   # flags any via/track inside a rule area, ignoring what the area forbids; KiCad DRC
                        # enforces rule areas (PCB-DRC-001). False FAILs on vias in areas that allow vias
    "LR-001": "warn",   # misses per-cathode resistors of RGB LEDs; CIR-LED-001 computes the
                        # current, and a missing resistor is a WARN there too (constant-current driver?)
    "FD-001": "warn",   # fiducial count: what the assembler needs (JLCPCB adds its own rails; 2 global
                        # fiducials are common practice), not a defect of the board
    "PP-001": "warn",   # "IC power pin has no DC path to a rail": wrong on all three cases checked on
                        # real boards (a buck bootstrap pin, an LDO input fed through a resistor, an
                        # ASIC's internally generated I/O supply decoupled to ground)
    "VM-001": "warn",   # voltage-domain crossing: the domain is inferred from rail names and the input
                        # tolerance of each pin is not known (a PMBus regulator on a 5 V rail with 3.3 V
                        # I/O was flagged)
}
SEV = {"fail": FAIL, "warn": WARN}
# placement rules whose finding does not exist on the assembled board when a footprint involved is
# DNP (an alternative laid over another part): reported as warnings that name the DNP parts
DNP_PLACEMENT_RULES = {"PM-001"}


def cache_dir():
    base = os.environ.get("KICAD_VERIFY_CACHE") or (
        Path(os.environ.get("LOCALAPPDATA", Path.home() / ".cache")) / "kicad-verify")
    return Path(base)


def locate(params):
    cfg = params.get("kicad_happy") or {}
    cands = [cfg.get("path"), os.environ.get("KICAD_HAPPY_DIR"), cache_dir() / f"kicad-happy-{PINNED}"]
    for c in cands:
        if c and (Path(c) / "skills" / "kicad" / "scripts" / "analyze_schematic.py").exists():
            return Path(c) / "skills" / "kicad" / "scripts"
    return None


def install(dest=None):
    dest = Path(dest or cache_dir() / f"kicad-happy-{PINNED}")
    if (dest / "skills").exists():
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "clone", "--depth", "1", "--branch", PINNED, REPO, str(dest)], check=True)
    return dest


def _run(scripts, args, out, timeout=600):
    subprocess.run([sys.executable, str(scripts / args[0]), *args[1:], "-o", str(out)],
                   capture_output=True, text=True, timeout=timeout)
    return json.loads(Path(out).read_text(encoding="utf-8")) if Path(out).exists() else None


def analyze(kicad, gerber_dir, work_dir, params):
    """Runs the analyzers; returns {name: json} or None when kicad-happy is unavailable."""
    scripts = locate(params)
    if scripts is None:
        return None
    work_dir = Path(work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    if kicad["sch"].exists():
        out["schematic"] = _run(scripts, ["analyze_schematic.py", str(kicad["sch"])], work_dir / "kh_schematic.json")
    if kicad["pcb"].exists():
        args = ["analyze_pcb.py", str(kicad["pcb"])]
        if out.get("schematic"):
            args += ["--schematic", str(work_dir / "kh_schematic.json")]
        out["pcb"] = _run(scripts, args, work_dir / "kh_pcb.json")
    if out.get("schematic") and out.get("pcb"):
        out["cross"] = _run(scripts, ["cross_analysis.py", "-s", str(work_dir / "kh_schematic.json"),
                                      "-p", str(work_dir / "kh_pcb.json")], work_dir / "kh_cross.json")
    if gerber_dir:
        out["gerbers"] = _run(scripts, ["analyze_gerbers.py", str(gerber_dir)], work_dir / "kh_gerbers.json")
    out["_version"] = PINNED
    return out


def findings(data):
    for name in ("schematic", "pcb", "cross", "gerbers"):
        for f in (data.get(name) or {}).get("findings") or []:
            yield name, f


def to_results(data, label, params, dnp=()):
    if data is None:
        return [Result("KH-ENGINE", WARN, "kicad-happy not installed: circuit/DFM detectors skipped "
                       "(run `kicadverify setup`)", outcome=NOT_VERIFIABLE)]
    cfg = params.get("kicad_happy") or {}
    overrides = {**DEFAULT_OVERRIDES, **(cfg.get("severity") or {})}
    groups, seen = {}, set()
    for _source, f in findings(data):
        rule = f.get("rule_id") or f.get("detector") or "UNKNOWN"
        sev = (f.get("severity") or "info").lower()
        mapped = overrides.get(rule) or {"error": "fail", "warning": "warn"}.get(sev, "info")
        if mapped in ("info", "off"):
            continue
        dnp_refs = sorted(set(f.get("components") or []) & set(dnp)) if rule in DNP_PLACEMENT_RULES else []
        if dnp_refs and mapped == "fail":
            mapped = "warn"
        g = groups.setdefault(rule, {"status": WARN, "items": []})
        if SEV[mapped] == FAIL:
            g["status"] = FAIL
        refs = ", ".join((f.get("components") or [])[:6])
        text = f.get("summary") or f.get("description") or rule
        if refs and refs not in (f.get("summary") or ""):
            text += f" [{refs}]"
        if dnp_refs:
            text += f" [DNP: {', '.join(dnp_refs)}]"
        if (rule, text) in seen:  # the same finding repeated (one per pad of a thermal via array)
            continue
        seen.add((rule, text))
        g["items"].append({"key": (f.get("finding_id") or f"{rule}:{text}")[-60:],
                           "text": f"{text} ({f.get('confidence', '?')})"})
    res = [Result(f"KH-{rule}", g["status"], f"{label}: kicad-happy {rule}: {count(len(g['items']), 'finding')}",
                  violations=g["items"]) for rule, g in sorted(groups.items())]
    n = sum(1 for _ in findings(data))
    ran = [k for k in ("schematic", "pcb", "cross", "gerbers") if data.get(k)]
    res.append(Result("KH-ENGINE", PASS, f"{label}: kicad-happy {data.get('_version')} ran "
                      f"({', '.join(ran) or 'no analyzer output'}), {n} findings "
                      f"({sum(len(g['items']) for g in groups.values())} gated)",
                      outcome=None if ran else NOT_VERIFIABLE))
    return res


def summary_for_review(data, limit=120):
    """Compact view for the reviewer bundle: every finding (info included) plus key analyses."""
    if not data:
        return None
    out = {"version": data.get("_version"), "findings": []}
    for _source, f in findings(data):
        out["findings"].append({k: f.get(k) for k in ("finding_id", "rule_id", "severity", "confidence",
                                                         "summary", "components", "nets", "recommendation")})
    out["findings"] = out["findings"][:limit]
    sch = data.get("schematic") or {}
    for k in ("rail_voltages", "power_sequencing_validation", "ic_pin_analysis"):
        if sch.get(k):
            out[k] = sch[k]
    pcb = data.get("pcb") or {}
    for k in ("decoupling_placement", "dfm_summary"):
        if pcb.get(k):
            out[k] = pcb[k]
    return out
