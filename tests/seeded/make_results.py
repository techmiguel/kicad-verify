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
             "Fixture: [smartRele](https://github.com/techmiguel/smartRele) "
             f"at commit `{data['fixture'].split('@')[1]}`, "
             "a two-layer KiCad 10 board that was fabricated and assembled. Each defect is injected into a fresh "
             "copy; the unmodified copy is the baseline. Reproduce with `python tests/seeded/run_seeded.py` "
             "(add `--reviewer opus sonnet --only circuit` for the reviewer columns).", "",
             "A layer catches a defect when it reports a new FAIL/WARN for it that the baseline did not have. "
             "For the reviewer, the finding must name the modified component (or value) and be new or more "
             "severe than on the baseline.", ""]
    det = sum(1 for r in rows if r.get("deterministic"))
    blocked = sum(1 for r in rows if r.get("fab_gate_blocked"))
    lines += [f"**Deterministic checks: {det}/{len(rows)}** defects caught. "
              f"**Fab gate: {blocked}/{len(rows)}** defects block it "
              "(the two that do not are judgement calls with no rule: they are the reviewer's job, "
              "at the release gate).",
              "",
              "Requirement-level run: the fixture is configured with the reference verification set in "
              "[tests/reference/rele](../tests/reference/rele) (fab profile, pins from the datasheets, mounting holes, "
              "approved footprints, waivers with reasons, three project assertions). On the unmodified board the fab "
              f"gate {'passes' if data.get('baseline_fab_gate') else 'does NOT pass'}; "
              "requirements FAILED at baseline: "
              f"{', '.join(data.get('baseline_failed') or []) or 'none'} (release gate only). "
              "\"Requirements newly FAILED\" lists the requirements whose verdict changed to FAILED "
              "with the defect.", ""]
    have_rev = any(f"reviewer_{m}" in r for r in rows for m in MODELS)
    for g, title in groups.items():
        gr = [r for r in rows if r["group"] == g]
        if not gr:
            continue
        lines += [f"## {title}", ""]
        head = ("| Defect | Expected check | Deterministic checks | Fab gate | Requirements newly FAILED "
                "| Other checks that also fired |")
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
                  "- A PASS without a quote that kicad-verify finds in the cited file becomes FAIL. This strictness "
                  "also produced one FAIL on an unrelated requirement, where the model claimed PASS without "
                  "citing evidence.", ""]
    lines += ["## Not measured here", "",
              "- Real defects from other boards, multi-sheet schematics and four-layer boards.",
              "- Rotation errors in the CPL (left to the fab house viewer and a human sign-off).",
              "- False-positive rate across many designs. On this fixture, the unmodified board gives no "
              "deterministic FAIL.", ""]
    (ROOT / "docs" / "RESULTS.md").write_text("\n".join(lines), encoding="utf-8")
    print("docs/RESULTS.md written")


if __name__ == "__main__":
    main()
