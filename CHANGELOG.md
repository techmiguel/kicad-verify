# Changelog

## Unreleased
False FAILs found by running the fab gate on three public KiCad boards (badjeff/paw3222-pcb, gobabygocarswithjoysticks/gbg-pcb, aronreid/ups-to-esp32). Each was checked against pcbnew before the fix. Seeded bank unchanged: 21 of 23. The verifier registry changed, so the policy digest changes: sign-offs made under 0.5.1 or earlier must be renewed.
- FAB-DFM-001 measures hole-to-hole distance with slots as capsules (pad orientation, both drill dimensions) instead of circles of the slot's length. A GND via next to a USB-C shield slot was reported at 0.078 mm; it is 0.227 mm.
- FAB-BOM-001 expands grouped designators as KiCad's BOM writes them (`B1-B4`, `R1-3`). A grouped line was reported both as "in the BOM but not on the PCB" and as four parts missing from the BOM.
- FAB-CPL-001 reads KiCad's ASCII position file whatever its extension, honouring `## Unit = inches|mm`. A `position.csv` in that format reported every part as missing.
- PCB-WIDTH-001 no longer takes a rail's control or status signal for the rail (`VBUS_EN`, `VBUS_FAULT`, `VIN_SENSE`, `VBAT_DIV`...; matched on the last path component). Each narrow power net now names the pads its thin tracks reach, so a feed to a divider or pull-up can be told from the main current path.
- FAB-DFM-001 judges a plated hole inside an SMD pad with the same number (the via array of an exposed pad, such as the ESP32-S3-WROOM-1 thermal pad) by the via drill and annular-ring limits, not the through-hole lead limits.
- FAB-BOM-001 and FAB-CPL-001 accept a line for a part that is fitted on the PCB but not required in that file: a padless heatsink in the BOM, or a fiducial in the CPL. A line for a part that is DNP on the PCB is still a mismatch.
- FAB-CPL-001 recognises an SMD-only placement file (no through-hole part in it, as KiCad's `--smd-only` writes): through-hole parts missing from it are warnings ("fitted by hand?"), not mismatches.
- FAB-BOM-001 understands a DNP column or a `DNP` value (KiCad's own BOM export marks unfitted parts that way): such a line must be DNP on the PCB, and one for a fitted part is a mismatch. "No supplier part number" looks at every part-number column (LCSC, MPN, PARTNO, Digi-Key, Mouser...), not only LCSC: a BOM with Digi-Key codes and an empty LCSC column gave 87 warnings on bitaxeGamma.
- FAB-BOM-001 and FAB-CPL-001 with several candidate files (a KiCad BOM next to a JLCPCB one) used whichever the file system listed first. The one the board agrees with best is now checked and the report names the others; `params.fab.bom` / `params.fab.cpl` choose explicitly. Project files are walked in sorted order.
- kicad-happy defaults: FD-001 (fiducial count), PP-001 ("IC power pin has no DC path": wrong on all three real cases checked, a buck bootstrap pin, an LDO input fed through a resistor and an ASIC's internally generated I/O supply) and VM-001 (voltage-domain crossing inferred from rail names) default to warn. Identical repeated findings are reported once.
- **Boards saved by KiCad 9 or earlier: track, via and zone nets were read as net numbers.** Those versions write `(net 1)` on tracks and vias and resolve it through the board's net table; kicad-verify kept the number. Every check that selects copper by net name was therefore blind on such boards: PCB-WIDTH-001 found no power net and passed, PCB-KEEPOUT-001 never recognised a hole's own net, and ISO-SEP-001 could not select mains tracks by net class (a shortfall across a declared barrier was missed). Nets are now resolved through the table. Found on bitaxeGamma and bms-c1 (KiCad 9); paw3222-pcb (KiCad 8) was affected too.
- PCB-KEEPOUT-001 ignores inner-layer tracks: a washer or screw head cannot touch them.
- PCB-PARITY-001 compares a net whose name KiCad derived from a pin (`Net-(...)`, `unconnected-(...)`) by the pins on it: four stacked VBUS pins that are `unconnected-(J3-VBUS-PadA4)` in the schematic and `Net-(J3-VBUS-PadA4)` on the PCB are the same net.
- PCB-PINMAP-001 accepts a symbol pin without a pad when another pin of the same symbol, on the same net, has a pad carrying that net (a 4-pin push-button symbol on the 2-contact KMR2 footprint). A renumbered pad still fails: it is not a pin of the symbol.
- PCB-MODEL-001 asks for a 3D model only for footprints that place a body: not for footprints without pads (logos, polarity marks), and the body-less kinds are matched case-insensitively anywhere in the name (`LIBRESOLAR_LOGO`), now including solder jumpers, wire pads, Tag-Connect and layer markers. PCB-PADNET-001 skips plated mounting-hole pads.
- FAB-STALE-001 re-plotted with `--board-plot-params`, i.e. the settings of the board's last plot. After a PDF plot (a common last step: printing the copper layers) kicad-cli wrote PDF content under `.gbr` names and the check reported "kicad-cli produced no Gerbers"; after a copper-only plot, the mask and outline were never compared. The exported layers (and the mandatory ones) are now re-plotted explicitly as Gerber, with the drill/place origin when the board plots with it. Coordinates are compared in mm within 6 µm, so a 4.5-format or inch export compares with the 4.6-format re-plot, and the board profile plotted on every layer is ignored outside the profile layer.
- FAB-STALE-001 also compares mask and paste openings, which KiCad writes without aperture attributes and were therefore never compared. Mask openings over non-plated holes are left out: KiCad 10 plots them and KiCad 9 does not.
- FAB-DRILL-001 matched hole positions by rounding to 0.01 mm with a ±0.01 mm window, which misses a hole whose two coordinates fall on opposite sides of a rounding step (87.005 on the PCB vs 87.0052 in a 4-decimal inch .drl). Holes now match within 0.02 mm. Found on bitaxeGamma (a pin header and a via) and air-quality-sensor (six vias).
- Courtyard conflicts where one footprint is DNP (an alternative part laid over another) are warnings naming the DNP parts: DRC `courtyards_overlap`, `pth_inside_courtyard` and `npth_inside_courtyard`, and kicad-happy PM-001. Hole-to-hole conflicts stay errors, because those holes are drilled whether or not the part is fitted.

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
