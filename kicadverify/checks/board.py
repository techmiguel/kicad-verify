"""Board checks from a direct parse of .kicad_pcb (independent of kicad-cli and of any MCP server
that generated the design)."""
import math
import re
from pathlib import Path

from .. import sexp
from ..config import DATA
from ..report import FAIL, NOT_VERIFIABLE, PASS, WARN, Result, count, coverage, not_verifiable
from ..waivers import vkey

POWER_RE = re.compile(r"(^|/)(\+|V)(\d|CC|DD|BUS|IN|BAT|SYS)|GND|VBAT|VMOT|VSYS", re.I)
# a word after the rail name that makes it a control or status signal of that rail: VBUS_EN, VIN_SENSE
# (matched on the last path component, so a sheet named /Sense/ does not hide its rails)
SIGNAL_RE = re.compile(r"[_\-.](N?EN|ENABLE|N?FAULT|N?FLT|PG|PGOOD|N?OK|SENSE|SNS|DET|DETECT|ADC|MON|STAT|STATUS"
                       r"|CTRL|CTL|ALERT|DIV)([_\-.]|$)", re.I)
# footprints with no physical body to model, matched case-insensitively anywhere in the footprint name
# (KiCad's Symbol:OSHW-Logo..., a project's LIBRESOLAR_LOGO, a Tag-Connect cable footprint)
NO_MODEL_OK = ("testpoint", "mountinghole", "fiducial", "nettie", "logo", "symbol:", "solderjumper",
               "solderwirepad", "tag-connect", "layermarker")


def needs_model(fp):
    """A footprint that places a body on the board: it has pads, is not board-only, and its name is
    not one of the body-less kinds above."""
    name = fp["name"].lower()
    return bool(fp["pads"]) and "board_only" not in fp["attr"] and not any(k in name for k in NO_MODEL_OK)


def _net(node, names=None):
    """Net name of a pad, track, via or zone. KiCad 10 writes `(net "GND")`; KiCad 9 and earlier write
    `(net 1 "GND")` on pads but only `(net 1)` on tracks, vias and zones, resolved through the board's
    net table (`names`)."""
    net = sexp.child(node, "net")
    if not net or len(net) < 2:
        return None
    if len(net) > 2:
        return net[2] or None
    return (names or {}).get(net[1], net[1]) or None


def _xy(node, name):
    c = sexp.child(node, name)
    return (sexp.num(c[1]), sexp.num(c[2])) if c and len(c) > 2 else None


def _drill(node):
    dr = sexp.child(node, "drill")
    if not dr or len(dr) < 2:
        return None
    vals = [sexp.num(v) for v in dr[1:] if not isinstance(v, list) and v != "oval"]
    if not vals:
        return None
    return (vals[0], vals[1] if len(vals) > 1 and dr[1] == "oval" else vals[0])


# a chain of thin tracks that only reaches capacitors or test points is a stub (decoupling, probing):
# no DC load current flows in it, so its width is not a current-capacity question
STUB_REF = re.compile(r"^(C|CP|TP)\d+$", re.I)


def ipc2221_width_mm(current_a, copper_mm, dt_c=10.0, internal=False):
    """IPC-2221 track width for `current_a` with a `dt_c` temperature rise: I = k dT^0.44 A^0.725
    (A in mil^2; k = 0.048 outer layers, 0.024 inner layers)."""
    k = 0.024 if internal else 0.048
    area_mil2 = (current_a / (k * dt_c ** 0.44)) ** (1 / 0.725)
    return area_mil2 / (copper_mm / 0.0254) * 0.0254


def _chains(segs):
    """Groups of segments joined end to end (within 1 µm)."""
    parent = list(range(len(segs)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    at = {}
    for i, s in enumerate(segs):
        for pt in (s["start"], s["end"]):
            key = (round(pt[0], 3), round(pt[1], 3))
            if key in at:
                parent[find(i)] = find(at[key])
            else:
                at[key] = i
    groups = {}
    for i in range(len(segs)):
        groups.setdefault(find(i), []).append(segs[i])
    return list(groups.values())


def _current(params, net):
    """Declared DC current (A) of a net: params.net_currents by exact name, or by a '~regex' key."""
    for k, v in (params.get("net_currents") or {}).items():
        k = str(k)
        if (k.startswith("~") and re.fullmatch(k[1:], net)) or k == net:
            return float(v)
    return None


def power_width(board, label, params):
    """PCB-WIDTH-001. Power nets are found by name; on each, the chains of tracks narrower than the
    limit are judged, except stubs to capacitors and test points. The limit is IPC-2221 for a declared
    current, else the project's min_power_width_mm; with neither, the chain is a coverage gap that asks
    for the net's current."""
    explicit = params.get("min_power_width_mm")
    review = float(params.get("power_review_width_mm", 0.25))
    dt = float(params.get("width_temp_rise_c", 10))
    extra = [re.compile(x, re.I) for x in params.get("power_net_patterns") or []]
    by_net = {}
    for tr in board["tracks"]:
        n = tr["net"] or ""
        if tr["start"] and tr["end"] and (any(r.search(n) for r in extra)
                                          or (POWER_RE.search(n) and not SIGNAL_RE.search(n.rsplit("/", 1)[-1]))):
            by_net.setdefault(n, []).append(tr)
    viol, gaps = [], []
    for net, segs in sorted(by_net.items()):
        cur = _current(params, net)

        def limit(tr, cur=cur):
            if cur is not None:
                cu = board.get("copper_mm", {}).get(tr["layer"]) or 0.035
                return ipc2221_width_mm(cur, cu, dt, internal=tr["layer"] not in ("F.Cu", "B.Cu"))
            return float(explicit) if explicit is not None else review
        thin = [tr for tr in segs if tr["width"] < limit(tr) - 1e-6]
        open_pts, open_w = [], None
        for chain in _chains(thin):
            pts = [pt for tr in chain for pt in (tr["start"], tr["end"])]
            refs = {r.split(".")[0] for r in _reached_pads(board, net, pts)}
            if refs and all(STUB_REF.match(r) for r in refs):
                continue
            w = min(tr["width"] for tr in chain)
            where = _reaches(board, net, pts)
            if cur is not None:
                tr = min(chain, key=lambda x: x["width"] - limit(x))
                cu = board.get("copper_mm", {}).get(tr["layer"]) or 0.035
                viol.append({"key": vkey("width", net, tr["layer"]),
                             "text": f"{net}: {tr['width']} mm on {tr['layer']} for {cur:g} A; IPC-2221 needs "
                                     f"{limit(tr):.2f} mm (+{dt:g} °C, {cu * 1000:.0f} µm copper){where}"})
            elif explicit is not None:
                viol.append({"key": vkey("width", net), "text": f"{net}: {w} mm{where}"})
            else:
                open_pts += pts
                open_w = w if open_w is None else min(open_w, w)
        if open_pts:  # one gap per net, whatever the number of chains: the input asked for is the same
            gaps.append({"key": vkey("widthgap", net),
                         "text": f"{net}: tracks down to {open_w} mm{_reaches(board, net, open_pts)}; declare the "
                                 "net's current in params.net_currents to check them against IPC-2221"})
    n = len(by_net)
    if explicit is not None and not viol:
        detail = f"{label}: no power net below {explicit} mm outside decoupling stubs"
    else:
        detail = f"{label}: {count(len(viol), 'power track')} too narrow for its current or limit"
    if gaps:
        detail += f"; {count(len(gaps), 'net')} with thin tracks and no declared current"
    return Result("PCB-WIDTH-001", FAIL if viol else PASS, detail, violations=viol,
                  coverage=coverage("routed power nets", n, gaps))


def _reaches(board, net, points, limit=8):
    """' (thin tracks reach R5.1, R13.1)': the pads of `net` that the narrow segments end on, so a
    reader can tell a feed to a divider or a pull-up from the rail's main current path."""
    hit = _reached_pads(board, net, points)
    if not hit:
        return ""
    more = f" and {len(hit) - limit} more" if len(hit) > limit else ""
    return f" (thin tracks reach {', '.join(hit[:limit])}{more})"


def _reached_pads(board, net, points):
    hit = []
    for fp in board["footprints"]:
        for p in fp["pads"]:
            if p["net"] != net:
                continue
            r = max(p["size"]) / 2 if p["size"] else 0.0
            if any(math.hypot(x - p["x"], y - p["y"]) <= r + 1e-6 for x, y in points):
                hit.append(f"{fp['ref']}.{p['number']}")
    return sorted(set(hit))


def load(pcb):
    tree = sexp.parse(Path(pcb).read_text(encoding="utf-8"))
    names = {n[1]: n[2] for n in sexp.children(tree, "net") if len(n) > 2}  # KiCad <= 9 net table
    fps = []
    for fp in sexp.children(tree, "footprint"):
        at = sexp.child(fp, "at") or ["at", "0", "0"]
        fx, fy = sexp.num(at[1]), sexp.num(at[2])
        rot = sexp.num(at[3]) if len(at) > 3 else 0.0
        a = math.radians(rot)
        pads = []
        for p in sexp.children(fp, "pad"):
            pat = sexp.child(p, "at") or ["at", "0", "0"]
            px, py = sexp.num(pat[1]), sexp.num(pat[2])
            d = _drill(p)
            pads.append({
                "number": p[1] if len(p) > 1 else "", "type": p[2] if len(p) > 2 else "",
                "shape": p[3] if len(p) > 3 and isinstance(p[3], str) else "",
                "angle": sexp.num(pat[3]) if len(pat) > 3 else rot,
                "net": _net(p),
                "x": fx + px * math.cos(a) + py * math.sin(a),
                "y": fy - px * math.sin(a) + py * math.cos(a),
                "drill": d[0] if d else None, "drill2": d[1] if d else None,
                "size": _xy(p, "size") or (0.0, 0.0),
                "layers": list((sexp.child(p, "layers") or [])[1:]),
            })
        attr = [x for x in (sexp.child(fp, "attr") or [])[1:]]
        edges_fp = []
        for kind in ("fp_line", "fp_arc", "fp_rect", "fp_circle", "fp_poly"):
            for g in sexp.children(fp, kind):
                if (sexp.child(g, "layer") or ["", ""])[1] == "Edge.Cuts":
                    edges_fp += [((fx + x * math.cos(a) + y * math.sin(a), fy - x * math.sin(a) + y * math.cos(a)),
                                  (fx + u * math.cos(a) + v * math.sin(a), fy - u * math.sin(a) + v * math.cos(a)))
                                 for (x, y), (u, v) in _outline_segments(kind, g)]
        fps.append({
            "edges": edges_fp,
            "name": fp[1] if len(fp) > 1 else "", "ref": sexp.prop(fp, "Reference") or "?",
            "value": sexp.prop(fp, "Value") or "", "layer": (sexp.child(fp, "layer") or ["", "F.Cu"])[1],
            "x": fx, "y": fy, "rot": rot, "attr": attr, "dnp": "dnp" in attr,
            "models": [m[1] for m in sexp.children(fp, "model") if len(m) > 1], "pads": pads,
        })
    tracks = []
    for kind in ("segment", "arc"):
        for s in sexp.children(tree, kind):
            w = sexp.child(s, "width")
            tracks.append({"net": _net(s, names), "width": sexp.num(w[1]) if w else 0.0,
                           "layer": (sexp.child(s, "layer") or ["", ""])[1],
                           "start": _xy(s, "start"), "end": _xy(s, "end")})
    vias = []
    for v in sexp.children(tree, "via"):
        d = _drill(v)
        size = sexp.child(v, "size")
        vias.append({"net": _net(v, names), "pos": _xy(v, "at"), "drill": d[0] if d else None,
                     "size": sexp.num(size[1]) if size else 0.0})
    zones = [{"net": (sexp.child(z, "net_name") or [None, None])[1] or _net(z, names),
              "layers": list((sexp.child(z, "layers") or sexp.child(z, "layer") or [])[1:])}
             for z in sexp.children(tree, "zone")]
    edge_segments = [s for fp in fps for s in fp["edges"]]
    for kind in ("gr_line", "gr_arc", "gr_rect", "gr_circle", "gr_poly"):
        for g in sexp.children(tree, kind):
            if (sexp.child(g, "layer") or ["", ""])[1] == "Edge.Cuts":
                edge_segments += _outline_segments(kind.replace("gr_", "fp_"), g)
    edge = [p for s in edge_segments for p in s]
    zone_fills = []
    for z in sexp.children(tree, "zone"):
        zn = (sexp.child(z, "net_name") or [None, None])[1] or _net(z, names)
        for fpoly in sexp.children(z, "filled_polygon"):
            pts = sexp.child(fpoly, "pts")
            if pts:
                zone_fills.append({"net": zn, "layer": (sexp.child(fpoly, "layer") or ["", ""])[1],
                                   "pts": [(sexp.num(q[1]), sexp.num(q[2])) for q in sexp.children(pts, "xy")]})
    setup = sexp.child(tree, "setup") or []
    layers = sexp.child(tree, "layers") or []
    general = sexp.child(tree, "general") or []
    th = sexp.child(general, "thickness")
    copper_mm = {}
    for ly in sexp.children(sexp.child(setup, "stackup") or [], "layer"):
        if (sexp.child(ly, "type") or ["", ""])[1] == "copper" and sexp.child(ly, "thickness"):
            copper_mm[ly[1]] = sexp.num(sexp.child(ly, "thickness")[1])
    return {"footprints": fps, "tracks": tracks, "vias": vias, "zones": zones,
            "edge_segments": edge_segments, "zone_fills": zone_fills,
            "aux_origin": _xy(setup, "aux_axis_origin") or (0.0, 0.0),
            "grid_origin": _xy(setup, "grid_origin") or (0.0, 0.0),
            "plot_aux_origin": (sexp.child(sexp.child(setup, "pcbplotparams") or [], "useauxorigin")
                                or ["", "no"])[1] in ("yes", "true"),
            "copper_layers": [ly[1] for ly in layers[1:] if isinstance(ly, list) and len(ly) > 2
                              and str(ly[1]).endswith(".Cu")],
            "thickness": sexp.num(th[1], 1.6) if th else 1.6,
            "copper_mm": copper_mm,  # per copper layer, from the board stackup (empty: not declared)
            "outline_bbox": ([min(p[0] for p in edge), min(p[1] for p in edge),
                              max(p[0] for p in edge), max(p[1] for p in edge)] if edge else None)}


def _circle_pts(cx, cy, r, a0=0.0, sweep=2 * math.pi, n=16):
    return [(cx + r * math.cos(a0 + sweep * i / n), cy + r * math.sin(a0 + sweep * i / n)) for i in range(n + 1)]


def _arc_pts(s, m, e, n=16):
    """Polyline through an arc given by start, mid and end points."""
    ax, ay = s
    bx, by = m
    cx, cy = e
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return [s, e]
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    r = math.hypot(ax - ux, ay - uy)
    a0, am, a1 = (math.atan2(p[1] - uy, p[0] - ux) for p in (s, m, e))

    def ccw(a, b):
        return (b - a) % (2 * math.pi)
    sweep = ccw(a0, a1) if ccw(a0, am) <= ccw(a0, a1) else -ccw(a1, a0)
    return _circle_pts(ux, uy, r, a0, sweep, n)


def _outline_segments(kind, g):
    """Line segments of one Edge.Cuts item (local coordinates for fp_* items inside footprints)."""
    st, en, mid, ce = _xy(g, "start"), _xy(g, "end"), _xy(g, "mid"), _xy(g, "center")
    pts = []
    if kind == "fp_line" and st and en:
        pts = [st, en]
    elif kind == "fp_arc" and st and en and mid:
        pts = _arc_pts(st, mid, en)
    elif kind == "fp_rect" and st and en:
        pts = [st, (en[0], st[1]), en, (st[0], en[1]), st]
    elif kind == "fp_circle" and ce and en:
        pts = _circle_pts(ce[0], ce[1], math.hypot(en[0] - ce[0], en[1] - ce[1]), n=32)
    elif kind == "fp_poly":
        pp = sexp.child(g, "pts")
        pts = [(sexp.num(p[1]), sexp.num(p[2])) for p in sexp.children(pp, "xy")] if pp else []
        if pts:
            pts.append(pts[0])
    return [(pts[i], pts[i + 1]) for i in range(len(pts) - 1)]


def mounting_holes(board, params):
    refs = set(params.get("mounting_refs") or [])
    out = []
    for fp in board["footprints"]:
        if fp["ref"] in refs or "mountinghole" in fp["name"].lower():
            holes = [p for p in fp["pads"] if p["drill"]]
            if holes:
                h = max(holes, key=lambda p: p["drill"])
                out.append({"ref": fp["ref"], "x": h["x"], "y": h["y"], "drill": h["drill"], "net": h["net"]})
    return out


def _seg_dist(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L = dx * dx + dy * dy
    t = 0 if L == 0 else max(0, min(1, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def approved_footprints(root):
    names = set()
    for f in [DATA / "approved_footprints.txt", Path(root) / "verification" / "pcb" / "approved_footprints.txt"]:
        if f.exists():
            for line in f.read_text(encoding="utf-8").splitlines():
                line = line.split("#")[0].strip()
                if line:
                    names.add(line)
    return names


def _norm(n):
    return (n or "").lstrip("/").upper()


def run(board, label, proj, pcb=None):
    root, params, pins = proj["root"], proj["params"], proj["pins"]
    res = []

    approved = approved_footprints(root)
    used = sorted({fp["name"] for fp in board["footprints"]})
    if not approved:
        res.append(Result("PCB-FOOT-001", WARN,
                          f"{label}: no approved-footprint list; {count(len(used), 'footprint')} awaiting approval "
                          "(add them to verification/pcb/approved_footprints.txt once checked against the datasheet)",
                          violations=[{"key": vkey("foot", n), "text": n} for n in used], outcome=NOT_VERIFIABLE,
                          coverage=coverage("footprints", len(used),
                                            [{"key": vkey("foot", n), "text": f"{n}: not approved yet"}
                                             for n in used])))
    else:
        bad = [n for n in used if n not in approved]
        res.append(Result("PCB-FOOT-001", FAIL if bad else PASS,
                          f"{label}: {count(len(bad), 'footprint')} not approved",
                          violations=[{"key": vkey("foot", n), "text": n} for n in bad],
                          coverage=coverage("footprints", len(used))))

    nomodel = [fp for fp in board["footprints"] if not fp["models"] and needs_model(fp)]
    res.append(Result("PCB-MODEL-001", WARN if nomodel else PASS,
                      f"{label}: {count(len(nomodel), 'footprint')} without a 3D model (missing from the board STEP, "
                      "so enclosure fit cannot be checked for them)",
                      violations=[{"key": vkey("model", fp["ref"]), "text": f"{fp['ref']} ({fp['name']})"}
                                  for fp in nomodel],
                      coverage=coverage("footprints", len(board["footprints"]))))

    electrical = [(fp["ref"], p) for fp in board["footprints"] for p in fp["pads"]
                  if p["type"] in ("smd", "thru_hole") and p["number"]
                  and "mountinghole" not in fp["name"].lower()]  # a plated mounting hole is not a pin
    nonet = [(r, p["number"]) for r, p in electrical if not p["net"]]
    res.append(Result("PCB-PADNET-001", WARN if nonet else PASS,
                      f"{label}: {count(len(nonet), 'pad')} without a net (fine only if the pin is NC)",
                      violations=[{"key": vkey("padnet", r, n), "text": f"{r}.{n}"} for r, n in nonet],
                      coverage=coverage("electrical pads", len(electrical))))

    res.append(power_width(board, label, params))

    if not pins:
        res.append(not_verifiable("PCB-PINS-001", f"{label}: no critical pins listed in verification/pcb/pins.yaml",
                                  "copy the critical pins of each IC/connector from its datasheet into pins.yaml"))
    else:
        byref = {fp["ref"]: fp for fp in board["footprints"]}
        bad = []
        for ref, table in pins.items():
            fp = byref.get(ref)
            if not fp:
                bad.append((f"{ref}: not on the board", ref))
                continue
            padnet = {}
            for p in fp["pads"]:
                padnet.setdefault(p["number"], p["net"])
            for pin, expected in (table or {}).items():
                got = padnet.get(str(pin))
                exp = str(expected)
                pat = exp[1:] if exp.startswith("~") else re.escape(_norm(exp))
                ok = got is not None and re.fullmatch(pat, _norm(got) if not exp.startswith("~") else got, re.I)
                if not ok:
                    bad.append((f"{ref}.{pin}: expected {expected}, found {got}", f"{ref}.{pin}"))
        npins = sum(len(t or {}) for t in pins.values())
        res.append(Result("PCB-PINS-001", FAIL if bad else PASS,
                          f"{label}: {count(len(bad), 'pin')} not matching pins.yaml",
                          violations=[{"key": vkey("pins", k), "text": t} for t, k in bad],
                          coverage=coverage("pins listed in pins.yaml", npins),
                          evidence=[proj["dir"] / "pins.yaml"]))

    mh = params.get("mounting_holes")
    if not mh:
        res.append(not_verifiable("PCB-HOLE-001", f"{label}: no expected mounting holes (params.mounting_holes)",
                                  "set params.mounting_holes from the enclosure drawing"))
    else:
        tol = float(mh.get("tol_mm", 0.1))
        holes = [p for fp in board["footprints"] for p in fp["pads"] if p["drill"]]
        bad = []
        for ex in mh.get("expected", []):
            near = min(holes, key=lambda h: math.hypot(h["x"] - ex[0], h["y"] - ex[1]), default=None)
            if near is None or math.hypot(near["x"] - ex[0], near["y"] - ex[1]) > tol:
                bad.append(f"{ex}: no hole within ±{tol} mm")
            elif mh.get("drill_mm") and abs((near["drill"] or 0) - mh["drill_mm"]) > tol:
                bad.append(f"{ex}: drill {near['drill']} mm, expected {mh['drill_mm']} mm")
        res.append(Result("PCB-HOLE-001", FAIL if bad else PASS,
                          f"{label}: {count(len(bad), 'mounting hole')} out of tolerance",
                          violations=[{"key": vkey("hole", b), "text": b} for b in bad],
                          coverage=coverage("expected mounting holes", len(mh.get("expected", [])))))

    mhs = mounting_holes(board, params)
    extra_r = float(params.get("hole_keepout_extra_mm", 1.4))
    if not mhs:
        res.append(not_verifiable("PCB-KEEPOUT-001", f"{label}: no mounting holes identified",
                                  "use MountingHole* footprints or list them in params.mounting_refs; "
                                  "exclude the requirement with a reason if the board has none"))
    else:
        bad = []
        for h in mhs:
            rk = h["drill"] / 2 + extra_r
            c = (h["x"], h["y"])
            for t in board["tracks"]:  # an inner-layer track cannot touch a washer or a screw head
                if t["layer"] not in ("F.Cu", "B.Cu"):
                    continue
                if t["start"] and t["end"] and (t["net"] != h["net"] or not h["net"]):
                    if _seg_dist(c, t["start"], t["end"]) - t["width"] / 2 < rk:
                        bad.append(f"{h['ref']}: track {t['net']} ({t['layer']}) inside the Ø{2 * rk:.1f} mm keep-out")
            for v in board["vias"]:
                if v["pos"] and (v["net"] != h["net"] or not h["net"]):
                    if math.hypot(v["pos"][0] - c[0], v["pos"][1] - c[1]) - v["size"] / 2 < rk:
                        bad.append(f"{h['ref']}: via {v['net']} inside the Ø{2 * rk:.1f} mm keep-out")
            for fp in board["footprints"]:
                if fp["ref"] == h["ref"]:
                    continue
                for p in fp["pads"]:
                    if p["net"] and p["net"] == h["net"]:
                        continue
                    if math.hypot(p["x"] - c[0], p["y"] - c[1]) - max(p["size"]) / 2 < rk:
                        bad.append(f"{h['ref']}: pad {fp['ref']}.{p['number']} inside the Ø{2 * rk:.1f} mm keep-out")
        bad = list(dict.fromkeys(bad))
        res.append(Result("PCB-KEEPOUT-001", FAIL if bad else PASS,
                          f"{label}: {count(len(bad), 'item')} inside the mounting-hole keep-out (washer/screw head)",
                          violations=[{"key": vkey("keepout", b), "text": b} for b in bad],
                          coverage=coverage("mounting holes", len(mhs))))
    for r in res:
        r.evidence = [pcb or label] + list(r.evidence)
    return res
