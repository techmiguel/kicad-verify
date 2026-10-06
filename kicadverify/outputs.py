"""Report renderings for people and CI systems: Markdown (traceability matrix, gate verdicts,
coverage, deviations, limits) and JUnit XML (one test case per requirement)."""
import re
import xml.etree.ElementTree as ET
from pathlib import Path

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
    prov = rep.get("provenance")
    if prov:
        t, pol, it = prov["tools"], prov["policy"], prov["intent"]
        kv = t["kicad-verify"]
        L += ["## Provenance", "", "| What | Identity |", "|---|---|",
              f"| Design | `{rep['design_hash'][:16]}` ({len(prov['artifacts']['groups']['design'])} files) |",
              f"| Artifacts | `{prov['artifacts']['digest'][:23]}` ("
              + ", ".join(f"{len(v)} {k}" for k, v in prov["artifacts"]["groups"].items()) + ") |",
              f"| Policy | `{pol['digest'][:23]}`: {pol['requirements']} requirements, {pol['waivers']} waivers, "
              f"{pol['excluded']} excluded, fab profile {pol['fab_profile'] or 'none'} |",
              f"| Design intent | `{it['digest'][:23]}`: {len(it['declared'])} fields declared, "
              f"{len(it['missing'])} missing{' (' + ', '.join(it['missing']) + ')' if it['missing'] else ''} |",
              f"| kicad-verify | {kv['version']} `{kv['code_digest'][:23]}`"
              + (f" git {kv['git']['commit'][:12]}{' (dirty)' if kv['git']['dirty'] else ''}" if kv.get("git") else "")
              + " |",
              f"| kicad-cli | {t['kicad-cli']['version']} |",
              f"| kicad-happy | {t['kicad-happy']['pinned']} {(t['kicad-happy'].get('commit') or 'not installed')[:12]} |",
              f"| Python / platform | {t['python']} / {t['platform']} |"]
        rp = rep.get("review_provenance")
        if rp:
            r = rp.get("reviewer") or {}
            L.append(f"| Reviewer | {r.get('models') or r.get('model')}, claude {rp.get('claude_cli')}, "
                     f"bundle `{rp.get('bundle_digest', '')[:23]}` |")
        L += ["", "Policy components: " + ", ".join(f"{k} `{v[7:15]}`" for k, v in pol["components"].items()), ""]
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


# ------------------------------------------------------------------ GitHub annotations
GH_LIMIT = 10  # GitHub shows at most 10 error and 10 warning annotations per step
_REF = re.compile(r"\b([A-Z]{1,4}[0-9]{1,4})\b")


def _gh_escape(s, prop=False):
    s = str(s).replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
    return s.replace(":", "%3A").replace(",", "%2C") if prop else s


def _locate(root, v, design=()):
    """(file, line) for an annotation: the first referenced component's block in the schematic or
    board the requirement's evidence points to; else the first evidence file (KiCad files first);
    else the board of the design; else None."""
    ev = [a["path"] for e in v.get("evidence", []) for a in e.get("artifacts", [])
          if a.get("path") and a.get("sha256") and not a["path"].startswith("verification/")]
    kicad = [f for f in ev if f.endswith((".kicad_sch", ".kicad_pcb", ".kicad_pro", ".kicad_dru"))]
    files = list(dict.fromkeys(kicad + ev)) or [f for f in design if f.endswith(".kicad_pcb")][:1]
    texts = [f.get("text", "") for f in v.get("findings", [])] + \
            [u.get("text", "") for u in (v.get("coverage") or {}).get("unchecked", [])]
    refs = [m for t in texts for m in _REF.findall(t)]
    for f in sorted(files, key=lambda p: not p.endswith((".kicad_sch", ".kicad_pcb"))):
        p = Path(root) / f
        if refs and p.suffix in (".kicad_sch", ".kicad_pcb") and p.is_file():
            needles = [f'(property "Reference" "{r}"' for r in refs[:5]]
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if any(n in line for n in needles):
                    return f, i
    return (files[0], None) if files else (None, None)


def _repo_root(root):
    import os
    import subprocess
    if os.environ.get("GITHUB_WORKSPACE"):
        return Path(os.environ["GITHUB_WORKSPACE"])
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=root, capture_output=True, text=True,
                             timeout=20).stdout.strip()
        return Path(top) if top else Path(root)
    except Exception:
        return Path(root)


def github_annotations(rep, root, gate="dev", repo_root=None):
    """GitHub Actions workflow commands, one per requirement that blocks `gate` or FAILED, most
    severe first, within GitHub's per-step limits; the rest are counted in a final notice. File
    paths are relative to the repository root, as GitHub expects."""
    repo_root = Path(repo_root or _repo_root(root)).resolve()
    ver = rep["verification"]
    blockers = {b["id"] for b in ver["gates"][gate]["blockers"]}
    order = {FAILED: 0, NOT_VERIFIABLE: 1, NOT_RUN: 2}
    vs = sorted((v for v in ver["requirements"] if v["status"] == FAILED or v["id"] in blockers),
                key=lambda v: (v.get("severity") != "error", order.get(v["status"], 3), v["id"]))
    design = [a["path"] for a in ((rep.get("provenance") or {}).get("artifacts") or {}).get("groups", {}).get("design", [])]
    lines, counts, dropped = [], {"error": 0, "warning": 0}, 0
    for v in vs:
        level = "error" if v["id"] in blockers and v["status"] == FAILED else "warning"
        if counts[level] >= GH_LIMIT:
            dropped += 1
            continue
        counts[level] += 1
        f, line = _locate(root, v, design)
        if f:
            try:
                f = str((Path(root) / f).resolve().relative_to(repo_root)).replace("\\", "/")
            except ValueError:
                pass
        props = ([f"file={_gh_escape(f, True)}"] if f else []) + ([f"line={line}"] if line else [])
        props.append(f"title={_gh_escape(v['id'] + ' ' + v['status'], True)}")
        body = [v["text"], v["reason"]]
        body += [f"- {x['text']}" for x in v.get("findings", [])[:5]]
        body += [f"- not checked: {u['text']}" for u in ((v.get("coverage") or {}).get("unchecked") or [])[:5]]
        lines.append(f"::{level} {','.join(props)}::{_gh_escape(chr(10).join(body))}")
    if dropped:
        lines.append(f"::notice title=kicad-verify::{dropped} more requirements are not annotated "
                     "(GitHub limit); see the job summary")
    return "\n".join(lines)
