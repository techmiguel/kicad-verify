# Coverage: what each check catches and what it does not

A PASS means "this kind of defect was not detected", never "the board is correct". The seeded-error
results in [RESULTS.md](RESULTS.md) show what each layer actually caught on a real board.

## KiCad checks
| Check | Catches | Does not catch |
|---|---|---|
| PCB-ERC-001 (`kicad-cli sch erc`) | Unconnected pins, pin-type conflicts, missing power flags | Wrong symbol pinout vs the datasheet; component values; function |
| PCB-DRC-001 (`kicad-cli pcb drc`, zones refilled in memory) | Clearances, drill sizes, overlaps, silkscreen, library drift | Whether the design rules (.kicad_dru) are the right ones (e.g. mains isolation); signal integrity; thermal |
| PCB-CONN-001 | Unrouted connections | Whether the netlist is the intended one |
| PCB-PARITY-001 (kicad-cli netlist + own parser) | Same references, footprints and net per pin in schematic and PCB | Errors present in both. PCB-only parts (MountingHole, Fiducial, TestPoint, board_only) are ignored |
| PCB-PINMAP-001 | Symbol pins without a pad; electrical pads without a symbol pin (wrong package or numbering) | A pin number that exists in both but means something else (that is MOD-PINOUT-001) |

## Board checks (direct `.kicad_pcb` parse)
| Check | Catches | Does not catch |
|---|---|---|
| PCB-FOOT-001 | Footprints not in the approved list | Whether the approved list itself is right: approve each entry against the datasheet |
| PCB-MODEL-001 | Footprints without a 3D model | A model of the wrong part or wrongly oriented |
| PCB-PADNET-001 | Pads without a net | Whether the pin was meant to be NC |
| PCB-WIDTH-001 | Narrow tracks on nets whose name looks like power | Real current capacity (copper, length, temperature); power nets with unusual names (`power_net_patterns`); planes |
| PCB-PINS-001 | Pins listed in `pins.yaml` vs PCB nets | Pins not listed; transcription errors in `pins.yaml` |
| PCB-HOLE-001 | Mounting-hole position and drill vs expected values | Whether the expected values match the enclosure (cad-verify checks that) |
| PCB-KEEPOUT-001 | Tracks, vias and pads of other nets inside the washer radius | Copper zones; screw heads larger than the configured radius |

## Circuit checks (schematic netlist)
Net voltages come from net names (`+3V3`, `5V`, `VBUS`, `GND`...), fixed regulators and `net_voltages`. Unknown voltages are never guessed, so a rail named `VCC` without a `net_voltages` entry disables the checks that need it.

| Check | Catches | Does not catch |
|---|---|---|
| CIR-POL-001 | LEDs whose anode resolves to GND, reverse-biased LEDs, diodes forward-shorting a rail, reversed flyback diodes across relays/inductors, forward-biased clamps, reversed polarized capacitors | Polarity on nets of unknown voltage; footprint pin-1 vs symbol (MOD-FOOT-001 and HUM-POL-001) |
| CIR-REG-001 | Fixed regulators whose output voltage (from the part value) differs from the rail name; input below output + dropout | Adjustable regulators (kicad-happy computes their feedback dividers); thermal dissipation |
| CIR-LED-001 | LED current outside `led_min_ma`..`led_max_ma`, from rail/logic level, series resistor and a colour-based Vf | LEDs driven by constant-current drivers or through more than one resistor |
| CIR-BJT-001 | NPN base driven without a resistor, base current above `gpio_max_ma` | Saturation margin vs collector load; MOSFET gate drive |
| CIR-FLOAT-001 | Enable/reset/boot pins with no other connection | Pins tied to the wrong level (MOD-STRAP-001) |
| KH-* (kicad-happy v2.3.1) | 60+ detectors: capacitor derating, regulator feedback, decoupling, bus pull-ups, protection mapping, edge distance, DFM, test points... | See kicad-happy's own documentation. Its findings at `error` gate as FAIL, `warning` as WARN; `info` goes to the reviewer only |

## Fabrication outputs
| Check | Catches | Does not catch |
|---|---|---|
| FAB-GERBER-001 | Missing copper, mask or outline layers (X2 FileFunction) | Paste and silkscreen (optional); Gerbers without X2 attributes |
| FAB-STALE-001 | Gerbers that no longer match the PCB: re-plots with kicad-cli and compares the geometry of apertures with a function (pads, tracks, vias, outline) | Drill marks and other function-less apertures; plot-setting differences that do not move coordinates |
| FAB-DRILL-001 | Drill diameters, counts and positions (±0.01 mm, absolute or aux origin) vs the PCB | Slots; plated/non-plated mix-ups; Excellon without a decimal point (diameters only) |
| FAB-BOM-001 | Assembled parts missing from the BOM or extra in it, footprint per reference, empty supplier codes | Whether the supplier code is the right part (value, voltage, tolerance); stock |
| FAB-CPL-001 | Missing parts, position not at the pad centre (±0.5 mm after detecting origin and Y direction), wrong side | **Rotations**: every fab house applies its own offsets. Check them in its viewer (HUM-CPL-001) |

## Independent reviewer (MOD-*)
A separate `claude -p` session without access to the design conversation. It receives a bundle (requirements, report, netlist with pin tables, kicad-happy summary, datasheet text) and must quote each piece of evidence verbatim. kicad-verify re-checks every quote against the cited file and applies the verdict rules in `review.py`. It can only judge what is in the bundle and the project files; it cannot browse the web. Missing datasheets make the pinout and rating requirements NOT_VERIFIABLE, which blocks `release` by default.

## Human sign-off (HUM-*)
Recorded with `kicadverify signoff`, bound to a content hash of the KiCad files: any later change invalidates it.

## General limits
- kicad-cli reads the saved file, not what is on screen in KiCad.
- `kicad-cli pcb drc --schematic-parity` hangs on KiCad 10.0.5 with at least one real project; parity is computed by kicad-verify instead.
- The checks and the reviewer can share blind spots with the tools that generated the design; the human sign-offs exist for that reason.
