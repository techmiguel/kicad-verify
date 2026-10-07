import argparse
import copy
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import (__version__, attest, config, interface, manifest, netlist, outputs, provenance, report, requirements,
               review, signoff, waivers)
from .checks import assertions, board as board_mod
from .checks import circuit, dfm, fab, happy, isolation, kicad_cli, parity
from .report import FAIL, PASS, SKIP, WARN, Result

EXIT_OK, EXIT_BLOCKED, EXIT_CONFIG, EXIT_NO_PROJECT = 0, 1, 2, 3
PROJECT_COMMANDS = (
    ("init", "create verification/pcb/ with templates (and, with --ci github, a CI workflow)"),
    ("verify", "run the deterministic verifiers and evaluate every requirement against a gate"),
    ("review", "verify, then run the independent reviewer (Claude Code CLI)"),
    ("release", "fab gate + review + sign-offs, then write the release manifest"),
    ("audit", "check that the files still match the release manifest"),
    ("attest", "verify and write an in-toto attestation"),
    ("status", "project status and the last verification, as JSON"),
    ("requirements", "list the requirement set (traceability table) or lint it"),
    ("interface", "export the board STEP and interface JSON for mechanical checks"),
    ("datasheets", "download the datasheets linked from the schematic"),
)


def _filter_disabled(results, disabled):
    return [r for r in results if r.check_id not in disabled
            and not any(d.endswith("*") and r.check_id.startswith(d[:-1]) for d in disabled)]


def _lint(proj):
    dfm.apply_profile_source(proj["requirements"], proj["params"])
    isolation.apply_sources(proj["requirements"], proj["params"])
    return requirements.lint(proj["requirements"], proj["excluded"], assertions.TYPES)


def _ambiguous(root):
    found = config.discover(root)
    names = "\n".join(f"  {os.path.relpath(k['pro'], root)}" for k in found)
    return (f"{len(found)} KiCad projects under {root}; kicad-verify reviews one board at a time. Choose one:\n"
            f"{names}\n  kicadverify init {root} --board <one of them>"
            "   (or `board:` in verification/pcb/project.yaml)")


def checks(root, proj, mode):
    """Every deterministic verifier. Returns (results before waivers, designs, kicad-happy summaries)."""
    params = proj["params"]
    cache = proj["dir"] / "reports"
    ev_dir = cache / "evidence"
    kicads = config.discover(root)
    results, designs, kh_summaries, jobs = [], {}, {}, []
    if not kicads:
        results.append(Result("DISCOVER", SKIP, "no KiCad project found"))
    elif len(kicads) > 1:  # never mixed: the checks and the design hash are about one board
        results.append(Result("DISCOVER", FAIL, _ambiguous(root).replace("\n", " ")))
        kicads = []
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
                results += board_mod.run(b, label, proj, k["pcb"], nl)
                results += parity.run(b, nl, label)
                results += dfm.run(b, k["pro"], label, params, k["pcb"])
                results += isolation.run(b, k["pro"], label, params, k["pcb"])
                results += fab.run(root, k["pcb"], b, label, params, mode)
            except Exception as e:
                results.append(Result("PCB-PARSE", FAIL, f"{label}: could not parse or check the PCB: {e}"))
        dnp = {fp["ref"] for fp in b["footprints"] if fp["dnp"]} if b else set()
        try:
            results += assertions.run(proj["requirements"], nl, b, label)
        except Exception as e:
            results.append(Result("ASSERT-ENGINE", FAIL, f"{label}: assertions crashed: {e}"))
        if (params.get("kicad_happy") or {}).get("enabled", True):
            try:
                kh = happy.analyze(k, gerber_dir if mode == "full" else None, cache / "kicad_happy" / label, params)
                results += happy.to_results(kh, label, params, dnp)
                kh_summaries[label] = happy.summary_for_review(kh)
            except Exception as e:
                results.append(Result("KH-ENGINE", WARN, f"{label}: kicad-happy failed: {e}",
                                      outcome=report.NOT_VERIFIABLE))
        if mode == "full":
            if k["pcb"].exists():
                jobs.append(lambda p=k["pcb"], lab=label, d=dnp: kicad_cli.drc(p, lab, ev_dir, d))
                jobs.append(lambda kk=k: _interface(kk, params, proj))
            jobs.append(lambda s=k["sch"], lab=label: kicad_cli.erc(s, lab, ev_dir))
    if jobs:  # ERC, DRC and the STEP export take tens of seconds each: run them in parallel
        with ThreadPoolExecutor(max_workers=4) as ex:
            for r in ex.map(lambda j: j(), jobs):
                results += r
    return results, designs, kh_summaries


def intent_check(prov, root):
    """GEN-INTENT-001: the design intent the board is judged against is declared."""
    i = prov["intent"]
    f = (i.get("file") or {}).get("path") or f"{config.DIRNAME}/{provenance.INTENT_FILE}".replace("\\", "/")
    if not i["present"]:
        return report.not_verifiable("GEN-INTENT-001", "no design intent file", f"write {f}")
    total = len(i["declared"]) + len(i["missing"])
    if not total:
        return report.not_verifiable("GEN-INTENT-001", f"{f} declares no fields ('- Field: value' lines)")
    gaps = [{"key": waivers.vkey("intent", m), "text": f"{m}: not declared"} for m in i["missing"]]
    return Result("GEN-INTENT-001", PASS, f"{len(i['declared'])}/{total} intent fields declared "
                  f"(intent {i['digest'][7:19]})", evidence=[Path(root) / f],
                  coverage=report.coverage("design intent fields", total, gaps))


def review_key(prov, reqs_hash):
    """What a review depends on besides the design: the requirements it judged, the intent and context
    it was given, the datasheets it could quote and the prompt it ran with."""
    return provenance.digest({"requirements": reqs_hash, "intent": prov["intent"]["digest"],
                              "datasheets": provenance.digest(prov["artifacts"]["groups"]["datasheets"]),
                              "prompt": prov["policy"]["components"]["reviewer_prompt"]})


def finish(root, proj, mode, raw_results, dh=None, review_note=None, prov=None, review_prov=None):
    """Waivers, requirement verdicts, gates and report files from the raw verifier results."""
    root = Path(root)
    dh = dh or config.design_hash(root)
    prov = prov or provenance.collect(root, proj)
    results = waivers.apply(_filter_disabled(copy.deepcopy(raw_results), proj["disabled"]), proj["waivers"])
    reqs = proj["requirements"]
    dfm.apply_profile_source(reqs, proj["params"])
    isolation.apply_sources(reqs, proj["params"])
    ver = requirements.verification(reqs, proj["excluded"], results, root, mode, proj["params"], _lint(proj))
    if review_note:
        for v in ver["requirements"]:
            if v["method"] == "model" and v["status"] == report.NOT_RUN:
                v["reason"] += f" ({review_note})"
    rep = report.build(proj["project"]["name"], mode, results, report.tool_versions(config.KICAD_CLI), {
        "status": proj["project"]["status"], "design_hash": dh,
        "requirements_hash": requirements.requirements_hash(reqs),
        "human_pending": signoff.pending(root, reqs, prov["policy"]), "verification": ver, "provenance": prov,
        "review_provenance": review_prov})
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
    prov = provenance.collect(root, proj, sorted(kh))
    raw = det + [intent_check(prov, root)] + signoff.results(root, proj["requirements"], dh, prov["policy"])
    rkey = review_key(prov, requirements.requirements_hash(proj["requirements"]))
    rev, why, rprov = review.load_current(proj, dh, rkey)
    rep, results = finish(root, proj, mode, raw + (rev or []), dh, None if rev else why, prov, rprov)
    return rep, {"proj": proj, "designs": designs, "kh": kh, "raw": raw, "results": results, "dh": dh,
                 "review_current": rev is not None, "prov": prov, "review_key": rkey}


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
    rkey = ctx["review_key"]
    rprov = review.provenance_of(proj["root"], bdir, ctx["prov"], rkey)
    try:
        raw, meta = review.run_reviewer(proj["root"], bdir, model, timeout)
    except Exception as e:
        res = [Result("REV-RUN", FAIL, f"reviewer did not run: {e}")]
        review.write_report(proj, {"model": model, "error": str(e)}, [], [], [], rkey, rprov)
        rep2, _ = finish(root, proj, rep["mode"], ctx["raw"] + res, ctx["dh"], prov=ctx["prov"])
        return res, None, rep2
    (bdir / "reviewer_raw.json").write_text(json.dumps(raw, indent=1, ensure_ascii=False), encoding="utf-8")
    results, detail, extras = review.judge(raw, reqs, proj["root"], bdir)
    rprov["reviewer"] = meta
    rr, path = review.write_report(proj, meta, detail, extras, results, rkey, rprov)
    rep2, waived = finish(root, proj, rep["mode"], ctx["raw"] + results, ctx["dh"], prov=ctx["prov"],
                          review_prov=rprov)
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


def build_parser():
    ap = argparse.ArgumentParser(prog="kicadverify",
                                 description="Requirements-based verification for KiCad 10 projects: every "
                                             "requirement has a source, a verification method, evidence and a status "
                                             "(VERIFIED / FAILED / NOT_VERIFIABLE / NOT_RUN); gates decide from them.",
                                 epilog="Start with `kicadverify init PATH`, "
                                        "then `kicadverify verify PATH --gate fab`.")
    ap.add_argument("--version", action="version", version=f"kicad-verify {__version__}")
    sub = ap.add_subparsers(dest="cmd", required=True, metavar="COMMAND")
    for name, help_ in PROJECT_COMMANDS:
        p = sub.add_parser(name, help=help_, description=help_)
        p.add_argument("path", nargs="?", default=".", help="KiCad project directory (default: current directory)")
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
        if name in ("verify", "review", "release", "attest"):
            p.add_argument("--attest", action="store_true", default=name in ("release", "attest"),
                           help="write an in-toto attestation of this verification to verification/pcb/attestations/")
            p.add_argument("--sign-key", default=os.environ.get("KICAD_VERIFY_SIGN_KEY"),
                           help="SSH private key to sign the attestation (ssh-keygen -Y sign); "
                                "default $KICAD_VERIFY_SIGN_KEY")
        if name == "release":
            p.add_argument("--rerun-review", action="store_true",
                           help="run the reviewer even when a review for this exact design is on record")
        if name == "init":
            p.add_argument("--ci", choices=["github"], help="also write a CI workflow")
            p.add_argument("--board", help="the .kicad_pro to verify when the folder holds several boards "
                                           "(recorded as `board:` in verification/pcb/project.yaml)")
        if name == "requirements":
            p.add_argument("--lint", action="store_true", help="validate the requirement set; exit 2 on errors")
            p.add_argument("--markdown", action="store_true", help="print the requirement table as Markdown")
        if name == "audit":
            p.add_argument("--manifest", help="manifest to check (default verification/pcb/release/manifest.json)")
    p = sub.add_parser("signoff", help="record a human sign-off for the current design")
    p.add_argument("id", help="human requirement, e.g. HUM-FIT-001")
    p.add_argument("--by", required=True, help="who checked it")
    p.add_argument("--note", default="", help="what was checked and how")
    p.add_argument("--fail", action="store_true", help="record that the check found the requirement NOT met")
    p.add_argument("--path", default=".", help="project path (default: current directory)")
    p = sub.add_parser("check-attestation", help="re-check an attestation against the files on disk")
    p.add_argument("attestation", help="the .intoto.json file")
    p.add_argument("--path", default=".")
    p.add_argument("--allowed-signers", help="ssh allowed_signers file to verify the signature")
    p.add_argument("--identity", help="expected signer identity (default: looked up in allowed signers)")
    p = sub.add_parser("explain", help="the chain of proof behind one requirement's verdict")
    p.add_argument("id", help="requirement, e.g. CIR-POL-001")
    p.add_argument("--path", default=".")
    p = sub.add_parser("checks", help="list the verifiers with what they cover and do not cover")
    p.add_argument("--markdown", action="store_true", help="print the table as Markdown (docs/COVERAGE.md)")
    sub.add_parser("profiles", help="list the built-in fab capability profiles")
    sub.add_parser("setup", help="download the pinned kicad-happy engine")
    p = sub.add_parser("install-claude", help="install hooks and skills into ~/.claude")
    p.add_argument("--no-hooks", action="store_true", help="install the skills only")
    sub.add_parser("uninstall-claude", help="remove the hooks and skills added by install-claude")
    return ap


def main(argv=None):
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    a = build_parser().parse_args(argv)

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

    if a.cmd == "check-attestation":
        return _check_attestation(a)

    board = getattr(a, "board", None)
    if str(a.path).lower().endswith(".kicad_pro"):  # `init path/to/board.kicad_pro` chooses the board
        board = board or a.path
        a.path = str(Path(a.path).parent)
    root, initialised = config.find_root(a.path)
    if root is None:
        print("No KiCad project (.kicad_pro) found from", a.path, file=sys.stderr)
        return EXIT_NO_PROJECT
    if a.cmd == "init" or not initialised:
        if board and not Path(board).resolve().is_file():
            print(f"--board {board}: no such .kicad_pro", file=sys.stderr)
            return EXIT_CONFIG
        if not board and len(config.discover(root)) > 1 and not (root / config.DIRNAME / "project.yaml").exists():
            print(_ambiguous(root), file=sys.stderr)
            return EXIT_CONFIG
        created = config.init_project(root, ci=getattr(a, "ci", None), board=board)
        if a.cmd == "init":
            print("\n".join(["Created:"] + [f"  {c}" for c in created]) if created else "Already initialised.")
            return 0
    proj = config.load_project(root)
    if a.cmd in ("verify", "attest", "review", "release", "interface", "datasheets") and not config.kicad_cli_found():
        print(f"kicad-cli not found ({config.KICAD_CLI}): ERC, DRC, the netlist and the Gerber re-plot cannot run, "
              "so the requirements that need them stay NOT_VERIFIABLE. Install KiCad 10 or point KICAD_CLI at it.",
              file=sys.stderr)

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
        signoff.add(root, a.id, a.by, a.note, "fail" if a.fail else "pass", provenance.policy(proj))
        print(f"Signed {a.id} ({'FAIL' if a.fail else 'pass'}) for design {config.design_hash(root)[:12]} "
              f"under policy {provenance.policy(proj)['digest'][7:19]}")
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
    if a.cmd == "explain":
        return _explain(root, a.id)
    if a.cmd in ("verify", "attest"):
        fast = getattr(a, "fast", False)
        gate = getattr(a, "gate", "fab")
        rep, _ = analyse(root, "fast" if fast else "full")
        _emit(rep, a, gate)
        print(json.dumps(rep, indent=2, ensure_ascii=False) if getattr(a, "json", False) else
              report.text(rep, not getattr(a, "all", False), gate=gate))
        if a.attest and not _attest(root, proj, rep, a.sign_key):
            return EXIT_CONFIG
        return _exit(rep, gate)
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
            if a.attest and a.cmd == "review":
                _attest(root, proj, rep, a.sign_key)
            return code
        _set_release(root, proj)
        review_path = proj["dir"] / "reports" / "review_report.json"
        att = _attest(root, proj, rep, a.sign_key) if a.attest else None
        man = manifest.build(root, proj, rep, review_path, att)
        path = manifest.write(root, proj, man)
        print(f"Released: design {config.design_hash(root)[:12]} (status: release). Manifest: {path}\n"
              "Run `kicadverify audit` before uploading the files to the fab.")
        return 0
    return 0


def _attest(root, proj, rep, key=None):
    """Writes (and signs, with a key) the attestation of `rep`. Returns its path, or None on error."""
    try:
        stmt = attest.statement(rep)
        f = attest.write(stmt, proj["dir"] / "attestations")
        sig = attest.sign(f, key) if key else None
    except Exception as e:
        print(f"Attestation failed: {e}", file=sys.stderr)
        return None
    print(f"Attestation: {f}" + (f" (signed: {sig.name})" if sig else " (unsigned: pass --sign-key to sign it)"))
    return f


def _check_attestation(a):
    f = Path(a.attestation)
    root, _ = config.find_root(a.path)
    if root is None:
        print("No KiCad project found from", a.path, file=sys.stderr)
        return EXIT_NO_PROJECT
    try:
        res = attest.check(f, root, a.allowed_signers, a.identity)
    except (ValueError, json.JSONDecodeError) as e:
        print(f"{f}: {e}", file=sys.stderr)
        return EXIT_CONFIG
    p = res["predicate"]
    print(f"Attestation {f.name}: {p['project']}, design {p['design_hash'][:12]}, verified {p['verified_at']}")
    sig = res["signature"]
    print("  signature:  " + ("not signed" if sig is None and not Path(str(f) + '.sig').exists() else
                              f"VALID ({sig['signer']})" if sig and sig["valid"] else
                              f"NOT VERIFIED ({(sig or {}).get('error')})"))
    sub = res["subject"]
    print(f"  artifacts:  {len(sub['unchanged'])} unchanged, {len(sub['changed'])} changed, "
          f"{len(sub['missing'])} missing, {len(res['new'])} new design/fabrication files")
    for k in ("changed", "missing"):
        for x in sub[k]:
            print(f"    {k}: {x}")
    for x in res["new"]:
        print(f"    new: {x}")
    for k, same in res["same"].items():
        was, now = res["values"][k]
        line = f"  {k:<18}{'same' if same else 'DIFFERENT'}"
        if not same:
            line += f": attested {was}, now {now}"
            if k == "policy":
                line += f" (changed: {', '.join(res.get('policy_components_changed', []))})"
        print(line)
    s = p["summary"]["by_status"]
    print(f"  claimed:    {', '.join(f'{k} {v}' for k, v in s.items())}; gates "
          + ", ".join(f"{k} {'PASS' if g['pass'] else 'BLOCKED'}" for k, g in p["gates"].items()))
    print("The attestation " + ("HOLDS for the files on disk." if res["holds"] else
                                "does NOT hold for the files on disk (see above)."))
    return 0 if res["holds"] else EXIT_BLOCKED


def _explain(root, rid):
    f = Path(root) / config.DIRNAME / "reports" / "verify_report.json"
    if not f.exists():
        print("No verification report yet: run `kicadverify verify` first.", file=sys.stderr)
        return EXIT_BLOCKED
    rep = json.loads(f.read_text(encoding="utf-8"))
    v = next((x for x in rep["verification"]["requirements"] if x["id"] == rid), None)
    if v is None:
        ex = next((e for e in rep["verification"].get("excluded", []) if e["id"] == rid), None)
        print(f"{rid}: excluded ({ex.get('reason') or 'no reason given'})" if ex else f"{rid}: not in the report",
              file=sys.stderr)
        return EXIT_CONFIG
    prov = rep.get("provenance") or {}
    cur = config.design_hash(root) == rep["design_hash"]
    L = [f"{v['id']}: {v['status']}" + (f" ({v['severity']})" if v.get("severity") else ""),
         f"  requirement  {v['text']}",
         f"  source       {(v.get('source') or {}).get('kind')}: {(v.get('source') or {}).get('ref')}"
         + (" (UNCONFIRMED)" if (v.get("source") or {}).get("confirmed") is False else ""),
         f"  method       {v['method']} via {', '.join(v['verified_by'])}; "
         f"acceptance {v['acceptance']}; gate {v['gate']}",
         f"  reason       {v['reason']}"]
    c = v.get("coverage")
    if c:
        L.append(f"  coverage     {c['checked']}/{c['total']} {c['scope']}")
        L += [f"    not checked [{u['key']}] {u['text']}" for u in c.get("unchecked", [])]
    L += [f"  finding      [{x['key']}] {x['text']}" for x in v.get("findings", [])]
    L += [f"  deviation    [{d['key']}] {d['text']} - waived: {d['reason']} ({d.get('by') or 'unnamed'}, {d['date']})"
          for d in v.get("deviations", [])]
    for e in v.get("evidence", []):
        L.append(f"  evidence     {e['check']}: {e['status']} -> {e.get('outcome') or '-'}: {e['detail'][:120]}")
        for art in e["artifacts"]:
            if art.get("sha256"):
                q = f" \"{art['quote'][:80]}\"" if art.get("quote") else ""
                L.append(f"               {art['path']} sha256:{art['sha256'][:16]}{q}")
    if v.get("limits"):
        L.append(f"  not covered  {v['limits']}")
    if prov:
        t = prov["tools"]
        L += [f"  verified at  {rep['time']} ({rep['mode']} mode) on design {rep['design_hash'][:12]}"
              + ("" if cur else " - the design has CHANGED since"),
              f"  policy       {prov['policy']['digest'][:23]} ({prov['policy']['requirements']} requirements, "
              f"{prov['policy']['waivers']} waivers, fab profile {prov['policy']['fab_profile']})",
              f"  intent       {prov['intent']['digest'][:23]} ({len(prov['intent']['declared'])} fields declared, "
              f"{len(prov['intent']['missing'])} missing)",
              f"  tools        kicad-verify {t['kicad-verify']['version']} {t['kicad-verify']['code_digest'][:23]}, "
              f"{t['kicad-cli']['version']}, kicad-happy {t['kicad-happy']['pinned']}"]
        rp = rep.get("review_provenance")
        if v["method"] == "model" and rp:
            who = rp.get("reviewer") or {}
            L.append(f"  reviewer     {who.get('models') or who.get('model')}, "
                     f"bundle {rp.get('bundle_digest', '')[:23]}, claude {rp.get('claude_cli')}")
    print("\n".join(L))
    return 0


def requirements_table(proj, md=False):
    rows = []
    for r in sorted(proj["requirements"], key=lambda r: r["id"]):
        src = r.get("source") or {}
        cite = f"{src.get('kind', '-')}: {src.get('ref', '-')}"
        if src.get("confirmed") is False:
            cite += " (unconfirmed)"
        rows.append((r["id"], r["method"], r["gate"], ", ".join(r["verified_by"]), r["acceptance"], cite,
                     r.get("text", "")))
    if md:
        out = ["| Requirement | Method | Gate | Verified by | Acceptance | Source | Text |",
               "|---|---|---|---|---|---|---|"]
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
          "| VERIFIED | Every verifier ran, found no defect beyond the acceptance criterion and checked its whole "
          "scope (unchecked items waived one by one by a person) |",
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
