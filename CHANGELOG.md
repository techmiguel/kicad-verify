# Changelog

## 0.4.0 — 2026-10-06
Closing the gaps between what the release record claims and what it checks.
- `audit` now checks the reports bound by the release manifest. `release` archives the verification report, the review report, the attestation and its signature next to the manifest (`archived`), because `reports/` is rewritten by every later `verify`; `audit` recomputes their SHA-256.
- `project.yaml` is split explicitly (`config.PROJECT_STATE_KEYS`): the release state (`status`, `release_design_hash`) stays out of the policy; `name` is a label; every other field (reviewer model, ...) is a verification input in the new policy component `project`.
- The review key includes the requested reviewer model: a review made with another model than the configured one is not reused.
- Reviewer evidence must come from an independent source. Quotes from `verify_report.json`, `kicad_happy.json`, the new `estimates.json` (rail voltages inferred by kicad-verify, moved out of `design.json`), `requirements.json`, `datasheets.json` or any kicad-verify output are discarded as "derived source"; the bundle carries `sources.json` with the classification. Re-checking the recorded seeded reviews: no requirement verdict depended on derived sources; 3 extra findings that only repeated gate findings now count as unverified.
- Every requirement verdict carries `assurance`: `deterministic`, `evidence-integrity` (reviewer: the quotes exist verbatim in independent sources; the reasoning is not verified by code) or `human-attestation`. `explain` prints it.
- One identity mechanism: the hooks' change key (`config.change_key`, formerly `artifact_hash`: SHA-1 over path, size and mtime) is SHA-256 over content, and so are the netlist cache and the board interface (`pcb_sha256`, schema `board-interface@2`). Waiver keys keep their SHA-1 form: they are identifiers stored in users' `waivers.yaml`, not integrity checks.
- End-to-end tests of the release chain through the CLI (`tests/test_release_flow.py`): release writes manifest and attestation, blocked releases write nothing, audit detects changed reports and outputs, a changed `.kicad_dru` or policy invalidates sign-offs and the review, a missing verifier is NOT_RUN, missing fabrication outputs block the fab gate, package and runtime versions agree.
- RESULTS.md states the scope of the seeded bank (a regression suite on one board, not a benchmark) and corrects the description of how an unproven PASS is mapped.
- Breaking: the policy digest gains the `project` component and the reviewer prompt and verifier registry changed, so sign-offs and reviews from 0.3 must be renewed; `board_interface.json` renames `pcb_sha1` to `pcb_sha256`.

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
