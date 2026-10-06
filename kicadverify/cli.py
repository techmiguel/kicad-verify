import argparse
import copy
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (config, interface, manifest, netlist, outputs, report, requirements, review, signoff,
               waivers)
from .checks import assertions, board as board_mod
from .checks import circuit, dfm, fab, happy, kicad_cli, parity
from .report import FAIL, PASS, SKIP, WARN, Result

EXIT_OK, EXIT_BLOCKED, EXIT_CONFIG, EXIT_NO_PROJECT = 0, 1, 2, 3


def _filter_disabled(results, disabled):
    return [r for r in results if r.check_id not in disabled
            and not any(d.endswith("*") and r.check_id.startswith(d[:-1]) for d in disabled)]


def _lint(proj):
    dfm.apply_profile_source(proj["requirements"], proj["params"])
    return requirements.lint(proj["requirements"], proj["excluded"], assertions.TYPES)


def checks(root, proj, mode):
    """Every deterministic verifier. Returns (results before waivers, designs, kicad-happy summaries)."""
    params = proj["params"]
    cache = proj["dir"] / "reports"
    ev_dir = cache / "evidence"
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
        nl, b = None, None
        if k["sch"].exists():
            try:
                nl = netlist.load(k["sch"], cache)
            except Exception as e:
                results.append(Result("PCB-NETLIST", FAIL, f"{label}: netlist export failed: {e}"))
        designs[label] = nl
        try:
            cres, _ = circuit.run(nl, label, params)
            for r in cres:
                r.evidence = r.evidence or [k["sch"]]
            results += cres
        except Exception as e:
            results.append(Result("CIR-ENGINE", FAIL, f"{label}: circuit checks crashed: {e}"))
        if k["pcb"].exists():
            try:
                b = board_mod.load(k["pcb"])
                results += board_mod.run(b, label, proj, k["pcb"])
                results += parity.run(b, nl, label)
                results += dfm.run(b, k["pro"], label, params, k["pcb"])
                results += fab.run(root, k["pcb"], b, label, params, mode)
            except Exception as e:
                results.append(Result("PCB-PARSE", FAIL, f"{label}: could not parse or check the PCB: {e}"))
        try:
            results += assertions.run(proj["requirements"], nl, b, label)
        except Exception as e:
            results.append(Result("ASSERT-ENGINE", FAIL, f"{label}: assertions crashed: {e}"))
        if (params.get("kicad_happy") or {}).get("enabled", True):
            try:
                kh = happy.analyze(k, gerber_dir if mode == "full" else None, cache / "kicad_happy" / label, params)
                results += happy.to_results(kh, label, params)
                kh_summaries[label] = happy.summary_for_review(kh)
            except Exception as e:
                results.append(Result("KH-ENGINE", WARN, f"{label}: kicad-happy failed: {e}",
                                      outcome=report.NOT_VERIFIABLE))
        if mode == "full":
            if k["pcb"].exists():
                jobs.append(lambda p=k["pcb"], l=label: kicad_cli.drc(p, l, ev_dir))
                jobs.append(lambda kk=k: _interface(kk, params, proj))
            jobs.append(lambda s=k["sch"], l=label: kicad_cli.erc(s, l, ev_dir))
    if jobs:  # ERC, DRC and the STEP export take tens of seconds each: run them in parallel
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(lambda j: j(), jobs):
                results += r
    return results, designs, kh_summaries


def finish(root, proj, mode, raw_results, dh=None, review_note=None):
    """Waivers, requirement verdicts, gates and report files from the raw verifier results."""
    root = Path(root)
    dh = dh or config.design_hash(root)
    results = waivers.apply(_filter_disabled(copy.deepcopy(raw_results), proj["disabled"]), proj["waivers"])
    reqs = proj["requirements"]
    dfm.apply_profile_source(reqs, proj["params"])
    ver = requirements.verification(reqs, proj["excluded"], results, root, mode, proj["params"], _lint(proj))
    if review_note:
        for v in ver["requirements"]:
            if v["method"] == "model" and v["status"] == report.NOT_RUN:
                v["reason"] += f" ({review_note})"
    rep = report.build(proj["project"]["name"], mode, results, report.tool_versions(config.KICAD_CLI), {
        "status": proj["project"]["status"], "design_hash": dh,
        "requirements_hash": requirements.requirements_hash(reqs),
        "human_pending": signoff.pending(root, reqs), "verification": ver})
    cache = proj["dir"] / "reports"
    report.write(cache / "verify_report.json", rep)
    (cache / "verification_report.md").write_text(outputs.markdown(rep), encoding="utf-8")
    return rep, results


def analyse(root, mode="full"):
    """Runs every deterministic check, adds the current sign-offs and the last review when it is still
    valid for this design, and evaluates every requirement. Returns (report dict, context)."""
    root = Path(root)
    proj = config.load_project(root)
    dh = config.design_hash(root)
    det, designs, kh = checks(root, proj, mode)
    raw = det + signoff.results(root, proj["requirements"], dh)
    rev, why = review.load_current(proj, dh, requirements.requirements_hash(proj["requirements"]))
    rep, results = finish(root, proj, mode, raw + (rev or []), dh, None if rev else why)
    return rep, {"proj": proj, "designs": designs, "kh": kh, "raw": raw, "results": results, "dh": dh,
                 "review_current": rev is not None}


def _interface(k, params, proj):
    try:
        f = interface.export(k, params, proj["dir"] / "interface")
        return [Result("PCB-IFACE-001", PASS, f"{k['label']}: board interface at {f.name}", evidence=[f])]
    except Exception as e:
        return [Result("PCB-IFACE-001", WARN, f"{k['label']}: board interface export failed: {e}")]


def do_review(root, model=None, rep=None, ctx=None):
    """Runs the independent reviewer and re-evaluates the requirements with its results.
    Returns (review results after waivers, review report dict, updated verification report)."""
    if rep is None:
        rep, ctx = analyse(root, "full")
    proj = ctx["proj"]
    model = model or proj["project"].get("reviewer_model") or (proj["params"].get("review") or {}).get("model", "opus")
    reqs_model = [r for r in proj["requirements"] if r["method"] == "model"]
    bdir, reqs = review.build_bundle(proj["root"], proj, ctx["designs"], rep, ctx["kh"])
    timeout = int((proj["params"].get("review") or {}).get("timeout_s", 1800))
    rh = requirements.requirements_hash(proj["requirements"])
    try:
        raw, meta = review.run_reviewer(proj["root"], bdir, model, timeout)
    except Exception as e:
        res = [Result("REV-RUN", FAIL, f"reviewer did not run: {e}")]
        review.write_report(proj, {"model": model, "error": str(e)}, [], [], [], rh)
        rep2, _ = finish(root, proj, rep["mode"], ctx["raw"] + res, ctx["dh"])
        return res, None, rep2
    (bdir / "reviewer_raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    results, detail, extras = review.judge(raw, reqs, proj["root"], bdir)
    rr, path = review.write_report(proj, meta, detail, extras, results, rh)
    rep2, waived = finish(root, proj, rep["mode"], ctx["raw"] + results, ctx["dh"])
    ids = {r["id"] for r in reqs_model} | {"REV-EXTRA"}
    return [r for r in waived if r.check_id in ids], rr, rep2


def _print_review(results, rr):
    lines = []
    for r in results:
        lines.append(f"  {r.status:<4} {r.check_id}: {r.detail}")
        for e in r.evidence[:3]:
            if isinstance(e, dict):
                lines.append(f"       evidence: {e.get('path')} [{e.get('locator', '')}] \"{e.get('quote', '')[:80]}\"")
        for v in r.violations[:6]:
            if v["text"] != r.detail:
                lines.append(f"       - {v['text']}")
    meta = (rr or {}).get("reviewer") or {}
    head = f"review ({meta.get('model')}, {meta.get('duration_s')} s, ${meta.get('cost_usd')})"
    return head + "\n" + "\n".join(lines)


def _emit(rep, a, gate):
    if getattr(a, "junit", None):
        Path(a.junit).write_text(outputs.junit(rep), encoding="utf-8")
    if getattr(a, "markdown", None):
        with open(a.markdown, "a" if a.markdown.endswith("STEP_SUMMARY") else "w", encoding="utf-8") as f:
            f.write(outputs.markdown(rep, gate))


def _exit(rep, gate):
    if any(x["level"] == "error" for x in rep["verification"]["lint"]):
        print("Requirement set has errors (`kicadverify requirements --lint`).", file=sys.stderr)
        return EXIT_CONFIG
    return EXIT_OK if rep["verification"]["gates"][gate]["pass"] else EXIT_BLOCKED


def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    ap = argparse.ArgumentParser(prog="kicadverify",
                                 description="Requirements-based verification for KiCad projects: every requirement "
                                             "has a source, a verification method, evidence, explicit coverage and a "
                                             "status VERIFIED / FAILED / NOT_VERIFIABLE / NOT_RUN")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("verify", "init", "status", "review", "release", "interface", "requirements", "datasheets", "audit"):
        p = sub.add_parser(name)
        p.add_argument("path", nargs="?", default=".")
        if name in ("verify", "review", "release"):
            p.add_argument("--junit", help="write a JUnit XML report (one test case per requirement)")
            p.add_argument("--markdown", help="write the Markdown report (appends when the path ends in STEP_SUMMARY)")
            p.add_argument("--json", action="store_true", help="print the full JSON report")
        if name == "verify":
            p.add_argument("--fast", action="store_true", help="skip ERC/DRC, Gerber re-plot and STEP export")
            p.add_argument("--all", action="store_true", help="also list VERIFIED requirements and PASS/SKIP checks")
            p.add_argument("--gate", choices=requirements.GATES, default="dev",
                           help="gate that decides the exit code (default dev: only FAILED blocks; fab: ready to "
                                "fabricate; release: everything VERIFIED)")
        if name in ("review", "release"):
            p.add_argument("--model", help="reviewer model (default: project reviewer_model)")
        if name == "release":
            p.add_argument("--rerun-review", action="store_true",
                           help="run the reviewer even when a review for this exact design is on record")
        if name == "init":
            p.add_argument("--ci", choices=["github"], help="also write a CI workflow")
        if name == "requirements":
            p.add_argument("--lint", action="store_true", help="validate the requirement set; exit 2 on errors")
            p.add_argument("--markdown", action="store_true", help="print the requirement table as Markdown")
        if name == "audit":
            p.add_argument("--manifest", help="manifest to check (default verification/pcb/release/manifest.json)")
    p = sub.add_parser("signoff", help="record a human sign-off for the current design")
    p.add_argument("id")
    p.add_argument("--by", required=True)
    p.add_argument("--note", default="")
    p.add_argument("--fail", action="store_true", help="record that the check found the requirement NOT met")
    p.add_argument("--path", default=".")
    p = sub.add_parser("checks", help="list the verifiers with what they cover and do not cover")
    p.add_argument("--markdown", action="store_true")
    sub.add_parser("profiles", help="list the built-in fab capability profiles")
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
    if a.cmd == "checks":
        print(checks_markdown() if a.markdown else "\n".join(
            f"{cid:<16} {c.get('layer', ''):<9} {'/'.join(c.get('modes', [])):<10} {c['title']}"
            for cid, c in requirements.registry().items()))
        return 0
    if a.cmd == "profiles":
        for name, pr in dfm.builtin_profiles().items():
            src = pr.get("source") or {}
            print(f"{name}: {pr.get('title')}\n  source: {src.get('ref')} {src.get('url', '')}"
                  f"{'  (UNCONFIRMED: ' + src.get('note', '').strip() + ')' if src.get('confirmed') is False else ''}")
            print("  " + ", ".join(f"{k}={v}" for k, v in pr.items() if k not in ("title", "source")))
        return 0

    root, initialised = config.find_root(a.path)
    if root is None:
        print("No KiCad project (.kicad_pro) found from", a.path, file=sys.stderr)
        return EXIT_NO_PROJECT
    if a.cmd == "init" or not initialised:
        created = config.init_project(root, ci=getattr(a, "ci", None))
        if a.cmd == "init":
            print("\n".join(["Created:"] + [f"  {c}" for c in created]) if created else "Already initialised.")
            return 0
    proj = config.load_project(root)

    if a.cmd == "requirements":
        items = _lint(proj)
        if a.lint:
            for x in items:
                print(f"{x['level']:<8} {x['id']:<16} {x['text']}")
            print(f"{sum(x['level'] == 'error' for x in items)} errors, "
                  f"{sum(x['level'] == 'warning' for x in items)} warnings")
            return EXIT_CONFIG if any(x["level"] == "error" for x in items) else 0
        dfm.apply_profile_source(proj["requirements"], proj["params"])
        print(requirements_table(proj, a.markdown))
        return 0
    if a.cmd == "status":
        st = {"root": str(root), **proj["project"], "design_hash": config.design_hash(root),
              "human_pending": signoff.pending(root, proj["requirements"])}
        last = proj["dir"] / "reports" / "verify_report.json"
        if last.exists():
            rep = json.loads(last.read_text(encoding="utf-8"))
            if rep.get("verification"):
                st["last_verification"] = {"time": rep["time"], "mode": rep["mode"],
                                           "current": rep.get("design_hash") == st["design_hash"],
                                           "summary": rep["verification"]["summary"]["by_status"],
                                           "gates": {k: v["pass"] for k, v in rep["verification"]["gates"].items()}}
        print(json.dumps(st, indent=2, default=str))
        return 0
    if a.cmd == "signoff":
        ids = {r["id"] for r in proj["requirements"] if r.get("method") == "human"}
        if a.id not in ids:
            print(f"{a.id} is not a human requirement ({', '.join(sorted(ids))})", file=sys.stderr)
            return EXIT_CONFIG
        signoff.add(root, a.id, a.by, a.note, "fail" if a.fail else "pass")
        print(f"Signed {a.id} ({'FAIL' if a.fail else 'pass'}) for design {config.design_hash(root)[:12]}")
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
    if a.cmd == "audit":
        diffs, man = manifest.audit(root, proj, a.manifest)
        if diffs is None:
            print(man, file=sys.stderr)
            return EXIT_BLOCKED
        if not diffs:
            print(f"Audit OK: files match the release manifest of {man['time']} (design {man['design_hash'][:12]})")
            return 0
        print(f"Audit FAILED: {len(diffs)} differences with the release manifest of {man['time']}:")
        for g, p_, d in diffs:
            print(f"  {g:<12} {p_}: {d}")
        return EXIT_BLOCKED
    if a.cmd == "verify":
        rep, _ = analyse(root, "fast" if a.fast else "full")
        _emit(rep, a, a.gate)
        print(json.dumps(rep, indent=2, ensure_ascii=False) if a.json else report.text(rep, not a.all, gate=a.gate))
        return _exit(rep, a.gate)
    if a.cmd in ("review", "release"):
        rep, ctx = analyse(root, "full")
        if a.cmd == "release" and not rep["verification"]["gates"]["fab"]["pass"]:
            print(report.text(rep, gate="fab"))
            print("Release blocked: the fab gate does not pass.", file=sys.stderr)
            _emit(rep, a, "fab")
            return EXIT_BLOCKED
        rr = None
        if a.cmd == "review" or a.rerun_review or not ctx["review_current"]:
            results, rr, rep = do_review(root, a.model, rep=rep, ctx=ctx)
            print(_print_review(results, rr))
        else:
            print("Reusing the independent review on record for this design and requirement set.")
        gate = "release" if a.cmd == "release" else "dev"
        _emit(rep, a, gate)
        print(json.dumps(rep, indent=2, ensure_ascii=False) if a.json else report.text(rep, gate=gate))
        code = _exit(rep, gate)
        if a.cmd == "review" or code != EXIT_OK:
            if a.cmd == "release" and code != EXIT_OK:
                print("Release blocked: see the blockers above.", file=sys.stderr)
            return code
        _set_release(root, proj)
        review_path = proj["dir"] / "reports" / "review_report.json"
        man = manifest.build(root, proj, rep, review_path)
        path = manifest.write(root, proj, man)
        print(f"Released: design {config.design_hash(root)[:12]} (status: release). Manifest: {path}\n"
              "Run `kicadverify audit` before uploading the files to the fab.")
        return 0
    return 0


def requirements_table(proj, md=False):
    rows = []
    for r in sorted(proj["requirements"], key=lambda r: r["id"]):
        src = r.get("source") or {}
        rows.append((r["id"], r["method"], r["gate"], ", ".join(r["verified_by"]), r["acceptance"],
                     f"{src.get('kind', '-')}: {src.get('ref', '-')}" + (" (unconfirmed)" if src.get("confirmed") is False else ""),
                     r.get("text", "")))
    if md:
        out = ["| Requirement | Method | Gate | Verified by | Acceptance | Source | Text |", "|---|---|---|---|---|---|---|"]
        out += ["| " + " | ".join(str(c).replace("|", "\\|") for c in row) + " |" for row in rows]
        out += [f"| ~~{e['id']}~~ | excluded | | | | | {e.get('reason') or 'no reason'} |" for e in proj["excluded"]]
        return "\n".join(out)
    out = [f"{r[0]:<16} {r[1]:<6} {r[2]:<8} {r[3]:<18} {r[6]}\n{'':<16} source: {r[5]}" for r in rows]
    out += [f"{e['id']:<16} EXCLUDED: {e.get('reason') or 'no reason given'}" for e in proj["excluded"]]
    return "\n".join(out)


def checks_markdown():
    """docs/COVERAGE.md, generated from data/checks.yaml."""
    reg = requirements.registry()
    layers = {"kicad": "KiCad checks", "board": "Board checks (direct `.kicad_pcb` parse)",
              "circuit": "Circuit checks (schematic netlist)", "fab": "Fabrication and manufacturability",
              "project": "Project assertions", "review": "Independent reviewer", "human": "Human sign-off",
              "interface": "Outputs"}
    L = ["# Coverage: what each verifier catches and what it does not", "",
         "Generated by `kicadverify checks --markdown` from `kicadverify/data/checks.yaml`; do not edit by hand.", "",
         "A requirement is VERIFIED only when every verifier behind it ran, found nothing outside its acceptance "
         "criterion and covered its whole scope. Even then it means \"this kind of defect was not detected within "
         "this coverage\", never \"the board is correct\". The limits below are printed next to each requirement "
         "in every report. Measured detection rates: [RESULTS.md](RESULTS.md).", ""]
    for layer, title in layers.items():
        items = [(cid, c) for cid, c in reg.items() if c.get("layer") == layer]
        if not items:
            continue
        L += [f"## {title}", "", "| Verifier | Modes | Inputs | Covers | Does not cover |", "|---|---|---|---|---|"]
        for cid, c in items:
            L.append(f"| {cid} | {', '.join(c.get('modes', []))} | {', '.join(c.get('inputs', []))} | "
                     f"{c['covers']} | {c['limits']} |")
        L.append("")
    L += ["## Statuses", "",
          "| Status | Meaning |", "|---|---|",
          "| VERIFIED | Every verifier ran, found no defect beyond the acceptance criterion and checked its whole scope "
          "(unchecked items waived one by one by a person) |",
          "| FAILED | A verifier found a defect that is not waived |",
          "| NOT_VERIFIABLE | A verifier ran but could not decide: missing input, partial coverage, unconfirmed "
          "reviewer claim |",
          "| NOT_RUN | No verifier produced a result for the current design (fast mode, review not run, no valid "
          "sign-off) |", "",
          "## General limits", "",
          "- kicad-cli reads the saved file, not what is on screen in KiCad.",
          "- `kicad-cli pcb drc --schematic-parity` hangs on KiCad 10.0.5 with at least one real project; parity "
          "is computed by kicad-verify instead.",
          "- The checks and the reviewer can share blind spots with the tools that generated the design; the human "
          "sign-offs exist for that reason.", ""]
    return "\n".join(L)


def _set_release(root, proj):
    import yaml
    f = proj["dir"] / "project.yaml"
    data = config.load_yaml(f, {}) or {}
    data["status"] = "release"
    data["release_design_hash"] = config.design_hash(root)
    f.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
