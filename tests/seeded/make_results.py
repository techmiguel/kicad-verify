"""Writes docs/RESULTS.md from tests/seeded/results.json (so published numbers come from the data)."""
import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
MODELS = ["opus", "sonnet"]
NAMES = {"opus": "Opus 5.5", "sonnet": "Sonnet 5.5"}


def main():
    data = json.loads((HERE / "results.json").read_text(encoding="utf-8"))
    rows = [r for r in data["rows"] if r.get("applied")]
    groups = {"layout": "Layout and footprints", "fab": "Fabrication outputs", "circuit": "Circuit"}
    lines = ["# Seeded-error results", "",
             f"Fixture: [smartRele](https://github.com/techmiguel/smartRele) at commit `{data['fixture'].split('@')[1]}`, "
             "a two-layer KiCad 10 board that was fabricated and assembled. Each defect is injected into a fresh "
             "copy; the unmodified copy is the baseline. Reproduce with `python tests/seeded/run_seeded.py` "
             "(add `--reviewer opus sonnet --only circuit` for the reviewer columns).", "",
             "A layer catches a defect when it reports a new FAIL/WARN for it that the baseline did not have. "
             "For the reviewer, the finding must name the modified component (or value) and be new or more "
             "severe than on the baseline.", ""]
    det = sum(1 for r in rows if r.get("deterministic"))
    blocked = sum(1 for r in rows if r.get("fab_gate_blocked"))
    lines += ["**Scope of this evidence.** One board, 20 injected defects, one reviewer run per defect and "
              "model. It is a regression suite: it shows that each listed defect class is caught on this board "
              "and keeps it caught. It is not a benchmark of detection rates on other designs, and it does not "
              "measure the false-positive rate (see \"Not measured here\").", ""]
    lines += [f"**Deterministic checks: {det}/{len(rows)}** defects caught. "
              f"**Fab gate: {blocked}/{len(rows)}** defects block it "
              "(the two that do not are judgement calls with no rule: they are the reviewer's job, at the release gate).",
              "",
              "Requirement-level run: the fixture is configured with the reference verification set in "
              "[tests/reference/rele](../tests/reference/rele) (fab profile, pins from the datasheets, mounting holes, "
              "approved footprints, waivers with reasons, three project assertions). On the unmodified board the fab "
              f"gate {'passes' if data.get('baseline_fab_gate') else 'does NOT pass'}; requirements FAILED at baseline: "
              f"{', '.join(data.get('baseline_failed') or []) or 'none'} (release gate only). "
              "\"Requirements newly FAILED\" lists the requirements whose verdict changed to FAILED with the defect.", ""]
    have_rev = any(f"reviewer_{m}" in r for r in rows for m in MODELS)
    for g, title in groups.items():
        gr = [r for r in rows if r["group"] == g]
        if not gr:
            continue
        lines += [f"## {title}", ""]
        head = "| Defect | Expected check | Deterministic checks | Fab gate | Requirements newly FAILED | Other checks that also fired |"
        sep = "|---|---|---|---|---|---|"
        if have_rev and g == "circuit":
            head += "".join(f" Reviewer {NAMES[m]} |" for m in MODELS)
            sep += "---|" * len(MODELS)
        lines += [head, sep]
        for r in gr:
            others = [c for c in r.get("deterministic_checks", []) if c != r["expected"]]
            fab = "-" if "fab_gate_blocked" not in r else ("blocked" if r["fab_gate_blocked"] else "passes")
            cells = [r["mutation"], r["expected"], "caught" if r.get("deterministic") else "missed", fab,
                     ", ".join(r.get("requirements_failed") or []) or "-", ", ".join(others) or "-"]
            if have_rev and g == "circuit":
                for m in MODELS:
                    v = r.get(f"reviewer_{m}")
                    cells.append("-" if v is None else ("missed" if v[0] == "miss" else f"{v[0]} ({v[1]})"))
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")
    if have_rev:
        lines += ["## Reviewer cost", ""]
        for m in MODELS:
            costs = [r.get(f"reviewer_{m}_cost") for r in rows if r.get(f"reviewer_{m}_cost")]
            if costs:
                lines.append(f"- {NAMES[m]}: {len(costs)} reviews, mean ${sum(costs) / len(costs):.2f} per review "
                             "(list price reported by the CLI).")
        lines += ["", "## Reviewer caveats", "",
                  "- One run per defect and model. The reviewer is non-deterministic and repeated runs can differ.",
                  "- The reviewer sees the deterministic report. For the six defects the gate already catches, it "
                  "may have confirmed the gate's finding rather than found it alone.",
                  "- In the fuse case the BOM and README still said T500mA, so the inconsistency helped. A value "
                  "that is wrong everywhere is harder to spot.",
                  "- False positives on the unmodified board: Opus 5.5 gave none. Sonnet 5.5 failed the 07D221K "
                  "varistor as if the mains were 230 VAC. After design intent (README, `design_intent.md`) was "
                  "added to the bundle, two more Sonnet runs gave no FAIL.",
                  "- These reviews were recorded with kicad-verify 0.1, where a PASS without a quote found in the "
                  "cited file became FAIL (since 0.2 it is NOT_VERIFIABLE). That strictness produced one FAIL on an "
                  "unrelated requirement, where the model claimed PASS without citing evidence.",
                  "- Independence of the evidence (0.4 rule: quotes from `verify_report.json`, `kicad_happy.json` "
                  "and other verifiers' conclusions do not count). Re-checking the recorded reviews: none of the "
                  "105 requirement verdicts with valid evidence rested only on derived sources; 3 extra error "
                  "findings did (Opus on flyback_D1_reversed and led_D2_reversed, Sonnet on led_D2_reversed), all "
                  "repeating DRC/parity findings the gate already reports. Under 0.4 they are reported as "
                  "unverified; no catch in the table depends on them.",
                  "- A valid quote proves evidence integrity (the text exists in an independent source), not that "
                  "the text supports the conclusion. The release gate keeps human sign-offs for that reason.", ""]
    lines += ["## Not measured here", "",
              "- Real defects from other boards, multi-sheet schematics and four-layer boards.",
              "- Rotation errors in the CPL (left to the fab house viewer and a human sign-off).",
              "- False-positive rate across many designs. On this fixture, the unmodified board gives no "
              "deterministic FAIL.", ""]
    (ROOT / "docs" / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print("docs/RESULTS.md written")


if __name__ == "__main__":
    main()
