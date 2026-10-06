# Changelog

## 0.2.0 — 2026-10-06
From a verification gate to a requirements-based verification framework.
- Every requirement has a source (`kind`, `ref`, `url`, `confirmed`), a method (auto / model / human), the verifiers that decide it (`verified_by`), an acceptance criterion and a gate, and ends VERIFIED, FAILED, NOT_VERIFIABLE or NOT_RUN. Nothing is VERIFIED by default.
- Explicit coverage: every verifier reports `checked/total` items and the items it could not check; partial coverage is NOT_VERIFIABLE until each gap is waived by a person. A verifier registry (`data/checks.yaml`) states what each one covers and does not; docs/COVERAGE.md is generated from it.
- Evidence: input files hashed with SHA-256, raw ERC/DRC JSON kept in `reports/evidence/`, reviewer quotes with the hashed cited file, sign-offs with outcome (`--fail`) and design hash.
- Gates `dev`, `fab` (ready to fabricate) and `release`, with configurable policies; `verify --gate`. Error-severity FAILED blocks every gate.
- Manufacturability against a target fab profile: FAB-RULES-001 (board rules at least as strict as the fab, so a clean DRC means manufacturable spacing) and FAB-DFM-001 (measured tracks, vias, holes, annular rings, hole-to-hole, copper to edge including zones, board size, layers, thickness). Built-in JLCPCB profile, marked unconfirmed.
- Declarative project assertions (`check:` with pin_net, value, footprint, field, net_exists, board_size, layer_count, track_width).
- GEN-TRACE-001: a verifier result outside every requirement that reports a defect fails the gates.
- Waivers keep the waived items as deviations (reason, date, author, expiry), apply to coverage gaps too, and stale waivers are reported.
- The reviewer's verdicts map to the four states (an unproven PASS is NOT_VERIFIABLE, not FAIL); a review is reused while the design hash and the reviewed requirement set are unchanged.
- Reports: Markdown traceability matrix with the blockers and the action for each, JUnit XML, JSON schema `kicad-verify/verification-report@2`.
- `release` writes a manifest with the SHA-256 of every design, fabrication and config file and every verdict; `audit` detects any later change.
- `init --ci github` writes a GitHub Actions workflow (kicad-cli from the official KiCad image); the repository's own CI runs the integration tests the same way.
- Commands: `requirements --lint/--markdown`, `checks`, `profiles`, `audit`; `disable:` entries take a reason; `params.fab.assembly: false` excludes BOM/CPL requirements.
- Fixes: FAB-STALE-001 reported PASS when kicad-cli produced no Gerbers to compare with; an empty plotted layer is no longer a missing export.
- Breaking: the design hash is now SHA-256 and includes `.kicad_pro` and `.kicad_dru` (design rules), so 0.1 sign-offs must be renewed; missing inputs (pins.yaml, mounting holes, outputs) are NOT_VERIFIABLE instead of SKIP and block the fab gate until provided or excluded with a reason.
- Seeded bank: reference configuration for the fixture, fab gate and requirement verdict per defect, two new defects (relaxed clearance rules, 0.1 mm track). Measured with KiCad 10.0.6: 18 of 20 defects block the fab gate; the unmodified board passes it.

## 0.1.0 — 2026-10-06
First public release.
- Deterministic gate: kicad-cli ERC/DRC, schematic/PCB parity and symbol-pin/pad mapping, board checks (approved footprints, 3D models, pad nets, power track width, critical pins, mounting holes and keep-out), circuit checks (polarity, fixed regulators, LED and BJT base current, floating enable/reset pins), kicad-happy v2.3.1 detectors, and fabrication outputs re-derived from the board (Gerber freshness, drill, BOM, CPL).
- Independent reviewer in a clean `claude -p` session with evidence re-checked against the cited files.
- Human sign-offs bound to the design content hash; `release` command.
- Board interface (STEP + JSON) for mechanical checks.
- Claude Code hooks and skills; per-finding waivers with expiry.
- Seeded-error bank on a public fabricated board.
