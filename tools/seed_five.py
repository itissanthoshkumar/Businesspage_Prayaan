"""Create 5 business pages THROUGH the admin panel and verify the whole loop.

Drives the exact HTTP surface the browser uses — session cookie, CSRF token per
form, the real /admin/pages/new and /status endpoints — so this is the admin
panel working, just scripted instead of clicked. Proves authoring works at scale:
create → draft-is-private → publish → live-and-rendering → listed → on sitemap.
"""
import html as _html
import http.cookiejar
import re
import urllib.error
import urllib.parse
import urllib.request

B = "http://localhost:8797"
jar = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))


def g(p):
    try:
        r = op.open(B + p); return r.getcode(), r.read().decode(), r.geturl()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), p


def post(p, data):
    r = op.open(urllib.request.Request(
        B + p, data=urllib.parse.urlencode(data).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"}))
    return r.getcode(), r.read().decode(), r.geturl()


def csrf(h):
    m = re.search(r'name="csrf" value="([^"]+)"', h)
    return m.group(1) if m else ""


# sign in (preview build: admin/prayaan)
post("/admin/login", {"username": "admin", "password": "prayaan"})

BUSINESSES = [
    {"business_name": "Meenakshi Silks", "state_code": "TN", "branch_slug": "madurai",
     "owner_name": "R. Meenakshi", "category": "Silk & Saree Retail",
     "locality": "Chinna Chokkikulam", "district": "Madurai", "state_name": "Tamil Nadu",
     "established_year": "2004", "summary": "Silk sarees and wedding wear in Madurai.",
     "offerings": "Kanchipuram silk\nWedding sarees\nReadymade blouses",
     "phones": "+91 90000 20001", "hours": "Mon-Sun, 10-9", "languages": "Tamil\nEnglish",
     "tier": "gold", "tier_status": "Active", "indexed": "on"},
    {"business_name": "Anand Auto Works", "state_code": "TN", "branch_slug": "trichy",
     "owner_name": "S. Anand", "category": "Automobile Repair",
     "locality": "Srirangam", "district": "Tiruchirappalli", "state_name": "Tamil Nadu",
     "established_year": "2012", "summary": "Two-wheeler and car service in Srirangam.",
     "offerings": "Engine service\nBrake work\nElectricals",
     "phones": "+91 90000 20002\n+91 431 200 2002", "hours": "Mon-Sat, 9-8",
     "languages": "Tamil", "tier": "silver", "tier_status": "Active", "indexed": "on"},
    {"business_name": "Gokulam Sweets", "state_code": "TN", "branch_slug": "salem",
     "owner_name": "K. Gokul", "category": "Sweets & Snacks",
     "locality": "Fairlands", "district": "Salem", "state_name": "Tamil Nadu",
     "established_year": "1998", "summary": "Sweets, savouries and fresh snacks in Salem.",
     "offerings": "Mysore pak\nFilter coffee\nDaily snacks\nGift boxes",
     "phones": "+91 90000 20003", "hours": "Mon-Sun, 7-10", "languages": "Tamil\nEnglish",
     "tier": "bronze", "tier_status": "Active", "indexed": "on"},
    {"business_name": "Priya Xerox & Stationery", "state_code": "TN", "branch_slug": "erode",
     "owner_name": "M. Priya", "category": "Printing & Stationery",
     "locality": "Perundurai Road", "district": "Erode", "state_name": "Tamil Nadu",
     "established_year": "2016", "summary": "Printing, xerox and school stationery in Erode.",
     "offerings": "Colour printing\nBinding\nSchool supplies",
     "phones": "+91 90000 20004", "hours": "Mon-Sat, 9-9", "languages": "Tamil",
     "indexed": "on"},   # no tier — tests the "no badge" path
    {"business_name": "Coastal Hardware", "state_code": "TN", "branch_slug": "thoothukudi",
     "owner_name": "J. Fernando", "category": "Hardware & Marine Supplies",
     "locality": "Bryant Nagar", "district": "Thoothukudi", "state_name": "Tamil Nadu",
     "established_year": "2009", "summary": "Hardware and marine fittings near the harbour.",
     "offerings": "Marine rope\nFasteners\nPaints\nTools",
     "phones": "+91 90000 20005\n+91 461 200 2005",
     "map_url": "https://maps.google.com/?q=Thoothukudi",
     "hours": "Mon-Sat, 8:30-8", "languages": "Tamil\nEnglish",
     "tier": "gold", "tier_status": "Active", "indexed": "on"},
]

fails = []
created = []
for biz in BUSINESSES:
    _, form_html, _ = g("/admin/pages/new")
    payload = dict(biz, csrf=csrf(form_html))
    code, _, url = post("/admin/pages/new", payload)
    m = re.search(r"/admin/pages/(\d+)", url)
    pid = m.group(1) if m else None
    if not pid:
        fails.append("create failed: " + biz["business_name"]); continue
    _, edit, _ = g("/admin/pages/%s" % pid)
    path = re.search(r'(/TN/[a-z]+/[a-z0-9-]+)"', edit)
    path = path.group(1) if path else None
    draft_public = g(path)[0] if path else 0
    # publish
    post("/admin/pages/%s/status" % pid, {"csrf": csrf(edit), "status": "live"})
    live_code, live_html, _ = g(path)
    ok_live = live_code == 200 and _html.escape(biz["business_name"]) in live_html
    ok_draft_hidden = draft_public == 404
    created.append({"name": biz["business_name"], "id": pid, "path": path,
                    "draft_hidden": ok_draft_hidden, "live": ok_live})
    if not ok_draft_hidden:
        fails.append("%s: draft was publicly visible (%s)" % (biz["business_name"], draft_public))
    if not ok_live:
        fails.append("%s: not live after publish (%s)" % (biz["business_name"], live_code))

# whole-list + sitemap checks
_, listing, _ = g("/admin/pages")
listed = sum(1 for c in created if _html.escape(c["name"]) in listing)
_, sitemap, _ = g("/sitemap.xml")
on_sitemap = sum(1 for c in created if c["path"] and c["path"] in sitemap)

print("\n=== created via admin panel ===")
for c in created:
    print("  %-26s id=%-3s %-34s draft-hidden=%s live=%s"
          % (c["name"], c["id"], c["path"], c["draft_hidden"], c["live"]))
print("\n  all 5 created         :", len(created) == 5)
print("  all drafts private    :", all(c["draft_hidden"] for c in created))
print("  all live after publish:", all(c["live"] for c in created))
print("  all in admin list     :", listed == len(created))
print("  all on public sitemap :", on_sitemap == len(created))
print("\n" + ("ALL PASS" if not fails and len(created) == 5 else "FAILURES: " + "; ".join(fails)))
raise SystemExit(1 if (fails or len(created) != 5) else 0)
