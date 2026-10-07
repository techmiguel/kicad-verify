"""Fabrication outputs (Gerber, drill, BOM, CPL) checked against the current .kicad_pcb.

Each check comes from a real board revision: CPL using the body centre instead of the pad centre
(parts offset by 3.8 and 1.4 mm), wrong SMD/THT attributes that dropped parts from the CPL, and
drill diameters changed after the outputs were exported.
"""
import csv
import math
import re
import statistics
import subprocess
import tempfile
from collections import Counter
from pathlib import Path

from .. import config
from ..config import walk
from ..report import FAIL, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey

GERBER_EXT = (".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp", ".gm1",
              ".gko", ".g1", ".g2", ".g3", ".g4", ".gml")


# ---------------------------------------------------------------- discovery
def find_outputs(root, params):
    """Fabrication files under params.fab.dir, or anywhere in the project."""
    fab = params.get("fab") or {}
    root = Path(root)
    gerbers, drills, boms, cpls = [], [], [], []
    base = root / fab["dir"] if fab.get("dir") else root
    for f in walk(base, 4):
        s, n = f.suffix.lower(), f.name.lower()
        if s in GERBER_EXT or (s.startswith(".g") and len(s) == 4 and s[2:].isdigit()):
            gerbers.append(f)
        elif s == ".drl":
            drills.append(f)
        elif s == ".csv" and ("bom" in n):
            boms.append(f)
        elif s == ".csv" and any(k in n for k in ("cpl", "pos", "placement", "pnp")):
            cpls.append(f)
    if fab.get("bom"):
        boms = [root / fab["bom"]]
    if fab.get("cpl"):
        cpls = [root / fab["cpl"]]
    return gerbers, drills, boms, cpls


# ---------------------------------------------------------------- Gerber
def _gerber_sets(files):
    """{FileFunction: coordinates flashed/drawn with apertures that carry %TA.AperFunction}. Drill
    marks and other function-less apertures depend on plot settings, not on the design."""
    out = {}
    for f in files:
        t = Path(f).read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"%TF\.FileFunction,([^*]+)\*%", t)
        if not m:
            continue
        func = {}
        pending = None
        cur = None
        coords = set()
        for line in t.splitlines():
            if line.startswith("%TA.AperFunction"):
                pending = line
            elif line.startswith("%TD"):
                pending = None
            elif line.startswith("%ADD"):
                code = re.match(r"%ADD(\d+)", line).group(1)
                func[code] = pending is not None
            elif re.fullmatch(r"D(\d+)\*", line) and int(line[1:-1]) >= 10:
                cur = line[1:-1]
            else:
                c = re.match(r"X(-?\d+)Y(-?\d+)", line)
                if c and cur and func.get(cur):
                    coords.add(c.groups())
        out[m.group(1)] = coords
    return out


def gerbers(pcb, board, files, label):
    res = []
    if not files:
        return [not_verifiable("FAB-GERBER-001", f"{label}: no Gerber files in the project (not exported yet)",
                               "export the Gerbers (or set params.fab.dir)"),
                not_verifiable("FAB-STALE-001", f"{label}: no Gerber files to compare with the PCB")]
    have = _gerber_sets(files)
    funcs = set(have)
    ncu = len(board["copper_layers"]) or 2
    need = ["Copper,L1,Top", f"Copper,L{ncu},Bot", "Soldermask,Top", "Soldermask,Bot", "Profile,NP"]
    missing = [n for n in need if n not in funcs]
    res.append(Result("FAB-GERBER-001", FAIL if missing else PASS,
                      f"{label}: {count(len(missing), 'mandatory Gerber layer')} missing",
                      violations=[{"key": vkey("gerb", n), "text": n} for n in missing],
                      evidence=list(files), coverage=coverage("mandatory layers", len(need))))
    # freshness: re-plot with kicad-cli and compare functional geometry per layer
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            subprocess.run([config.KICAD_CLI, "pcb", "export", "gerbers", "--board-plot-params",
                            "-o", td, str(pcb)], capture_output=True, text=True, timeout=300)
            fresh = _gerber_sets([p for p in Path(td).iterdir() if p.suffix != ".gbrjob"])
    except Exception as e:
        res.append(not_verifiable("FAB-STALE-001", f"{label}: could not re-plot Gerbers: {e}"))
        return res
    if not fresh:
        res.append(not_verifiable("FAB-STALE-001", f"{label}: kicad-cli produced no Gerbers to compare with"))
        return res
    diffs, gap = [], []
    for func, coords in fresh.items():
        if func in have:
            extra, lost = len(coords - have[func]), len(have[func] - coords)
            if extra or lost:
                diffs.append(f"{func}: {extra} items new on the PCB, {lost} items no longer on the PCB")
        elif coords:  # an empty layer (e.g. bottom paste with no bottom SMD) needs no export
            gap.append({"key": vkey("stalegap", func),
                        "text": f"{func}: plotted from the PCB, not in the exported set"})
    for func in [f for f in set(have) - set(fresh) if have[f]]:
        gap.append({"key": vkey("stalegap", func),
                    "text": f"{func}: exported, not produced by the board plot settings"})
    res.append(Result("FAB-STALE-001", FAIL if diffs else PASS,
                      f"{label}: {count(len(diffs), 'Gerber layer')} not matching the current PCB"
                      + (" (re-export them)" if diffs else ""),
                      violations=[{"key": vkey("stale", d.split(':')[0]), "text": d} for d in diffs],
                      evidence=list(files),
                      coverage=coverage("Gerber layers",
                                        len({f for f in set(have) | set(fresh) if have.get(f) or fresh.get(f)}), gap)))
    return res


# ---------------------------------------------------------------- drill
def _drill_holes(files):
    """[(diameter mm, x, y)] of round holes from Excellon files. Positions only when written in decimal format.
    Routed slots (`X..Y..G85X..Y..`, KiCad's default slot mode) are skipped, as on the PCB side."""
    holes, decimal = [], True
    for f in files:
        t = Path(f).read_text(encoding="utf-8", errors="ignore")
        inch = "INCH" in t.split("%")[0]
        k = 25.4 if inch else 1
        tools = {m.group(1): float(m.group(2)) * k for m in re.finditer(r"^T(\d+)C([\d.]+)", t, re.M)}
        body = t.split("%", 1)[1] if "%" in t else t
        cur = None
        for line in body.splitlines():
            m = re.fullmatch(r"T(\d+)", line.strip())
            if m:
                cur = m.group(1)
                continue
            c = re.match(r"X(-?[\d.]+)Y(-?[\d.]+)", line)
            if cur and c and "G85" not in line:
                if "." not in c.group(1):
                    decimal = False
                holes.append((round(tools.get(cur, 0), 2), float(c.group(1)) * k, float(c.group(2)) * k))
    return holes, decimal


def drills(board, files, label):
    if not files:
        return [not_verifiable("FAB-DRILL-001", f"{label}: no drill files", "export the drill files")]
    want = []
    for fp in board["footprints"]:
        for p in fp["pads"]:
            if p["drill"] and p["drill"] == p["drill2"]:
                want.append((round(p["drill"], 2), p["x"], p["y"]))
    for v in board["vias"]:
        if v["drill"] and v["pos"]:
            want.append((round(v["drill"], 2), v["pos"][0], v["pos"][1]))
    got, decimal = _drill_holes(files)
    hw, hg = Counter(d for d, _, _ in want), Counter(d for d, _, _ in got)
    diffs = [f"Ø{d} mm: {hw.get(d, 0)} on the PCB vs {hg.get(d, 0)} in the .drl files"
             for d in sorted(set(hw) | set(hg)) if hw.get(d, 0) != hg.get(d, 0)]
    note, gap = "", []
    if decimal and got and want:
        # absolute or auxiliary origin, Y flipped or not: keep the convention that fits best
        gset = {(d, round(x, 2), round(y, 2)) for d, x, y in got}
        best = None
        for ox, oy in {(0.0, 0.0), board["aux_origin"]}:
            for sy in (-1, 1):
                miss = [(d, x, y) for d, x, y in want
                        if not any((d, round(x - ox + ex, 2), round(sy * (y - oy) + ey, 2)) in gset
                                   for ex in (-0.01, 0, 0.01) for ey in (-0.01, 0, 0.01))]
                if best is None or len(miss) < len(best):
                    best = miss
        diffs += [f"hole Ø{d} mm at ({x:.2f}, {y:.2f}) on the PCB has no match in the .drl files" for d, x, y in best]
    else:
        note = " (positions not compared: non-decimal format)"
        gap = [{"key": vkey("drillgap", "positions"), "text": f"{count(len(want), 'hole position')} not compared "
                                                               "(Excellon without decimal point)"}]
    return [Result("FAB-DRILL-001", FAIL if diffs else PASS,
                   f"{label}: {count(len(diffs), 'PCB/.drl drill mismatch')} (slots not compared){note}",
                   violations=[{"key": vkey("drill", d.split(' on the PCB')[0].split(':')[0]), "text": d}
                               for d in diffs],
                   evidence=[str(f) for f in files], coverage=coverage("drilled holes", len(want) + (1 if gap else 0),
                                                                        gap))]


# ---------------------------------------------------------------- BOM / CPL
def _rows(path):
    raw = Path(path).read_bytes().decode("utf-8-sig", errors="ignore")
    lines = raw.splitlines()
    if any(ln.startswith("# Ref") for ln in lines[:10]):
        return _kicad_pos_rows(lines)
    return list(csv.DictReader(lines))


def _kicad_pos_rows(lines):
    """KiCad's ASCII position file (whatever its extension): '#' comments, '## Unit = inches|mm',
    whitespace-separated `Ref Val Package PosX PosY Rot Side`. Positions are returned in mm."""
    scale = 1.0
    rows = []
    for ln in lines:
        if ln.startswith("#"):
            if m := re.match(r"##\s*Unit\s*=\s*(\w+)", ln):
                scale = 25.4 if m.group(1).lower().startswith("inch") else 1.0
            continue
        tok = ln.split()
        if len(tok) < 7:  # Val and Package may hold spaces; Ref first and the four last are fixed
            continue
        x, y = _num(tok[-4]), _num(tok[-3])
        if x is None or y is None:
            continue
        rows.append({"ref": tok[0], "posx": str(x * scale), "posy": str(y * scale), "rot": tok[-2],
                     "side": tok[-1]})
    return rows


def _designators(field):
    """Designators in a BOM cell: 'C1,C2 C3;C4', and ranges as KiCad groups them, 'B1-B4' or 'B1-4'."""
    out = []
    for d in [x.strip() for x in re.split(r"[,; ]+", field or "") if x.strip()]:
        m = re.fullmatch(r"([A-Za-z_]+)(\d+)-(?:\1)?(\d+)", d)
        if m and int(m.group(2)) < int(m.group(3)) <= int(m.group(2)) + 999:
            out += [f"{m.group(1)}{i}" for i in range(int(m.group(2)), int(m.group(3)) + 1)]
        else:
            out.append(d)
    return out


def _col(row, *names):
    low = {k.lower().strip(): v for k, v in row.items() if k}
    for n in names:
        if n in low:
            return low[n]
    return None


def _assembled(board):
    return {fp["ref"]: fp for fp in board["footprints"]
            if not fp["dnp"] and "exclude_from_bom" not in fp["attr"]
            and "board_only" not in fp["attr"] and not fp["ref"].startswith(("#", "MH", "TP", "FID"))
            and fp["pads"]}


def _fitted(board, excluded_by):
    """PCB footprints that are fitted and not excluded by `excluded_by` (exclude_from_bom or
    exclude_from_pos_files): a BOM or CPL line for one of them is not a mismatch even when the part
    is not required there (a padless heatsink, a fiducial)."""
    return {fp["ref"] for fp in board["footprints"] if not fp["dnp"] and excluded_by not in fp["attr"]}


def bom(board, files, label):
    if not files:
        return [not_verifiable("FAB-BOM-001", f"{label}: no BOM", "export the BOM (or set params.fab.bom)")]
    f = files[0]
    rows = _rows(f)
    want = _assembled(board)
    fitted = _fitted(board, "exclude_from_bom")
    seen, diffs, warns = {}, [], []
    for r in rows:
        des = _col(r, "designator", "reference", "references", "ref", "designators") or ""
        fpn = (_col(r, "footprint", "package") or "").strip()
        lcsc = _col(r, "lcsc part #", "lcsc", "jlcpcb part #", "lcsc part number")
        for d in _designators(des):
            seen[d] = True
            if d not in want:
                if d not in fitted:
                    diffs.append(f"{d}: in the BOM but not assembled on the PCB (or excluded from BOM)")
                continue
            short = want[d]["name"].split(":")[-1]
            if fpn and fpn != short and fpn != want[d]["name"]:
                diffs.append(f"{d}: footprint {fpn} in BOM vs {short} on PCB")
            if lcsc is not None and not lcsc.strip():
                warns.append(f"{d}: no supplier part number")
    for d in want:
        if d not in seen:
            diffs.append(f"{d}: on the PCB but not in the BOM")
    vs = [{"key": vkey("bom", d), "text": d} for d in diffs + warns]
    st = FAIL if diffs else (WARN if warns else PASS)
    return [Result("FAB-BOM-001", st,
                   f"{label}: {count(len(diffs), 'BOM/PCB mismatch')}, {count(len(warns), 'warning')}",
                   violations=vs, evidence=[str(f)], coverage=coverage("assembled parts", len(want)))]


def _num(s):
    m = re.search(r"-?[\d.]+", s or "")
    return float(m.group(0)) if m else None


def cpl(board, files, label, tol):
    if not files:
        return [not_verifiable("FAB-CPL-001", f"{label}: no CPL", "export the placement file (or set params.fab.cpl)")]
    f = files[0]
    rows = _rows(f)
    want = _assembled(board)
    entries = {}
    for r in rows:
        d = (_col(r, "designator", "ref", "reference") or "").strip()
        x, y = _num(_col(r, "mid x", "posx", "x", "center-x(mm)")), _num(_col(r, "mid y", "posy", "y",
                                                                               "center-y(mm)"))
        side = (_col(r, "layer", "side") or "").strip().lower()
        if d and x is not None and y is not None:
            entries[d] = (x, y, side)

    def padcenter(fp):
        xs = [p["x"] for p in fp["pads"] if p["type"] != "np_thru_hole"] or [fp["x"]]
        ys = [p["y"] for p in fp["pads"] if p["type"] != "np_thru_hole"] or [fp["y"]]
        return ((min(xs) + max(xs)) / 2, (min(ys) + max(ys)) / 2)

    # CPL<->PCB transform: absolute/aux/grid origin, Y flipped or not; keep the lowest median error
    # (each fab house and exporter version uses its own convention)
    common = [d for d in entries if d in want]
    best = None
    for ox, oy in {(0.0, 0.0), board["aux_origin"], board["grid_origin"]}:
        for sy in (-1, 1):
            errs = []
            for d in common:
                cx, cy = padcenter(want[d])
                e = entries[d]
                errs.append(math.hypot(e[0] - (cx - ox), e[1] - sy * (cy - oy)))
            if errs:
                m = statistics.median(errs)
                if best is None or m < best[0]:
                    best = (m, ox, oy, sy)
    diffs, warns = [], []
    fitted = _fitted(board, "exclude_from_pos_files")
    # a placement file exported for SMD parts only (KiCad's --smd-only, the usual for machine assembly)
    # holds no through-hole part: those parts are then hand-fitted, which is a decision, not a mismatch
    tht = {d for d, fp in want.items() if "smd" not in fp["attr"]}
    smd_only = bool(entries) and not tht & set(entries)
    for d in want:
        if d not in entries:
            if smd_only and d in tht:
                warns.append(f"{d}: through-hole, not in the SMD-only CPL (fitted by hand?)")
            else:
                diffs.append(f"{d}: missing from the CPL (wrong SMD/THT or 'exclude from position files' attribute?)")
    for d in entries:
        if d not in want and d not in fitted:
            diffs.append(f"{d}: in the CPL but not assembled according to the PCB")
    if best:
        _, ox, oy, sy = best
        for d in common:
            cx, cy = padcenter(want[d])
            e = entries[d]
            err = math.hypot(e[0] - (cx - ox), e[1] - sy * (cy - oy))
            if err > tol:
                diffs.append(f"{d}: CPL position {err:.2f} mm from the pad centre (tolerance {tol} mm)")
            side = "bottom" if want[d]["layer"].startswith("B.") else "top"
            if e[2] and not e[2].startswith(side[0]):
                diffs.append(f"{d}: side {e[2]} in CPL vs {side} on PCB")
    return [Result("FAB-CPL-001", FAIL if diffs else (WARN if warns else PASS),
                   f"{label}: {count(len(diffs), 'CPL/PCB mismatch')}, {count(len(warns), 'warning')} "
                   "(rotations NOT verified)",
                   violations=[{"key": vkey("cpl", d.split(':')[0], d.split(':')[1][:12]), "text": d}
                               for d in diffs + warns],
                   evidence=[str(f)], coverage=coverage("assembled parts", len(want)))]


def run(root, pcb, board, label, params, mode):
    g, dr, b, c = find_outputs(root, params)
    tol = float((params.get("fab") or {}).get("cpl_tol_mm", 0.5))
    res = []
    if mode == "full":
        res += gerbers(pcb, board, g, label)
    res += drills(board, dr, label)
    if (params.get("fab") or {}).get("assembly", True):
        res += bom(board, b, label)
        res += cpl(board, c, label, tol)
    return res
