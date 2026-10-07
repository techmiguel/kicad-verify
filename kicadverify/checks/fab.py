"""Fabrication outputs (Gerber, drill, BOM, CPL) checked against the current .kicad_pcb.

Each check comes from a real board revision: CPL using the body centre instead of the pad centre
(parts offset by 3.8 and 1.4 mm), wrong SMD/THT attributes that dropped parts from the CPL, and
drill diameters changed after the outputs were exported.
"""
import csv
import math
import os
import re
import shutil
import statistics
import subprocess
import tempfile
import zipfile
from collections import Counter
from pathlib import Path

from .. import config
from ..config import walk
from ..report import FAIL, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey

GERBER_EXT = (".gbr", ".gtl", ".gbl", ".gts", ".gbs", ".gto", ".gbo", ".gtp", ".gbp", ".gm1",
              ".gko", ".g1", ".g2", ".g3", ".g4", ".gml")


# ---------------------------------------------------------------- discovery
def _is_gerber(name):
    s = Path(name).suffix.lower()
    return s in GERBER_EXT or (s.startswith(".g") and len(s) == 4 and s[2:].isdigit())


def _fab_member(name):
    return _is_gerber(name) or Path(name).suffix.lower() == ".drl"


def archives(root, params):
    """Zip archives holding Gerber or drill files (what is uploaded to the fab)."""
    fab = params.get("fab") or {}
    root = Path(root)
    base = root / fab["dir"] if fab.get("dir") else root
    out = []
    for f in walk(base, 4):
        if f.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(f) as z:
                    if any(_fab_member(n) for n in z.namelist()):
                        out.append(f)
            except (zipfile.BadZipFile, OSError):
                continue
    return out


def _unzip(zf, dest, root):
    """Gerber and drill members of `zf`, flattened into dest/<zip path relative to root>/ (two
    archives with one name in different folders do not collide; member names only, so no path from
    the archive can write outside dest)."""
    out = []
    rel = Path(os.path.relpath(Path(zf).resolve(), Path(root).resolve()))
    target = Path(dest).joinpath(*[part if part != ".." else "_up" for part in rel.parts])
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)
    with zipfile.ZipFile(zf) as z:
        for info in z.infolist():
            name = Path(info.filename).name
            if info.is_dir() or not name or not _fab_member(name):
                continue
            path = target / name
            path.write_bytes(z.read(info))
            out.append(path)
    return out


def detect_outside(root):
    """Fabrication outputs kept outside the KiCad project folder but in the same repository (a common
    layout: hardware/kicad/ next to hardware/fab/ and hardware/bom/). Returns params.fab entries
    relative to `root` ({} when the project folder already holds outputs or nothing is found)."""
    root = Path(root).resolve()
    if any(find_outputs(root, {})) or archives(root, {}):
        return {}
    repo = next((d for d in [root, *root.parents] if (d / ".git").exists()), root.parent)
    # only outputs named after this board (KiCad names them <board>-F_Cu.gbr): a repository with
    # several boards keeps the others' outputs next to this one's
    stems = [f.stem.lower() for f in root.glob("*.kicad_pcb")]

    def ours(name):
        return any(s in name.lower() for s in stems)

    counts, boms, cpls = Counter(), [], []
    for f in walk(repo, 6):
        if root in f.parents:
            continue
        s, n = f.suffix.lower(), f.name.lower()
        if _fab_member(f.name):
            if ours(f.name):
                counts[f.parent] += 1
        elif s == ".zip":
            try:
                with zipfile.ZipFile(f) as z:
                    if any(_fab_member(m) and ours(Path(m).name) for m in z.namelist()):
                        counts[f.parent] += 1
            except (zipfile.BadZipFile, OSError):
                pass
        elif s == ".csv" and "bom" in n:
            boms.append(f)
        elif s == ".csv" and any(k in n for k in ("cpl", "pos", "placement", "pnp")):
            cpls.append(f)
    out = {}
    if counts:
        best = max(counts, key=lambda d: (counts[d], -len(d.parts)))
        # the folder holding the export(s): a gerber/ sub-folder's parent keeps the archives next to it
        out["dir"] = os.path.relpath(best.parent if best.name.lower().startswith("gerber") else best, root)
    base = (root / out["dir"]).resolve() if "dir" in out else None
    for key, found in (("bom", boms), ("cpl", cpls)):
        if base and any(base in f.parents for f in found):
            continue  # found under the output folder anyway
        # the closest to the project folder, when one is clearly closest (tools/test/jlc_cpl_rotations.csv
        # elsewhere in the repository is not the placement file)
        ranked = sorted(found, key=lambda f: len(Path(os.path.relpath(f, root)).parts))
        if ranked and (len(ranked) == 1 or len(Path(os.path.relpath(ranked[0], root)).parts)
                       < len(Path(os.path.relpath(ranked[1], root)).parts)):
            out[key] = os.path.relpath(ranked[0], root)
    return {k: v.replace(os.sep, "/") for k, v in out.items()}


def find_outputs(root, params, unzip_to=None):
    """Fabrication files under params.fab.dir, or anywhere in the project. With `unzip_to`, the Gerber
    and drill files inside zip archives are extracted there and included, one set per archive."""
    fab = params.get("fab") or {}
    root = Path(root)
    gerbers, drills, boms, cpls = [], [], [], []
    base = root / fab["dir"] if fab.get("dir") else root
    files = list(walk(base, 4))
    if unzip_to:
        for zf in archives(root, params):
            files += _unzip(zf, unzip_to, root)
    for f in files:
        s, n = f.suffix.lower(), f.name.lower()
        if _is_gerber(f.name):
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
# coordinates of the exported and the re-plotted set match within this distance: a 4.5 export
# resolves 10 µm, a 4.6 one 1 µm; a design change moves copper by far more
GERBER_TOL_MM = 0.006


def _gerber_sets(files):
    """{FileFunction: {(x, y) mm}} of what is flashed/drawn with apertures that carry
    %TA.AperFunction, plus every flashed opening on mask and paste layers (KiCad gives those no
    aperture attributes). Drill marks and other function-less apertures depend on plot settings, not
    on the design; so do legend layers and the board profile plotted on every layer, which is ignored
    outside the profile layer. Coordinates are converted to mm from the file's %FS format and %MO unit."""
    out = {}
    for f in files:
        t = Path(f).read_text(encoding="utf-8", errors="ignore")
        m = re.search(r"%TF\.FileFunction,([^*]+)\*%", t)
        if not m:
            continue
        fs = re.search(r"%FS[LT]?A?X(\d)(\d)Y(\d)(\d)\*%", t)
        xd, yd = (int(fs.group(2)), int(fs.group(4))) if fs else (6, 6)
        k = 25.4 if "%MOIN*%" in t else 1.0
        profile_layer = m.group(1).startswith("Profile")
        # KiCad writes no aperture attributes on mask and paste layers: there every flashed opening counts
        openings = m.group(1).startswith(("Soldermask", "Paste"))
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
                func[code] = pending is not None and (profile_layer or "AperFunction,Profile" not in pending)
            elif re.fullmatch(r"D(\d+)\*", line) and int(line[1:-1]) >= 10:
                cur = line[1:-1]
            else:
                c = re.match(r"X(-?\d+)Y(-?\d+)", line)
                if c and cur and (func.get(cur) or (openings and line.rstrip().endswith("D03*"))):
                    coords.add((int(c.group(1)) / 10 ** xd * k, int(c.group(2)) / 10 ** yd * k))
        out[m.group(1)] = coords
    return out


def _unmatched(a, b, tol=GERBER_TOL_MM):
    """Points of `a` with no point of `b` within `tol`."""
    grid = {}
    for x, y in b:
        grid.setdefault((math.floor(x / tol), math.floor(y / tol)), []).append((x, y))
    out = 0
    for x, y in a:
        i, j = math.floor(x / tol), math.floor(y / tol)
        if not any(math.hypot(x - u, y - v) <= tol for di in (-1, 0, 1) for dj in (-1, 0, 1)
                   for u, v in grid.get((i + di, j + dj), ())):
            out += 1
    return out


def _plot_layer(func):
    """KiCad layer name of a Gerber FileFunction ('Copper,L2,Inr' -> 'In1.Cu'), or None."""
    parts = func.split(",")
    side = {"Top": "F", "Bot": "B"}.get(parts[-1])
    if parts[0] == "Copper" and len(parts) >= 3:
        return f"{side}.Cu" if side else f"In{int(parts[1][1:]) - 1}.Cu"
    names = {"Soldermask": "Mask", "Paste": "Paste", "Legend": "Silkscreen"}
    if parts[0] in names and side:
        return f"{side}.{names[parts[0]]}"
    return "Edge.Cuts" if parts[0] == "Profile" else None


def output_sets(files):
    """{set name: files}: the files of one export, i.e. one directory or one zip archive. Two exports
    side by side (loose files and the zip that was uploaded) are checked one against the other's PCB,
    never merged: a stale archive next to fresh loose files is exactly what must be caught."""
    groups = {}
    for f in files:
        groups.setdefault(Path(f).parent, []).append(f)
    names = [d.name for d in groups]
    return {(d.name if names.count(d.name) == 1 else str(d)): v for d, v in sorted(groups.items())}


def gerbers(pcb, board, files, label):
    if not files:
        return [not_verifiable("FAB-GERBER-001", f"{label}: no Gerber files in the project (not exported yet)",
                               "export the Gerbers (or set params.fab.dir)"),
                not_verifiable("FAB-STALE-001", f"{label}: no Gerber files to compare with the PCB")]
    sets = {name: _gerber_sets(fs) for name, fs in output_sets(files).items()}
    tag = (lambda name: f"[{name}] ") if len(sets) > 1 else (lambda name: "")
    ncu = len(board["copper_layers"]) or 2
    copper = ["Copper,L1,Top"] + [f"Copper,L{i},Inr" for i in range(2, ncu)] + [f"Copper,L{ncu},Bot"]
    need = [copper[0], copper[-1], "Soldermask,Top", "Soldermask,Bot", "Profile,NP"]
    missing = [tag(name) + n for name, have in sets.items() for n in need if n not in have]
    res = [Result("FAB-GERBER-001", FAIL if missing else PASS,
                  f"{label}: {count(len(missing), 'mandatory Gerber layer')} missing"
                  + (f" ({count(len(sets), 'Gerber set')})" if len(sets) > 1 else ""),
                  violations=[{"key": vkey("gerb", n), "text": n} for n in missing],
                  evidence=list(files), coverage=coverage("mandatory layers", len(need) * len(sets)))]
    # freshness: re-plot with kicad-cli and compare functional geometry per layer. The layers and the
    # format are given explicitly: the board's stored plot settings are those of the LAST plot, which
    # may have been a PDF or a copper-only set (kicad-cli then writes PDF content under .gbr names)
    exported = set().union(*sets.values())
    layers = sorted({ly for ly in map(_plot_layer, exported | set(copper) | set(need)) if ly})
    args = [config.KICAD_CLI, "pcb", "export", "gerbers", "-l", ",".join(layers), "-o", "{td}", str(pcb)]
    if board.get("plot_aux_origin"):
        args.insert(-1, "--use-drill-file-origin")
    try:
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as td:
            subprocess.run([a.replace("{td}", td) for a in args], capture_output=True, text=True, timeout=300)
            fresh = _gerber_sets([p for p in Path(td).iterdir() if p.suffix != ".gbrjob"])
    except Exception as e:
        res.append(not_verifiable("FAB-STALE-001", f"{label}: could not re-plot Gerbers: {e}"))
        return res
    if not fresh:
        res.append(not_verifiable("FAB-STALE-001", f"{label}: kicad-cli produced no Gerbers to compare with"))
        return res
    # mask openings over non-plated holes are plotted by some KiCad versions and not by others: they
    # are left out of the mask/paste comparison (the holes themselves are FAB-DRILL-001's)
    ox, oy = board["aux_origin"] if board.get("plot_aux_origin") else (0.0, 0.0)
    npth = {(p["x"] - ox, -(p["y"] - oy)) for fp in board["footprints"] for p in fp["pads"]
            if p["type"] == "np_thru_hole"}
    diffs, gap, total = [], [], 0
    for name, have in sets.items():
        total += len({f for f in set(have) | set(fresh) if have.get(f) or fresh.get(f)})
        for func, coords in fresh.items():
            if func in have:
                ref = have[func]
                if func.startswith(("Soldermask", "Paste")) and npth:
                    coords = {c for c in coords if _unmatched([c], npth)}
                    ref = {c for c in ref if _unmatched([c], npth)}
                extra, lost = _unmatched(coords, ref), _unmatched(ref, coords)
                if extra or lost:
                    diffs.append(f"{tag(name)}{func}: {extra} items new on the PCB, {lost} items no longer on the PCB")
            elif coords:  # an empty layer (e.g. bottom paste with no bottom SMD) needs no export
                gap.append({"key": vkey("stalegap", *([name] if len(sets) > 1 else []), func),
                            "text": f"{tag(name)}{func}: plotted from the PCB, not in the exported set"})
        for func in [f for f in set(have) - set(fresh) if have[f]]:
            gap.append({"key": vkey("stalegap", *([name] if len(sets) > 1 else []), func),
                        "text": f"{tag(name)}{func}: exported, not compared (no KiCad layer re-plots it)"})
    res.append(Result("FAB-STALE-001", FAIL if diffs else PASS,
                      f"{label}: {count(len(diffs), 'Gerber layer')} not matching the current PCB"
                      + (" (re-export them)" if diffs else "")
                      + (f" ({count(len(sets), 'Gerber set')} compared)" if len(sets) > 1 else ""),
                      violations=[{"key": vkey("stale", d.split(':')[0]), "text": d} for d in diffs],
                      evidence=list(files), coverage=coverage("Gerber layers", total, gap)))
    return res


# ---------------------------------------------------------------- drill
# a hole matches when its centre is this close: Excellon in inches with 4 decimals resolves 0.00254 mm,
# and the PCB stores positions to 1 µm, so a real hole is always well inside it
DRILL_TOL_MM = 0.02

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


def _drill_set(board, files, label):
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
        grid = {}
        for d, x, y in got:
            grid.setdefault((d, math.floor(x / DRILL_TOL_MM), math.floor(y / DRILL_TOL_MM)), []).append((x, y))

        def found(d, x, y):
            i, j = math.floor(x / DRILL_TOL_MM), math.floor(y / DRILL_TOL_MM)
            return any(math.hypot(x - gx, y - gy) <= DRILL_TOL_MM
                       for di in (-1, 0, 1) for dj in (-1, 0, 1) for gx, gy in grid.get((d, i + di, j + dj), ()))

        best = None
        for ox, oy in {(0.0, 0.0), board["aux_origin"]}:
            for sy in (-1, 1):
                miss = [(d, x, y) for d, x, y in want if not found(d, x - ox, sy * (y - oy))]
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


def drills(board, files, label):
    if not files:
        return [not_verifiable("FAB-DRILL-001", f"{label}: no drill files", "export the drill files")]
    sets = output_sets(files)
    if len(sets) == 1:
        return [_drill_set(board, files, label)[0]]
    # several exports: each is compared with the PCB on its own (merged, every hole counts twice)
    res = {name: _drill_set(board, fs, label)[0] for name, fs in sets.items()}
    viol = [{"key": vkey(name, v["key"]), "text": f"[{name}] {v['text']}"}
            for name, r in res.items() for v in r.violations]
    gaps = [{"key": vkey(name, u["key"]), "text": f"[{name}] {u['text']}"}
            for name, r in res.items() for u in r.coverage["unchecked"]]
    return [Result("FAB-DRILL-001", FAIL if viol else PASS,
                   f"{label}: {count(len(viol), 'PCB/.drl drill mismatch')} in {count(len(sets), 'drill set')} "
                   "(slots not compared)", violations=viol, evidence=[str(f) for f in files],
                   coverage=coverage("drilled holes", sum(r.coverage["total"] for r in res.values()), gaps))]


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


PART_NO_COLUMNS = ("lcsc", "jlc", "mpn", "part number", "part_number", "partno", "part #", "digi", "dk",
                   "mouser", "farnell", "arrow")


def _part_numbers(row):
    """The row's part-number cells (supplier code or manufacturer part number), or None when the BOM
    has no such column at all."""
    cols = [k for k in row if k and any(c == k.lower().strip() or (len(c) > 2 and c in k.lower())
                                        for c in PART_NO_COLUMNS)]
    return [row[k] or "" for k in cols] if cols else None


def _dnp_row(row):
    """A BOM line that lists parts as not fitted: a DNP / 'Do not populate' column with a mark, or
    'DNP' as the value."""
    for k, v in row.items():
        key, val = (k or "").lower().strip(), (v or "").strip().lower()
        if key in ("dnp", "do not populate", "do_not_populate", "not fitted", "nofit") and val not in ("", "0", "no",
                                                                                                    "false"):
            return True
        if key in ("fitted", "populate", "populated") and val in ("no", "0", "false", "n"):
            return True
        if key in ("value", "comment") and val in ("dnp", "dnf", "nf", "do not populate", "not fitted"):
            return True
    return False


def _bom_check(board, f):
    rows = _rows(f)
    want = _assembled(board)
    fitted = _fitted(board, "exclude_from_bom")
    seen, diffs, warns = {}, [], []
    for r in rows:
        des = _col(r, "designator", "reference", "references", "ref", "designators") or ""
        fpn = (_col(r, "footprint", "package") or "").strip()
        parts = _part_numbers(r)
        dnp = _dnp_row(r)
        for d in _designators(des):
            seen[d] = True
            if dnp:
                if d in want:
                    diffs.append(f"{d}: marked DNP in the BOM but fitted on the PCB")
                continue
            if d not in want:
                if d not in fitted:
                    diffs.append(f"{d}: in the BOM but not assembled on the PCB (or excluded from BOM)")
                continue
            short = want[d]["name"].split(":")[-1]
            if fpn and fpn != short and fpn != want[d]["name"]:
                diffs.append(f"{d}: footprint {fpn} in BOM vs {short} on PCB")
            if parts is not None and not any(x.strip() for x in parts):
                warns.append(f"{d}: no supplier or manufacturer part number")
    for d in want:
        if d not in seen:
            diffs.append(f"{d}: on the PCB but not in the BOM")
    return diffs, warns, len(want)


def _best(files, check):
    """With several candidate files, the one the board agrees with best (fewest mismatches, then
    warnings, then path), and a note naming the others."""
    scored = sorted(((check(f), str(f)) for f in files), key=lambda x: (len(x[0][0]), len(x[0][1]), x[1]))
    (res, chosen), others = scored[0], [Path(s).name for _, s in scored[1:]]
    note = f"; {Path(chosen).name} checked, also found {', '.join(others)} (params.fab chooses one)" if others else ""
    return res, chosen, note


def bom(board, files, label):
    if not files:
        return [not_verifiable("FAB-BOM-001", f"{label}: no BOM", "export the BOM (or set params.fab.bom)")]
    (diffs, warns, nwant), f, note = _best(files, lambda f: _bom_check(board, f))
    vs = [{"key": vkey("bom", d), "text": d} for d in diffs + warns]
    st = FAIL if diffs else (WARN if warns else PASS)
    return [Result("FAB-BOM-001", st,
                   f"{label}: {count(len(diffs), 'BOM/PCB mismatch')}, {count(len(warns), 'warning')}{note}",
                   violations=vs, evidence=[str(f)], coverage=coverage("assembled parts", nwant))]


def _num(s):
    m = re.search(r"-?[\d.]+", s or "")
    return float(m.group(0)) if m else None


def _cpl_check(board, f, tol):
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
    # through-hole by its pads, not by its attribute: a wrong SMD/THT attribute is what drops an SMD part
    # from a placement file, and must stay a mismatch
    tht = {d for d, fp in want.items() if any(p["type"] == "thru_hole" for p in fp["pads"])}
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
    return diffs, warns, len(want)


def cpl(board, files, label, tol):
    if not files:
        return [not_verifiable("FAB-CPL-001", f"{label}: no CPL", "export the placement file (or set params.fab.cpl)")]
    (diffs, warns, nwant), f, note = _best(files, lambda f: _cpl_check(board, f, tol))
    return [Result("FAB-CPL-001", FAIL if diffs else (WARN if warns else PASS),
                   f"{label}: {count(len(diffs), 'CPL/PCB mismatch')}, {count(len(warns), 'warning')} "
                   f"(rotations NOT verified){note}",
                   violations=[{"key": vkey("cpl", d.split(':')[0], d.split(':')[1][:12]), "text": d}
                               for d in diffs + warns],
                   evidence=[str(f)], coverage=coverage("assembled parts", nwant))]


def run(root, pcb, board, label, params, mode):
    g, dr, b, c = find_outputs(root, params, unzip_to=Path(root) / config.DIRNAME / "reports" / "unzipped")
    tol = float((params.get("fab") or {}).get("cpl_tol_mm", 0.5))
    res = []
    if mode == "full":
        res += gerbers(pcb, board, g, label)
    res += drills(board, dr, label)
    if (params.get("fab") or {}).get("assembly", True):
        res += bom(board, b, label)
        res += cpl(board, c, label, tol)
    return res
