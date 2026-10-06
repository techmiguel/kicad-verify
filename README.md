# kicad-verify

**A verification gate for KiCad 10 projects.** It catches the board defects that ERC and DRC let through: reversed LEDs and diodes, regulators on the wrong rail, LED and transistor currents out of range, symbol pins with no pad, fabrication files that no longer match the board, CPL offsets, and drill changes after export. An independent reviewer then checks pinouts, protection and values against the datasheets, and every piece of evidence it cites is re-checked by code.

It runs on its own: install it once and every KiCad project you open in [Claude Code](https://docs.anthropic.com/en/docs/claude-code) is checked after each change, with a hard gate before the agent can finish. It also works as a plain CLI and in CI.

## What it catches (measured)

None of these is an ERC or DRC rule. Defects seeded into a real, fabricated board ([smartRele](https://github.com/techmiguel/smartRele), KiCad 10) and the layer that caught them. Full table and method: [docs/RESULTS.md](docs/RESULTS.md).

| Seeded defect | Caught by |
|---|---|
| LED reversed (schematic and PCB updated together) | CIR-POL-001 |
| Relay flyback diode reversed | CIR-POL-001 |
| 3.3 V rail fed by a 1.8 V regulator | CIR-REG-001 |
| LED resistor 220 Ω → 10 Ω (≈30 mA) | CIR-LED-001 |
| Transistor base resistor 1 kΩ → 10 Ω | CIR-BJT-001 |
| Electrolytic rated 4 V on a 5 V rail | KH-VD-001 (kicad-happy) |
| Footprint pad renumbered (symbol pin without a pad) | PCB-PINMAP-001 |
| Gerbers exported before a track moved | FAB-STALE-001 |
| Drill diameter changed / mounting hole moved after export | FAB-DRILL-001 |
| CPL at the body centre instead of the pad centre | FAB-CPL-001 |
| Part missing from the CPL or BOM | FAB-CPL-001 / FAB-BOM-001 |
| Track inside a mounting-hole washer area | PCB-KEEPOUT-001 |
| EN pull-up 10 kΩ → 10 MΩ, fuse 500 mA → 50 A | independent reviewer (Opus 5.5 and Sonnet 5.5 both caught them) |

Deterministic gate: 16 of 18 defects. Independent reviewer: 8 of 8 circuit defects with either model, at about $0.54 (Opus) or $0.21 (Sonnet) per review. On the unmodified board the gate reports no FAIL. The reviewer caveats are in the results page.

## How it works

```
  KiCad files ──► deterministic gate ──────────────► independent reviewer ──► human sign-off ──► release
                  kicad-cli ERC/DRC                  clean `claude -p` session  bound to the
                  parity + pin map                   quotes re-checked by code  design hash
                  circuit checks
                  kicad-happy detectors
                  Gerber/drill/BOM/CPL vs PCB
```

1. **Deterministic gate.** ERC and DRC through `kicad-cli`; its own schematic/PCB parity and symbol-pin/pad mapping; circuit checks on the netlist (polarity, regulators, LED and base current, floating enable pins); the 60+ detectors of [kicad-happy](https://github.com/aklofas/kicad-happy) (derating, decoupling, buses, DFM...); and fabrication outputs re-derived from the current board (Gerbers re-plotted and compared, drill positions, BOM, CPL).
2. **Independent reviewer.** A separate `claude -p` session that never saw the design conversation receives a bundle (requirements, report, pin tables, datasheet text) and must quote its evidence verbatim. kicad-verify searches each quote in the cited file: a PASS without a verifiable quote becomes a FAIL, a FAIL without one becomes an unverified warning.
3. **Human sign-off.** Physical checks (1:1 print, polarity, CPL rotations, prototype) are signed with `kicadverify signoff`. Each sign-off is bound to a content hash of the KiCad files, so any later change invalidates it.

Every finding can be waived individually (check + key, reason, date, expiry). Nothing is silenced in bulk. What each check covers and does not cover: [docs/COVERAGE.md](docs/COVERAGE.md).

## Install

Requires Python 3.10+, KiCad 10 (`kicad-cli`; tested with 10.0.5) and, for the reviewer, the [Claude Code](https://docs.anthropic.com/en/docs/claude-code) CLI.

```bash
pip install git+https://github.com/techmiguel/kicad-verify
kicadverify setup            # downloads the pinned kicad-happy engine (v2.3.1)
kicadverify install-claude   # optional: hooks + /pcb-verify, /pcb-review, /pcb-release skills
```

`kicad-cli` is found on `PATH`, in the default KiCad install folders, or through `KICAD_CLI`.

## Use

```bash
kicadverify verify path/to/project          # full gate (~20 s on a two-layer board)
kicadverify verify path/to/project --fast   # no ERC/DRC/re-plot (~4 s), used after each edit
kicadverify review path/to/project          # gate + independent reviewer (~2-5 min)
kicadverify datasheets path/to/project      # download the PDFs referenced by Datasheet fields
kicadverify signoff HUM-FIT-001 --by "Name" --path path/to/project
kicadverify release path/to/project         # gate + review + sign-offs, then status: release
```

Exit code 1 when anything fails. Reports go to `verification/pcb/reports/` inside the project.

### With Claude Code
After `kicadverify install-claude`:
- **On session start** in a KiCad project, `verification/pcb/` is created with templates.
- **After every KiCad MCP call or edit of a KiCad file**, the fast checks run (~4 s) and any FAIL goes back to the agent.
- **When the agent finishes**, the full gate runs if the design changed. A FAIL blocks the stop up to three times for the same design state, then lets it stop and reports.
- **In release status**, a design change also re-runs the independent reviewer.

Existing hooks in `~/.claude/settings.json` are kept; a backup is written on first install. `kicadverify uninstall-claude` removes only what it added.

## Project configuration

`verification/pcb/` in each project:

| File | Purpose |
|---|---|
| `design_intent.md` | Supply, loads, environment and intentional decisions, read by the reviewer so it judges the board against what it is for |
| `requirements.yaml` | Parameters and extra requirements; inherits [the base requirements](kicadverify/data/requirements_base.yaml). `disable:` turns individual checks off |
| `pins.yaml` | Critical pins copied from the datasheet, compared with the PCB |
| `approved_footprints.txt` | Footprints checked against their datasheets |
| `waivers.yaml` | One entry per accepted finding, with reason, date and expiry |
| `signoff.yaml` | Human sign-offs (written by `kicadverify signoff`) |

Useful parameters: `net_voltages` (rails whose voltage is not in the name), `logic_high_v`, `led_max_ma`, `gpio_max_ma`, `min_power_width_mm`, `mounting_holes`, `fab.cpl_tol_mm`, `kicad_happy.severity` (per-rule override), `review.model`.

## Board interface for mechanical checks

The full gate also exports the current board as STEP plus a JSON with outline, mounting holes and component positions in STEP coordinates (`verification/pcb/interface/`). Its companion tool for FreeCAD enclosures and printed parts consumes it, so the enclosure is always checked against the board as it is now.

## Limits

No tool can prove a board works. The gate removes classes of mistakes; the reviewer finds some design-intent problems and can miss others; the sign-offs exist for what only a person with the parts in hand can check. See [docs/COVERAGE.md](docs/COVERAGE.md).

## Credits

Circuit and DFM detectors from [kicad-happy](https://github.com/aklofas/kicad-happy) by aklofas (MIT), run as an external engine at a pinned version.

## License

MIT
