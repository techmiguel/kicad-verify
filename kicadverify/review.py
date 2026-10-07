"""Independent reviewer: builds a context bundle, runs `claude -p` in a clean session (no access to
the conversation that generated the design), and re-checks every piece of evidence it cites.

Verdict rules (enforced here, not trusted to the model):
- PASS with >= 1 evidence quote found verbatim (whitespace/case-insensitive) in the cited file -> VERIFIED.
- PASS without valid evidence -> NOT_VERIFIABLE (unproven). Missing verdict -> NOT_VERIFIABLE.
- FAIL with valid evidence -> FAILED; without -> NOT_VERIFIABLE (unverified claim, check by hand).
- NOT_VERIFIABLE -> NOT_VERIFIABLE (the gates decide whether it blocks).
A review is evidence for one design hash and one requirement set; `load_current` reuses it only
while both are unchanged.
"""
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config, evidence
from .checks.circuit import Circuit
from .report import FAIL, FAILED, NOT_VERIFIABLE, PASS, VERIFIED, WARN, Result

DATA = config.DATA
SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["PASS", "FAIL", "NOT_VERIFIABLE"]},
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"file": {"type": "string"}, "locator": {"type": "string"},
                                   "quote": {"type": "string"}},
                    "required": ["file", "quote"]}}},
            "required": ["id", "verdict", "summary", "evidence"]}},
        "extra_findings": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "severity": {"type": "string", "enum": ["error", "warning"]},
                "summary": {"type": "string"},
                "evidence": {"type": "array", "items": {
                    "type": "object",
                    "properties": {"file": {"type": "string"}, "locator": {"type": "string"},
                                   "quote": {"type": "string"}},
                    "required": ["file", "quote"]}}},
            "required": ["severity", "summary", "evidence"]}},
    },
    "required": ["verdicts", "extra_findings"],
}
GUARD_ENV = "KICAD_VERIFY_REVIEWER"
logging.getLogger("pypdf").setLevel(logging.ERROR)  # malformed vendor PDFs are common; keep output clean


# ------------------------------------------------------------------ bundle
def _datasheets(root, params, comps):
    dirs = (params.get("review") or {}).get("datasheet_dirs") or ["datasheets", "docs"]
    files = []
    for d in dirs:
        p = Path(root) / d
        if p.is_dir():
            files += [f for f in p.rglob("*.pdf")]
    files = sorted(set(files))
    index = []
    for f in files:
        stem = re.sub(r"[^a-z0-9]", "", f.stem.lower())
        refs = [r for r, c in comps.items()
                if any(tok and len(tok) >= 4 and tok in stem
                       for tok in (re.sub(r"[^a-z0-9]", "", (c["value"] or "").lower()),
                                   re.sub(r"[^a-z0-9]", "", (c["fields"].get("MPN") or "").lower()),
                                   re.sub(r"[^a-z0-9]", "", (c["fields"].get("LCSC") or "").lower())))]
        index.append({"file": str(f.relative_to(root)).replace("\\", "/"), "likely_refs": refs})
    urls = {r: c["datasheet"] for r, c in comps.items() if c["datasheet"] and c["datasheet"] != "~"}
    return {"local_files": index, "urls": urls}


def build_bundle(root, proj, designs, report, kh_summaries):
    bdir = proj["dir"] / "review" / "bundle"
    if bdir.exists():
        shutil.rmtree(bdir)
    bdir.mkdir(parents=True)
    reqs = [{"id": r["id"], "text": r["text"]} for r in proj["requirements"] if r.get("method") == "model"]
    (bdir / "requirements.json").write_text(json.dumps(reqs, indent=1), encoding="utf-8")
    (bdir / "verify_report.json").write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    design = {}
    all_comps = {}
    for label, nl in designs.items():
        if nl is None:
            continue
        cir = Circuit(nl, proj["params"])
        comps = []
        for r, c in sorted(nl["components"].items()):
            if r.startswith("#"):
                continue
            all_comps[r] = c
            comps.append({"ref": r, "value": c["value"], "footprint": c["footprint"],
                          "symbol": f"{c['lib']}:{c['part']}", "datasheet": c["datasheet"],
                          "fields": {k: v for k, v in c["fields"].items()
                                     if k not in ("Footprint", "Datasheet") and v}})
        pin_tables = {r: [{"pin": n, "name": p["name"], "type": p["type"], "net": nl["pin_net"].get((r, n))}
                          for n, p in sorted(c["pins"].items(), key=lambda kv: (len(kv[0]), kv[0]))]
                      for r, c in nl["components"].items() if not r.startswith("#") and len(c["pins"]) >= 3}
        design[label] = {
            "components": comps, "pin_tables": pin_tables,
            "nets": {n: [f"{x['ref']}.{x['pin']}" + (f"({x['name']})" if x['name'] else "") for x in nodes]
                     for n, nodes in nl["nets"].items()},
            "rail_voltages_estimated": {n: v for n, v in cir.v.items() if v is not None},
            "fixed_regulators": {r: {"output_net": n, "vout": v} for r, (n, v, _) in cir.reg_out.items()},
        }
    (bdir / "design.json").write_text(json.dumps(design, indent=1, ensure_ascii=False), encoding="utf-8")
    if any(kh_summaries.values()):
        (bdir / "kicad_happy.json").write_text(json.dumps(kh_summaries, indent=1, default=str), encoding="utf-8")
    _copy_context(root, proj, bdir)
    ds = _datasheets(root, proj["params"], all_comps)
    _extract_datasheets(root, bdir, ds)
    (bdir / "datasheets.json").write_text(json.dumps(ds, indent=1), encoding="utf-8")
    return bdir, reqs


def _copy_context(root, proj, bdir, limit=60000):
    """Design intent for the reviewer: operating conditions and decisions the netlist cannot show."""
    pats = (proj["params"].get("review") or {}).get("context_files") or []
    out = bdir / "context"
    files = []
    for pat in pats:
        files += [f for f in Path(root).glob(pat) if f.is_file()]
    files = list(dict.fromkeys(files))
    if not files:
        return
    out.mkdir(exist_ok=True)
    for f in files:
        dst = out / str(f.relative_to(root)).replace("\\", "__").replace("/", "__")
        dst.write_text(f.read_text(encoding="utf-8", errors="ignore")[:limit], encoding="utf-8")


def _extract_datasheets(root, bdir, ds, max_pages=80):
    """Plain-text copy of each local datasheet (the reviewer may lack a PDF renderer); quotes from
    these .txt files are validated like any other file."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return
    out = bdir / "datasheets_text"
    out.mkdir(exist_ok=True)
    for item in ds["local_files"]:
        src = Path(root) / item["file"]
        try:
            pages = PdfReader(str(src)).pages[:max_pages]
            text = "\n".join(f"--- page {i + 1} ---\n{(p.extract_text() or '')}" for i, p in enumerate(pages))
        except Exception as e:
            item["text_error"] = str(e)[:120]
            continue
        dst = out / (re.sub(r"[^A-Za-z0-9._-]+", "_", src.stem) + ".txt")
        dst.write_text(text, encoding="utf-8")
        item["text_file"] = str(dst.relative_to(bdir)).replace("\\", "/")


# ------------------------------------------------------------------ evidence validation
def _norm(s):
    s = (s or "").lower().replace(" ", " ")
    s = re.sub(r"[\"'{}\[\],`]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_TEXT_CACHE = {}


def _file_text(path):
    key = str(path)
    if key in _TEXT_CACHE:
        return _TEXT_CACHE[key]
    txt = None
    try:
        if path.suffix.lower() == ".pdf":
            try:
                from pypdf import PdfReader
                txt = "\n".join((p.extract_text() or "") for p in PdfReader(str(path)).pages)
            except Exception:
                txt = None
        else:
            txt = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        txt = None
    _TEXT_CACHE[key] = _norm(txt) if txt is not None else None
    return _TEXT_CACHE[key]


def check_evidence(ev, root, bdir):
    q = ev.get("quote") or ""
    if len(q.strip()) < 8:
        return False, "quote shorter than 8 characters"
    f = (ev.get("file") or "").replace("\\", "/").lstrip("./")
    cands = [Path(root) / f, bdir / f, bdir / Path(f).name]
    path = next((c for c in cands if c.is_file()), None)
    if path is None:
        return False, f"file not found: {f}"
    txt = _file_text(path)
    if txt is None:
        return False, f"cannot extract text from {f}"
    nq = _norm(q)
    if nq in txt:
        return True, "ok"
    # PDF text extraction often breaks words across lines: retry without spaces
    if path.suffix.lower() == ".pdf" and nq.replace(" ", "") in txt.replace(" ", ""):
        return True, "ok (whitespace-insensitive)"
    return False, f"quote not found in {f}"


def judge(raw, reqs, root, bdir):
    """Requirement results from the reviewer's answer. The model's verdict is an input, not the result:
    only quotes found verbatim in the cited files count as evidence."""
    verdicts = {v["id"]: v for v in (raw or {}).get("verdicts", [])}
    results, detail = [], []
    for r in reqs:
        v = verdicts.get(r["id"])
        if v is None:
            results.append(Result(r["id"], WARN, "reviewer returned no verdict", outcome=NOT_VERIFIABLE))
            detail.append({"id": r["id"], "status": WARN, "outcome": NOT_VERIFIABLE, "reason": "no verdict"})
            continue
        checked = [(e, *check_evidence(e, root, bdir)) for e in v.get("evidence", [])]
        good = [e for e, ok, _ in checked if ok]
        bad = [f"{e.get('file')}: {why}" for e, ok, why in checked if not ok]
        verdict = v["verdict"]
        outcome = None
        if verdict == "PASS":
            st, outcome = (PASS, VERIFIED) if good else (WARN, NOT_VERIFIABLE)
            why = v["summary"] if good else f"PASS claimed without verifiable evidence (unproven): {v['summary']}"
        elif verdict == "FAIL":
            st, outcome = (FAIL, FAILED) if good else (WARN, NOT_VERIFIABLE)
            why = v["summary"] if good else f"unverified defect claim (check manually): {v['summary']}"
        else:  # NOT_VERIFIABLE, or the legacy WARN
            st, outcome = WARN, NOT_VERIFIABLE
            why = f"not verifiable: {v['summary']}"
        vio = [] if st == PASS else [{"key": r["id"], "text": why}]
        vio += [{"key": f"{r['id']}-ev{i}", "text": f"discarded evidence: {b}"} for i, b in enumerate(bad)]
        ev = [{"path": str(_resolve(e, root, bdir)), "locator": e.get("locator", ""), "quote": e["quote"]}
              for e in good]
        results.append(Result(r["id"], st, why, evidence=ev, violations=vio, outcome=outcome))
        detail.append({"id": r["id"], "model_verdict": verdict, "status": st, "outcome": outcome,
                       "summary": v["summary"], "evidence_ok": len(good), "evidence_rejected": bad,
                       "evidence": [{**e, "valid": ok, "why": w} for e, ok, w in checked]})
    extras = []
    for i, x in enumerate((raw or {}).get("extra_findings", [])):
        checked = [(e, *check_evidence(e, root, bdir)) for e in x.get("evidence", [])]
        good = [e for e, ok, _ in checked if ok]
        st = FAIL if (x["severity"] == "error" and good) else WARN
        tag = "" if good else " [unverified]"
        extras.append({"key": f"extra-{i}", "text": f"{x['severity']}: {x['summary']}{tag}", "status": st,
                       "evidence": [{**e, "valid": ok, "why": w} for e, ok, w in checked]})
    if extras:
        results.append(Result("REV-EXTRA", FAIL if any(e["status"] == FAIL for e in extras) else WARN,
                              f"{len(extras)} additional findings from the reviewer",
                              violations=[{"key": e["key"], "text": e["text"]} for e in extras]))
    return results, detail, extras


def _resolve(ev, root, bdir):
    f = (ev.get("file") or "").replace("\\", "/").lstrip("./")
    return next((c for c in (Path(root) / f, bdir / f, bdir / Path(f).name) if c.is_file()), Path(root) / f)


# ------------------------------------------------------------------ run
def run_reviewer(root, bdir, model, timeout_s=1800):
    claude = shutil.which("claude")
    if not claude:
        raise RuntimeError("`claude` CLI not found in PATH")
    prompt = (DATA / "reviewer_prompt.md").read_text(encoding="utf-8").format(
        root=str(root), bundle=str(bdir.relative_to(root)).replace("\\", "/"))
    # the prompt goes through stdin: on Windows `claude` is a .CMD shim that truncates arguments at
    # the first newline
    cmd = [claude, "-p", "--model", model, "--output-format", "json",
           "--json-schema", json.dumps(SCHEMA), "--no-session-persistence",
           "--allowedTools", "Read", "Grep", "Glob"]
    env = dict(os.environ, **{GUARD_ENV: "1"})
    t0 = time.time()
    p = subprocess.run(cmd, cwd=str(root), input=prompt, capture_output=True, text=True, timeout=timeout_s,
                       env=env, encoding="utf-8", errors="replace")
    meta = {"model": model, "duration_s": round(time.time() - t0, 1), "returncode": p.returncode}
    try:
        out = json.loads(p.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"reviewer output is not JSON: {p.stdout[:400]} {p.stderr[:400]}") from e
    meta.update({"cost_usd": out.get("total_cost_usd"), "num_turns": out.get("num_turns"),
                 "is_error": out.get("is_error"), "models": list((out.get("modelUsage") or {}).keys())})
    raw = out.get("structured_output")
    if raw is None:
        try:
            raw = json.loads(out.get("result") or "")
        except Exception as e:
            raise RuntimeError(f"reviewer returned no structured output: {str(out.get('result'))[:400]}") from e
    return raw, meta


def provenance_of(root, bdir, prov, key):
    """What the reviewer was given: a digest of every bundle file, the intent and policy digests, the
    `claude` CLI version. The model that actually answered is added from the run metadata."""
    from . import provenance
    files = {str(f.relative_to(bdir)).replace("\\", "/"): evidence.sha256(f)
             for f in sorted(bdir.rglob("*")) if f.is_file() and f.name != "reviewer_raw.json"}
    claude = shutil.which("claude")
    ver = None
    if claude:
        try:
            ver = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
        except Exception:
            ver = None
    return {"review_key": key, "bundle_digest": provenance.digest(files), "bundle_files": files,
            "intent_digest": prov["intent"]["digest"], "policy_digest": prov["policy"]["digest"],
            "claude_cli": ver}


def write_report(proj, model_meta, detail, extras, results, review_key=None, prov=None):
    rep = {"tool": "kicad-verify", "kind": "review", "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
           "design_hash": config.design_hash(proj["root"]), "review_key": review_key, "provenance": prov,
           "reviewer": model_meta,
           "overall": (FAIL if any(r.status == FAIL for r in results)
                       else WARN if any(r.status == WARN for r in results) else PASS),
           "requirements": detail, "extra_findings": extras, "results": [r.as_dict() for r in results]}
    f = proj["dir"] / "reports" / "review_report.json"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(rep, indent=2, ensure_ascii=False), encoding="utf-8")
    return rep, f


def load_current(proj, design_hash, review_key):
    """(results, reason, provenance) of the last review when it was made for this design and the same
    review inputs (requirements, design intent, datasheets, prompt), else (None, reason, None)."""
    f = proj["dir"] / "reports" / "review_report.json"
    if not f.exists():
        return None, "no review report", None
    try:
        rep = json.loads(f.read_text(encoding="utf-8"))
    except Exception as e:
        return None, f"unreadable review report: {e}", None
    if rep.get("design_hash") != design_hash:
        return None, f"last review is for design {str(rep.get('design_hash'))[:12]}", None
    if rep.get("review_key") != review_key:
        return None, ("the review inputs changed since the last review (requirements, design intent, "
                      "datasheets or reviewer prompt)"), None
    if not rep.get("results"):
        return None, "the last review did not run", None
    res = [Result.from_dict(d) for d in rep["results"]]
    for r in res:
        r.evidence = list(r.evidence) + [f]
    prov = rep.get("provenance") or {}
    prov = {**prov, "reviewer": rep.get("reviewer"), "report": {"path": str(f), "sha256": evidence.sha256(f)}}
    return res, "reused", prov
