"""P0-fix verification: metadata stripping (unit), the /report intake (E2E),
consent-gated publish is covered in test_admin.py.

    python3 pbn-public/tools/test_p0.py     # needs the preview on :8797
"""
import base64
import http.cookiejar
import json
import re
import struct
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import uploads  # noqa: E402

BASE = "http://localhost:8797"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("  -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)


def get(path):
    try:
        r = opener.open(BASE + path)
        return r.getcode(), r.read(), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read(), path


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = opener.open(req)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


# ---- crafted images with metadata -------------------------------------------
def _jseg(marker, payload):
    return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload


JPG = (b"\xff\xd8"
       + _jseg(0xE0, b"JFIF\x00\x01\x02\x00\x00\x01\x00\x01\x00\x00")
       + _jseg(0xE1, b"Exif\x00\x00" + b"GPS-LAT-12.9716-LON-77.5946")
       + _jseg(0xFE, b"shot on my phone at home")
       + _jseg(0xDB, bytes(65))
       + bytes([0xFF, 0xDA]) + (8).to_bytes(2, "big") + bytes(6)
       + b"\x12\x34\x56\x78" + b"\xff\xd9")

def _pchunk(typ, data):
    return (struct.pack(">I", len(data)) + typ + data
            + struct.pack(">I", zlib.crc32(typ + data) & 0xFFFFFFFF))


# A fully well-formed 1x1 grayscale PNG built from scratch (the popular tiny
# base64 fixtures have a lying IDAT length, which the strict walk rightly
# refuses), with metadata chunks spliced between IHDR and IDAT.
PNG = (b"\x89PNG\r\n\x1a\n"
       + _pchunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
       + _pchunk(b"tEXt", b"Comment\x00owner lives at 12 Some Street")
       + _pchunk(b"eXIf", b"FAKE-EXIF-BLOCK")
       + _pchunk(b"IDAT", zlib.compress(b"\x00\x00"))
       + _pchunk(b"IEND", b""))


def _wchunk(four, data):
    pad = b"\x00" if len(data) % 2 else b""
    return four + len(data).to_bytes(4, "little") + data + pad


_wpayload = (b"WEBP" + _wchunk(b"VP8X", bytes([0x0C]) + bytes(9))
             + _wchunk(b"VP8L", b"\x2f\x00\x00\x00\x00fake-pixels")
             + _wchunk(b"EXIF", b"FAKE-GPS-COORDS"))
WEBP = b"RIFF" + len(_wpayload).to_bytes(4, "little") + _wpayload


print("\n-- metadata stripping (unit) --")
out = uploads.strip_metadata(JPG, "jpg")
check("jpeg: EXIF segment removed", b"Exif" not in out and b"GPS-LAT" not in out)
check("jpeg: comment removed", b"shot on my phone" not in out)
check("jpeg: pixel segments kept", b"\xff\xdb" in out and out.endswith(b"\x12\x34\x56\x78\xff\xd9"))
check("jpeg: still sniffs as jpeg", uploads._sniff_ext(out) == "jpg")

out = uploads.strip_metadata(PNG, "png")
check("png: tEXt removed", b"Some Street" not in out)
check("png: eXIf removed", b"eXIf" not in out)
check("png: render chunks kept", b"IHDR" in out and b"IDAT" in out and b"IEND" in out)
check("png: still sniffs as png", uploads._sniff_ext(out) == "png")

out = uploads.strip_metadata(WEBP, "webp")
check("webp: EXIF chunk removed", b"FAKE-GPS-COORDS" not in out)
check("webp: pixels kept", b"fake-pixels" in out)
vp8x_at = out.find(b"VP8X")
check("webp: VP8X EXIF/XMP flags cleared",
      vp8x_at > 0 and out[vp8x_at + 8] & 0x0C == 0)
check("webp: RIFF size rewritten",
      int.from_bytes(out[4:8], "little") == len(out) - 8)
check("webp: still sniffs as webp", uploads._sniff_ext(out) == "webp")

check("garbage input falls back to original",
      uploads.strip_metadata(b"\xff\xd8\xffbroken", "jpg") == b"\xff\xd8\xffbroken")


print("\n-- /report intake (E2E) --")
code, body, _ = get("/report?p=/TN/vellore/santhosh-enterprise")
html = body.decode("utf-8", "replace")
check("report page carries the form", code == 200 and 'action="/report"' in html, code)
check("prefilled page path echoed", "/TN/vellore/santhosh-enterprise" in html)

# The time-floor token is HMAC-signed now: scrape a real one and wait it out —
# a crafted past timestamp must NOT work (checked below).
tok_t = re.search(r'name="t" value="([^"]+)"', html).group(1)
time.sleep(3.2)

code, html, _ = post("/report", {
    "t": str(int(time.time()) - 30), "website": "", "page": "x",
    "rtype": "remove", "details": "forged timestamp should be silently dropped",
    "contact": ""})
check("forged (unsigned) timestamp treated as bot", "Received" in html)

code, html, _ = post("/report", {
    "t": tok_t, "website": "", "page": "/TN/vellore/santhosh-enterprise",
    "rtype": "remove", "details": "This is my shop and I want the page taken down.",
    "contact": "9876500000"})
check("valid report accepted", code == 200 and "Received" in html, code)

code, html, _ = post("/report", {
    "t": tok_t, "website": "", "page": "x", "rtype": "remove",
    "details": "short", "contact": ""})
check("too-short details bounced with error", "sentence or two" in html)

code, html, _ = post("/report", {
    "t": tok_t, "website": "http://spam.example", "page": "x",
    "rtype": "remove", "details": "a bot filled the honeypot field here",
    "contact": ""})
check("honeypot answered with fake success", "Received" in html)

print("\n-- report reaches the back-office --")
post("/admin/login", {"username": "admin", "password": "prayaan"})
code, body, _ = get("/admin/reports")
html = body.decode("utf-8", "replace")
check("report listed in /admin/reports", "want the page taken down" in html, code)
check("honeypot row NOT stored", "bot filled the honeypot" not in html)
check("reporter contact shown", "9876500000" in html)

tok = re.search(r'name="csrf" value="([^"]+)"', html).group(1)
rid = re.search(r'/admin/reports/(\d+)"', html).group(1)
post("/admin/reports/%s" % rid, {"csrf": tok, "status": "DONE",
                                 "note": "Page unpublished, caller informed"})
_, body, _ = get("/admin/reports?status=DONE")
check("report marked DONE with note",
      "Page unpublished" in body.decode("utf-8", "replace"))

print("\n-- upload strips EXIF end-to-end --")
_, body, _ = get("/admin/pages/new")
tok = re.search(r'name="csrf" value="([^"]+)"', body.decode()).group(1)
up = json.dumps({"csrf": tok,
                 "data": "data:image/jpeg;base64," + base64.b64encode(JPG).decode()})
req = urllib.request.Request(BASE + "/admin/upload", data=up.encode(),
                             headers={"Content-Type": "application/json"})
r = opener.open(req)
url = json.loads(r.read()).get("url", "")
check("EXIF jpeg upload accepted", r.getcode() == 200 and url, url)
code, served, _ = get(url)
check("served photo carries NO EXIF/GPS",
      code == 200 and b"Exif" not in served and b"GPS-LAT" not in served, code)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
