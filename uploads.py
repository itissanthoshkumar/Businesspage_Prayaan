"""Git-backed image store for customer photographs.

The admin uploads a photo; it is written into a dedicated git repository and
served from a static path. Git is the versioned store behind the image, exactly
as asked — "load this in git and render the image from that".

Design choices, each load-bearing:

  * CONTENT-ADDRESSED filenames — sha256(bytes)[:16] + a sniffed extension. The
    client's filename never touches the filesystem, so there is no path to
    traverse (no "../../etc") and no way to overwrite someone else's image; the
    same photo uploaded twice is one file.

  * TYPE IS SNIFFED, not trusted. The extension and the Content-Type a client
    claims are ignored; the first bytes decide, and only JPEG / PNG / WebP pass.
    An HTML or SVG file renamed .jpg cannot get in and become stored XSS on a
    lender's domain.

  * A SEPARATE repo, not the project's. Committing customer uploads into the code
    repo would be noise, and there isn't one here anyway. The uploads dir is its
    own repository, git-init'd on demand, holding nothing but images.

  * Git is BEST-EFFORT and never blocks the render. If git is missing or the
    commit fails, the file is still written and still served — the photo appears
    either way — and the failure is reported in the response, not swallowed.
"""
import hashlib
import os
import subprocess
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# Overridable so a real deployment can point at a persistent volume; defaults to
# a folder under the served static tree so the image is reachable with no extra
# route.
UPLOAD_DIR = Path(os.getenv("PBN_UPLOAD_DIR", str(BASE_DIR / "static" / "img" / "uploads")))
PUBLIC_PREFIX = "/static/img/uploads"

MAX_IMAGE_BYTES = int(os.getenv("PBN_MAX_IMAGE_BYTES", str(6 * 1024 * 1024)))

GIT_NAME = os.getenv("PBN_GIT_NAME", "Prayaan Business Pages")
GIT_EMAIL = os.getenv("PBN_GIT_EMAIL", "uploads@prayaan.local")


class UploadError(ValueError):
    """Carries a sentence fit to show the admin."""


def _sniff_ext(data: bytes):
    """Return 'jpg' | 'png' | 'webp' from the magic bytes, or None. The client's
    filename and Content-Type are deliberately ignored."""
    if len(data) < 12:
        return None
    if data[:3] == b"\xff\xd8\xff":
        return "jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return None


def _git(args, cwd):
    """Run one git command, no shell. Returns (ok, output)."""
    try:
        r = subprocess.run(
            ["git"] + args, cwd=str(cwd),
            capture_output=True, text=True, timeout=20,
            # -c on each call rather than global config: this repo owns its
            # identity and cannot disturb the user's git setup.
        )
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    except (OSError, subprocess.SubprocessError) as e:
        return False, str(e)


def _ensure_repo():
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    if (UPLOAD_DIR / ".git").exists():
        return True, "existing repo"
    ok, out = _git(["init", "-q"], UPLOAD_DIR)
    if not ok:
        return False, "git init failed: " + out
    _git(["config", "user.name", GIT_NAME], UPLOAD_DIR)
    _git(["config", "user.email", GIT_EMAIL], UPLOAD_DIR)
    return True, "initialised repo"


def _commit(filename, by):
    """Best-effort commit of ONE file. Never raises."""
    ok, out = _ensure_repo()
    if not ok:
        return False, out
    ok, out = _git(["add", "--", filename], UPLOAD_DIR)
    if not ok:
        return False, "git add failed: " + out
    # -c author on the commit so the uploading admin is on the record without
    # rewriting the repo's committer identity.
    who = "{} <{}>".format((by or "admin"), GIT_EMAIL)
    ok, out = _git(
        ["-c", "user.name=" + GIT_NAME, "-c", "user.email=" + GIT_EMAIL,
         "commit", "-q", "--author", who, "-m", "upload " + filename, "--", filename],
        UPLOAD_DIR)
    if not ok:
        # "nothing to commit" means the identical image was already committed —
        # a success for our purposes, not a failure.
        if "nothing to commit" in out or "no changes added" in out:
            return True, "already stored"
        return False, "git commit failed: " + out
    return True, "committed"


def save_image(data: bytes, by=None) -> dict:
    """Validate, store, commit. Returns {url, committed, note}.

    Raises UploadError with a user-facing message on anything that should stop
    the upload (too big, empty, not an image)."""
    if not data:
        raise UploadError("The upload was empty.")
    if len(data) > MAX_IMAGE_BYTES:
        raise UploadError("That image is larger than {} MB. Please upload a "
                          "smaller one.".format(MAX_IMAGE_BYTES // (1024 * 1024)))
    ext = _sniff_ext(data)
    if not ext:
        raise UploadError("That file is not a JPEG, PNG or WebP image.")

    digest = hashlib.sha256(data).hexdigest()[:16]
    filename = "{}.{}".format(digest, ext)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    dest = UPLOAD_DIR / filename
    if not dest.exists():
        # write to a temp name then rename, so a served file is never half-written.
        # A full or read-only disk raises OSError here; turn it into a clean,
        # admin-facing message rather than letting a raw 500 escape.
        try:
            tmp = UPLOAD_DIR / (filename + ".part")
            tmp.write_bytes(data)
            tmp.replace(dest)
        except OSError:
            raise UploadError("Could not save the image on the server. Please try "
                              "again, or contact support if it keeps happening.")

    committed, note = _commit(filename, by)
    return {"url": "{}/{}".format(PUBLIC_PREFIX, filename),
            "committed": committed, "note": note}


def is_upload_url(url) -> bool:
    return bool(url) and str(url).startswith(PUBLIC_PREFIX + "/")
