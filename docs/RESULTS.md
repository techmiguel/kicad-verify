# Seeded-error results

Fixture: [smartRele](https://github.com/techmiguel/smartRele) at commit `6053b84d`, a two-layer KiCad 10 board that was fabricated and assembled. Each defect is injected into a fresh copy; the unmodified copy is the baseline. Reproduce with `python tests/seeded/run_seeded.py` (add `--reviewer opus sonnet --only circuit` for the reviewer columns).

A layer catches a defect when it reports a new FAIL/WARN for it that the baseline did not have. For the reviewer, the finding must name the modified component (or value) and be new or more severe than on the baseline.

**Deterministic checks: 18/20** defects caught. **Fab gate: 18/20** defects block it (the two that do not are judgement calls with no rule: they are the reviewer's job, at the release gate).

Requirement-level run: the fixture is configured with the reference verification set in [tests/reference/rele](../tests/reference/rele) (fab profile, pins from the datasheets, mounting holes, approved footprints, waivers with reasons, three project assertions). On the unmodified board the fab gate passes; requirements FAILED at baseline: PCB-MODEL-001 (release gate only). "Requirements newly FAILED" lists the requirements whose verdict changed to FAILED with the defect.

## Layout and footprints

| Defect | Expected check | Deterministic checks | Fab gate | Requirements newly FAILED | Other checks that also fired |
|---|---|---|---|---|---|
| pcb_pin_swap_U1 | PCB-PARITY-001 | caught | blocked | PCB-CONN-001, PCB-DRC-001, PCB-PARITY-001, PCB-PINS-001 | PCB-CONN-001, PCB-DRC-001, PCB-PINS-001 |
| footprint_swapped | PCB-PARITY-001 | caught | blocked | FAB-BOM-001, PCB-FOOT-001, PCB-PARITY-001 | FAB-BOM-001, PCB-FOOT-001 |
| track_in_MH1_keepout | PCB-KEEPOUT-001 | caught | blocked | FAB-STALE-001, PCB-CONN-001, PCB-DRC-001, PCB-KEEPOUT-001 | FAB-STALE-001, PCB-CONN-001, PCB-DRC-001 |
| pinmap_pad_renumbered | PCB-PINMAP-001 | caught | blocked | PCB-PINMAP-001, PCB-PINS-001 | PCB-PINS-001 |

## Fabrication outputs

| Defect | Expected check | Deterministic checks | Fab gate | Requirements newly FAILED | Other checks that also fired |
|---|---|---|---|---|---|
| gerbers_stale | FAB-STALE-001 | caught | blocked | FAB-STALE-001, PCB-CONN-001, PCB-DRC-001 | PCB-CONN-001, PCB-DRC-001 |
| drill_J1_1.3mm | FAB-DRILL-001 | caught | blocked | FAB-DRILL-001 | - |
| MH1_moved_1mm | FAB-DRILL-001 | caught | blocked | FAB-DRILL-001, PCB-HOLE-001, PCB-KEEPOUT-001 | PCB-HOLE-001, PCB-KEEPOUT-001 |
| cpl_body_centre_U2 | FAB-CPL-001 | caught | blocked | FAB-CPL-001 | - |
| cpl_missing_K1 | FAB-CPL-001 | caught | blocked | FAB-CPL-001 | - |
| bom_missing_C9 | FAB-BOM-001 | caught | blocked | FAB-BOM-001 | - |
| rules_clearance_relaxed | FAB-RULES-001 | caught | blocked | FAB-RULES-001 | - |
| track_0.1mm | FAB-DFM-001 | caught | blocked | FAB-DFM-001, PCB-DRC-001 | KH-CC-002, KH-DFM-001, PCB-DRC-001 |

## Circuit

| Defect | Expected check | Deterministic checks | Fab gate | Requirements newly FAILED | Other checks that also fired | Reviewer Opus 5.5 | Reviewer Sonnet 5.5 |
|---|---|---|---|---|---|---|---|
| led_D2_reversed | CIR-POL-001 | caught | blocked | CIR-POL-001, PCB-CONN-001, PCB-DRC-001, PCB-PARITY-001 | CIR-LED-001, PCB-CONN-001, PCB-DRC-001, PCB-PARITY-001 | FAIL (MOD-VALUE-001) | FAIL (MOD-PINOUT-001) |
| flyback_D1_reversed | CIR-POL-001 | caught | blocked | CIR-POL-001, PCB-CONN-001, PCB-DRC-001 | PCB-CONN-001, PCB-DRC-001 | FAIL (MOD-PINOUT-001) | FAIL (REV-EXTRA) |
| regulator_U1_1V8 | CIR-REG-001 | caught | blocked | CIR-REG-001 | - | FAIL (MOD-POWER-001) | FAIL (MOD-POWER-001) |
| led_resistor_R9_10R | CIR-LED-001 | caught | blocked | CIR-LED-001 | - | FAIL (MOD-VALUE-001) | FAIL (MOD-VALUE-001) |
| base_resistor_R6_10R | CIR-BJT-001 | caught | blocked | CIR-BJT-001 | - | FAIL (MOD-VALUE-001) | FAIL (MOD-VALUE-001) |
| cap_C1_rated_4V | KH-VD-001 | caught | blocked | KH-* | - | FAIL (MOD-POWER-001) | FAIL (MOD-POWER-001) |
| en_pullup_R1_10M | MOD-VALUE-001 | missed | passes | - | - | FAIL (MOD-STRAP-001) | FAIL (MOD-STRAP-001) |
| fuse_F1_50A | MOD-PROT-001 | missed | passes | - | - | FAIL (MOD-PROT-001) | FAIL (MOD-PROT-001) |

## Reviewer cost

- Opus 5.5: 8 reviews, mean $0.54 per review (list price reported by the CLI).
- Sonnet 5.5: 8 reviews, mean $0.21 per review (list price reported by the CLI).

## Reviewer caveats

- One run per defect and model. The reviewer is non-deterministic and repeated runs can differ.
- The reviewer sees the deterministic report. For the six defects the gate already catches, it may have confirmed the gate's finding rather than found it alone.
- In the fuse case the BOM and README still said T500mA, so the inconsistency helped. A value that is wrong everywhere is harder to spot.
- False positives on the unmodified board: Opus 5.5 gave none. Sonnet 5.5 failed the 07D221K varistor as if the mains were 230 VAC. After design intent (README, `design_intent.md`) was added to the bundle, two more Sonnet runs gave no FAIL.
- A PASS without a quote that kicad-verify finds in the cited file becomes FAIL. This strictness also produced one FAIL on an unrelated requirement, where the model claimed PASS without citing evidence.

## Not measured here

- Real defects from other boards, multi-sheet schematics and four-layer boards.
- Rotation errors in the CPL (left to the fab house viewer and a human sign-off).
- False-positive rate across many designs. On this fixture, the unmodified board gives no deterministic FAIL.
