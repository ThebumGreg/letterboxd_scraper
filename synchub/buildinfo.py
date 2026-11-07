"""Which code is this? The version only changes at a release, so the commit (and when the image was built) says what is really running.

Docker images get SYNCHUB_COMMIT / SYNCHUB_BUILT from the build arguments REVISION / BUILD_DATE (see the Dockerfile and docker-publish.yml). A copy run straight from a
git checkout asks git. `label` is what pages show: "1.4.0 (061cd30)".
"""
import os
import subprocess
from functools import lru_cache

from synchub import __version__

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def short(commit):
    """The first 7 characters of a commit id, or "" when it is not a plain hex id (docker's default 'unknown' and the like)."""
    text = str(commit or "").strip().lower()
    return text[:7] if len(text) >= 7 and all(c in "0123456789abcdef" for c in text) else ""


def _git(*args):
    try:
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", ROOT, *args], capture_output=True, text=True, timeout=5)
        return out.stdout.strip() if out.returncode == 0 else ""
    except (OSError, subprocess.SubprocessError):
        return ""


@lru_cache(maxsize=1)
def get():
    commit, built = short(os.environ.get("SYNCHUB_COMMIT")), (os.environ.get("SYNCHUB_BUILT") or "").strip()
    if not commit:
        commit = short(_git("rev-parse", "HEAD"))
        built = built or _git("log", "-1", "--format=%cI") if commit else built
    label = f"{__version__} ({commit})" if commit else __version__
    return {"version": __version__, "commit": commit, "built": built, "label": label}
