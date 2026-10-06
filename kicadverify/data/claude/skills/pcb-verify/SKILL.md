---
name: pcb-verify
description: Run the kicad-verify deterministic gate (ERC, DRC, schematic/PCB parity, pin mapping, circuit checks, kicad-happy detectors, Gerber/drill/BOM/CPL consistency) on a KiCad project. Use when asked to verify, validate or check a PCB design, or for "/pcb-verify [path]".
---
Run from any directory:

`kicadverify verify <project-path> [--fast]`

- Full mode (default) also runs kicad-cli ERC/DRC, re-plots the Gerbers and exports the board STEP for cad-verify; it can take a minute or two. Run it in the background if needed.
- Full report: `<project>/verification/pcb/reports/verify_report.json`.
- Summarise for the user: overall status, every FAIL and WARN with its evidence, and the pending human sign-offs.
- A clean result is not proof the board works: read `docs/COVERAGE.md` of kicad-verify for what each check does not cover.
- Never delete or hide a finding. If it is intentional, propose a waiver (check, key, reason, date, expiry) in `verification/pcb/waivers.yaml` and ask the user to confirm it.
