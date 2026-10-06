import argparse
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, interface, netlist, report, review, signoff, waivers
from .checks import board as board_mod
from .checks import circuit, fab, happy, kicad_cli, parity
from .report import FAIL, PASS, SKIP, WARN, Result


def _filter_disabled(results, disabled):
    return [r for r in results if r.check_id not in disabled
            and not any(d.endswith("*") and r.check_id.startswith(d[:-1]) for d in disabled)]


def analyse(root, mode="full"):
    """Runs every deterministic check. Returns (report dict, context for the reviewer)."""
    root = Path(root)
    proj = config.load_project(root)
    params = proj["params"]
    cache = proj["dir"] / "reports"
    kicads = config.discover(root)
    results, designs, kh_summaries, jobs = [], {}, {}, []
    if not kicads:
        results.append(Result("DISCOVER", SKIP, "no KiCad project found"))
    gerber_dir = None
    gerbers, _, _, _ = fab.find_outputs(root, params)
    if gerbers:
        gerber_dir = gerbers[0].parent
    for k in kicads:
        label = k["label"]
        nl = None
        if k["sch"].exists():
            try:
                nl = netlist.load(k["sch"], cache)
            except Exception as e:
                results.append(Result("PCB-NETLIST", FAIL, f"{label}: netlist export failed: {e}"))
        designs[label] = nl
        try:
            cres, _ = circuit.run(nl, label, params)
            results += cres
        except Exception as e:
            results.append(Result("CIR-ENGINE", FAIL, f"{label}: circuit checks crashed: {e}"))
        if k["pcb"].exists():
            try:
                b = board_mod.load(k["pcb"])
                results += board_mod.run(b, label, proj)
                results += parity.run(b, nl, label)
                results += fab.run(root, k["pcb"], b, label, params, mode)
            except Exception as e:
                results.append(Result("PCB-PARSE", FAIL, f"{label}: could not parse the PCB: {e}"))
        if (params.get("kicad_happy") or {}).get("enabled", True):
            try:
                kh = happy.analyze(k, gerber_dir if mode == "full" else None, cache / "kicad_happy" / label, params)
                results += happy.to_results(kh, label, params)
                kh_summaries[label] = happy.summary_for_review(kh)
            except Exception as e:
                results.append(Result("KH-ENGINE", WARN, f"{label}: kicad-happy failed: {e}"))
        if mode == "full":
            if k["pcb"].exists():
                jobs.append(lambda p=k["pcb"], l=label: kicad_cli.drc(p, l))
                jobs.append(lambda kk=k: _interface(kk, params, proj))
            jobs.append(lambda s=k["sch"], l=label: kicad_cli.erc(s, l))
    if jobs:  # ERC, DRC and the STEP export take tens of seconds each: run them in parallel
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(lambda j: j(), jobs):
                results += r
    results = _filter_disabled(results, proj["disabled"])
    results = waivers.apply(results, proj["waivers"])
    rep = report.build(proj["project"]["name"], mode, results, report.tool_versions(config.KICAD_CLI), {
        "status": proj["project"]["status"], "design_hash": config.design_hash(root),
        "human_pending": signoff.pending(root, proj["requirements"])})
    report.write(cache / "verify_report.json", rep)
    return rep, {"proj": proj, "designs": designs, "kh": kh_summaries}


def _interface(k, params, proj):
    try:
        f = interface.export(k, params, proj["dir"] / "interface")
        return [Result("PCB-IFACE-001", PASS, f"{k['label']}: board interface for cad-verify at {f.name}",
                       evidence=[f])]
    except Exception as e:
        return [Result("PCB-IFACE-001", WARN, f"{k['label']}: board interface export failed: {e}")]


def do_review(root, model=None, release=False, rep=None, ctx=None):
    if rep is None:
        rep, ctx = analyse(root, "full")
    proj = ctx["proj"]
    model = model or proj["project"].get("reviewer_model") or (proj["params"].get("review") or {}).get("model", "opus")
    bdir, reqs = review.build_bundle(proj["root"], proj, ctx["designs"], rep, ctx["kh"])
    timeout = int((proj["params"].get("review") or {}).get("timeout_s", 1800))
    try:
        raw, meta = review.run_reviewer(proj["root"], bdir, model, timeout)
    except Exception as e:
        res = [Result("REV-RUN", FAIL, f"reviewer did not run: {e}")]
        review.write_report(proj, {"model": model, "error": str(e)}, [], [], res)
        return res, None
    (bdir / "reviewer_raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    mode = (proj["params"].get("review") or {}).get("unverifiable_at_release", "fail")
    results, detail, extras = review.judge(raw, reqs, proj["root"], bdir, release=release,
                                           unverifiable_at_release=mode)
    results = waivers.apply(results, proj["waivers"])
    rr, path = review.write_report(proj, meta, detail, extras, results)
    return results, rr


def _print_review(results, rr):
    lines = []
    for r in results:
        lines.append(f"  {r.status:<4} {r.check_id}: {r.detail}")
        for e in r.evidence[:3]:
            lines.append(f"       evidence: {e}")
        for v in r.violations[:6]:
            if v["text"] != r.detail:
                lines.append(f"       - {v['text']}")
    meta = (rr or {}).get("reviewer") or {}
    head = f"review ({meta.get('model')}, {meta.get('duration_s')} s, ${meta.get('cost_usd')})"
    return head + "\n" + "\n".join(lines)


def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="kicadverify", description="Verification gate for KiCad projects")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "init", "status", "review", "release", "interface", "requirements", "datasheets"):
        p = sub.add_parser(name)
        p.add_argument("path", nargs="?", default=".")
        if name == "verify":
            p.add_argument("--fast", action="store_true", help="skip ERC/DRC, Gerber re-plot and STEP export")
            p.add_argument("--all", action="store_true", help="also list PASS/SKIP checks")
            p.add_argument("--json", action="store_true")
        if name in ("review", "release"):
            p.add_argument("--model", help="reviewer model (default: project reviewer_model)")
    p = sub.add_parser("signoff", help="record a human sign-off for the current design")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--path", default=".")
    sub.add_parser("setup", help="download the pinned kicad-happy engine")
    p = sub.add_parser("install-claude", help="install hooks and skills into ~/.claude")
    p.add_argument("--no-hooks", action="store_true")
    sub.add_parser("uninstall-claude")
    a = ap.parse_args(argv)

    if a.cmd == "setup":
        print("kicad-happy installed at", happy.install())
        return 0
    if a.cmd in ("install-claude", "uninstall-claude"):
        from . import claude_setup
        return claude_setup.install(hooks=not a.no_hooks) if a.cmd == "install-claude" else claude_setup.uninstall()

    root, initialised = config.find_root(a.path if a.cmd != "signoff" else a.path)
    if root is None:
        print("No KiCad project (.kicad_pro) found from", a.path, file=sys.stderr)
        return 3
    if a.cmd == "init" or not initialised:
        created = config.init_project(root)
        if a.cmd == "init":
            print("\n".join(["Created:"] + [f"  {c}" for c in created]) if created else "Already initialised.")
            return 0
    proj = config.load_project(root)

    if a.cmd == "requirements":
        for r in proj["requirements"]:
            print(f"{r['id']:<16} {r.get('method', ''):<6} {r['text']}")
        return 0
    if a.cmd == "status":
        st = {"root": str(root), **proj["project"], "design_hash": config.design_hash(root),
              "human_pending": signoff.pending(root, proj["requirements"])}
        print(json.dumps(st, indent=2, default=str))
        return 0
    if a.cmd == "signoff":
        ids = {r["id"] for r in proj["requirements"] if r.get("method") == "human"}
        if a.id not in ids:
            print(f"{a.id} is not a human requirement ({', '.join(sorted(ids))})", file=sys.stderr)
            return 2
        signoff.add(root, a.id, a.by, a.note)
        print(f"Signed {a.id} for design {config.design_hash(root)[:12]}")
        return 0
    if a.cmd == "datasheets":
        from . import datasheets
        for k in config.discover(root):
            nl = netlist.load(k["sch"], proj["dir"] / "reports")
            done, skipped, failed = datasheets.fetch(nl, root / "datasheets")
            print(f"{k['label']}: {len(done)} downloaded, {len(skipped)} already present, {len(failed)} failed")
            for f in failed:
                print("  -", f)
        print("Datasheets without a URL must be added by hand to datasheets/ (any file name).")
        return 0
    if a.cmd == "interface":
        for k in config.discover(root):
            print(interface.export(k, proj["params"], proj["dir"] / "interface", force=True))
        return 0
    if a.cmd == "verify":
        rep, _ = analyse(root, "fast" if a.fast else "full")
        print(json.dumps(rep, indent=2, ensure_ascii=False) if a.json else report.text(rep, not a.all))
        return 1 if rep["overall"] == FAIL else 0
    if a.cmd in ("review", "release"):
        rep, ctx = analyse(root, "full")
        print(report.text(rep))
        if a.cmd == "release" and rep["overall"] == FAIL:
            print("Release blocked: deterministic checks fail.", file=sys.stderr)
            return 1
        results, rr = do_review(root, a.model, release=a.cmd == "release", rep=rep, ctx=ctx)
        print(_print_review(results, rr))
        failed = any(r.status == FAIL for r in results)
        if a.cmd == "review":
            return 1 if failed else 0
        pend = signoff.pending(root, proj["requirements"])
        if failed or pend:
            if pend:
                print(f"Release blocked: human sign-off pending: {', '.join(pend)} "
                      "(kicadverify signoff <ID> --by <name>)", file=sys.stderr)
            if failed:
                print("Release blocked: reviewer requirements fail.", file=sys.stderr)
            return 1
        _set_release(root, proj)
        print(f"Released: design {config.design_hash(root)[:12]} (status: release)")
        return 0
    return 0


def _set_release(root, proj):
    import yaml
    f = proj["dir"] / "project.yaml"
    data = config.load_yaml(f, {}) or {}
    data["status"] = "release"
    data["release_design_hash"] = config.design_hash(root)
    f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
