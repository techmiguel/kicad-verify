---
name: pcb-release
description: Release gate for a KiCad board - every requirement (deterministic, independent review, human sign-off) must be VERIFIED before the project is marked as release and a manifest binds the verified files by SHA-256. Use for "/pcb-release" or when the user says the board is ready to manufacture.
---
Only when the user asks. Run `kicadverify release <project-path>`.
- It stops first if the fab gate does not pass, then runs the reviewer (or reuses the review on record for this exact design), then evaluates the release gate.
- If it is blocked, show the blockers table (requirement, status, reason, action) and stop.
- Human sign-offs are recorded by the user, never by you: tell them `kicadverify signoff <ID> --by "<name>"` (or `--fail --note "..."` when the check finds a problem) and what each item requires them to check physically.
- After release, tell the user to run `kicadverify audit <project-path>` right before uploading the files to the fab: it fails if any design or fabrication file differs from the release manifest.
