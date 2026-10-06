---
name: pcb-release
description: Release gate for a KiCad board - deterministic checks, independent review and human sign-offs must all pass before the project is marked as release. Use for "/pcb-release" or when the user says the board is ready to manufacture.
---
Only when the user asks. Run `kicadverify release <project-path>`.
- If it is blocked, show exactly what blocks it (FAIL checks, reviewer requirements, pending sign-offs) and stop.
- Human sign-offs are recorded by the user, never by you: tell them the command `kicadverify signoff <ID> --by "<name>"` and what each item requires them to check physically.
- After release, any change to the KiCad files invalidates the sign-offs and the hook re-runs the reviewer.
