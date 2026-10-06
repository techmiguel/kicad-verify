"""Report renderings for people and CI systems: Markdown (traceability matrix, gate verdicts,
coverage, deviations, limits) and JUnit XML (one test case per requirement)."""
import xml.etree.ElementTree as ET

from .report import FAILED, NOT_RUN, NOT_VERIFIABLE, VERDICTS, VERIFIED

ICON = {VERIFIED: "✅", FAILED: "❌", NOT_VERIFIABLE: "❔", NOT_RUN: "⏸️"}


def _esc(s):
    return str(s if s is not None else "").replace("|", "\\|").replace("\n", " ")


def _src(src):
    if not src:
        return "**no source**"
    ref = src.get("ref", "")
    txt = f"[{_esc(ref)}]({src['url']})" if src.get("url") else _esc(ref)
    flag = " (unconfirmed)" if src.get("confirmed") is False else ""
    return f"{src.get('kind', '?')}: {txt}{flag}"


def _cov(c):
    if not c:
        return "-"
    return f"{c['checked']}/{c['total']} {_esc(c['scope'])}"


def markdown(rep, gate=None, limit=15):
    ver = rep["verification"]
    s = ver["summary"]
    L = [f"# Verification report: {rep['project']}", "",
         f"Design `{str(rep.get('design_hash'))[:16]}` · mode `{rep['mode']}` · {rep['time']} · "
         f"kicad-verify {rep['tools'].get('kicad-verify')} · {rep['tools'].get('kicad-cli')}", ""]
    L += ["## Gates", "", "| Gate | Result | Blockers | Blocks on |", "|---|---|---|---|"]
    for name, g in ver["gates"].items():
        L.append(f"| {name} | {'PASS' if g['pass'] else 'BLOCKED'} | {len(g['blockers'])} | "
                 f"{', '.join(g.get('blocks_on', []))} (+ error-severity FAILED) |")
    L.append("")
    shown = [gate] if gate else [n for n, g in ver["gates"].items() if not g["pass"]][:1]
    for name in shown:
        g = ver["gates"][name]
        if g["blockers"]:
            L += [f"### What blocks `{name}`", "", "| Requirement | Status | Reason | Action |", "|---|---|---|---|"]
            L += [f"| {b['id']} | {b['status']} | {_esc(b['reason'])[:200]} | {_esc(b['action'])} |"
                  for b in g["blockers"]]
            L.append("")
    L += ["## Summary", "",
          f"{s['total']} applicable requirements: "
          + ", ".join(f"{ICON[k]} {k} {s['by_status'][k]}" for k in VERDICTS)
          + f". Verified {s['verified_pct']}%. {s['excluded']} excluded, {s['deviations']} waived deviations. "
          f"Scope items checked: {s['scope_items']['checked']}/{s['scope_items']['total']}.", ""]
    L += ["| Method | " + " | ".join(VERDICTS) + " |", "|---" * (len(VERDICTS) + 1) + "|"]
    for m, c in s["by_method"].items():
        L.append(f"| {m} | " + " | ".join(str(c[k]) for k in VERDICTS) + " |")
    L.append("")
    L += ["## Traceability matrix", "",
          "| Requirement | Status | Source | Method | Verified by | Gate | Coverage | Reason |",
          "|---|---|---|---|---|---|---|---|"]
    for v in ver["requirements"]:
        dev = f" ({len(v['deviations'])} waived)" if v.get("deviations") else ""
        L.append(f"| **{v['id']}** {_esc(v['text'])} | {ICON[v['status']]} {v['status']}{dev} | {_src(v['source'])} "
                 f"| {v['method']} | {', '.join(v['verified_by'])} | {v['gate']} | {_cov(v.get('coverage'))} "
                 f"| {_esc(v['reason'])[:220]} |")
    L.append("")
    det = [v for v in ver["requirements"] if v["status"] != VERIFIED or v.get("deviations")]
    if det:
        L += ["## Details", ""]
        for v in det:
            L += [f"### {v['id']}: {v['status']}", "", _esc(v["reason"]), ""]
            for f in v.get("findings", [])[:limit]:
                L.append(f"- finding `{f['key']}` ({f['check']}): {_esc(f['text'])}")
            for u in ((v.get("coverage") or {}).get("unchecked") or [])[:limit]:
                L.append(f"- not checked `{u['key']}`: {_esc(u['text'])}")
            for d in v.get("deviations", [])[:limit]:
                L.append(f"- waived `{d['key']}` ({d['kind']}): {_esc(d['text'])}. Reason: {_esc(d['reason'])} "
                         f"({d.get('by') or 'unnamed'}, {d['date']}{', expires ' + d['expires'] if d.get('expires') else ''})")
            if v.get("limits"):
                L.append(f"- does not cover: {_esc(v['limits'])}")
            L.append("")
    if ver.get("excluded"):
        L += ["## Excluded requirements", "", "| Requirement | Reason |", "|---|---|"]
        L += [f"| {e['id']} | {_esc(e.get('reason') or '**no reason given**')} |" for e in ver["excluded"]]
        L.append("")
    if ver.get("lint"):
        L += ["## Requirement set problems", ""]
        L += [f"- {x['level']}: {x['id']}: {_esc(x['text'])}" for x in ver["lint"]]
        L.append("")
    L += ["## Evidence", "", "Files read by the verifiers, with their SHA-256 at verification time:", ""]
    seen = {}
    for v in ver["requirements"]:
        for e in v["evidence"]:
            for a in e["artifacts"]:
                if a.get("sha256"):
                    seen[a["path"]] = a["sha256"]
    L += [f"- `{p}` `{h[:16]}`" for p, h in sorted(seen.items())]
    L += ["", "A VERIFIED requirement means its verifiers found no defect within their coverage, not that "
          "the board is correct. Each requirement's limits are listed above and in docs/COVERAGE.md.", ""]
    return "\n".join(L)


def junit(rep):
    ver = rep["verification"]
    suites = ET.Element("testsuites", name=f"kicad-verify {rep['project']}")
    by = {}
    for v in ver["requirements"]:
        by.setdefault(v["method"], []).append(v)
    for method, vs in by.items():
        ts = ET.SubElement(suites, "testsuite", name=f"requirements.{method}", tests=str(len(vs)),
                           failures=str(sum(v["status"] == FAILED for v in vs)),
                           skipped=str(sum(v["status"] in (NOT_RUN, NOT_VERIFIABLE) for v in vs)), errors="0")
        for v in vs:
            tc = ET.SubElement(ts, "testcase", classname=f"{rep['project']}.{method}", name=f"{v['id']} {v['text']}"[:200])
            msg = f"{v['status']}: {v['reason']}"
            if v["status"] == FAILED:
                f = ET.SubElement(tc, "failure", message=msg[:500], type=FAILED)
                f.text = "\n".join(f"{x['check']} [{x['key']}] {x['text']}" for x in v.get("findings", []))
            elif v["status"] in (NOT_RUN, NOT_VERIFIABLE):
                ET.SubElement(tc, "skipped", message=msg[:500])
            out = [f"status: {v['status']}", f"source: {(v.get('source') or {}).get('ref')}",
                   f"verified by: {', '.join(v['verified_by'])}", f"coverage: {_cov(v.get('coverage'))}"]
            out += [f"waived {d['key']}: {d['reason']}" for d in v.get("deviations", [])]
            ET.SubElement(tc, "system-out").text = "\n".join(out)
    return ET.tostring(suites, encoding="unicode", xml_declaration=True)
