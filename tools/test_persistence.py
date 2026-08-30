"""Restart test for filestore.py — does an admin's work actually survive a reboot?

    python3 pbn-public/tools/test_persistence.py [port]

Boots its OWN server on port 8799 against a fresh scratch PBN_DATA_DIR, so it
never disturbs the preview on 8797 or its /tmp/pbn-data directory. Then:

    boot 1   sign in, create a page, publish it, capture a lead, remove a demo page
    kill     SIGTERM, wait for the port to go quiet
    boot 2   the created page must still be live, the lead must still be in the
             inbox, the removed page must still be gone (a seeder that ran every
             boot would resurrect it), and ids must continue rather than restart

Deletes nothing: each run leaves its scratch directory behind, named in the
output, so a failed run can be inspected.
"""
import http.cookiejar
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SVC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8799
BASE = "http://127.0.0.1:{}".format(PORT)
DATA_DIR = "/tmp/pbn-persist-test-{}".format(int(time.time()))

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
        r = opener.open(urllib.request.Request(BASE + path))
        return r.getcode(), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def post(path, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        BASE + path, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"})
    try:
        r = opener.open(req)
        return r.getcode(), r.read().decode("utf-8", "replace"), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace"), path


def csrf_from(html):
    m = re.search(r'name="csrf" value="([^"]+)"', html)
    return m.group(1) if m else ""


def boot(label):
    env = dict(os.environ, PBN_PORT=str(PORT), PBN_DATA_DIR=DATA_DIR)
    proc = subprocess.Popen([sys.executable, "-u", os.path.join(SVC, "run_preview.py")],
                            cwd=SVC, env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(60):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
            print("  ({} up on {})".format(label, PORT))
            return proc
        except Exception:                              # noqa: BLE001
            if proc.poll() is not None:
                print(proc.stdout.read().decode("utf-8", "replace"))
                raise SystemExit("{} died on start".format(label))
            time.sleep(0.5)
    proc.kill()
    raise SystemExit("{} never became healthy".format(label))


def halt(proc):
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
    for _ in range(30):
        try:
            urllib.request.urlopen(BASE + "/healthz", timeout=1).read()
            time.sleep(0.5)
        except Exception:                              # noqa: BLE001
            return
    raise SystemExit("port {} never went quiet".format(PORT))


print("scratch data dir: " + DATA_DIR)

print("\n-- boot 1 --")
server = boot("boot 1")

_, html, _ = post("/admin/login", {"username": "admin", "password": "prayaan"})
_, form = get("/admin/pages/new")
tok = csrf_from(form)
check("seeded admin user signs in", bool(tok))

code, _, url = post("/admin/pages/new", {
    "csrf": tok, "business_name": "Persisted Provisions", "state_code": "TN",
    "branch_slug": "ranipet", "owner_name": "V. Anand", "category": "Provisions",
    "locality": "Walajapet", "district": "Ranipet", "state_name": "Tamil Nadu",
    "summary": "Provisions wholesaler in Walajapet.",
    "offerings": "Rice\nPulses", "figures": "Years in business | 9",
    "phones": "+91 90000 44444", "indexed": "on"})
pid = (re.search(r"/admin/pages/(\d+)", url) or [None, None])[1]
check("page created", bool(pid), url)
check("id continues past the four fixtures", pid and int(pid) == 5, pid)

# A FIRST publish requires consent evidence — admin.py's page_status sends a
# page that cannot say how the customer agreed back to the form (?err=consent)
# instead of taking it live. Posting the status alone leaves it a silent draft,
# and every downstream check here then fails against a 404.
post("/admin/pages/%s/status" % pid,
     {"csrf": tok, "status": "live", "consent_method": "written",
      "consent_ref": "Signed form 3 Sep, kept at Ranipet branch"})
code, live_html = get("/TN/ranipet/persisted-provisions")
check("page is live before the restart", code == 200, code)

# The enquiry form's anti-bot token is HMAC-signed at render time, so it must be
# scraped from the page just fetched: a hand-built t reads as zero seconds on
# page, and a lead that trips the bot floor is answered with a FAKE success and
# never stored — which looks like a passing POST and a missing lead. The wait
# clears MIN_FILL_SECONDS.
m = re.search(r'name="t" value="([^"]+)"', live_html)
check("enquiry form carries a signed timestamp", bool(m))
time.sleep(3.2)
# One affirmative tick (v2 consent) — the old age_ok field no longer exists, and
# pincode is off the form.
_, thanks, _ = post("/TN/ranipet/persisted-provisions",
                    {"name": "Persisted Person", "mobile": "9876500022",
                     "agree": "on", "t": m.group(1) if m else "", "website": ""})
check("lead accepted before the restart", "Thank you" in thanks)
_, leads = get("/admin/leads")
check("lead captured before the restart", "Persisted Person" in leads)

post("/admin/pages/2/status", {"csrf": tok, "status": "removed",
                               "note": "takedown, persistence test"})
code, _ = get("/TN/vellore/amman-garments")
check("demo page removed before the restart", code == 404, code)

print("\n-- restart --")
halt(server)
code = None
try:
    urllib.request.urlopen(BASE + "/healthz", timeout=2).read()
    code = 200
except Exception:                                      # noqa: BLE001
    pass
check("server is really down", code is None)

server = boot("boot 2")

print("\n-- boot 2: everything must still be there --")
code, page_html = get("/TN/ranipet/persisted-provisions")
check("created page survived the restart", code == 200, code)
check("its content survived", "Persisted Provisions" in page_html)

code, _ = get("/TN/vellore/amman-garments")
check("removed page was NOT resurrected by re-seeding", code == 404, code)

_, html, _ = post("/admin/login", {"username": "admin", "password": "prayaan"})
_, leads = get("/admin/leads")
check("lead survived the restart", "Persisted Person" in leads)
check("lead kept its referral attribution", "Persisted Provisions" in leads)

_, edit = get("/admin/pages/%s" % pid)
check("audit trail survived", "PUBLISHED" in edit and "CREATED" in edit)

_, form = get("/admin/pages/new")
tok = csrf_from(form)
_, _, url = post("/admin/pages/new", {"csrf": tok, "business_name": "After Restart Co",
                                      "state_code": "TN", "branch_slug": "ranipet"})
nid = (re.search(r"/admin/pages/(\d+)", url) or [None, None])[1]
check("ids continue after a restart instead of colliding",
      nid and int(nid) == int(pid) + 1, "{} -> {}".format(pid, nid))

for name in ("pages.json", "leads.json", "events.json", "users.json", "counters.json"):
    check("on disk: " + name, os.path.exists(os.path.join(DATA_DIR, name)))
check("no temp files left behind",
      not [f for f in os.listdir(DATA_DIR) if f.endswith(".tmp")],
      os.listdir(DATA_DIR))

halt(server)
print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
print("scratch data dir kept at: " + DATA_DIR)
raise SystemExit(1 if fails else 0)
