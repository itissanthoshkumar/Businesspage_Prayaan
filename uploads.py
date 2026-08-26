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

  * METADATA IS STRIPPED before the file is stored. A phone photo of the shop
    carries EXIF — including GPS coordinates, which for many customers is the
    owner's HOME. Publishing that on a public page is exactly the leak the
    "locality only, never a street address" rule exists to prevent, so EXIF/XMP
    (JPEG), textual+eXIf chunks (PNG) and EXIF/XMP chunks (WebP) are removed
    in pure stdlib byte surgery. The pixels are untouched. Stripping runs
    BEFORE hashing, so the same photo re-uploaded still dedupes to one file.

  * A SEPARATE repo, not the project's. Committing customer uploads into the code
    repo would be noise, and there isn't one here anyway. The uploads dir is its
    own repository, git-init'd on demand, holding nothing but images.

  * Git is BEST-EFFORT and never blocks the render. If git is missing or the
    commit fails, the file is still written and still served — the photo appears
    either way — and the failure is reported in the response, not swallowed.
"""
import hashlib
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("pbn")

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


# ---- metadata stripping -----------------------------------------------------
def _strip_jpeg(data: bytes) -> bytes:
    """Walk the JPEG segments up to SOS and drop the metadata carriers:
    APP1–APP13 (EXIF, XMP, IPTC/Photoshop), APP15 and COM. APP0 (JFIF) and
    APP14 (Adobe colour-transform — dropping it shifts colours on some files)
    are kept. From SOS on, everything is entropy-coded pixels and is copied
    verbatim."""
    out = bytearray(data[:2])                       # FF D8
    i, n = 2, len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            raise ValueError("bad segment marker")
        marker = data[i + 1]
        if marker == 0xDA:                          # SOS — rest is pixel data
            out += data[i:]
            return bytes(out)
        if marker in (0x01,) or 0xD0 <= marker <= 0xD9:   # standalone markers
            out += data[i:i + 2]
            i += 2
            continue
        seglen = (data[i + 2] << 8) | data[i + 3]
        if seglen < 2 or i + 2 + seglen > n:
            raise ValueError("bad segment length")
        drop = (0xE1 <= marker <= 0xED) or marker in (0xEF, 0xFE)
        if not drop:
            out += data[i:i + 2 + seglen]
        i += 2 + seglen
    raise ValueError("no SOS marker")


def _strip_png(data: bytes) -> bytes:
    """Drop the metadata chunks (eXIf, tEXt, zTXt, iTXt, tIME); every other
    chunk — including the ones that affect rendering — is kept byte-for-byte."""
    drop = {b"eXIf", b"tEXt", b"zTXt", b"iTXt", b"tIME"}
    out = bytearray(data[:8])                       # signature
    i, n = 8, len(data)
    while i + 12 <= n:
        length = int.from_bytes(data[i:i + 4], "big")
        ctype = data[i + 4:i + 8]
        end = i + 12 + length
        if end > n:
            raise ValueError("bad chunk length")
        if ctype not in drop:
            out += data[i:end]
        i = end
        if ctype == b"IEND":
            return bytes(out)
    raise ValueError("no IEND chunk")


def _strip_webp(data: bytes) -> bytes:
    """Drop EXIF and XMP RIFF chunks, clear their flag bits in VP8X, and
    rewrite the RIFF size."""
    out = bytearray(b"RIFF\x00\x00\x00\x00WEBP")
    i, n = 12, len(data)
    while i + 8 <= n:
        four = data[i:i + 4]
        size = int.from_bytes(data[i + 4:i + 8], "little")
        total = 8 + size + (size & 1)               # chunks are 2-byte aligned
        if i + total > n:
            total = n - i                            # tolerate missing pad byte
        if four in (b"EXIF", b"XMP "):
            i += total
            continue
        chunk = bytearray(data[i:i + total])
        if four == b"VP8X" and size >= 1:
            chunk[8] &= ~0x0C                        # clear EXIF(8) + XMP(4) flags
        out += chunk
        i += total
    out[4:8] = (len(out) - 8).to_bytes(4, "little")
    return bytes(out)


def strip_metadata(data: bytes, ext: str) -> bytes:
    """Best-effort but expected to succeed: these are the same bytes _sniff_ext
    just validated. If a malformed file defeats the walk anyway, the ORIGINAL
    is stored and the miss is logged — a failed strip must not block the admin,
    and the log line is the prompt to look at the file."""
    try:
        if ext == "jpg":
            return _strip_jpeg(data)
        if ext == "png":
            return _strip_png(data)
        if ext == "webp":
            return _strip_webp(data)
    except Exception:                                # noqa: BLE001
        log.exception("upload: metadata strip failed for %s — storing as-is", ext)
    return data


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

    # EXIF/GPS and other metadata come off BEFORE hashing, so the digest — and
    # the dedupe — keys on what is actually served.
    data = strip_metadata(data, ext)

    digest = hashlib.sha256(data).hexdigest()[:16]

    # On the Mongo backend, photos live IN the database: ephemeral hosts wipe
    # the local disk on every deploy (a live upload died that way on Render).
    # The file-store backend keeps the git-backed local directory below.
    import store
    if getattr(store.page_by_path, "__module__", "store") != "filestore":
        store.save_photo(digest, ext, data, by=by)
        return {"url": "/photo/{}.{}".format(digest, ext),
                "committed": True, "note": "stored in the database"}
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
