"""End-to-end tests of the release chain through the real CLI (no KiCad, no network, no model):
the deterministic verifiers and the reviewer are replaced by fakes, everything else is the code that
runs in production: requirement evaluation, gates, sign-offs, review reuse, manifest, attestation,
audit."""
import json

import pytest
import yaml

from kicadverify import cli, config, requirements as rq, review, signoff
from kicadverify.report import FAIL, NOT_RUN, PASS, Result

INTENT = "\n".join(f"- {k}: value" for k in (
    "Purpose of the board", "Supply input (voltage range, source, max current)",
    "Loads and their currents (motors, relays, LEDs, radios)", "External connectors and what plugs into them",
    "Environment (temperature, humidity, mains, battery, enclosure)", "Target fab/assembly house and process",
    "Decisions that look wrong but are intentional")) + "\n"
SCH = "(kicad_sch (symbol (lib_id \"Device:R\") (property \"Reference\" \"R1\") (property \"Value\" \"10k\")))"


@pytest.fixture
def project(tmp_path, monkeypatch):
    """An initialised project whose every automatic requirement passes, unless a test says otherwise."""
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    (tmp_path / "b.kicad_sch").write_text(SCH)
    (tmp_path / "fab").mkdir()
    (tmp_path / "fab" / "b-F_Cu.gbr").write_text("G04*")
    config.init_project(tmp_path)
    vd = tmp_path / config.DIRNAME
    (vd / "design_intent.md").write_text(INTENT)
    failing = set()

    def fake_checks(root, proj, mode):
        ids = set()
        for r in proj["requirements"]:
            if r["method"] == "auto" and r["id"] != "GEN-INTENT-001":
                ids |= {v.replace("*", "X") for v in r["verified_by"]}
        res = [Result(i, FAIL if i in failing else PASS, "fake", evidence=[root / "b.kicad_pcb"],
                      violations=[{"key": "k", "text": "fake defect"}] if i in failing else None)
               for i in sorted(ids)]
        return res, {}, {}

    calls = []

    def fake_reviewer(root, bdir, model, timeout_s=1800):
        calls.append(model)
        reqs = json.loads((bdir / "requirements.json").read_text())
        return ({"verdicts": [{"id": r["id"], "verdict": "PASS", "summary": "checked",
                               "evidence": [{"file": "b.kicad_sch", "quote": "property \"Value\" \"10k\""}]}
                              for r in reqs], "extra_findings": []},
                {"model": model, "duration_s": 0, "cost_usd": 0})

    monkeypatch.setattr(cli, "checks", fake_checks)
    monkeypatch.setattr(review, "run_reviewer", fake_reviewer)
    monkeypatch.delenv("KICAD_VERIFY_SIGN_KEY", raising=False)
    return {"root": tmp_path, "dir": vd, "failing": failing, "reviewer_calls": calls}


def _sign_all(root):
    proj = config.load_project(root)
    for r in proj["requirements"]:
        if r["method"] == "human":
            assert cli.main(["signoff", r["id"], "--by", "ana", "--path", str(root)]) == 0


def _release(root):
    return cli.main(["release", str(root)])


def test_release_creates_manifest_and_attestation(project):
    root, vd = project["root"], project["dir"]
    _sign_all(root)
    assert _release(root) == cli.EXIT_OK
    assert project["reviewer_calls"] == ["opus"]
    man = json.loads((vd / "release" / "manifest.json").read_text())
    assert {r["status"] for r in man["requirements"]} == {"VERIFIED"}
    assert man["gates"] == {"dev": True, "fab": True, "release": True}
    assert set(man["reports"]) >= {"verification_report", "review_report", "attestation"}
    for r in man["reports"].values():
        assert r["archived"].startswith(f"{config.DIRNAME}/release/".replace("\\", "/"))
    atts = list((vd / "attestations").glob("*.intoto.json"))
    assert len(atts) == 1 and man["reports"]["attestation"]["path"].endswith(atts[0].name)
    assert yaml.safe_load((vd / "project.yaml").read_text())["status"] == "release"
    assert cli.main(["audit", str(root)]) == cli.EXIT_OK
    assert cli.main(["check-attestation", str(atts[0]), "--path", str(root)]) == cli.EXIT_OK


def test_release_blocked_without_signoffs_writes_no_manifest(project):
    root, vd = project["root"], project["dir"]
    assert _release(root) == cli.EXIT_BLOCKED
    assert not (vd / "release" / "manifest.json").exists()
    assert yaml.safe_load((vd / "project.yaml").read_text())["status"] == "dev"


def test_release_blocked_by_failed_fab_requirement(project):
    root, vd = project["root"], project["dir"]
    project["failing"].add("PCB-DRC-001")
    _sign_all(root)
    assert any("PCB-DRC-001" in r["verified_by"] for r in config.load_project(root)["requirements"])
    assert _release(root) == cli.EXIT_BLOCKED
    assert project["reviewer_calls"] == []  # the fab gate stops it before the reviewer runs
    assert not (vd / "release" / "manifest.json").exists()


def test_audit_detects_changed_report_and_outputs(project):
    root, vd = project["root"], project["dir"]
    _sign_all(root)
    assert _release(root) == cli.EXIT_OK
    # a later `verify` rewrites reports/: the archived copies bound by the manifest still match
    assert cli.main(["verify", str(root)]) in (cli.EXIT_OK, cli.EXIT_BLOCKED)
    assert cli.main(["audit", str(root)]) == cli.EXIT_OK
    man = json.loads((vd / "release" / "manifest.json").read_text())
    archived = root / man["reports"]["review_report"]["archived"]
    archived.write_text(archived.read_text().replace('"PASS"', '"FAIL"', 1) + " ")
    diffs, _ = cli.manifest.audit(root, config.load_project(root))
    assert ("reports", man["reports"]["review_report"]["archived"], "review_report changed") in diffs
    (root / "fab" / "b-F_Cu.gbr").write_text("G04 changed after release*")
    assert cli.main(["audit", str(root)]) == cli.EXIT_BLOCKED


def test_changed_rules_invalidate_signoffs_and_review(project):
    """A .kicad_dru change is a design change: sign-offs and the review stop counting."""
    root = project["root"]
    _sign_all(root)
    assert _release(root) == cli.EXIT_OK
    n = len(project["reviewer_calls"])
    (root / "b.kicad_dru").write_text("(version 1) (rule x (constraint clearance (min 0.1mm)))")
    proj = config.load_project(root)
    humans = [r["id"] for r in proj["requirements"] if r["method"] == "human"]
    assert signoff.pending(root, proj["requirements"]) == humans
    rep, ctx = cli.analyse(root, "full")
    assert not ctx["review_current"]
    v = {x["id"]: x for x in rep["verification"]["requirements"]}
    assert all(v[h]["status"] == NOT_RUN for h in humans)
    assert not rep["verification"]["gates"]["release"]["pass"]
    assert len(project["reviewer_calls"]) == n


def test_changed_policy_invalidates_signoffs(project):
    root, vd = project["root"], project["dir"]
    _sign_all(root)
    data = yaml.safe_load((vd / "requirements.yaml").read_text()) or {}
    data.setdefault("params", {})["fab"] = {"profile": "jlcpcb-1-2-layer-standard"}
    (vd / "requirements.yaml").write_text(yaml.safe_dump(data))
    proj = config.load_project(root)
    assert signoff.pending(root, proj["requirements"])


def test_reviewer_model_is_part_of_the_policy_and_the_review_key(project):
    """project.yaml: the reviewer model is a verification input; the release state is not."""
    root, vd = project["root"], project["dir"]
    _sign_all(root)
    assert _release(root) == cli.EXIT_OK
    proj = config.load_project(root)
    # release wrote status/release_design_hash: sign-offs and the review still hold
    assert signoff.pending(root, proj["requirements"]) == []
    assert cli.analyse(root, "full")[1]["review_current"]
    data = yaml.safe_load((vd / "project.yaml").read_text())
    data["name"] = "renamed"  # a label: changes nothing
    (vd / "project.yaml").write_text(yaml.safe_dump(data))
    assert signoff.pending(root, config.load_project(root)["requirements"]) == []
    data["reviewer_model"] = "some-other-model"
    (vd / "project.yaml").write_text(yaml.safe_dump(data))
    proj = config.load_project(root)
    from kicadverify import provenance
    pending = signoff.pending(root, proj["requirements"])
    assert pending and "project" in signoff._stale(signoff.load(root)[0], config.design_hash(root),
                                                   provenance.policy(proj))
    assert not cli.analyse(root, "full")[1]["review_current"]


def test_missing_verifier_is_not_run(project):
    root = project["root"]
    orig = cli.checks

    def without_drc(r, p, m):
        res, d, k = orig(r, p, m)
        return [x for x in res if x.check_id != "PCB-DRC-001"], d, k
    cli.checks = without_drc
    try:
        rep, _ = cli.analyse(root, "full")
    finally:
        cli.checks = orig
    v = {x["id"]: x for x in rep["verification"]["requirements"]}
    drc = next(r for r in v.values() if "PCB-DRC-001" in r["verified_by"])
    assert drc["status"] == NOT_RUN and not rep["verification"]["gates"]["fab"]["pass"]


def test_missing_fab_outputs_block_the_fab_gate(tmp_path):
    """The real FAB-OUT check (no fakes): a board with no Gerbers cannot pass the fab gate."""
    from kicadverify.checks import fab
    (tmp_path / "b.kicad_pro").write_text("{}")
    (tmp_path / "b.kicad_pcb").write_text("(kicad_pcb)")
    config.init_project(tmp_path)
    proj = config.load_project(tmp_path)
    g, d, b, c = fab.find_outputs(tmp_path, proj["params"])
    assert not g
    res = fab.run(tmp_path, tmp_path / "b.kicad_pcb", None, "b", proj["params"], "fast")
    reqs = [r for r in proj["requirements"] if any(rq.matches(v, x.check_id) for x in res for v in r["verified_by"])]
    ver = rq.verification(reqs, [], res, tmp_path, "fast", proj["params"])
    assert reqs and not ver["gates"]["fab"]["pass"]


def test_package_version_is_consistent():
    """The version recorded in every provenance block must be the one the package is installed as."""
    import re
    from pathlib import Path

    from kicadverify import __version__
    text = (Path(__file__).resolve().parent.parent / "pyproject.toml").read_text()
    assert re.search(r'^version = "([^"]+)"', text, re.M).group(1) == __version__
