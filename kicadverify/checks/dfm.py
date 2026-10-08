"""Manufacturability against a target fab capability profile.

FAB-RULES-001: copper-to-copper spacing is not measured here; it is guaranteed by the KiCad DRC only
when the board rules are at least as strict as the fab's minimum spacing. KiCad always enforces the
board minimum clearance (Board Setup > Constraints) on top of net classes and custom rules, so the
spacing is guaranteed when that minimum >= the fab's, or when every net-class and custom-rule
clearance is.

FAB-DFM-001: everything that can be measured directly on the .kicad_pcb is measured: track width,
via drill/pad/annular ring, plated-hole drill and annular ring, hole-to-hole distance, copper to
board edge (tracks, vias, pads, filled zones), board size, layer count and thickness. A limit
missing from the profile is reported as an unchecked item, never assumed.
"""
import json
import math
import re
from pathlib import Path

from .. import config
from ..report import FAIL, PASS, Result, count, coverage, not_verifiable
from ..waivers import vkey

PROFILE_FILE = config.DATA / "fab_profiles.yaml"
# a measured length is outside a limit only by more than 0.1 mil (2.54 µm): fab limits are stated in
# mil or mm and converted (4 mil = 0.1016 mm against a 0.1 mm annular ring), and no CAM resolves less
TOL_MM = 0.00254
MEASURED = {
    "layers_max": "copper layer count", "thickness_mm": "board thickness", "board_max_mm": "board size (max)",
    "board_min_mm": "board size (min)", "min_track_mm": "track width", "min_via_drill_mm": "via drill",
    "min_via_diameter_mm": "via pad diameter", "min_via_annular_mm": "via annular ring",
    "min_pth_drill_mm": "plated hole drill", "min_pth_annular_mm": "plated hole annular ring",
    "max_drill_mm": "maximum drill", "min_hole_to_hole_mm": "hole-to-hole distance",
    "min_copper_to_edge_mm": "copper to board edge",
}


def builtin_profiles():
    return (config.load_yaml(PROFILE_FILE, {}) or {}).get("profiles", {})


def resolve_profile(params):
    """(profile dict with 'name' and 'source', or None, error text)."""
    spec = (params.get("fab") or {}).get("profile")
    if not spec:
        return None, "no target fab profile (params.fab.profile); `kicadverify profiles` lists the built-in ones"
    built = builtin_profiles()
    if isinstance(spec, str):
        if spec not in built:
            return None, f"unknown fab profile {spec!r} (built-in: {', '.join(built)})"
        return {"name": spec, **built[spec]}, None
    if isinstance(spec, dict):
        base = built.get(spec.get("base"), {}) if spec.get("base") else {}
        if spec.get("base") and not base:
            return None, f"unknown base profile {spec['base']!r}"
        src = {**(base.get("source") or {}), **(spec.get("source") or {})}
        prof = {**base, **spec, "source": src}
        prof["name"] = spec.get("name") or (f"{spec['base']} (project override)" if spec.get("base") else "project")
        if not src.get("ref"):
            prof["source"] = {"kind": "fab_capability", "ref": "inline profile without a source", "confirmed": False}
        return prof, None
    return None, "params.fab.profile must be a profile name or a dict"


def apply_profile_source(reqs, params):
    """The fab profile is the source of FAB-RULES-001 and FAB-DFM-001: copy its citation into them."""
    prof, _ = resolve_profile(params)
    if not prof:
        return
    for r in reqs:
        if r["id"] in ("FAB-RULES-001", "FAB-DFM-001") and (r.get("source") or {}).get("kind") == "fab_capability":
            r["source"] = {**prof["source"], "profile": prof["name"]}


# ------------------------------------------------------------------ FAB-RULES-001
_DRU_CLEARANCE = re.compile(r"\(rule\s+\"?([^\"\n)]*)\"?.*?\(constraint\s+clearance\s+\(min\s+([\d.]+)\s*(mm|mil)?\)",
                            re.S)


def _dru_clearances(dru_text):
    out = []
    for block in re.split(r"(?=\(rule\s)", dru_text):
        m = _DRU_CLEARANCE.search(block)
        if m:
            v = float(m.group(2)) * (0.0254 if m.group(3) == "mil" else 1)
            out.append((m.group(1).strip(), v))
    return out


def rules(pro, label, prof):
    cid = "FAB-RULES-001"
    if prof is None:
        return []
    spacing = prof.get("min_spacing_mm")
    if spacing is None:
        return [not_verifiable(cid, f"{label}: fab profile {prof['name']} has no min_spacing_mm")]
    pro = Path(pro)
    if not pro.exists():
        return [not_verifiable(cid, f"{label}: no .kicad_pro, board rules unknown")]
    data = json.loads(pro.read_text(encoding="utf-8"))
    rules_ = ((data.get("board") or {}).get("design_settings") or {}).get("rules") or {}
    board_min = rules_.get("min_clearance")
    classes = [(c.get("name"), c.get("clearance")) for c in (data.get("net_settings") or {}).get("classes") or []]
    dru = pro.with_suffix(".kicad_dru")
    custom = _dru_clearances(dru.read_text(encoding="utf-8")) if dru.exists() else []
    evidence = [pro] + ([dru] if dru.exists() else [])
    n = 1 + len(classes) + len(custom)
    if board_min is not None and board_min >= spacing - TOL_MM:
        return [Result(cid, PASS, f"{label}: board minimum clearance {board_min} mm >= fab {spacing} mm "
                       "(always enforced by DRC)", evidence=evidence,
                       coverage=coverage("clearance rules", n))]
    bad = [f"net class {name}: clearance {c} mm < fab minimum spacing {spacing} mm"
           for name, c in classes if c is not None and c < spacing - TOL_MM]
    bad += [f"custom rule '{name}': clearance {c} mm < fab minimum spacing {spacing} mm"
            for name, c in custom if c < spacing - TOL_MM]
    head = (f"{label}: board minimum clearance {board_min} mm < fab {spacing} mm"
            if board_min is not None else f"{label}: board minimum clearance not set")
    if bad:
        return [Result(cid, FAIL, f"{head}; {count(len(bad), 'rule')} letting DRC accept spacing the fab cannot make",
                       evidence=evidence, coverage=coverage("clearance rules", n),
                       violations=[{"key": vkey("rules", b.split(':')[0]), "text": b} for b in bad])]
    return [Result(cid, PASS, f"{head}, but every net-class and custom clearance is >= {spacing} mm",
                   evidence=evidence, coverage=coverage("clearance rules", n))]


# ------------------------------------------------------------------ geometry helpers
def _pt_seg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def _cross(o, a, b):
    return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])


def _intersect(p1, p2, q1, q2):
    d1, d2 = _cross(q1, q2, p1), _cross(q1, q2, p2)
    d3, d4 = _cross(p1, p2, q1), _cross(p1, p2, q2)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def seg_seg(p1, p2, q1, q2):
    if _intersect(p1, p2, q1, q2):
        return 0.0
    return min(_pt_seg(p1, q1, q2), _pt_seg(p2, q1, q2), _pt_seg(q1, p1, p2), _pt_seg(q2, p1, p2))


def point_in_poly(p, poly):
    inside = False
    for i in range(len(poly)):
        a, b = poly[i], poly[(i + 1) % len(poly)]
        if (a[1] > p[1]) != (b[1] > p[1]):
            x = a[0] + (p[1] - a[1]) * (b[0] - a[0]) / (b[1] - a[1])
            if x > p[0]:
                inside = not inside
    return inside


def _rot(x, y, deg):
    a = math.radians(deg)
    return (x * math.cos(a) + y * math.sin(a), -x * math.sin(a) + y * math.cos(a))


def pad_shape(p):
    """('capsule', a, b, r) or ('poly', [points]) in board coordinates."""
    w, h = p["size"]
    cx, cy = p["x"], p["y"]
    if p.get("shape") == "circle" or (p.get("shape") == "oval" and abs(w - h) < 1e-9):
        return ("capsule", (cx, cy), (cx, cy), w / 2)
    if p.get("shape") == "oval":
        L = abs(w - h) / 2
        dx, dy = _rot(L, 0, p.get("angle", 0)) if w > h else _rot(0, L, p.get("angle", 0))
        return ("capsule", (cx - dx, cy - dy), (cx + dx, cy + dy), min(w, h) / 2)
    corners = [_rot(sx * w / 2, sy * h / 2, p.get("angle", 0)) for sx, sy in ((-1, -1), (1, -1), (1, 1), (-1, 1))]
    return ("poly", [(cx + x, cy + y) for x, y in corners])


def hole_shape(p):
    """('capsule', a, b, r) of a pad's drill: a slot is the capsule of its oval, not a circle."""
    w, h = p["drill"], p["drill2"] or p["drill"]
    return pad_shape({"x": p["x"], "y": p["y"], "size": (w, h), "shape": "oval", "angle": p.get("angle", 0)})


def _thermal_via(fp, p):
    """A plated hole of a footprint that sits inside an SMD pad with the same number: the via array
    of an exposed pad (ESP32 modules, QFN thermal pads), not a through-hole lead."""
    if p["type"] != "thru_hole" or p.get("drill2") not in (None, p["drill"]):
        return False
    for q in fp["pads"]:
        if q["type"] == "smd" and q["number"] == p["number"]:
            shape = pad_shape(q)
            if shape[0] == "poly" and point_in_poly((p["x"], p["y"]), shape[1]):
                return True
            if shape[0] == "capsule" and _pt_seg((p["x"], p["y"]), shape[1], shape[2]) <= shape[3]:
                return True
    return False


def _bbox(shape):
    if shape[0] == "capsule":
        _, a, b, r = shape
        return (min(a[0], b[0]) - r, min(a[1], b[1]) - r, max(a[0], b[0]) + r, max(a[1], b[1]) + r)
    pts = shape[1]
    return (min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts))


class EdgeIndex:
    """Uniform grid over the board-edge segments, so each copper segment is compared only with the
    edges near it (zone fills have thousands of vertices)."""

    def __init__(self, edges, cell=2.0):
        self.edges, self.cell, self.grid = edges, cell, {}
        for i, (a, b) in enumerate(edges):
            for key in self._cells(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])):
                self.grid.setdefault(key, []).append(i)

    def _cells(self, x0, y0, x1, y1):
        c = self.cell
        return [(i, j) for i in range(int(x0 // c), int(x1 // c) + 1) for j in range(int(y0 // c), int(y1 // c) + 1)]

    def near(self, x0, y0, x1, y1, margin):
        ids = set()
        for key in self._cells(x0 - margin, y0 - margin, x1 + margin, y1 + margin):
            ids.update(self.grid.get(key, ()))
        return [self.edges[i] for i in ids]


def _segments(shape):
    if shape[0] == "capsule":
        return [(shape[1], shape[2])], shape[3]
    pts = shape[1]
    return [(pts[i], pts[(i + 1) % len(pts)]) for i in range(len(pts))], 0.0


def edge_distance(shape, edges, index=None, margin=None):
    """Smallest distance from a copper shape to the board-edge segments (0 when they touch). With an
    index, only edges within `margin` of each copper segment are compared; farther ones count as
    `inf`, which is all a threshold check needs."""
    segs, r = _segments(shape)
    if shape[0] == "poly" and len(shape[1]) <= 64:  # a board edge inside a pad polygon touches it
        bb = _bbox(shape)
        cand = index.near(*bb, 0.0) if index else edges
        for e1, _ in cand:
            if bb[0] <= e1[0] <= bb[2] and bb[1] <= e1[1] <= bb[3] and point_in_poly(e1, shape[1]):
                return 0.0
    best = math.inf
    for p1, p2 in segs:
        if index:
            cand = index.near(min(p1[0], p2[0]), min(p1[1], p2[1]), max(p1[0], p2[0]), max(p1[1], p2[1]),
                              (margin or 0.0) + r)
        else:
            cand = edges
        for e1, e2 in cand:
            best = min(best, seg_seg(p1, p2, e1, e2) - r)
    return max(best, 0.0)


def _on_copper(p):
    return any(ly.endswith(".Cu") for ly in p.get("layers") or [])


# ------------------------------------------------------------------ FAB-DFM-001
def geometry(board, label, prof, pcb=None):
    cid = "FAB-DFM-001"
    if prof is None:
        return []
    viol, unchecked, measured = [], [], 0

    def lim(key):
        if prof.get(key) is None:
            unchecked.append({"key": f"profile-{key}", "text": f"{MEASURED[key]}: no {key} in profile {prof['name']}"})
            return None
        return prof[key]

    def bad(kind, ident, text):
        viol.append({"key": vkey("dfm", kind, ident), "text": text})

    ncu = len(board["copper_layers"]) or 2
    if (v := lim("layers_max")) is not None:
        measured += 1
        if ncu > v:
            bad("layers", ncu, f"{ncu} copper layers > {v} in profile {prof['name']}")
    if (v := lim("thickness_mm")) is not None:
        measured += 1
        if not (v[0] - 1e-9 <= board["thickness"] <= v[1] + 1e-9):
            bad("thickness", board["thickness"], f"board thickness {board['thickness']} mm outside {v[0]}..{v[1]} mm")
    bb = board["outline_bbox"]
    dims = sorted([bb[2] - bb[0], bb[3] - bb[1]]) if bb else None
    for key, cmp_ in (("board_max_mm", lambda d, m: d[0] > m[0] + 1e-9 or d[1] > m[1] + 1e-9),
                      ("board_min_mm", lambda d, m: d[0] < m[0] - 1e-9 or d[1] < m[1] - 1e-9)):
        if (v := lim(key)) is not None:
            if dims is None:
                unchecked.append({"key": "no-outline", "text": "board size: no Edge.Cuts outline"})
                continue
            measured += 1
            if cmp_(dims, sorted(v)):
                bad("size", key, f"board {dims[0]:.2f} x {dims[1]:.2f} mm outside the {MEASURED[key]} {v} mm")

    if (v := lim("min_track_mm")) is not None:
        thin = {}
        for t in board["tracks"]:
            measured += 1
            if t["width"] < v - TOL_MM:
                thin[t["net"]] = min(t["width"], thin.get(t["net"], 99))
        for net, w in thin.items():
            bad("track", net, f"net {net}: track {w} mm < {v} mm")
    vd, vdia, vring = lim("min_via_drill_mm"), lim("min_via_diameter_mm"), lim("min_via_annular_mm")
    for via in board["vias"]:
        if not via["drill"]:
            continue
        measured += 1
        where = f"via {via['net']} at ({via['pos'][0]:.2f}, {via['pos'][1]:.2f})" if via["pos"] else f"via {via['net']}"
        if vd is not None and via["drill"] < vd - TOL_MM:
            bad("viadrill", via["drill"], f"{where}: drill {via['drill']} mm < {vd} mm")
        if vdia is not None and via["size"] < vdia - TOL_MM:
            bad("viadia", via["size"], f"{where}: diameter {via['size']} mm < {vdia} mm")
        ring = (via["size"] - via["drill"]) / 2
        if vring is not None and ring < vring - TOL_MM:
            bad("viaring", f"{via['size']}/{via['drill']}", f"{where}: annular ring {ring:.3f} mm < {vring} mm")
    pd, pring, dmax = lim("min_pth_drill_mm"), lim("min_pth_annular_mm"), lim("max_drill_mm")
    holes = []
    for fp in board["footprints"]:
        for p in fp["pads"]:
            if not p["drill"]:
                continue
            holes.append((hole_shape(p), f"{fp['ref']}.{p['number']}"))
            measured += 1
            name = f"{fp['ref']}.{p['number'] or '(np)'}"
            if dmax is not None and max(p["drill"], p["drill2"] or 0) > dmax + TOL_MM:
                bad("dmax", name, f"{name}: drill {max(p['drill'], p['drill2'] or 0)} mm > {dmax} mm")
            if p["type"] != "thru_hole":
                continue
            if _thermal_via(fp, p):  # the fab drills and plates it as a via, so via limits apply
                if vd is not None and p["drill"] < vd - TOL_MM:
                    bad("viadrill", name, f"{name}: thermal via drill {p['drill']} mm < {vd} mm")
                ring = (min(p["size"]) - p["drill"]) / 2
                if vring is not None and ring < vring - TOL_MM:
                    bad("viaring", name, f"{name}: thermal via annular ring {ring:.3f} mm < {vring} mm")
                continue
            if pd is not None and min(p["drill"], p["drill2"] or p["drill"]) < pd - TOL_MM:
                bad("pthdrill", name, f"{name}: plated drill {p['drill']} mm < {pd} mm")
            ring = min(p["size"][0] - p["drill"], p["size"][1] - (p["drill2"] or p["drill"])) / 2
            if pring is not None and ring < pring - TOL_MM:
                bad("pthring", name, f"{name}: annular ring {ring:.3f} mm < {pring} mm "
                                     f"(pad {p['size'][0]}x{p['size'][1]}, drill {p['drill']})")
    holes += [(("capsule", tuple(v_["pos"]), tuple(v_["pos"]), v_["drill"] / 2), f"via {v_['net']}")
              for v_ in board["vias"] if v_["drill"] and v_["pos"]]
    if (v := lim("min_hole_to_hole_mm")) is not None:
        cell = {}
        # a hole is compared with the cells around its centre, so a cell spans the largest hole extent
        reach = max((math.dist(h[0][1], h[0][2]) / 2 + h[0][3] for h in holes), default=0.5)
        size = max(1.0, 2 * reach + v)
        centre = [((h[0][1][0] + h[0][2][0]) / 2, (h[0][1][1] + h[0][2][1]) / 2) for h in holes]
        for i, (x, y) in enumerate(centre):
            cell.setdefault((int(x // size), int(y // size)), []).append(i)
        for i, ((_, a, b, r), name) in enumerate(holes):
            x, y = centre[i]
            cx, cy = int(x // size), int(y // size)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for j in cell.get((cx + dx, cy + dy), []):
                        if j <= i:
                            continue
                        (_, a2, b2, r2), n2 = holes[j]
                        x2, y2 = centre[j]
                        d = seg_seg(a, b, a2, b2) - r - r2
                        if d < v - TOL_MM and not (abs(x - x2) < 1e-6 and abs(y - y2) < 1e-6):
                            bad("h2h", f"{name}|{n2}", f"{name} and {n2}: {max(d, 0):.3f} mm hole to hole < {v} mm")
    if (v := lim("min_copper_to_edge_mm")) is not None:
        edges = board.get("edge_segments") or []
        if not edges:
            unchecked.append({"key": "no-outline-edge", "text": "copper to edge: no Edge.Cuts outline"})
        else:
            shapes = [(("capsule", t["start"], t["end"], t["width"] / 2), f"track {t['net']} ({t['layer']})")
                      for t in board["tracks"] if t["start"] and t["end"]]
            shapes += [(("capsule", via["pos"], via["pos"], via["size"] / 2), f"via {via['net']}")
                       for via in board["vias"] if via["pos"]]
            shapes += [(pad_shape(p), f"pad {fp['ref']}.{p['number']}") for fp in board["footprints"]
                       for p in fp["pads"] if p["type"] != "np_thru_hole" and _on_copper(p)]
            shapes += [(("poly", z["pts"]), f"zone {z['net']} ({z['layer']})") for z in board.get("zone_fills", [])
                       if len(z["pts"]) >= 3]
            worst = {}
            index = EdgeIndex(edges)
            for shape, name in shapes:
                measured += 1
                d = edge_distance(shape, edges, index, v)
                if d < v - TOL_MM:
                    worst[name] = min(d, worst.get(name, math.inf))
            for name, d in worst.items():
                bad("edge", name, f"{name}: {d:.3f} mm from the board edge < {v} mm")
    st = FAIL if viol else PASS
    # the same finding repeated (every via of a thermal-pad array): one line with its count
    grouped = {}
    for v in viol:
        g = grouped.setdefault((v["key"], v["text"]), {**v, "n": 0})
        g["n"] += 1
    viol = [{"key": g["key"], "text": g["text"] + (f" (x{g['n']})" if g["n"] > 1 else "")}
            for g in grouped.values()]
    return [Result(cid, st, f"{label}: {count(len(viol), 'geometry item')} outside fab profile {prof['name']}",
                   violations=viol, evidence=[pcb] if pcb else [],
                   coverage=coverage("measured items", measured + len(unchecked), unchecked,
                                     checked=measured))]


def run(board, pro, label, params, pcb=None):
    prof, err = resolve_profile(params)
    if prof is None:
        return [not_verifiable("FAB-RULES-001", f"{label}: {err}", "choose the target fab: params.fab.profile"),
                not_verifiable("FAB-DFM-001", f"{label}: {err}", "choose the target fab: params.fab.profile")]
    return rules(pro, label, prof) + geometry(board, label, prof, pcb)
