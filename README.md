# kicad-verify

**Requirements-based verification for KiCad 10 projects: CI/CD for hardware.** "This PCB is correctly designed and ready to manufacture" is treated as a claim to be supported requirement by requirement. Every requirement has a **source** (datasheet, fab capability, standard, design intent, lesson learned...), a **verification method** (deterministic check, independent reviewer with evidence re-checked by code, or human sign-off), **evidence** (hashed files, raw tool output, verbatim quotes, signed records), **explicit coverage** (what was checked, what could not be, and what the verifier never sees) and a status:

| VERIFIED | FAILED | NOT_VERIFIABLE | NOT_RUN |
|---|---|---|---|
| verified within its full coverage | a defect not waived | could not decide: missing input, partial coverage, unproven claim | no verifier ran for this design |

It is not another DRC, and not an "AI PCB review" with a confidence score. ERC/DRC, a set of circuit, board and fabrication checks, a fab-capability profile, an independent reviewer and human sign-offs are verifiers that feed the requirements; gates (`dev`, `fab`, `release`) decide from the verdicts, and a release manifest binds them to the exact files sent to the fab.

## What it catches (measured)

Defects seeded into a real board that was fabricated and assembled ([smartRele](https://github.com/techmiguel/smartRele), KiCad 10.0.6), configured with the [reference verification set](tests/reference/rele). Full table and method: [docs/RESULTS.md](docs/RESULTS.md).

| Seeded defect | Requirement FAILED |
|---|---|
| LED / relay flyback diode reversed (schematic and PCB updated together) | CIR-POL-001 |
| 3.3 V rail fed by a 1.8 V regulator | CIR-REG-001 |
| LED resistor 220 Ω → 10 Ω, transistor base resistor 1 kΩ → 10 Ω | CIR-LED-001, CIR-BJT-001 |
| Electrolytic rated 4 V on a 5 V rail | KH-* (kicad-happy VD-001) |
| Footprint pad renumbered | PCB-PINMAP-001, PCB-PINS-001 |
| Board clearance rules relaxed below the fab's minimum spacing (DRC stays clean) | FAB-RULES-001 only |
| 0.1 mm track | FAB-DFM-001, PCB-DRC-001 |
| Gerbers exported before a track moved; drill changed or hole moved after export | FAB-STALE-001, FAB-DRILL-001 |
| CPL at the body centre, part missing from the CPL or BOM | FAB-CPL-001, FAB-BOM-001 |
| Track inside a mounting-hole washer area | PCB-KEEPOUT-001 |
| EN pull-up 10 kΩ → 10 MΩ, fuse 500 mA → 50 A | none deterministic: reviewer (MOD-*), release gate |

**Fab gate: 18 of 20 seeded defects block it**; the unmodified board passes it. The two that do not are judgement calls with no rule, caught by the independent reviewer at the release gate (Opus 5.5 and Sonnet 5.5 both caught them, ≈$0.54 / $0.21 per review; caveats in the results page).

## How it works

```
 requirements (base + project)        verifiers                          verdicts           gates
 ─ source                         ┌─ kicad-cli ERC / DRC              ┐
 ─ method / verified_by           ├─ parity, pin map, board checks    │  VERIFIED
 ─ acceptance            ───────► ├─ circuit checks, kicad-happy      ├─►FAILED          ─► dev / fab / release
 ─ gate                           ├─ fab profile: rules + DFM         │  NOT_VERIFIABLE      │
                                  ├─ Gerber / drill / BOM / CPL       │  NOT_RUN             ▼
                                  ├─ declarative assertions           │  + evidence      release manifest
                                  ├─ independent reviewer (quotes)    │  + coverage      (SHA-256 of every
                                  └─ human sign-offs (design hash)    ┘  + deviations     file) ─► audit
```

- **Requirements** come from [the base set](kicadverify/data/requirements_base.yaml), each with its source, plus the project's own in `verification/pcb/requirements.yaml`. A project requirement can be verified by a declarative check (`pin_net`, `value`, `footprint`, `field`, `net_exists`, `board_size`, `layer_count`, `track_width`), by the reviewer or by a sign-off. Excluding a requirement needs a reason, and every report lists it.
- **Coverage** is explicit: each verifier reports `checked/total` items and lists what it could not check (a polarized part on a net of unknown voltage, a Gerber layer never exported, a fab limit missing from the profile). Partial coverage is NOT_VERIFIABLE until a person checks those items and waives them with a reason. What each verifier never covers is printed next to every verdict ([docs/COVERAGE.md](docs/COVERAGE.md)).
- **Evidence**: hashed input files, raw ERC/DRC JSON, the reviewer's verbatim quotes (searched in the cited file, which must be an independent source: design files, datasheets, design intent, never another verifier's report; a PASS without a valid quote is not accepted), sign-offs bound to the design hash and the policy digest, waivers with reason, author and expiry. A reviewer quote proves evidence integrity, not that the quote supports the conclusion: each verdict says which (`assurance`), and the release gate keeps human sign-offs for that reason.
- **Gates**: `dev` blocks on confirmed defects (Claude Code hooks, after every edit); `fab` answers "ready to fabricate" and runs in CI without API keys; `release` adds the reviewer and the human sign-offs. A FAILED requirement with an error-level defect blocks every gate.
- **Provenance**: every report records the policy digest (per component), the tools (kicad-verify code digest and commit, kicad-cli, kicad-happy commit), every artifact's SHA-256 and the design intent it was judged against. `--attest` writes an in-toto attestation, signable with an SSH key, that `check-attestation` re-verifies against the files on disk; `explain <REQ>` prints the chain of proof behind one verdict.
- **Release record**: `release` writes a manifest with the SHA-256 of every design file, fabrication output and config, of the reports and the attestation (archived with the manifest), and every verdict. `audit` fails if any of them changed before upload.

The model in detail: [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Install

Requires Python 3.10+, KiCad 10 (`kicad-cli`; tested with 10.0.5 and 10.0.6) and, for the reviewer, the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.

```bash
pip install git+https://github.com/techmiguel/kicad-verify
kicadverify setup            # downloads the pinned kicad-happy engine (v2.3.1)
kicadverify install-claude   # optional: hooks + /pcb-verify, /pcb-review, /pcb-release skills
```

`kicad-cli` is found on `PATH`, in the default KiCad install folders, or through `KICAD_CLI` (which can point to a wrapper that runs the official `kicad/kicad:10.0` image; the CI template does exactly that).

## Use

```bash
kicadverify init path/to/project --ci github     # verification/pcb/ + .github/workflows/hw-verify.yml
kicadverify verify path/to/project --gate fab    # ready to fabricate? (~10-20 s on a two-layer board)
kicadverify verify path/to/project --fast        # no ERC/DRC/re-plot (~4 s), used after each edit
kicadverify review path/to/project               # + independent reviewer (~2-5 min)
kicadverify signoff HUM-FIT-001 --by "Name" [--fail --note "..."] --path path/to/project
kicadverify release path/to/project              # fab gate + review + sign-offs -> release manifest
kicadverify audit path/to/project                # files still match the release manifest?
kicadverify verify path/to/project --attest --sign-key ~/.ssh/id_ed25519   # signed in-toto attestation
kicadverify check-attestation FILE --path path/to/project --allowed-signers allowed_signers
kicadverify explain CIR-POL-001 --path path/to/project   # source, evidence, coverage, policy, tools, intent
kicadverify requirements path/to/project [--lint | --markdown]   # traceability table
kicadverify checks [--markdown]                  # what each verifier covers and does not
kicadverify profiles                             # built-in fab capability profiles
```

`verify` writes `verification/pcb/reports/verification_report.md` (gates, blockers with the action for each, traceability matrix, details, deviations, evidence hashes) and `verify_report.json`; `--junit` and `--markdown` write copies for CI (`--markdown "$GITHUB_STEP_SUMMARY"` appends to the job summary). Exit codes: 0 gate passes, 1 blocked, 2 the requirement set has errors, 3 no KiCad project.

### With Claude Code
After `kicadverify install-claude`:
- **On session start** in a KiCad project, `verification/pcb/` is created with templates.
- **After every KiCad MCP call or edit of a KiCad file**, the fast checks run (~4 s) and a blocked `dev` gate goes back to the agent with the failing requirements.
- **When the agent finishes**, the full gate runs if the design changed. A blocked `dev` gate blocks the stop up to three times for the same design state, then lets it stop and reports.
- **In release status**, a design change also re-runs the independent reviewer.

Existing hooks in `~/.claude/settings.json` are kept; a backup is written on first install. `kicadverify uninstall-claude` removes only what it added.

## Project configuration

`verification/pcb/` in each project:

| File | Purpose |
|---|---|
| `requirements.yaml` | Parameters (`fab.profile`, `net_voltages`, `mounting_holes`...), project requirements with source and method, overrides of base requirements, exclusions with reasons |
| `design_intent.md` | Supply, loads, environment and intentional decisions, read by the reviewer |
| `pins.yaml` | Critical pins copied from the datasheets, compared with the PCB |
| `approved_footprints.txt` | Footprints checked against their datasheets |
| `waivers.yaml` | One entry per accepted finding or coverage gap: reason, date, author, expiry |
| `signoff.yaml` | Human sign-offs (written by `kicadverify signoff`) |
| `release/` | Release manifests (keep them in version control) |
| `attestations/` | In-toto verification attestations and their SSH signatures (keep them in version control) |

A complete example for a real board: [tests/reference/rele](tests/reference/rele).

## Board interface for mechanical checks

The full gate also exports the current board as STEP plus a JSON with outline, mounting holes and component positions in STEP coordinates (`verification/pcb/interface/`), so an enclosure tool can check against the board as it is now.

## Limits

No tool can prove a board works. VERIFIED means "no defect of this kind within this coverage". The deterministic verifiers remove classes of mistakes; the reviewer finds some design-intent problems and can miss others; the sign-offs exist for what only a person with the parts in hand can check. The built-in fab profile is marked unconfirmed until someone checks it against the fab's current capability page. See [docs/COVERAGE.md](docs/COVERAGE.md) and [docs/RESULTS.md](docs/RESULTS.md).

## Credits

Circuit and DFM detectors from [kicad-happy](https://github.com/aklofas/kicad-happy) by aklofas (MIT), run as an external engine at a pinned version.

## License

MIT
