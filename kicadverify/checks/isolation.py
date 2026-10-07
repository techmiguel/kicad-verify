"""ISO-SEP-001: copper separation across isolation barriers (mains to low voltage, between mains
conductors, primary to secondary...).

Each barrier is declared by the project with the distance its standard requires and the source of
that number; kicad-verify ships no normative values:

    params:
      isolation:
        barriers:
          - name: mains to SELV
            a: {netclass: MAINS}            # or nets: ["/L_IN", "~/?N"]
            b: {not: a}                     # default: every other net (and pads with no net)
            required_mm: 4.0                # outer layers
            required_inner_mm: 2.0          # optional, inner layers (default: required_mm)
            source: {kind: regulatory, ref: "IEC 62368-1 Table ..., reinforced, 250 V, PD2, IIIb"}

What is measured: the smallest copper-to-copper distance between group A and group B on each copper
layer (tracks, vias, pads, zone fills). On one surface any path along the board between two copper
features is at least as long as the straight line, so a measured distance >= required means both
the surface (creepage) and the through-air (clearance) distance of the PCB copper meet it.

What is not: component bodies and leads above the board, transformer and relay internals, distance
through the insulation between layers, and creepage around slots. When a barrier is not met by the
straight-line distance and the board has cutouts or slots, the gap may still be met around them, so
the result is an unchecked item (NOT_VERIFIABLE), never a FAIL.
"""
import fnmatch
import json
import math
import re
from pathlib import Path

from ..report import FAIL, PASS, Result, count, coverage, not_verifiable
from ..waivers import vkey
from .dfm import pad_shape, seg_seg

CELL = 2.0


def netclasses(pro):
    """{net name: netclass} from .kicad_pro assignments and wildcard patterns."""
    try:
        ns = json.loads(Path(pro).read_text(encoding="utf-8")).get("net_settings") or {}
    except (OSError, ValueError):
        return {}, []
    explicit = ns.get("netclass_assignments") or {}
    if isinstance(explicit, list):
        explicit = {e.get("net"): e.get("netclass") for e in explicit if isinstance(e, dict)}
    patterns = [(p.get("pattern", ""), p.get("netclass")) for p in ns.get("netclass_patterns") or []]
    return explicit, patterns


def netclass_of(net, explicit, patterns):
    if net in explicit:
        return explicit[net]
    for pat, cls in patterns:
        if fnmatch.fnmatchcase(net, pat) or fnmatch.fnmatchcase(net.lstrip("/"), pat.lstrip("/")):
            return cls
    return "Default"


def _match_net(spec, net):
    s = str(spec)
    if s.startswith("~"):
        return re.fullmatch(s[1:], net or "", re.I) is not None
    return (net or "").lstrip("/").upper() == s.lstrip("/").upper()


def select(group, nets, explicit, patterns, other=None):
    """Nets of one side of a barrier. `{not: a}` (or nothing) means every net not in `other`."""
    group = group or {"not": "a"}
    if group.get("not"):
        return {n for n in nets if n not in (other or set())}
    out = set()
    for n in nets:
        if group.get("netclass") and netclass_of(n, explicit, patterns) in (
                [group["netclass"]] if isinstance(group["netclass"], str) else group["netclass"]):
            out.add(n)
        if any(_match_net(s, n) for s in group.get("nets") or []):
            out.add(n)
    return out


def copper(board):
    """[(layer, net, segments, radius, label)] for every copper feature; a polygon is its edge
    segments with radius 0."""
    cu = board["copper_layers"] or ["F.Cu", "B.Cu"]
    out = []
    for t in board["tracks"]:
        if t["start"] and t["end"]:
            out.append((t["layer"], t["net"] or "", [(t["start"], t["end"])], t["width"] / 2, f"track {t['net']}"))
    for v in board["vias"]:
        if v["pos"]:
            for layer in cu:
                out.append((layer, v["net"] or "", [(v["pos"], v["pos"])], v["size"] / 2, f"via {v['net']}"))
    for fp in board["footprints"]:
        for p in fp["pads"]:
            if p["type"] == "np_thru_hole":
                continue
            layers = [ly for ly in p["layers"] if ly.endswith(".Cu")]
            if any(ly.startswith("*") for ly in layers):
                layers = cu
            shape = pad_shape(p)
            if shape[0] == "capsule":
                segs, r = [(shape[1], shape[2])], shape[3]
            else:
                pts = shape[1]
                segs, r = [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], 0.0
            for layer in layers:
                out.append((layer, p["net"] or "", segs, r, f"pad {fp['ref']}.{p['number']}"))
    for z in board.get("zone_fills", []):
        pts = z["pts"]
        if len(pts) >= 3:
            out.append((z["layer"], z["net"] or "", [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))],
                        0.0, f"zone {z['net']}"))
    return out


def _cells(x0, y0, x1, y1):
    return [(i, j) for i in range(int(x0 // CELL), int(x1 // CELL) + 1)
            for j in range(int(y0 // CELL), int(y1 // CELL) + 1)]


def min_distance(feats_a, feats_b, limit):
    """Smallest distance between any feature of A and any of B, searched up to `limit`; returns
    (distance, label a, label b) or (inf, None, None) when nothing is closer than `limit`."""
    grid = {}
    for k, (_, _, segs, r, _) in enumerate(feats_b):
        for si, (p, q) in enumerate(segs):
            bb = (min(p[0], q[0]) - r, min(p[1], q[1]) - r, max(p[0], q[0]) + r, max(p[1], q[1]) + r)
            for c in _cells(*bb):
                grid.setdefault(c, []).append((k, si))
    best = (math.inf, None, None)
    for _, _, segs, r, label in feats_a:
        for p, q in segs:
            bb = (min(p[0], q[0]) - r - limit, min(p[1], q[1]) - r - limit,
                  max(p[0], q[0]) + r + limit, max(p[1], q[1]) + r + limit)
            seen = set()
            for c in _cells(*bb):
                for k, si in grid.get(c, ()):
                    if (k, si) in seen:
                        continue
                    seen.add((k, si))
                    _, _, segs_b, rb, label_b = feats_b[k]
                    a2, b2 = segs_b[si]
                    d = seg_seg(p, q, a2, b2) - r - rb
                    if d < best[0]:
                        best = (max(d, 0.0), label, label_b)
    return best


def outline_loops(edges, tol=0.01):
    """Edge.Cuts segments grouped into connected loops (by shared endpoints)."""
    parent = {}

    def key(pt):
        return (round(pt[0] / tol), round(pt[1] / tol))

    def find(x):
        while parent.setdefault(x, x) != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for a, b in edges:
        ra, rb = find(key(a)), find(key(b))
        if ra != rb:
            parent[ra] = rb
    loops = {}
    for a, b in edges:
        loops.setdefault(find(key(a)), []).append((a, b))
    return list(loops.values())


def has_cutouts(board):
    """Interior Edge.Cuts loops (cutouts, routed slots) or non-plated slotted holes: creepage could run
    around them. Oval drills of plated pads are not isolation slots."""
    if any(p["type"] == "np_thru_hole" and p.get("drill2") and p["drill2"] != p["drill"]
           for fp in board["footprints"] for p in fp["pads"]):
        return True
    return len(outline_loops(board.get("edge_segments") or [])) > 1


def run(board, pro, label, params, pcb=None):
    cid = "ISO-SEP-001"
    barriers = (params.get("isolation") or {}).get("barriers") or []
    if not barriers:
        return [not_verifiable(cid, f"{label}: no isolation barriers declared (params.isolation.barriers)",
                               "declare each barrier with its required distance and source, or exclude the "
                               "requirement with a reason (e.g. no hazardous voltage on the board)")]
    explicit, patterns = netclasses(pro)
    feats = copper(board)
    nets = {f[1] for f in feats}
    cutouts = has_cutouts(board)
    outer = {"F.Cu", "B.Cu"}
    bad, gaps, ok, n = [], [], [], 0
    for i, br in enumerate(barriers):
        name = br.get("name") or f"barrier {i + 1}"
        req = br.get("required_mm")
        if req is None:
            gaps.append({"key": vkey("iso", name, "req"), "text": f"{name}: no required_mm"})
            continue
        a = select(br.get("a"), nets, explicit, patterns)
        b = select(br.get("b"), nets, explicit, patterns, other=a) - a
        if not a or not b:
            gaps.append({"key": vkey("iso", name, "empty"),
                         "text": f"{name}: side {'A' if not a else 'B'} selects no net on the board"})
            continue
        for layer in sorted({f[0] for f in feats}):
            need = float(req if layer in outer else br.get("required_inner_mm", req))
            fa = [f for f in feats if f[0] == layer and f[1] in a]
            fb = [f for f in feats if f[0] == layer and f[1] in b]
            if not fa or not fb:
                continue
            n += 1
            d, la, lb = min_distance(fa, fb, need)
            where = f"{name} on {layer}"
            if d >= need - 1e-9:
                ok.append(f"{where}: >= {need} mm")
            elif cutouts:
                gaps.append({"key": vkey("iso", name, layer),
                             "text": f"{where}: {d:.2f} mm straight line ({la} to {lb}) < {need} mm; the board "
                                     "has cutouts or slots, so creepage around them was not computed"})
            else:
                bad.append({"key": vkey("iso", name, layer),
                            "text": f"{where}: {d:.2f} mm between {la} and {lb} < required {need} mm"})
    return [Result(cid, FAIL if bad else PASS,
                   f"{label}: {count(len(bad), 'barrier/layer pair')} below the required separation, {len(ok)} met",
                   violations=bad, evidence=[pcb, pro] if pcb else [],
                   coverage=coverage("barrier/layer pairs", n + len(gaps), gaps, checked=n))]


def apply_sources(reqs, params):
    """ISO-SEP-001 cites the sources of its barriers; one unconfirmed or missing source leaves it unconfirmed."""
    barriers = (params.get("isolation") or {}).get("barriers") or []
    if not barriers:
        return
    srcs = [b.get("source") or {} for b in barriers]
    refs = [f"{b.get('name', 'barrier')}: {s.get('ref', 'NO SOURCE')}" for b, s in zip(barriers, srcs, strict=True)]
    confirmed = all(s.get("ref") and s.get("confirmed", True) is not False for s in srcs)
    kinds = sorted({s.get("kind") for s in srcs if s.get("kind")})
    for r in reqs:
        if r["id"] == "ISO-SEP-001":
            r["source"] = {"kind": kinds[0] if len(kinds) == 1 else "regulatory", "ref": "; ".join(refs),
                           **({} if confirmed else {"confirmed": False,
                                                    "note": "a barrier has no source or an unconfirmed one"})}
