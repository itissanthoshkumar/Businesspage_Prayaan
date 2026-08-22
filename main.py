"""Prayaan Business Pages — public customer micro-sites.

Serves one page per customer at /<STATE>/<branch>/<business-slug>, with a Prayaan
loan-enquiry form beside the customer's own content. Separate process, separate
subdomain, no session middleware and no auth: the only write path is the lead
form, and it holds insert-only credentials.

Server-rendered rather than statically generated on purpose. Pages publish with a
takedown-on-request consent model, so takedown speed is a compliance control —
flipping status in Mongo removes a page on the very next request, where a static
build would need a regenerate-and-redeploy cycle.

Run: uvicorn main:app --port 8797
"""
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

import admin
import store

BASE_DIR = Path(__file__).resolve().parent
BASE_URL = os.getenv("PBN_BASE_URL", "https://business.prayaancapital.com").rstrip("/")
CONSENT_VERSION = os.getenv("PBN_CONSENT_VERSION", "v1-2026-08")

# Anti-abuse. CGNAT means many genuine users share one IP on Indian mobile
# networks, so the per-IP cap is deliberately generous — a tight one would
# silently drop real leads, which is worse than admitting some spam.
LEAD_CAP_PER_IP_DAY = int(os.getenv("PBN_LEAD_CAP_IP", "40"))
LEAD_CAP_GLOBAL_DAY = int(os.getenv("PBN_LEAD_CAP_GLOBAL", "2000"))
MIN_FILL_SECONDS = int(os.getenv("PBN_MIN_FILL_SECONDS", "3"))
MAX_FIELD_LEN = 200
MAX_BODY_BYTES = int(os.getenv("PBN_MAX_BODY_BYTES", "4096"))

app = FastAPI(title="Prayaan Business Pages", docs_url=None, redoc_url=None,
              openapi_url=None)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
admin.init(templates)
app.include_router(admin.router)
# Jinja2Templates autoescapes .html by default; every value below is
# customer-supplied, so this must stay on.


# Page designs. All render the same content and the same compliance-reviewed
# enquiry form; only the layout differs. Selected with ?v=N so the whole set can
# be compared on one real page before we commit to a house style.
# IDs are deliberately NOT renumbered when a design is dropped — a shared /preview
# link or a bookmarked ?v=N would otherwise silently land on a different design.
#
# FINAL: Reflex is the house design. Every other exploration was dropped on
# 2026-08-14 — Aperture, Graphite, Sun-faded, Park Green, Candy, and before them
# Storefront, Clay 3D and Sage.
#
# The variant machinery is kept even though only one design remains: the page
# still resolves ?v= against this list, so the dozens of ?v=N links already shared
# from the gallery keep working (they fall back here rather than 404ing), and
# adding a design later is still one file. Structure lives in
# static/aperture.css + templates/_aperture.html; v27.html only sets palette
# tokens, typefaces and a hero-layout modifier.
VARIANTS = [
    # Reflex, in its two kept versions — same palette, type and everything below
    # the fold; only the hero composition differs. 28-30 and 32-35 were hero
    # explorations that were rejected and removed; their ids are never reused.
    (27, "Reflex"),            # wide panel photo, name plate tucked under its corner
    (31, "Reflex Cover"),      # cover photo + overlapping avatar — the profile anatomy
    # Skeleton is a SPECIFICATION, not a customer design: every dynamic value is
    # rendered as a literal <field_name> placeholder, so the page doubles as the
    # authoritative list of what an admin must supply. Review tool; never a
    # customer-facing default.
    (40, "Skeleton · Cover"),   # spec for the ?v=31 cover hero
    (41, "Skeleton · Reflex"),  # spec for the ?v=27 wide-panel hero
]
DEFAULT_VARIANT = int(os.getenv("PBN_DEFAULT_VARIANT", "27"))
# The designs a CUSTOMER may be shown. Others (e.g. the Skeleton spec) exist only
# as review tools; ?v= must fall back to a house design for them in production,
# the same way /preview and the gallery are gated on SHOW_SWITCHER.
PUBLIC_VARIANTS = {27, 31}
VARIANTS_DIR = BASE_DIR / "templates" / "variants"


def available_variants():
    """Only the designs whose template actually exists.

    The registry above is the intended set; a template may not be on disk yet
    (one still being authored) or may have been pulled. Checking per request
    means the switcher never offers a design that would 500, and a newly added
    file appears without a restart."""
    return [(i, n) for i, n in VARIANTS if (VARIANTS_DIR / "v{}.html".format(i)).exists()]
# The switcher is a review tool, not a customer-facing control — off in production.
SHOW_SWITCHER = os.getenv("PBN_SHOW_SWITCHER", "true").lower() in ("1", "true", "yes")
# The public business directory (/browse). A ROSTER of live customers, so it is
# deliberately gated: defaults to SHOW_SWITCHER, meaning it shows while testing
# and disappears in production unless explicitly turned on. Flip PBN_PUBLIC_DIR
# to keep it during a soft-launch, or to "false" to hide it immediately.
PUBLIC_DIRECTORY = os.getenv("PBN_PUBLIC_DIR",
                             "true" if SHOW_SWITCHER else "false").lower() in ("1", "true", "yes")


# Content-Security-Policy. The page runs NO JavaScript at all, which lets this be
# unusually strict — script-src 'none' is an honest description, not an
# aspiration. (The schema.org block is type="application/ld+json": a data block
# the browser parses but never executes, so 'none' does not suppress it.)
#
# frame-ancestors is 'self', not 'none': the review gallery and the device-width
# toggle embed pages in same-origin iframes. 'self' still blocks the attack that
# matters — a lookalike domain framing a real, RBI-registered-NBFC-branded page
# and wrapping it in a fake "pay a processing fee" flow. The footer already warns
# customers about advance-fee fraud; this makes the page enforce it.
#
# form-action 'self' means the lead form can only ever post back here, so injected
# markup cannot redirect a customer's name and number to someone else's endpoint.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'none'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    # https: because photo_url may point at an external image host
    "img-src 'self' data: https:",
    "form-action 'self'",
    "frame-ancestors 'self'",
    "base-uri 'none'",
    "object-src 'none'",
])

# The review surfaces — the gallery and the device-width wrapper — carry inline
# <style>/<script> for their own toggles, which the customer policy rightly
# forbids. They are internal tools that 404 in production (PBN_SHOW_SWITCHER
# off), so they get their own relaxed policy rather than weakening the one that
# ships. The framed page INSIDE the wrapper is served on its own request and
# keeps the strict policy, so review still exercises what customers get.
CSP_REVIEW = CSP.replace("script-src 'none'", "script-src 'self' 'unsafe-inline'") \
                .replace("style-src 'self'", "style-src 'self' 'unsafe-inline'")

# The back-office is the ONE place this service runs JavaScript, so it gets its
# own policy rather than the public one: script-src 'self' for admin.js,
# connect-src 'self' for the live-preview and upload fetches, frame-src 'self'
# for the preview iframe. frame-ancestors stays 'self' (not 'none') precisely so
# that same-origin preview iframe can render — an outside origin still cannot
# frame an admin page. No 'unsafe-inline' anywhere: the admin has no inline
# script or style, so the policy stays tight.
CSP_ADMIN = "; ".join([
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self' https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https:",
    "connect-src 'self'",
    "form-action 'self'",
    "frame-src 'self'",
    "frame-ancestors 'self'",
    "base-uri 'none'",
    "object-src 'none'",
])

SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    # legacy fallback for browsers predating frame-ancestors
    "X-Frame-Options": "SAMEORIGIN",
    # never leak a customer's page path to a third-party host
    "Referrer-Policy": "strict-origin-when-cross-origin",
    # the page asks for none of these; say so
    "Permissions-Policy": "geolocation=(), camera=(), microphone=(), payment=(), usb=()",
}


@app.middleware("http")
async def _security_and_cache_headers(request: Request, call_next):
    """Security headers on every response, plus the review-mode cache override.

    Cache: the browser once held an old /static/site.css while the templates were
    current, so half the cascade was one version and half another — a design
    reviewed in that state is not the design that ships, and a contrast audit run
    against it reports failures that do not exist. Review only; in production
    these files should cache hard."""
    response = await call_next(request)
    for header, value in SECURITY_HEADERS.items():
        response.headers[header] = value

    # Review-only surfaces: the gallery, and the device-width wrapper (?w= without
    # frame=1, which renders _device.html around an iframe).
    # The back-office is same-origin, form-driven and JS-free, so it keeps the
    # strict policy — but it must never be framable by anything, including us.
    if request.url.path.startswith("/admin"):
        response.headers["Content-Security-Policy"] = CSP_ADMIN
        # SAMEORIGIN, not DENY: the editor embeds a same-origin live-preview
        # iframe, which DENY would block. An outside origin still cannot frame
        # an admin page.
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Cache-Control"] = "no-store, private"

    if SHOW_SWITCHER and not request.url.path.startswith("/admin"):
        is_gallery = request.url.path == "/preview"
        is_device_wrapper = ("w" in request.query_params
                             and request.query_params.get("frame") != "1")
        if is_gallery or is_device_wrapper:
            response.headers["Content-Security-Policy"] = CSP_REVIEW
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store, must-revalidate"
    return response


# Device widths offered by the header toggle. 0 = the real browser window.
DEVICES = [(0, "Full"), (375, "Phone"), (768, "Tablet"), (1100, "Desktop")]


def _device_of(request: Request) -> int:
    try:
        w = int(request.query_params.get("w", 0))
    except (TypeError, ValueError):
        return 0
    return w if w in [d for d, _ in DEVICES] else 0


def _variant_of(request: Request) -> int:
    ids = [i for i, _ in available_variants()] or [DEFAULT_VARIANT]
    # In production only house designs are selectable; review builds may preview
    # any registered variant.
    if not SHOW_SWITCHER:
        ids = [i for i in ids if i in PUBLIC_VARIANTS] or [DEFAULT_VARIANT]
    try:
        v = int(request.query_params.get("v", DEFAULT_VARIANT))
    except (TypeError, ValueError):
        v = DEFAULT_VARIANT
    if v in ids:
        return v
    return DEFAULT_VARIANT if DEFAULT_VARIANT in ids else ids[0]


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return (fwd.split(",")[0].strip() if fwd else "") or (request.client.host if request.client else "")


def _local_business_jsonld(page: dict, canonical: str, abs_photo: str) -> dict:
    """schema.org/LocalBusiness for the page.

    The page already carries everything a search engine wants — name, category,
    locality, phone numbers, year established — but only as prose, so none of it
    was machine-readable. This is what turns a plain blue link into a result
    showing the phone number and the town.

    Emitted ONLY for indexed pages. A page carrying noindex is one the customer
    asked to be hidden or one still in draft, and handing a crawler a tidy
    structured record of it would undo exactly that.

    Deliberately absent:
      - openingHours. Our hours are free text ("Mon-Sat, 8:30 am - 8:30 pm") and
        the schema wants a strict format; emitting an unparseable value is worse
        than emitting none.
      - Any mention of Prayaan, lending, or a relationship between the two. The
        page never states the customer is a borrower, and the structured data
        must not leak what the prose withholds.
    """
    place = [page.get("locality"), page.get("district")]
    data = {
        "@context": "https://schema.org",
        "@type": "LocalBusiness",
        "name": page.get("business_name"),
        "url": canonical,
    }
    if page.get("summary"):
        data["description"] = page["summary"]
    if abs_photo:
        data["image"] = abs_photo
    if page.get("phones"):
        # itemised rather than joined — "telephone" is a single value
        data["telephone"] = page["phones"][0]
    address = {"@type": "PostalAddress", "addressCountry": "IN"}
    if page.get("locality"):
        address["addressLocality"] = page["locality"]
    if page.get("state_name"):
        address["addressRegion"] = page["state_name"]
    data["address"] = address
    if page.get("established_year"):
        data["foundingDate"] = str(page["established_year"])
    if page.get("category"):
        # not @type — mapping a free-text category onto schema's subtypes would
        # be guesswork, and a wrong @type is worse than a right keyword
        data["knowsAbout"] = page["category"]
    if page.get("languages"):
        data["knowsLanguage"] = page["languages"]
    if page.get("owner_name"):
        data["founder"] = {"@type": "Person", "name": page["owner_name"]}
    if page.get("map_url"):
        data["hasMap"] = page["map_url"]
    if any(place):
        data["areaServed"] = ", ".join([p for p in place if p])
    return data


def _render_page(request: Request, page: dict, status_code: int = 200, error: str = None,
                 submitted: bool = False, form: dict = None):
    variant = _variant_of(request)
    device = _device_of(request)
    framed = request.query_params.get("frame") == "1"

    # A width toggle has to reflow the layout, not just narrow a column: CSS media
    # queries key off the viewport, so a 375px-wide div on a desktop window would
    # still get the desktop layout and mislead. Rendering the page inside an iframe
    # of that width gives it a real viewport, so the breakpoints actually fire.
    if device and not framed:
        return templates.TemplateResponse("_device.html", {
            "request": request, "page": page, "device": device, "devices": DEVICES,
            "variant": variant,
            "variants": [{"id": i, "name": n} for i, n in available_variants()],
            "show_switcher": SHOW_SWITCHER and request.query_params.get("chrome") != "0",
            "src": "{}?v={}&w={}&frame=1".format(page["path"], variant, device),
        }, status_code=status_code)

    photo = page.get("photo_url") or ""
    canonical = BASE_URL + page["path"]
    abs_photo = photo if photo.startswith("http") else (BASE_URL + photo if photo else "")
    indexed = bool(page.get("indexed", True))
    return templates.TemplateResponse(
        "variants/v{}.html".format(variant),
        {"request": request, "page": page, "base_url": BASE_URL,
         "jsonld": _local_business_jsonld(page, canonical, abs_photo) if indexed else None,
         "device": device, "devices": DEVICES, "framed": framed,
         # Keep the frame flag on submit so a form post inside the device view
         # comes back inside the frame rather than escaping to a full page.
         "form_action": "{}?v={}{}".format(
             page["path"], variant, "&w={}&frame=1".format(device) if framed else ""),
         "canonical": canonical, "error": error, "submitted": submitted,
         "form": form or {}, "now": int(time.time()),
         "indexed": indexed,
         "variant": variant, "variants": [{"id": i, "name": n} for i, n in available_variants()],
         # chrome=0 strips the review switcher — the gallery and the device frame
         # embed pages and must show exactly what a customer would see.
         "show_switcher": (SHOW_SWITCHER and not framed
                           and request.query_params.get("chrome") != "0"),
         # og:image must be absolute — relative paths are ignored by crawlers.
         "abs_photo": abs_photo},
        status_code=status_code,
    )


def render_variant_html(page: dict, variant: int = None) -> str:
    """Render a customer page to an HTML string WITHOUT a Request.

    The admin live-preview posts draft field values here and shows the result in
    an iframe, so what an editor sees is the very same variant template the
    public page uses — not a mock that could drift from production. Rendered
    through the Jinja environment directly (the variant templates reference no
    `request`), so no Request object has to be faked.

    Falls back to DEFAULT_VARIANT for an unknown id, exactly like a live URL."""
    ids = [i for i, _ in available_variants()] or [DEFAULT_VARIANT]
    v = variant if variant in ids else (DEFAULT_VARIANT if DEFAULT_VARIANT in ids else ids[0])
    photo = page.get("photo_url") or ""
    path = page.get("path") or "/preview"
    canonical = BASE_URL + path
    abs_photo = photo if photo.startswith("http") else (BASE_URL + photo if photo else "")
    indexed = bool(page.get("indexed", True))
    ctx = {
        "page": page, "base_url": BASE_URL,
        "jsonld": _local_business_jsonld(page, canonical, abs_photo) if indexed else None,
        "device": 0, "devices": DEVICES, "framed": True,
        "form_action": path, "canonical": canonical, "error": None,
        "submitted": False, "form": {}, "now": int(time.time()),
        "indexed": indexed, "variant": v,
        "variants": [{"id": i, "name": n} for i, n in available_variants()],
        # never show the review switcher inside the editor's own preview
        "show_switcher": False, "abs_photo": abs_photo,
    }
    return templates.env.get_template("variants/v{}.html".format(v)).render(ctx)


def _not_found(request: Request):
    """One response for removed, draft and never-existed alike.

    Deliberately identical: a distinct 'removed' page would confirm that a named
    business is a Prayaan customer, which is exactly what a takedown was asked
    to undo."""
    return templates.TemplateResponse(
        "notfound.html", {"request": request, "base_url": BASE_URL}, status_code=404)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/preview", response_class=HTMLResponse)
def preview(request: Request):
    """Internal review gallery — every design against every live page, in one
    place, at any device width. Not linked from a customer page; disabled wherever
    the switcher is (PBN_SHOW_SWITCHER=false), so it never ships to production."""
    if not SHOW_SWITCHER:
        return _not_found(request)
    return templates.TemplateResponse("preview.html", {
        "request": request,
        "variants": [{"id": i, "name": n} for i, n in available_variants()],
        "pages": store.live_pages(limit=200),
        "default_variant": DEFAULT_VARIANT,
    })


# ---- standing pages -------------------------------------------------------
# Every customer page links to these four. All of them 404'd until now —
# including /report, the takedown route, which is the basis on which pages are
# published without prior consent at all. noindex: they are utility pages, not
# search results.
def _doc(request: Request, template: str, **extra):
    ctx = {"request": request, "base_url": BASE_URL}
    ctx.update(extra)
    return templates.TemplateResponse(template, ctx)


@app.api_route("/privacy", methods=["GET", "HEAD"], response_class=HTMLResponse)
def privacy(request: Request):
    return _doc(request, "privacy.html")


@app.api_route("/grievance", methods=["GET", "HEAD"], response_class=HTMLResponse)
def grievance(request: Request):
    return _doc(request, "grievance.html")


@app.api_route("/referral-terms", methods=["GET", "HEAD"], response_class=HTMLResponse)
def referral_terms(request: Request):
    return _doc(request, "referral_terms.html")


@app.api_route("/report", methods=["GET", "HEAD"], response_class=HTMLResponse)
def report(request: Request):
    """Takedown route. The ?p= path is echoed back so the person reporting does
    not have to retype it — but it is NOT trusted: it is rendered as text through
    autoescaping and never used to look anything up."""
    raw = (request.query_params.get("p") or "")[:200]
    path = raw if raw.startswith("/") else ""
    return _doc(request, "report.html", path=path)


@app.get("/favicon.ico")
def favicon():
    """Browsers request this on every page; without it each view logs a 404."""
    from fastapi.responses import Response
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
           '<rect width="32" height="32" rx="6" fill="#1c2743"/>'
           '<path d="M11 23V9h6a4.5 4.5 0 010 9h-6" fill="none" stroke="#e3b44f" '
           'stroke-width="2.6" stroke-linecap="round" stroke-linejoin="round"/></svg>')
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=86400"})


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return "User-agent: *\nAllow: /\nSitemap: {}/sitemap.xml\n".format(BASE_URL)


@app.get("/sitemap.xml")
def sitemap():
    from xml.sax.saxutils import escape
    rows = [p for p in store.live_pages() if p.get("indexed", True)]
    out = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    for r in rows:
        lastmod = str(r.get("updated_at") or "")[:10]
        out.append("<url><loc>{}{}</loc>{}</url>".format(
            escape(BASE_URL), escape(r["path"]),
            "<lastmod>{}</lastmod>".format(escape(lastmod)) if lastmod else ""))
    out.append("</urlset>")
    return PlainTextResponse("\n".join(out), media_type="application/xml")


@app.api_route("/browse", methods=["GET", "HEAD"], response_class=HTMLResponse)
def browse(request: Request):
    """Directory of live business pages.

    A roster of customers, so it is gated on PUBLIC_DIRECTORY — on while testing,
    off in production by default. When off it 404s exactly like any other unknown
    path, so turning it off leaves no trace that it ever existed. The page itself
    is noindex: even while public-for-testing, search engines must not cache the
    whole customer list as one document (the individual pages stay indexable)."""
    if not PUBLIC_DIRECTORY:
        return _not_found(request)
    if request.method == "HEAD":
        from fastapi.responses import Response
        return Response(status_code=200, media_type="text/html; charset=utf-8")
    pages = [p for p in store.live_pages(limit=2000)]
    return templates.TemplateResponse("browse.html", {
        "request": request, "base_url": BASE_URL, "pages": pages,
        "count": len(pages)})


@app.api_route("/{state}/{branch}/{slug}", methods=["GET", "HEAD"],
               response_class=HTMLResponse)
def customer_page(request: Request, state: str, branch: str, slug: str):
    """HEAD is answered as well as GET.

    Link checkers, several crawlers and some link-preview fetchers probe with
    HEAD before spending a full request, and FastAPI does not add it to a GET
    route the way plain Starlette does — so every one of them was getting a bare
    405. The lookup still runs in full, so the status a HEAD returns is the real
    one (200 / 301 / 404); only the body is dropped, which is what HEAD means."""
    path = "/{}/{}/{}".format(state, branch, slug)
    page, canonical = store.page_by_path(path)
    if not page or page.get("status") != store.PAGE_LIVE:
        response = _not_found(request)
    elif canonical:
        # Matched an alias (the page was rehomed). 301 to the original URL so the
        # link the customer already shared stays the one search engines keep.
        response = RedirectResponse(url=canonical, status_code=301)
    else:
        response = _render_page(request, page)

    if request.method == "HEAD":
        from fastapi.responses import Response
        return Response(status_code=response.status_code,
                        headers={k: v for k, v in response.headers.items()
                                 if k.lower() != "content-length"},
                        media_type="text/html; charset=utf-8")
    return response


@app.post("/{state}/{branch}/{slug}", response_class=HTMLResponse)
async def submit_lead(request: Request, state: str, branch: str, slug: str):
    """The only write path in this service.

    Posting to the page's own URL means the referral attribution comes from the
    request path — a form field could be forged to credit any customer.

    The urlencoded body is parsed directly rather than via FastAPI's Form(...)
    or request.form(): both pull in python-multipart, and this form never carries
    a file. One less dependency on the only internet-facing process, and it lets
    us reject an oversized body before doing any work."""
    from urllib.parse import parse_qs

    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        # Belt and braces — the reverse proxy should cap this first.
        raise HTTPException(413, "Request too large")
    fields = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)

    def _f(key):
        return (fields.get(key) or [""])[0][:MAX_FIELD_LEN]

    name = _f("name")
    mobile = _f("mobile")
    pincode = _f("pincode")
    age_ok = _f("age_ok")
    website = _f("website")
    t = _f("t")

    path = "/{}/{}/{}".format(state, branch, slug)
    page, canonical = store.page_by_path(path)
    if not page or page.get("status") != store.PAGE_LIVE:
        return _not_found(request)
    if canonical:
        return RedirectResponse(url=canonical, status_code=301)

    form = {"name": name[:MAX_FIELD_LEN], "mobile": mobile[:MAX_FIELD_LEN],
            "pincode": pincode[:MAX_FIELD_LEN]}

    # Honeypot: a real browser never fills a hidden field. Answer with the normal
    # success view so a bot cannot tell it was caught.
    if website.strip():
        return _render_page(request, page, submitted=True)

    # Time-on-page floor — scripted posts submit instantly.
    try:
        # a missing or unparseable token is treated as 0 seconds on page, which
        # fails the floor below — a scripted post that simply omits t must not
        # be able to skip the wait.
        started = int(t)
        elapsed = int(time.time()) - started if started > 0 else 0
    except (TypeError, ValueError):
        elapsed = 0
    if elapsed < MIN_FILL_SECONDS:
        return _render_page(request, page, submitted=True)

    name_v = name.strip()[:120]
    mobile_v = "".join(ch for ch in mobile if ch.isdigit())[:10]
    pincode_v = "".join(ch for ch in pincode if ch.isdigit())[:6]

    if len(name_v) < 2:
        return _render_page(request, page, error="Please enter your name.", form=form)
    if len(mobile_v) != 10:
        return _render_page(request, page, error="Please enter a valid 10-digit mobile number.", form=form)
    if pincode_v and len(pincode_v) != 6:
        return _render_page(request, page, error="Pincode must be 6 digits.", form=form)
    if age_ok.strip().lower() not in ("on", "true", "1", "yes"):
        return _render_page(request, page, error="Please confirm you are 18 or older.", form=form)

    ip_hash = store.hash_ip(_client_ip(request))
    if not store.reserve_lead_slot(LEAD_CAP_PER_IP_DAY, scope="ip:" + ip_hash):
        return _render_page(request, page,
                            error="Too many enquiries from this connection today. "
                                  "Please call us instead.", form=form)
    if not store.reserve_lead_slot(LEAD_CAP_GLOBAL_DAY, scope="global"):
        return _render_page(request, page,
                            error="We are receiving an unusually high number of enquiries. "
                                  "Please try again later.", form=form)

    store.insert_lead(name_v, mobile_v, pincode_v, path, CONSENT_VERSION, ip_hash)
    return _render_page(request, page, submitted=True)


@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def root(request: Request):
    """Landing page.

    This used to return the same 404 as a mistyped path, so anyone who trimmed a
    shared link back to the domain hit a dead end on an RBI-registered lender's
    own address. It explains what a Business Page is and offers a route to
    Prayaan — and lists NO businesses, because a directory would hand anyone a
    roster of customers in one request, which is precisely what the identical
    404 on every unknown path exists to prevent."""
    if request.method == "HEAD":
        from fastapi.responses import Response
        return Response(status_code=200, media_type="text/html; charset=utf-8")
    return templates.TemplateResponse("home.html", {
        "request": request, "base_url": BASE_URL,
        "public_directory": PUBLIC_DIRECTORY,
        # Gated on SHOW_SWITCHER, like every other review surface. The gallery
        # lists every live business, so linking it from the public root would
        # publish the customer directory this page deliberately withholds.
        "show_switcher": SHOW_SWITCHER,
        "sample": (store.live_pages(limit=1) or [{}])[0].get("path", ""),
    })
