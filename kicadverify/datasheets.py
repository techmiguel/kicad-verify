"""Downloads the PDFs referenced in each symbol's Datasheet field into <project>/datasheets/, so the
reviewer can cite them and kicad-verify can check those citations. Runs only on demand."""
import re
import urllib.request
from pathlib import Path

UA = {"User-Agent": "Mozilla/5.0 (kicad-verify datasheet fetch)"}


def _name(c):
    base = c["fields"].get("MPN") or c["value"] or c["ref"]
    return re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("_")[:60] + ".pdf"


def fetch(nl, dest, timeout=60):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    done, failed, skipped = [], [], []
    seen = {}
    for ref, c in sorted(nl["components"].items()):
        url = (c["datasheet"] or "").strip()
        if ref.startswith("#") or not url.lower().startswith("http"):
            continue
        if url in seen:
            continue
        target = dest / _name(c)
        seen[url] = target
        if target.exists():
            skipped.append(str(target))
            continue
        try:
            req = urllib.request.Request(url, headers=UA)
            data = urllib.request.urlopen(req, timeout=timeout).read()
            if not data.startswith(b"%PDF"):
                failed.append(f"{ref}: {url} did not return a PDF")
                continue
            target.write_bytes(data)
            done.append(str(target))
        except Exception as e:
            failed.append(f"{ref}: {url}: {e}")
    return done, skipped, failed
