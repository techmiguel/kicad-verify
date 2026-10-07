# kicad-verify

[![tests](https://github.com/techmiguel/kicad-verify/actions/workflows/tests.yml/badge.svg)](https://github.com/techmiguel/kicad-verify/actions/workflows/tests.yml)
[![release](https://img.shields.io/github/v/tag/techmiguel/kicad-verify?label=release)](CHANGELOG.md)
[![license](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

**Is this KiCad 10 board ready to manufacture?** kicad-verify answers requirement by requirement, with evidence, locally, in CI or inside Claude Code.

Every requirement has a source (datasheet, fab capability, standard, design intent), a way to verify it (a deterministic check, an independent reviewer whose quotes are re-checked by code, or a human sign-off) and ends in one of four states:

| VERIFIED | FAILED | NOT_VERIFIABLE | NOT_RUN |
|---|---|---|---|
| checked over its whole scope, no defect | a defect that is not waived | could not decide: missing input or partial coverage | nothing has checked it for this design |

Gates (`dev`, `fab`, `release`) decide from those states. Nothing is VERIFIED by default, and every report says what each check does not cover.

## Quick start

Requires Python 3.10+ and KiCad 10 (`kicad-cli`, tested with 10.0.5 and 10.0.6).

```bash
pip install "git+https://github.com/techmiguel/kicad-verify@v0.5.0"
kicadverify setup                               # downloads the pinned kicad-happy engine
kicadverify init path/to/project --ci github    # verification/pcb/ + a GitHub Actions workflow
kicadverify verify path/to/project --gate fab   # ready to fabricate? (10-20 s on a two-layer board)
```

On a real board with a reversed LED (output abridged):

```
kicad-verify [full] rele-esp12f  design b48682a0dbc1
Requirements: 41 applicable  VERIFIED 26  FAILED 5  NOT_VERIFIABLE 0  NOT_RUN 10
  FAILED         PCB-PARITY-001   rele-esp12f: 2 schematic/PCB differences [32/32 schematic components]
  FAILED         CIR-POL-001      rele-esp12f: 1 polarity error [4/4 polarized parts]
  ...
  FAIL CIR-POL-001: rele-esp12f: 1 polarity error
       - D2 (LED relay (red)): LED anode on Net-(D2-A) resolves to GND - reversed, it will never light
Gate fab: BLOCKED by 4
```

The first run on a new project is expected to block on NOT_VERIFIABLE requirements: they name the input they need, such as the target fab profile, the critical pins from the datasheets, the mounting holes or the approved footprints. Provide it, or exclude the requirement with a reason. [`tests/reference/rele`](tests/reference/rele) is a complete configuration for a real board.

If `kicad-cli` is not on `PATH` or in the default install folder, set `KICAD_CLI`. It can point to a wrapper around the official `kicad/kicad:10.0` image, which is what the CI workflow does.

## What it catches

Defects seeded into a board that was fabricated and assembled ([smartRele](https://github.com/techmiguel/smartRele)), configured with the reference set. **21 of 23 block the fab gate**; the unmodified board passes it. Method and full table: [docs/RESULTS.md](docs/RESULTS.md).

| Seeded defect | Requirement FAILED |
|---|---|
| LED or relay flyback diode reversed | CIR-POL-001 |
| 3.3 V rail fed by a 1.8 V regulator | CIR-REG-001 |
| LED resistor 220 Ω → 10 Ω, transistor base resistor 1 kΩ → 10 Ω | CIR-LED-001, CIR-BJT-001 |
| Electrolytic rated 4 V on a 5 V rail | KH-* (kicad-happy VD-001) |
| Footprint pad renumbered | PCB-PINMAP-001, PCB-PINS-001 |
| Resistor value changed in the schematic only | PCB-PARITY-001 |
| Clearance rules relaxed below the fab's minimum (DRC stays clean) | FAB-RULES-001 |
| 0.1 mm track | FAB-DFM-001, PCB-DRC-001 |
| Ground copper 2.8 mm from mains, barrier declared at 4 mm | ISO-SEP-001 |
| Gerbers exported before a track moved; drill changed after export | FAB-STALE-001, FAB-DRILL-001 |
| CPL at the body centre; part missing from the CPL or BOM | FAB-CPL-001, FAB-BOM-001 |
| Track inside a mounting-hole washer area | PCB-KEEPOUT-001 |
| EN pull-up 10 kΩ → 10 MΩ, fuse 500 mA → 50 A | no rule: caught by the reviewer at the release gate |

## How it works

```
 requirements (base + project)      verifiers                        verdicts          gates
 ─ source                       ┌─ kicad-cli ERC / DRC            ┐
 ─ verification method          ├─ parity, pin map, board checks  │  VERIFIED
 ─ acceptance criterion  ─────► ├─ circuit checks, kicad-happy    ├─► FAILED        ─► dev / fab / release
 ─ gate                         ├─ fab profile: rules + DFM       │  NOT_VERIFIABLE      │
                                ├─ Gerber / drill / BOM / CPL     │  NOT_RUN             ▼
                                ├─ declarative assertions         │  + evidence     release manifest
                                ├─ independent reviewer           │  + coverage     (SHA-256 of every file)
                                └─ human sign-offs                ┘
```

- **Requirements** come from [the base set](kicadverify/data/requirements_base.yaml) plus the project's own. A project requirement can be checked declaratively (`pin_net`, `value`, `footprint`, `field`, `net_exists`, `board_size`, `layer_count`, `track_width`), by the reviewer or by a sign-off.
- **Coverage is explicit.** Each verifier reports what it checked out of how many items and lists what it could not check. Partial coverage stays NOT_VERIFIABLE until a person waives each gap with a reason. What each verifier never covers is in [docs/COVERAGE.md](docs/COVERAGE.md).
- **Evidence** is kept: hashed inputs, raw ERC/DRC output, the reviewer's verbatim quotes (a PASS without a quote found in the cited file is rejected) and sign-offs bound to the design and policy hashes.
- **Gates.** `dev` blocks on confirmed defects and runs after every edit in Claude Code. `fab` answers "ready to fabricate" and runs in CI without API keys. `release` adds the reviewer and the sign-offs.
- **Release record.** `release` writes a manifest with the SHA-256 of every design file, fabrication output and report; `audit` fails if anything changed before upload. Reports also record the policy, tools and design intent, and can be signed as in-toto attestations.

The full model: [docs/VERIFICATION.md](docs/VERIFICATION.md).

## Project configuration

`kicadverify init` creates `verification/pcb/`:

| File | Purpose |
|---|---|
| `requirements.yaml` | Parameters (fab profile, net voltages, mounting holes, isolation barriers), project requirements, overrides and exclusions with reasons |
| `pins.yaml` | Critical pins copied from the datasheets, compared with the PCB |
| `approved_footprints.txt` | Footprints checked against their datasheets |
| `waivers.yaml` | Accepted findings and coverage gaps: reason, date, author, optional expiry |
| `design_intent.md` | Supply, loads, environment and intentional decisions, read by the reviewer |
| `signoff.yaml` | Human sign-offs, written by `kicadverify signoff` |
| `release/`, `attestations/` | Release manifests and attestations; keep them in version control |

A project requirement with its source and a declarative check:

```yaml
requirements:
  - id: PRJ-PWR-001
    text: "The ESP-12F module is supplied from +3V3 on its VCC pin"
    source: {kind: datasheet, ref: "ESP-12F pin definitions, pin 8 VCC", file: docs/datasheets/esp8266ex.pdf}
    check: {type: pin_net, ref: U2, pin: 8, net: "+3V3"}
```

## Commands

```bash
kicadverify init PATH [--ci github]          # create verification/pcb/ (and a CI workflow)
kicadverify verify PATH [--gate fab] [--fast] # deterministic checks; --fast skips ERC/DRC/re-plot (~4 s)
kicadverify review PATH                      # + independent reviewer (Claude Code CLI, 2-5 min)
kicadverify signoff HUM-FIT-001 --by "Name" --path PATH [--fail --note "..."]
kicadverify release PATH                     # fab gate + review + sign-offs, then the release manifest
kicadverify audit PATH                       # do the files still match the release manifest?
kicadverify explain CIR-POL-001 --path PATH  # source, evidence and coverage behind one verdict
kicadverify requirements PATH [--lint]       # traceability table, or validate the requirement set
kicadverify checks                           # what each verifier covers and does not
kicadverify profiles                         # built-in fab capability profiles
```

`kicadverify --help` lists every command. `verify` writes `verification/pcb/reports/verification_report.md` and `verify_report.json`; `--junit FILE` and `--markdown FILE` write copies for CI. Exit codes: 0 the gate passes, 1 blocked, 2 the requirement set has errors, 3 no KiCad project.

**In CI.** The workflow written by `init --ci github` runs the fab gate on every push with kicad-cli from the official KiCad image. It publishes the report in the job summary, a JUnit file and the evidence, and pins the kicad-verify version that wrote it.

**In Claude Code.** `kicadverify install-claude` adds hooks and `/pcb-verify`, `/pcb-review` and `/pcb-release` skills. The fast checks run after every KiCad edit and a blocked `dev` gate goes back to the agent with the failing requirements. When the agent finishes, the full gate runs if the design changed. Existing hooks are kept, and `uninstall-claude` removes only what was added.

Install a tagged release rather than `main`: a new version can change verdicts and invalidate sign-offs ([CHANGELOG.md](CHANGELOG.md)).

## Limits

No tool can prove a board works. VERIFIED means "no defect of this kind within this coverage". The deterministic checks remove classes of mistakes; the reviewer finds some design-intent problems and can miss others; sign-offs cover what only a person with the parts in hand can check. The built-in JLCPCB profile is marked unconfirmed until someone checks it against the fab's current capabilities, and other fabs or layer counts need a profile in the project.

## Development

```bash
pip install -e ".[dev]"
ruff check .
pytest                                  # unit tests; integration tests run when kicad-cli is available
python tests/seeded/run_seeded.py       # seeded-defect bank (~10 min), then tests/seeded/make_results.py
```

Circuit and DFM detectors come from [kicad-happy](https://github.com/aklofas/kicad-happy) by aklofas (MIT), run as an external engine at a pinned version.

## License

MIT
