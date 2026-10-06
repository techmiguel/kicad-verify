---
name: pcb-verify
description: Run the kicad-verify requirements gate on a KiCad project - every requirement gets VERIFIED / FAILED / NOT_VERIFIABLE / NOT_RUN with its source, evidence and coverage (ERC, DRC, parity, pin mapping, circuit checks, fab-profile DFM, Gerber/drill/BOM/CPL consistency, kicad-happy). Use when asked to verify, validate or check a PCB design, whether it is ready to fabricate, or for "/pcb-verify [path]".
---
Run from any directory:

`kicadverify verify <project-path> [--fast] [--gate dev|fab|release]`

- `--gate fab` answers "is this ready to fabricate?": it passes only when every requirement gated at fab is VERIFIED (or each finding/gap is waived with a reason, or the requirement is excluded with a reason). Full mode (default) can take a minute or two; run it in the background if needed.
- Reports: `<project>/verification/pcb/reports/verification_report.md` (traceability matrix) and `verify_report.json`; raw ERC/DRC output in `reports/evidence/`.
- Summarise for the user: the gate verdict, every requirement that is not VERIFIED with its reason and the action listed under the gate, the coverage gaps, and the pending human sign-offs.
- NOT_VERIFIABLE and NOT_RUN are not passes. Say what input or step is missing; never present them as OK.
- A VERIFIED requirement is only as strong as its verifier's coverage: mention the "does not cover" limits when relevant (`kicadverify checks`).
- Never delete or hide a finding. If it is intentional, propose a waiver (check, key, reason, date, by, expiry) in `verification/pcb/waivers.yaml` and ask the user to confirm it. Never write `by:` with your own name and never sign off human requirements.
