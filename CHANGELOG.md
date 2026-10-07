# Changelog

## Unreleased
- PCB-PARITY-001 compares component values between schematic and PCB. A value changed in only one of them (the board would be built with a value the circuit checks never saw) is FAILED and blocks every gate; each difference can be waived with a reason. Values are compared as numbers when both parse (`4k7` = `4.7k` = `4700`, `100n` = `100nF`, `220` = `220Ω`), the rest of the value (`10V`, `X7R`) as text ignoring case and spacing. Found by testing on smartRele: R9 changed to 330 Ω in the schematic alone passed the fab gate with only a kicad-happy warning.
- The requirement text of PCB-PARITY-001 now names values, so the policy digest changes: sign-offs made under 0.4.0 or earlier must be renewed.
- Seeded defect `value_R9_schematic_only` added to the bank (23 defects with the 0.4.0 isolation cases).

## 0.4.0 — 2026-10-06
- ISO-SEP-001: copper separation across declared isolation barriers (mains to low voltage, line to neutral...), selected by net class (from the `.kicad_pro` patterns) or nets, measured per copper layer over tracks, vias, pads and zone fills, independently of the board's DRC rules. A shortfall on a board with cutouts or slots is NOT_VERIFIABLE (creepage around slots is not computed). The required distances and their sources are declared by the project; none are shipped. Breaking: projects without a hazardous voltage exclude the requirement with that reason, otherwise it is NOT_VERIFIABLE at the fab gate.
- Seeded bank: ground copper 2.8 mm from mains with and without the designer's mains rule in `.kicad_dru`; without it the DRC requirement stays VERIFIED and only ISO-SEP-001 fails. 20 of 22 defects block the fab gate.

## 0.3.0 — 2026-10-06
Provenance: the system can show exactly what it verified, what it could not verify, under which policy and tool versions, over which artifacts and against which design intent.
- `provenance` block in every report: policy digest with one digest per component (requirements, params, gates, waivers, exclusions, fab profile, verifier registry, reviewer prompt, pins) and the hashed policy files; tools (kicad-verify version, code digest and git commit, kicad-cli, kicad-happy pinned tag and installed commit, Python, platform, CI variables); every artifact (design, fabrication, datasheets, config) with SHA-256; the design intent (declared and empty fields, context files). Markdown report gains a Provenance table.
- GEN-INTENT-001: the design intent must be declared; each empty field of design_intent.md is a coverage gap (NOT_VERIFIABLE at the release gate).
- The independent review is reused only while the design hash and the review key (requirements, design intent, datasheets, reviewer prompt) are unchanged; the review report records the bundle digest, the `claude` CLI version and the model that answered.
- In-toto attestations (`verify --attest`, always on for `release`), signable with `ssh-keygen -Y sign` (`--sign-key`, `KICAD_VERIFY_SIGN_KEY`); `check-attestation` verifies the signature and recomputes every digest against the files on disk; `explain <REQ>` prints the chain of proof behind one verdict.
- Human sign-offs are bound to the policy digest as well as the design hash: a sign-off made under another policy is NOT_RUN, naming the policy components that changed. Breaking: sign-offs from 0.2 must be renewed.
- Release manifest records the policy, intent and artifacts digests and the attestation.

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
