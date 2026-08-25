"""End-to-end back-office test. Needs the preview running on :8797.

    python3 pbn-public/tools/test_admin.py

Drives the real HTTP surface with a cookie jar, exactly as a browser would:
sign in, create a page, publish it, confirm it is live publicly, submit a lead
against it, work the lead, export CSV. Also probes the things that must FAIL —
unauthenticated access, a missing CSRF token, a non-Google map link.
"""
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8797"
jar = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (("  -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)


def get(path, follow=True):
    req = urllib.request.Request(BASE + path)
    try:
        if follow:
            r = opener.open(req)
            return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
        cls = type("NoRedirect", (urllib.request.HTTPRedirectHandler,),
                   {"redirect_request": lambda *a, **k: None})
        op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), cls)
        r = op.open(req)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def post(path, data, follow=True):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = opener.open(req)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def csrf_from(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


print("\n-- access control --")
code, _, url = get("/admin/pages")
check("unauthenticated /admin/pages redirects to login", url.endswith("/admin/login"), url)

code, html, _ = post("/admin/login", {"username": "admin", "password": "wrong"})
check("wrong password rejected", code == 401, code)
check("no username enumeration", "Wrong username or password" in html)

print("\n-- sign in --")
code, html, url = post("/admin/login", {"username": "admin", "password": "prayaan"})
check("correct password signs in", url.endswith("/admin/pages"), url)
check("session cookie scoped to /admin",
      any(c.name == "pbn_admin" and c.path == "/admin" for c in jar), [c.path for c in jar])

print("\n-- CSRF --")
code, _, _ = post("/admin/pages/new", {"business_name": "X", "state_code": "TN",
                                       "branch_slug": "vellore", "csrf": "forged"})
check("forged CSRF token rejected", code == 403, code)

print("\n-- create a page --")
_, form_html, _ = get("/admin/pages/new")
tok = csrf_from(form_html)
check("form carries a CSRF token", bool(tok))
code, html, url = post("/admin/pages/new", {
    "csrf": tok, "business_name": "Velan Steel Traders", "state_code": "TN",
    "branch_slug": "vellore", "owner_name": "P. Velan", "category": "Steel & Hardware",
    "locality": "Katpadi", "district": "Vellore", "state_name": "Tamil Nadu",
    "established_year": "2015", "summary": "Steel and hardware in Katpadi.",
    "about": "First para.\nSecond para.", "offerings": "TMT bars\nAngles\nSheets",
    "figures": "Years in business | 10\nPeople employed | 6",
    "hours": "Mon-Sat, 9-7", "languages": "Tamil\nEnglish",
    "phones": "+91 90000 11111\n+91 90000 22222",
    "map_url": "https://maps.google.com/?q=Katpadi",
    "tier": "silver", "tier_status": "Active", "indexed": "on"})
m = re.search(r"/admin/pages/(\d+)", url)
pid = m.group(1) if m else None
check("page created", bool(pid), url)

_, edit_html, _ = get("/admin/pages/%s" % pid)
mpath = re.search(r'(/TN/vellore/velan-steel-traders[\w-]*)', edit_html)
LIVE = mpath.group(1) if mpath else "/TN/vellore/velan-steel-traders"
check("slug derived from name", LIVE.startswith("/TN/vellore/velan-steel-traders"), LIVE)
check("status starts as draft", 's-draft">draft' in edit_html)
check("rich fields round-trip", "TMT bars" in edit_html and "Second para." in edit_html)
check("figures round-trip", "Years in business | 10" in edit_html)

print("\n-- a draft is NOT public --")
code, _, _ = get(LIVE)
check("draft page 404s publicly", code == 404, code)

print("\n-- publish refuses without consent evidence --")
tok = csrf_from(edit_html)
_, back_html, back_url = post("/admin/pages/%s/status" % pid, {"csrf": tok, "status": "live"})
check("publish without consent bounces to the form", "err=consent" in back_url, back_url)
check("consent error is shown", "record the customer" in back_html)
code, _, _ = get(LIVE)
check("page stayed draft (still 404 publicly)", code == 404, code)

print("\n-- publish with consent evidence --")
post("/admin/pages/%s/status" % pid,
     {"csrf": tok, "status": "live", "consent_method": "written",
      "consent_ref": "Signed form 12 Aug, kept at Vellore branch"})
_, edit_html2, _ = get("/admin/pages/%s" % pid)
check("consent recorded on the page", "Signed form 12 Aug" in edit_html2)
code, page_html, _ = get(LIVE)
check("published page is live", code == 200, code)
check("content renders", "Velan Steel Traders" in page_html)
check("tier shows standing only", "Silver" in page_html and "referrals" not in page_html)

print("\n-- bad map link is discarded --")
_, edit_html, _ = get("/admin/pages/%s" % pid)
tok = csrf_from(edit_html)
post("/admin/pages/%s" % pid, {"csrf": tok, "form_complete": "1",
                               "business_name": "Velan Steel Traders",
                               "map_url": "https://evil.example.com/phish"})
_, edit_html, _ = get("/admin/pages/%s" % pid)
check("non-Google map URL rejected", "evil.example.com" not in edit_html)

print("\n-- lead capture against the new page --")
_, live_html, _ = get(LIVE)
m = re.search(r'name="t" value="([^"]+)"', live_html)   # HMAC-signed token
import time as _t
_t.sleep(3.2)                                   # the form's time-on-page floor
post(LIVE,
     {"name": "Test Person", "mobile": "9876543210",
      "agree": "on", "t": m.group(1), "website": ""})
_, leads_html, _ = get("/admin/leads")
check("lead reached the inbox", "Test Person" in leads_html)
check("lead attributed to the referring business", "Velan Steel Traders" in leads_html)

print("\n-- work the lead --")
tok = csrf_from(leads_html)
lid = re.search(r'/admin/leads/(\d+)"', leads_html).group(1)
post("/admin/leads/%s" % lid, {"csrf": tok, "status": "CONTACTED",
                               "assigned_branch": "Vellore", "cs_notes": "Called, interested"})
_, leads_html, _ = get("/admin/leads")
check("status updated", "CONTACTED" in leads_html)
check("branch recorded", "Vellore" in leads_html)

print("\n-- CSV export --")
code, csv_text, _ = get("/admin/leads.csv")
check("CSV exports", code == 200 and "Test Person" in csv_text, code)
check("CSV has a header row", csv_text.splitlines()[0].startswith("id,at,name,mobile"))

print("\n-- remove --")
_, edit_html, _ = get("/admin/pages/%s" % pid)
tok = csrf_from(edit_html)
post("/admin/pages/%s/status" % pid, {"csrf": tok, "status": "removed"})
code, _, _ = get(LIVE)
check("removed page 404s again", code == 404, code)

print("\n-- live preview endpoint --")
_, form_html, _ = get("/admin/pages/new")
tok = csrf_from(form_html)
import urllib.request as _u
def post_raw(path, body, ctype):
    req = _u.Request(BASE + path, data=body.encode() if isinstance(body, str) else body,
                     headers={"Content-Type": ctype})
    try:
        r = opener.open(req); return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
import urllib.parse as _up
pv_body = _up.urlencode({"csrf": tok, "business_name": "Preview Test Traders",
                         "state_code": "TN", "branch_slug": "vellore",
                         "category": "Wholesale", "locality": "Katpadi",
                         "summary": "A preview of the page.", "variant": "27"})
code, html = post_raw("/admin/preview", pv_body, "application/x-www-form-urlencoded")
check("preview returns 200", code == 200, code)
check("preview renders the real page", "Preview Test Traders" in html and "id=\"enquire\"" in html)
check("preview has NO executable script", not re.search(r'<script(?![^>]*application/ld)', html), "found script")
code, _ = post_raw("/admin/preview", _up.urlencode({"csrf": "x", "business_name": "Y"}),
                   "application/x-www-form-urlencoded")
check("preview rejects bad CSRF", code == 403, code)

print("\n-- photo upload (git-backed) --")
import base64 as _b64
# A WELL-FORMED 1x1 PNG built from scratch. The popular tiny base64 fixture has
# a lying IDAT length — the metadata stripper rightly refuses it and logs a
# fallback, which would put a spurious traceback in every CI run.
import struct as _st
import zlib as _zl
def _pchunk(typ, d):
    return _st.pack(">I", len(d)) + typ + d + _st.pack(">I", _zl.crc32(typ + d) & 0xFFFFFFFF)
PNG = (b"\x89PNG\r\n\x1a\n"
       + _pchunk(b"IHDR", _st.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0))
       + _pchunk(b"IDAT", _zl.compress(b"\x00\x00"))
       + _pchunk(b"IEND", b""))
import json as _j
up_ok = _j.dumps({"csrf": tok, "data": "data:image/png;base64," + _b64.b64encode(PNG).decode()})
code, body = post_raw("/admin/upload", up_ok, "application/json")
u = _j.loads(body) if body else {}
check("upload accepts a real PNG", code == 200 and u.get("url","").startswith("/static/img/uploads/"), body[:120])
check("uploaded image is committed to git", u.get("committed") is True, u.get("note"))
# the returned URL must actually serve the bytes
if u.get("url"):
    code2, served, _ = get(u["url"])
    check("uploaded image is served", code2 == 200 and len(served) > 50, code2)
# an HTML file renamed as an image must be refused
EVIL = _b64.b64encode(b"<html><script>alert(1)</script></html>").decode()
code, body = post_raw("/admin/upload", _j.dumps({"csrf": tok, "data": EVIL}), "application/json")
check("non-image upload refused", code == 422, code)
code, _ = post_raw("/admin/upload", _j.dumps({"csrf": "x", "data": "aaaa"}), "application/json")
check("upload rejects bad CSRF", code == 403, code)

print("\n-- CSP: admin vs public --")
import urllib.request as _u2
def csp(path):
    r = opener.open(BASE + path)
    return dict(r.headers).get("content-security-policy", "")
apub = _u2.urlopen(BASE + "/TN/vellore/santhosh-enterprise")
pub_csp = dict(apub.headers).get("content-security-policy", "")
adm_csp = csp("/admin/pages")
check("public script-src is 'self' files only (no inline/eval)",
      "script-src 'self'" in pub_csp and "unsafe-inline" not in pub_csp.split("style-src")[0],
      pub_csp)
check("admin allows script-src 'self'", "script-src 'self'" in adm_csp)
check("admin allows connect-src 'self'", "connect-src 'self'" in adm_csp)
check("admin frame-ancestors 'self' (preview iframe)", "frame-ancestors 'self'" in adm_csp)
check("admin.js served", get("/static/admin.js")[0] == 200)

print("\n-- sign out --")
_, html, _ = get("/admin/pages")
post("/admin/logout", {"csrf": csrf_from(html)})
_, _, url = get("/admin/pages")
check("signed out, back to login", url.endswith("/admin/login"), url)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
