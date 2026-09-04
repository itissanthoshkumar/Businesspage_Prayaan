"""P1-fix verification: users/roles/revocation, share + attribution, consent
checkbox, photo lockdown, partial-update guard, robots posture, view counter.

    python3 pbn-public/tools/test_p1.py     # needs the preview on :8797
"""
import http.cookiejar
import re
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://localhost:8797"
fails = []


def jar_opener():
    jar = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("  -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)


def get(op, path):
    try:
        r = op.open(BASE + path)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def post(op, path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(BASE + path, data=body,
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = op.open(req)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def csrf_of(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


LIVE = "/TN/vellore/santhosh-enterprise"
STAFF = "asha.t{}".format(int(time.time()) % 1000000)   # unique per run
admin = jar_opener()

print("\n-- robots posture (preview build) --")
code, txt, _ = get(admin, "/robots.txt")
check("preview build disallows crawling", "Disallow: /" in txt, txt)

print("\n-- share affordance + attribution --")
code, html, _ = get(admin, LIVE)
check("WhatsApp share link on the page", "wa.me/?text=" in html)
check("share link carries via=wa", "via%3Dwa" in html)
code, html, _ = get(admin, LIVE + "?via=wa")
check("via survives into the form action", 'via=wa"' in html or "via=wa" in html)
tok_t = re.search(r'name="t" value="([^"]+)"', html).group(1)
time.sleep(3.2)

print("\n-- consent checkbox is enforced --")
code, html, _ = post(admin, LIVE + "?via=wa",
                     {"name": "Via Tester", "mobile": "9812345670",
                      "t": tok_t, "website": ""})
check("lead without contact consent refused", "agree to be contacted" in html)
code, html, _ = post(admin, LIVE + "?via=wa",
                     {"name": "Via Tester", "mobile": "9812345670",
                      "agree": "on", "t": tok_t, "website": ""})
check("lead with consent accepted", "Thank you" in html)

print("\n-- admin sign-in --")
post(admin, "/admin/login", {"username": "admin", "password": "prayaan"})
code, leads_html, _ = get(admin, "/admin/leads")
check("via chip shown on the lead", "WhatsApp" in leads_html and "Via Tester" in leads_html)

print("\n-- view counter --")
code, edit_html, _ = get(admin, "/admin/pages/1")
m = re.search(r"(\d+) views in 14 days", edit_html)
check("views counted and shown", m and int(m.group(1)) >= 2, edit_html.count("views"))

print("\n-- photo lockdown + partial-update guard --")
tok = csrf_of(edit_html)
code, _, _ = post(admin, "/admin/pages/1",
                  {"csrf": tok, "business_name": "Santhosh Enterprise"})
check("partial update (no sentinel) rejected 400", code == 400, code)
_, form_html, _ = get(admin, "/admin/pages/new")
code, html, url = post(admin, "/admin/pages/new", {
    "csrf": csrf_of(form_html), "business_name": "Hotlink Test Co",
    "state_code": "TN", "branch_slug": "vellore",
    "photo_url": "https://evil.example.com/watch.jpg"})
pid = re.search(r"/admin/pages/(\d+)", url).group(1)
_, edit2, _ = get(admin, "/admin/pages/%s" % pid)
check("external photo_url dropped", "evil.example.com" not in edit2)

# A /photo/ reference is database-backed and survives a deploy, so it must be
# stored on EVERY backend. This is the other half of the missing-photo guard:
# the rule that drops un-keepable disk paths must not also throw away the
# storage that replaced them.
_, form_html, _ = get(admin, "/admin/pages/new")
code, _, url = post(admin, "/admin/pages/new", {
    "csrf": csrf_of(form_html), "business_name": "Db Photo Co",
    "state_code": "TN", "branch_slug": "vellore",
    "photo_url": "/photo/abc123def456abcd.jpg"})
pid2 = re.search(r"/admin/pages/(\d+)", url).group(1)
_, edit3, _ = get(admin, "/admin/pages/%s" % pid2)
check("database photo_url kept", "/photo/abc123def456abcd.jpg" in edit3)

print("\n-- user management --")
code, users_html, _ = get(admin, "/admin/users")
check("admin reaches /admin/users", code == 200, code)
code, users_html, _ = post(admin, "/admin/users/new",
                           {"csrf": csrf_of(users_html),
                            "username": STAFF, "role": "staff"})
mpw = re.search(r"Temporary password for {}:</b> <code>([^<]+)</code>".format(re.escape(STAFF)),
                users_html)
check("staff user created with one-time temp password", bool(mpw))
temp_pw = mpw.group(1) if mpw else ""

print("\n-- first sign-in forces a password change --")
staff = jar_opener()
code, html, url = post(staff, "/admin/login",
                       {"username": STAFF, "password": temp_pw})
check("temp sign-in lands on the password screen", url.endswith("/admin/password"), url)
code, html, url = get(staff, "/admin/pages")
check("everything else redirects until changed", url.endswith("/admin/password"), url)
code, html, url = post(staff, "/admin/password",
                       {"csrf": csrf_of(html), "current": temp_pw,
                        "new": "asha-strong-9", "confirm": "asha-strong-9"})
check("password change signs back in", url.endswith("/admin/pages"), url)
code, html, _ = post(staff, "/admin/password",
                     {"csrf": csrf_of(html), "current": "wrong",
                      "new": "whatever-123", "confirm": "whatever-123"})
check("wrong current password refused", "Current password is wrong" in html)

print("\n-- role gates --")
code, _, _ = get(staff, "/admin/users")
check("staff cannot open Users", code == 403, code)
code, _, _ = get(staff, "/admin/leads.csv")
check("staff cannot export the lead book", code == 403, code)
code, _, _ = get(staff, "/admin/leads")
check("staff can still work leads", code == 200, code)

print("\n-- suspension revokes live sessions immediately --")
_, users_html, _ = get(admin, "/admin/users")
uid = re.search(r'/admin/users/(\d+)"', users_html.split(STAFF, 1)[1]).group(1)
post(admin, "/admin/users/%s" % uid,
     {"csrf": csrf_of(users_html), "action": "suspend"})
code, _, url = get(staff, "/admin/pages")
check("suspended user's session is dead NOW", url.endswith("/admin/login"), url)

print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
