---
name: pcb-review
description: Full on-demand review of one KiCad project - deterministic gate plus an independent reviewer (clean `claude -p` session) whose cited evidence is re-checked by code. Use for "/pcb-review [path]", "review this board", "is this PCB ready to order?".
---
Run: `kicadverify review <project-path> [--model <model>]` (several minutes; run it in the background).

The command builds a context bundle, runs the reviewer in a separate session without this conversation, and discards any evidence quote that is not found verbatim in the cited file. Then:
1. Report each requirement with its status and the validated evidence from `verification/pcb/reports/review_report.json`.
2. List reviewer claims that were downgraded as unverified, so the user can check them by hand.
3. List the human sign-offs still pending (`kicadverify status`).
Do not overrule the reviewer silently; if you disagree with a verdict, say why and point to the file.
