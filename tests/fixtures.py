"""Test fixture: a real, fabricated open-hardware board (smartRele, CERN-OHL-W) at a pinned commit.

Fetched with a partial git clone on first use into tests/.fixtures/ (git-ignored); nothing from it
is redistributed in this repository.
"""
import shutil
import subprocess
from pathlib import Path

REPO = "https://github.com/techmiguel/smartRele.git"
COMMIT = "6053b84d7a22fa937b46a375834441383320a045"
HERE = Path(__file__).resolve().parent
CACHE = HERE / ".fixtures"
SPARSE = ["/*.kicad_*", "/fabrication/", "/enclosure/", "/3dmodels/", "/docs/datasheets/", "/*.md", "/LICENSE"]


def _git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=900)


def rele_board():
    dest = CACHE / f"smartRele-{COMMIT[:8]}"
    if (dest / "rele-esp12f.kicad_pcb").exists():
        if not (dest / "docs" / "datasheets").exists():  # older sparse set
            _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=dest)
        return dest
    shutil.rmtree(dest, ignore_errors=True)
    CACHE.mkdir(exist_ok=True)
    _git("clone", "--filter=blob:none", "--no-checkout", REPO, str(dest))
    _git("sparse-checkout", "set", "--no-cone", *SPARSE, cwd=dest)
    _git("checkout", COMMIT, cwd=dest)
    return dest


def copy_of(src, dst):
    shutil.copytree(src, dst, ignore=shutil.ignore_patterns(".git", "verification", "firmware", "*.lck"))
    return Path(dst)
