"""Seeded-error bank: injects known defects into COPIES of a real fabricated board and measures which
layer catches each one. Requires KiCad (kicad-cli) and, for --reviewer, the `claude` CLI.

  python tests/seeded/run_seeded.py                      # deterministic layer (~10 min)
  python tests/seeded/run_seeded.py --reviewer opus sonnet --only circuit

A defect counts as detected by a layer when that layer reports a new FAIL/WARN finding for the
expected check that was not present on the unmodified board.
"""
import argparse
import json
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE.parent.parent))

import fixtures  # noqa: E402

from kicadverify.cli import analyse, do_review  # noqa: E402

PCB, SCH = "rele-esp12f.kicad_pcb", "rele-esp12f.kicad_sch"
REFERENCE = HERE.parent / "reference" / "rele" / "verification"
FP_SPLIT = re.compile(r"(?m)^(?=\t\(footprint )")


# ------------------------------------------------------------------ text helpers
def fp_edit(text, ref, fn):
    bl = FP_SPLIT.split(text)
    for i, b in enumerate(bl):
        if f'(property "Reference" "{ref}"' in b:
            nb = fn(b)
            if nb is None or nb == b:
                return None
            bl[i] = nb
            return "".join(bl)
    return None


def fp_move(ref, dx, dy):
    def fn(b):
        m = re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+)", b)
        return b.replace(m.group(0), f"\n\t\t(at {float(m.group(1)) + dx:.4f} {float(m.group(2)) + dy:.4f}", 1)
    return lambda t: fp_edit(t, ref, fn)


def fp_swap_pad_nets(ref, a, b):
    def fn(blk):
        def net_of(pad):
            m = re.search(r'\(pad "%s".*?\(net "([^"]*)"\)' % pad, blk, re.S)
            return m
        ma, mb = net_of(a), net_of(b)
        if not ma or not mb:
            return None
        na, nb = ma.group(1), mb.group(1)
        out = re.sub(r'(\(pad "%s".*?\(net ")([^"]*)("\))' % a, lambda m: m.group(1) + "@@A" + m.group(3), blk, count=1, flags=re.S)
        out = re.sub(r'(\(pad "%s".*?\(net ")([^"]*)("\))' % b, lambda m: m.group(1) + na + m.group(3), out, count=1, flags=re.S)
        return out.replace('(net "@@A")', f'(net "{nb}")', 1)
    return lambda t: fp_edit(t, ref, fn)


def sch_block(t, ref):
    for m in re.finditer(r"\n\t\(symbol\n", t):
        i, depth = m.start() + 1, 0
        for j in range(i, len(t)):
            if t[j] == "(":
                depth += 1
            elif t[j] == ")":
                depth -= 1
                if depth == 0:
                    break
        if f'(property "Reference" "{ref}"' in t[i:j + 1]:
            return i, j + 1
    raise KeyError(ref)


def sch_value(ref, old, new, also_pcb=True):
    def sch(t):
        i, j = sch_block(t, ref)
        blk = t[i:j]
        assert f'(property "Value" "{old}"' in blk, (ref, old)
        return t[:i] + blk.replace(f'(property "Value" "{old}"', f'(property "Value" "{new}"', 1) + t[j:]

    def pcb(t):
        return fp_edit(t, ref, lambda b: b.replace(f'(property "Value" "{old}"', f'(property "Value" "{new}"', 1))
    return {SCH: sch, PCB: pcb if also_pcb else None}


def sch_rotate(ref):
    """Rotating a 2-pin symbol by 180 deg swaps its pins on the same wires (verified on D1/D2).
    The PCB is updated consistently, as 'Update PCB from schematic' would do."""
    def sch(t):
        i, j = sch_block(t, ref)
        blk = t[i:j]
        m = re.search(r"\n\t\t\(at ([-\d.]+) ([-\d.]+) ([-\d.]+)\)", blk)
        ang = (float(m.group(3)) + 180) % 360
        return t[:i] + blk.replace(m.group(0), f"\n\t\t(at {m.group(1)} {m.group(2)} {ang:g})", 1) + t[j:]
    return {SCH: sch, PCB: fp_swap_pad_nets(ref, "1", "2")}


def csv_edit(fname, fn):
    return {("csv", fname): fn}


def cpl_offset(lines):
    for i, l in enumerate(lines):
        if l.startswith("U2,"):
            p = l.split(",")
            p[1] = f"{float(p[1].replace('mm', '')) + 3.8:.3f}mm"
            lines[i] = ",".join(p)
            return lines


def drop_line(prefix):
    return lambda lines: [l for l in lines if not l.startswith(prefix) and f",{prefix[:-1]}," not in l]


def add_segment(t):
    seg = ('\t(segment\n\t\t(start 149.0 141.6)\n\t\t(end 153.0 141.6)\n\t\t(width 0.25)\n\t\t(layer "F.Cu")\n'
           '\t\t(net "/RST")\n\t\t(uuid "00000000-0000-4000-8000-000000000001")\n\t)\n')
    i = t.rfind("\t(embedded_fonts")
    return t[:i] + seg + t[i:]


def relax_rules(t):
    """Board rules below the fab's minimum spacing: the DRC stays clean, it just stops guaranteeing it."""
    data = json.loads(t)
    data["board"]["design_settings"]["rules"]["min_clearance"] = 0.1
    for c in data["net_settings"]["classes"]:
        if c["name"] == "Default":
            c["clearance"] = 0.1
    return json.dumps(data, indent=2)


def thin_track(t):
    return t.replace("(width 0.25)", "(width 0.1)", 1)


PRO = "rele-esp12f.kicad_pro"
# (files to edit, expected check, layer group)
MUTATIONS = {
    # layout and fabrication
    "pcb_pin_swap_U1": ({PCB: fp_swap_pad_nets("U1", "2", "3")}, "PCB-PARITY-001", "layout"),
    "footprint_swapped": ({PCB: lambda t: t.replace('"Package_TO_SOT_SMD:SOT-23"', '"Package_TO_SOT_SMD:SOT-23-5"', 1)},
                          "PCB-PARITY-001", "layout"),
    "value_R9_schematic_only": (sch_value("R9", "220", "330", also_pcb=False), "PCB-PARITY-001", "layout"),
    "pinmap_pad_renumbered": ({PCB: lambda t: fp_edit(t, "U1", lambda b: b.replace('(pad "3"', '(pad "4"', 1))},
                              "PCB-PINMAP-001", "layout"),
    "track_in_MH1_keepout": ({PCB: add_segment}, "PCB-KEEPOUT-001", "layout"),
    "gerbers_stale": ({PCB: lambda t: t.replace("(end 156.4 111.6)", "(end 156.4 112.6)", 1)}, "FAB-STALE-001", "fab"),
    "drill_J1_1.3mm": ({PCB: lambda t: fp_edit(t, "J1", lambda b: b.replace("(drill 1.5)", "(drill 1.3)"))},
                       "FAB-DRILL-001", "fab"),
    "MH1_moved_1mm": ({PCB: fp_move("MH1", 1.0, 0)}, "FAB-DRILL-001", "fab"),
    "cpl_body_centre_U2": (csv_edit("CPL_JLCPCB.csv", cpl_offset), "FAB-CPL-001", "fab"),
    "cpl_missing_K1": (csv_edit("CPL_JLCPCB.csv", drop_line("K1,")), "FAB-CPL-001", "fab"),
    "rules_clearance_relaxed": ({PRO: relax_rules}, "FAB-RULES-001", "fab"),
    "track_0.1mm": ({PCB: thin_track}, "FAB-DFM-001", "fab"),
    "bom_missing_C9": (csv_edit("BOM_JLCPCB.csv", lambda ls: [l for l in ls if ",C9," not in l]), "FAB-BOM-001", "fab"),
    # circuit (schematic and PCB updated consistently; parity cannot see them)
    "led_D2_reversed": (sch_rotate("D2"), "CIR-POL-001", "circuit"),
    "flyback_D1_reversed": (sch_rotate("D1"), "CIR-POL-001", "circuit"),
    "regulator_U1_1V8": (sch_value("U1", "AMS1117-3.3", "AMS1117-1.8"), "CIR-REG-001", "circuit"),
    "led_resistor_R9_10R": (sch_value("R9", "220", "10"), "CIR-LED-001", "circuit"),
    "base_resistor_R6_10R": (sch_value("R6", "1k", "10"), "CIR-BJT-001", "circuit"),
    "cap_C1_rated_4V": (sch_value("C1", "470uF 10V", "470uF 4V"), "KH-VD-001", "circuit"),
    "en_pullup_R1_10M": (sch_value("R1", "10k", "10M"), "MOD-VALUE-001", "circuit"),
    "fuse_F1_50A": (sch_value("F1", "T500mA 250V", "T50A 250V"), "MOD-PROT-001", "circuit"),
}


def apply(d, edits):
    for target, fn in edits.items():
        if fn is None:
            continue
        if isinstance(target, tuple):
            f = next(d.rglob(target[1]))
            lines = f.read_text(encoding="utf-8").splitlines(keepends=True)
            new = fn(list(lines))
            if new is None or new == lines:
                return False
            f.write_text("".join(new), encoding="utf-8")
        else:
            f = d / target
            new = fn(f.read_text(encoding="utf-8"))
            if new is None:
                return False
            f.write_text(new, encoding="utf-8")
    return True


def configure(d):
    """Reference verification config (tests/reference/rele): the unmodified board passes the fab gate."""
    shutil.rmtree(d / "verification", ignore_errors=True)
    shutil.copytree(REFERENCE, d / "verification")


def failed_reqs(rep):
    return {v["id"] for v in rep["verification"]["requirements"] if v["status"] == "FAILED"}


def keys(results):
    out = set()
    for r in results:
        st = r["status"] if isinstance(r, dict) else r.status
        cid = r["check"] if isinstance(r, dict) else r.check_id
        vio = r["violations"] if isinstance(r, dict) else r.violations
        if st in ("FAIL", "WARN"):
            out |= {(cid, v["key"]) for v in vio} or {(cid, "*")}
    return out


# what a reviewer finding must mention to count as catching each circuit defect
TARGET = {
    "led_D2_reversed": r"\bD2\b", "flyback_D1_reversed": r"\bD1\b", "regulator_U1_1V8": r"\bU1\b|1\.8 ?V",
    "led_resistor_R9_10R": r"\bR9\b", "base_resistor_R6_10R": r"\bR6\b", "cap_C1_rated_4V": r"\bC1\b",
    "en_pullup_R1_10M": r"\bR1\b|10 ?M", "fuse_F1_50A": r"\bF1\b|50 ?A",
}


def review_texts(results):
    out = []
    for r in results:
        if r.status in ("FAIL", "WARN"):
            out.append((r.status, r.check_id, " ".join([r.detail] + [v["text"] for v in r.violations])))
    return out


def reviewer_hit(name, texts, base_texts):
    pat = re.compile(TARGET.get(name, r"$^"))
    fails = [t for t in texts if t[0] == "FAIL" and pat.search(t[2])]
    if fails and not any(b[0] == "FAIL" and pat.search(b[2]) for b in base_texts):
        return "FAIL", fails[0][1]
    warns = [t for t in texts if pat.search(t[2])]
    if warns and not any(pat.search(b[2]) for b in base_texts):
        return "WARN", warns[0][1]
    return "miss", None


def _texts_from_saved(d):
    t = [(x["status"], x["id"], x.get("summary", "")) for x in d.get("requirements", []) if x["status"] != "PASS"]
    return t + [(e["status"], "REV-EXTRA", e["text"]) for e in d.get("extra_findings", [])]


def rescore(models):
    rev_dir = HERE / "reviews"
    out = HERE / "results.json"
    data = json.loads(out.read_text(encoding="utf-8"))
    for row in data["rows"]:
        for m in models:
            f = rev_dir / f"{row['mutation']}_{m}.json"
            b = rev_dir / f"baseline_{m}.json"
            if f.exists() and b.exists():
                cur = json.loads(f.read_text(encoding="utf-8"))
                base = json.loads(b.read_text(encoding="utf-8"))
                row[f"reviewer_{m}"] = reviewer_hit(row["mutation"], _texts_from_saved(cur), _texts_from_saved(base))
                row[f"reviewer_{m}_cost"] = (cur.get("reviewer") or {}).get("cost_usd")
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in data["rows"]:
        print(f"  {row['mutation']:<24} det={'Y' if row.get('deterministic') else 'n'}"
              + "".join(f" | {m}: {row.get(f'reviewer_{m}')}" for m in models))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reviewer", nargs="*", default=[], help="reviewer models to measure")
    ap.add_argument("--only", choices=["layout", "fab", "circuit"])
    ap.add_argument("--mutations", nargs="*")
    ap.add_argument("--rescore", action="store_true", help="recompute reviewer hits from saved reviews")
    a = ap.parse_args()
    if a.rescore:
        return rescore(a.reviewer or ["opus", "sonnet"])
    for s in (sys.stdout, sys.stderr):
        s.reconfigure(encoding="utf-8")
    src = fixtures.rele_board()
    rows = []
    out = HERE / "results.json"
    prev = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    with tempfile.TemporaryDirectory() as td:
        base = fixtures.copy_of(src, Path(td) / "base")
        configure(base)
        t0 = time.time()
        rep0, _ = analyse(base, "full")
        k0 = keys(rep0["results"])
        f0 = failed_reqs(rep0)
        fab0 = rep0["verification"]["gates"]["fab"]["pass"]
        print(f"baseline: {rep0['overall']}, fab gate {'PASS' if fab0 else 'BLOCKED'}, FAILED {sorted(f0)} "
              f"in {time.time() - t0:.0f} s", flush=True)
        rev_dir = HERE / "reviews"
        rbase = {}
        rev_dir = HERE / "reviews"
        rev_dir.mkdir(exist_ok=True)
        for m in a.reviewer:
            res, rr, _ = do_review(base, m, rep=rep0, ctx=analyse(base, "full")[1])
            rbase[m] = review_texts(res)
            (rev_dir / f"baseline_{m}.json").write_text(json.dumps(rr, indent=1, ensure_ascii=False), encoding="utf-8")
            print(f"baseline review {m}: {[(r.check_id, r.status) for r in res if r.status != 'PASS']} "
                  f"${(rr or {}).get('reviewer', {}).get('cost_usd')}", flush=True)
        for name, (edits, expect, group) in MUTATIONS.items():
            if (a.only and group != a.only) or (a.mutations and name not in a.mutations):
                continue
            d = fixtures.copy_of(base, Path(td) / name)
            configure(d)
            if not apply(d, edits):
                rows.append({"mutation": name, "group": group, "expected": expect, "applied": False})
                continue
            rep, ctx = analyse(d, "full")
            found = sorted({c for c, _ in keys(rep["results"]) - k0})
            row = {"mutation": name, "group": group, "expected": expect, "applied": True,
                   "deterministic": expect in found, "deterministic_checks": found,
                   "requirements_failed": sorted(failed_reqs(rep) - f0),
                   "fab_gate_blocked": not rep["verification"]["gates"]["fab"]["pass"]}
            for m in a.reviewer:
                res, rr, _ = do_review(d, m, rep=rep, ctx=ctx)
                (rev_dir / f"{name}_{m}.json").write_text(json.dumps(rr, indent=1, ensure_ascii=False), encoding="utf-8")
                rf = reviewer_hit(name, review_texts(res), rbase[m])
                row[f"reviewer_{m}"] = rf
                row[f"reviewer_{m}_cost"] = (rr or {}).get("reviewer", {}).get("cost_usd")
            rows.append(row)
            print(f"  {name:<24} {expect:<15} det={'Y' if row['deterministic'] else 'n'} "
                  f"fab={'BLOCKED' if row['fab_gate_blocked'] else 'pass'} {row['requirements_failed']} {found}"
                  + "".join(f" | {m}: {row[f'reviewer_{m}']}" for m in a.reviewer), flush=True)
    merged = {r["mutation"]: r for r in prev.get("rows", [])} if isinstance(prev, dict) else {}
    for r in rows:
        merged[r["mutation"]] = {**merged.get(r["mutation"], {}), **r}
    out.write_text(json.dumps({"fixture": f"smartRele@{fixtures.COMMIT[:8]}", "baseline_fab_gate": fab0,
                               "baseline_failed": sorted(f0), "rows": list(merged.values())},
                              indent=2, ensure_ascii=False), encoding="utf-8")
    applied = [r for r in rows if r.get("applied")]
    print(f"deterministic: {sum(r['deterministic'] for r in applied)}/{len(applied)}")


if __name__ == "__main__":
    main()
