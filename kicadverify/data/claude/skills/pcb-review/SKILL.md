---
name: pcb-review
description: Full on-demand review of one KiCad project - requirements gate plus an independent reviewer (clean `claude -p` session) whose cited evidence is re-checked by code. Use for "/pcb-review [path]", "review this board", "is this PCB ready to order?".
---
Run: `kicadverify review <project-path> [--model <model>]` (several minutes; run it in the background).

The command builds a context bundle, runs the reviewer in a separate session without this conversation, and keeps only evidence quotes found verbatim in the cited files. A reviewer PASS without such a quote is NOT_VERIFIABLE, a FAIL with one is FAILED. The review is reused by later `verify`/`release` runs while the design hash and the reviewed requirement set are unchanged. Then:
1. Report each model requirement with its status and the validated evidence from `verification/pcb/reports/review_report.json`.
2. List reviewer claims that stayed NOT_VERIFIABLE (unproven PASS or unverified FAIL), so the user can check them by hand.
3. List the human sign-offs still pending (`kicadverify status`).
Do not overrule the reviewer silently; if you disagree with a verdict, say why and point to the file.
