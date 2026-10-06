# Verification model

kicad-verify treats "this PCB is correctly designed and ready to manufacture" as a claim that has to
be supported requirement by requirement, the way a CI pipeline supports "this commit can ship". It is
not an AI board review with a confidence score: every requirement says where it comes from, how it is
verified, what evidence was produced, how much of its scope was actually covered, and ends in one of
four states.

## Requirement

```yaml
- id: PRJ-PWR-001
  text: "The ESP-12F module is supplied from +3V3 on its VCC pin"
  source: {kind: datasheet, ref: "ESP-12F pin definitions, pin 8 VCC", file: docs/datasheets/esp12f.pdf}
  method: auto                       # auto | model | human
  check: {type: pin_net, ref: U2, pin: 8, net: "+3V3"}
  acceptance: no_fail                # no_fail | no_findings
  gate: fab                          # dev | fab | release | none
```

| Field | Meaning |
|---|---|
| `source` | Where the requirement comes from: `kind` (standard, regulatory, datasheet, fab_capability, design_intent, customer, lesson_learned, best_practice, tool), `ref`, optional `url`, `file`, `locator`. `confirmed: false` marks a source nobody has checked yet; `kicadverify requirements --lint` reports it. |
| `method` | `auto`: a deterministic verifier (built-in check or a declarative `check:` assertion). `model`: the independent reviewer, whose quotes are re-checked by code. `human`: a sign-off bound to the design hash. Aliases: analysis, inspection, test, demonstration, review. |
| `verified_by` | Verifier ids or patterns (`KH-*`) whose results decide the requirement. Defaults to the requirement id. |
| `acceptance` | `no_fail`: warning-level findings are accepted (the requirement text allows them). `no_findings`: every finding must be fixed or waived one by one. |
| `gate` | The first gate at which the requirement must be VERIFIED. `none` makes it advisory. |

The base set (`kicadverify requirements`) is inherited by every project. A project adds its own
requirements, overrides fields of base ones by id, and excludes ids only with a reason:
`disable: [{id: PCB-HOLE-001, reason: "...", by: "...", date: "..."}]`. Exclusions are listed in
every report.

## States

| State | When |
|---|---|
| **VERIFIED** | Every verifier ran, found nothing outside the acceptance criterion and covered its whole scope (or a person waived each unchecked item with a reason). |
| **FAILED** | A verifier found a defect that is not waived. |
| **NOT_VERIFIABLE** | A verifier ran but could not decide: an input is missing (datasheet, pins.yaml, fab profile, exported outputs), the coverage is partial, the tool failed, or the reviewer's claim did not survive the evidence check. |
| **NOT_RUN** | No verifier produced a result for the current design: fast mode, review not run (or run for another design or requirement set), no valid sign-off. |

With several verifier results the precedence is FAILED > NOT_VERIFIABLE > NOT_RUN > VERIFIED. A
requirement is never VERIFIED by default: no result means NOT_RUN.

A FAILED requirement carries a severity: `error` when a verifier reported a defect at gating
severity, `warning` when it failed only through a `no_findings` criterion on lower-severity findings.

## Coverage

Coverage is explicit at two levels.

1. **Scope of each run.** Every verifier reports what it examined: `{scope, total, checked,
   unchecked[]}`. Example: CIR-POL-001 examined 4 polarized parts and could decide 3; the fourth sits
   on a net of unknown voltage, so it is listed as unchecked with its own key. Partial coverage
   turns a clean run into NOT_VERIFIABLE. After checking the item by hand, a person waives that key
   with the reason, and the requirement becomes VERIFIED with the deviation on record.
2. **What each verifier can and cannot see.** `kicadverify/data/checks.yaml` declares, for every
   verifier, what it covers and what it does not. Each verdict carries those limits, and
   [COVERAGE.md](COVERAGE.md) is generated from the same file. A VERIFIED requirement means "no defect
   of this kind within this coverage", never "the board is correct".

Traceability closes the loop: a verifier result that no requirement claims is evaluated by
GEN-TRACE-001, so an untraced failure (for example a defect the reviewer reports outside every
requirement) still fails the gates.

## Evidence

- Every file a verifier read is recorded with its SHA-256 at verification time.
- Raw tool output is kept: `verification/pcb/reports/evidence/<board>.erc.json` and `.drc.json`, the
  netlist, the kicad-happy JSON.
- Reviewer evidence is a verbatim quote, the cited file (hashed) and a locator; a quote not found in
  the file is discarded and listed.
- Sign-offs record who, when, the outcome (`pass` or `--fail`), a note and the design hash.
- Waived findings and coverage gaps stay in the report as deviations with reason, date, author and
  expiry. A waiver that no longer matches anything is reported as stale; an expired one stops
  applying.

## Gates

| Gate | Blocks on | Typical use |
|---|---|---|
| `dev` | FAILED with error severity | After each edit (Claude Code hooks), `kicadverify verify` |
| `fab` | Any FAILED/NOT_VERIFIABLE/NOT_RUN requirement gated at dev or fab, plus every error-severity FAILED | CI on every push: "ready to fabricate" |
| `release` | Everything gated up to release, including reviewer and human requirements | `kicadverify release` |

A FAILED requirement with error severity blocks every gate: a confirmed defect is never deferred to a
later gate. Policies can be tuned per project with `params.gates`, for example
`gates: {fab: [FAILED, NOT_VERIFIABLE]}` to let NOT_RUN through on a machine without kicad-cli; the
report always shows the policy each gate used.

## Manufacturability against the target fab

A clean DRC only means the board obeys its own rules. Two requirements tie those rules to what the
chosen fab can make, from a profile that is itself a cited source (`kicadverify profiles`,
`params.fab.profile`):

- **FAB-RULES-001**: copper spacing is guaranteed by the DRC only when the board minimum clearance (or
  every net-class and custom-rule clearance) is at least the fab's minimum spacing. A board whose
  rules were relaxed to 0.1 mm passes DRC and fails here (seeded defect `rules_clearance_relaxed`).
- **FAB-DFM-001**: everything that can be measured on the board file is measured: track width, via
  drill, pad and annular ring, plated-hole drill and ring, hole-to-hole distance, copper to board
  edge (tracks, vias, pads, zone fills), board size, layer count and thickness. A limit missing from
  the profile is an unchecked item, not an assumption.

The built-in JLCPCB profile is marked `confirmed: false`: its values come from secondary summaries
of the capability page and should be checked against the fab's current page. Confirm it by
overriding it in the project with the source you checked:
`profile: {base: jlcpcb-1-2-layer-standard, source: {confirmed: true, by: "...", date: "..."}}`.

## Release record

`kicadverify release` runs the fab gate, the reviewer (or reuses the review on record for the same
design hash and requirement set) and the release gate. When everything passes it writes
`verification/pcb/release/manifest.json` (and an archived copy per design): the SHA-256 of every
design file, fabrication output and verification config, the reports, the verdict of every
requirement and the deviations accepted. `kicadverify audit` recomputes the hashes right before the
files go to the fab and fails on any difference.

## Provenance and attestations

Every report answers, for the run that produced it: what was verified, what could not be, under
which policy and tools, over which artifacts and against which design intent. The `provenance`
block of `verify_report.json` (and the Provenance table of the Markdown report) records:

| | Identity | Changes when |
|---|---|---|
| Policy | SHA-256 over the effective policy, with one digest per component: requirements (normalised), params, gate policy, waivers, exclusions, fab profile, verifier registry, reviewer prompt, pins; plus the hash of every policy source file | any requirement, threshold, waiver, exclusion or profile value changes |
| Tools | kicad-verify version, digest of its own code and data and git commit; kicad-cli version and path; kicad-happy pinned tag and installed commit; Python, platform, CI variables | a tool or its build changes |
| Artifacts | SHA-256 of every design file, fabrication output, datasheet and config file | any byte of an input changes |
| Design intent | the `- Field: value` lines of `design_intent.md` (declared and empty fields) and the context files given to the reviewer | the intent or the context changes |
| Review | the review key (requirements, intent, datasheets, prompt), the digest of every bundle file, the `claude` CLI version and the model that actually answered | a review input changes |

Digests are computed over canonical JSON, so identical inputs give identical digests.

The design intent is a requirement of its own, GEN-INTENT-001: each empty field of
`design_intent.md` is an unchecked item, so an undeclared intent is NOT_VERIFIABLE at the release
gate instead of being silently assumed. A review is reused only while the design hash and the review
key are unchanged: editing the intent, a datasheet, a reviewed requirement or the reviewer prompt
makes the reviewer requirements NOT_RUN until it runs again.

`kicadverify verify --attest` (always on for `release`) writes an
[in-toto Statement v1](https://in-toto.io/Statement/v1) to `verification/pcb/attestations/`:
`subject` lists every artifact with its SHA-256; the predicate
(`https://github.com/techmiguel/kicad-verify/verification/v1`) holds the policy, tools, intent,
gates and one entry per requirement with status, reason, source, verifiers, coverage and its gaps,
limits, evidence digests (quotes included) and deviations, plus the excluded requirements with their
reasons. With `--sign-key` (or `KICAD_VERIFY_SIGN_KEY`) it is signed with `ssh-keygen -Y sign`,
namespace `kicad-verify`, the mechanism git uses for SSH-signed commits.

```bash
kicadverify verify . --gate fab --attest --sign-key ~/.ssh/id_ed25519
kicadverify check-attestation verification/pcb/attestations/<file>.intoto.json --allowed-signers allowed_signers
kicadverify explain CIR-POL-001        # the chain of proof behind one verdict
```

`check-attestation` verifies the signature and recomputes every digest from the files on disk: it
lists the artifacts that changed, are missing or are new, and says whether the design, the policy
(naming the components that changed), the intent, the kicad-verify code and the kicad-cli version
are the same. The attestation holds when the signature is valid (if given), every subject artifact is
unchanged, no design or fabrication file was added, and the design, policy and intent digests match.
A different tool version is reported but does not by itself void the claim about the files.

`project.yaml` is not an input of the verification (`release` writes the status into it), so it is
not part of the subject.

## CI

`kicadverify init --ci github` writes `.github/workflows/hw-verify.yml`: kicad-cli from the official
KiCad image, `kicadverify verify --gate fab`, the Markdown report in the job summary, a JUnit file
(one test case per requirement: FAILED → failure, NOT_VERIFIABLE/NOT_RUN → skipped with the reason)
and the raw evidence as an artifact.
