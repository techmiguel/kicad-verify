# Changelog

## 0.6.0 — 2026-10-08
Tested on ten public KiCad 8/9/10 boards. Every finding was checked against pcbnew and the design files; findings that were checker bugs are fixed below, each with a regression test. Seeded bank unchanged: 21 of 23.

**Breaking**
- The policy digest changes: sign-offs made under 0.5.1 or earlier must be renewed.
- One board per verification. With several boards and none chosen, `init` lists them and exits 2, and `verify` reports DISCOVER FAIL. Choose one with `init PATH --board X.kicad_pro`.
- `min_power_width_mm` has no base default: declare `params.net_currents` or set the limit (see PCB-WIDTH-001 below).
- ERC/DRC waivers for library or off-grid findings are reported as stale: those findings moved to PCB-HYGIENE-001.

**New**
- BRD-LINK-001 checks declared board-to-board connections pin by pin on both schematics: voltage, role (ground, supply, signal), two outputs on one net, pin count. Declared in `params.interconnects`; excluded with a reason when nothing is declared.
- `kicadverify profiles --from FILE` builds a fab profile from the fab's own KiCad rules (`.kicad_pro`, KiCad 5 template `.kicad_pcb`, `.kicad_dru`), citing the source of each value.
- Built-in `oshpark-4-layer` profile (unconfirmed). No JLCPCB multilayer profile: the sources disagree; use `profiles --from` with JLCPCB's rules.
- PCB-WIDTH-001 checks current capacity with IPC-2221 for the currents in `params.net_currents` (exact name or `~regex`), using the stackup copper thickness. Dead-end stubs to one capacitor or test point are not judged. Without a current, a thin power track is a coverage gap that names the net and the pads it reaches.
- PCB-HYGIENE-001 (advisory): library-table and off-grid findings no longer block ERC/DRC. They depend on the machine, not the design.
- Gerber and drill zip archives are checked. Each export set (folder or archive) is compared with the board on its own and hashed into the release manifest.
- `init` finds fabrication outputs outside the KiCad folder (`hardware/fab/`), matched by board name.

**Blind spots fixed** (checks that passed without looking)
- KiCad ≤ 9 boards: track, via and zone nets were read as numbers, so ISO-SEP-001, PCB-WIDTH-001 and PCB-KEEPOUT-001 saw no named nets.
- Arc tracks were measured as their chord; a mains arc 1.8 mm from low voltage measured 7 mm.
- Negative rails (`-12V`) were read as positive.
- FAB-STALE-001 re-plotted with the board's last plot settings: after a PDF plot nothing was compared. Layers are now re-plotted explicitly as Gerber, compared in mm, mask and paste included.
- `pin_net` assertions fell back to the schematic net when the PCB pad had none.
- A failed reviewer run was reused by the next `release`.

**False FAILs fixed**
- FAB-DRILL-001: holes matched by rounding; now within 0.02 mm.
- FAB-DFM-001: slots measured as circles; via arrays in exposed pads judged as through-hole leads; 0.1 mil tolerance on fab limits (4 mil = 0.1016 mm); identical findings grouped.
- FAB-BOM-001 / FAB-CPL-001: grouped designators (`R1-3`), DNP columns, every part-number column, KiCad ASCII `.pos`, fiducials and padless parts, SMD-only placement files, best match among several files.
- PCB-PARITY-001 / PCB-PINMAP-001: auto-generated net names compared by pins; shared pads (KMR2); unconnected pins without a pad are warnings.
- PCB-MODEL-001 / PCB-PADNET-001 / PCB-KEEPOUT-001: logos and jumpers, mounting holes, pads that are no pin of the symbol, inner layers.
- Courtyard overlaps with a DNP alternative part are warnings.
- kicad-happy FD-001, PP-001 and VM-001 default to warn.
- A board at the top of a folder no longer picks up the Gerbers and drill files of another board in a sub-folder.
- A BOM whose rows end with a trailing comma no longer crashes the fabrication checks.
- `V-12V` is read as -12 V; `init --board` pre-fills outside outputs regardless of file-system order.

## 0.5.1 — 2026-10-07
Usability and code-health release; no verifier logic changes (seeded bank unchanged, 21 of 23).
- `kicadverify --version`; every command has a description in `--help`, listed in workflow order.
- A clear notice when `kicad-cli` is not found, instead of one `[Errno 2]` per requirement.
- Report summaries use singular and plural correctly ("1 polarity error", "3 DRC violations").
- CIR-LED-001 reports a reverse-biased LED as not lit instead of a negative current.
- Code checked with ruff in CI; tests also run on Python 3.13.
- README rewritten around a quick start with real output; docs/VERIFICATION.md shows a project fab profile for other fabs and layer counts.

## 0.5.0 — 2026-10-07
First tagged release: install a fixed version with `pip install "git+https://github.com/techmiguel/kicad-verify@v0.5.0"`.
- `init --ci github` pins the workflow to the kicad-verify version that wrote it (`@v<version>`) instead of the development branch.
- PCB-PARITY-001 compares component values between schematic and PCB. A value changed in only one of them (the board would be built with a value the circuit checks never saw) is FAILED and blocks every gate; each difference can be waived with a reason. Values are compared as numbers when both parse (`4k7` = `4.7k` = `4700`, `100n` = `100nF`, `220` = `220Ω`), the rest of the value (`10V`, `X7R`) as text ignoring case and spacing. Found by testing on smartRele: R9 changed to 330 Ω in the schematic alone passed the fab gate with only a kicad-happy warning.
- The requirement text of PCB-PARITY-001 now names values, so the policy digest changes: sign-offs made under 0.4.0 or earlier must be renewed.
- Seeded defect `value_R9_schematic_only` added to the bank (23 defects with the 0.4.0 isolation cases).
- False FAILs found by running on a second real board (private; 4 layers, BGA, RGB LED), all of which blocked the fab gate:
  - FAB-DRILL-001 counted a routed slot in the .drl (`G85`, KiCad's default slot mode) as a round hole, while slots on the PCB side were already skipped. Slots are now skipped on both sides.
  - kicad-happy KO-001 (via/track inside a rule area) ignores what the area forbids (vias flagged in areas with `vias allowed`) and LR-001 misses per-cathode resistors of RGB LEDs: both default to warn. KiCad DRC enforces rule areas (PCB-DRC-001) and CIR-LED-001 computes LED current.
- CIR-POL-001 and CIR-LED-001 handle multi-colour LEDs (common anode `A` + `RK/GK/BK`, or common cathode `RA/GA/BA` + `K`): one channel per colour, with the colour's forward voltage. They were NOT_VERIFIABLE (pins not named A/K).
- CIR-LED-001 no longer follows resistors on an LED pin that sits straight on a rail (a pull-up on +3V3 next to a common anode is not a series resistor).

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
