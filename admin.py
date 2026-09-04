"""The /admin back-office.

Server-rendered HTML forms, with admin.js as the one enhancement layer
(live preview, uploads, chip editors). The public pages run a single
same-origin script (reveal.js) under script-src 'self' — no inline script
anywhere in the service, so no nonce plumbing.

Bodies are parsed straight from urlencoded text rather than through FastAPI's
Form(...), which would pull in python-multipart — a dependency this process
does not otherwise need.
"""
import base64
import csv
import io
import json
import os
import secrets
from urllib.parse import parse_qs, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

import auth
import store
import uploads

router = APIRouter(prefix="/admin")
MAX_ADMIN_BODY = 64 * 1024          # generous for prose, still bounded
MAX_PREVIEW_BODY = 256 * 1024       # the live-preview post carries the whole draft
# base64 inflates ~33%, so this admits a ~6MB image with headroom and still
# rejects anything absurd before a byte is decoded.
MAX_UPLOAD_BODY = 9 * 1024 * 1024

# The domain is fixed by the deployment; everything after it is the admin's to
# choose. Shown beside the address field so what they type reads as a URL.
BASE_URL = os.getenv("PBN_BASE_URL", "https://business.prayaancapital.com").rstrip("/")

_templates = None                    # injected by main.py to share one env


def init(templates):
    global _templates
    _templates = templates


# ---- request helpers ------------------------------------------------------
async def _form(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_ADMIN_BODY:
        return {}
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True)
    return {k: v[0] for k, v in parsed.items()}


# When on, only the admin role can publish: branch staff prepare pages, a
# second person reviews and takes them live. Off by default for the pilot —
# a one-person office cannot four-eyes itself.
MAKER_CHECKER = os.getenv("PBN_MAKER_CHECKER", "false").lower() in ("1", "true", "yes")


def _session(request: Request):
    """Cookie signature AND the user row, every request. The cookie alone
    cannot be revoked (stateless HMAC); re-checking active + session-version
    against the store means suspending a user or changing their password kills
    every session they hold on their very next request — not 8 hours later."""
    sess = auth.read_session(request.cookies.get(auth.SESSION_COOKIE, ""))
    if not sess:
        return None
    username = sess.get("u", "")
    if username == auth.DEV_USERNAME:
        return sess if auth.PREVIEW_BUILD else None
    user = store.user_by_name(username)
    if not user or not user.get("active", True):
        return None
    if int(user.get("sv", 1)) != int(sess.get("sv", 1)):
        return None
    sess["_user"] = {k: v for k, v in user.items() if k != "password"}
    return sess


def _open_reports() -> int:
    """Badge count for the Reports tab. A takedown request is a clock, so the
    number belongs in the chrome of every screen rather than only on the one
    nobody opens. Never allowed to break a page render: a storage hiccup here
    costs a badge, not the back-office."""
    try:
        return int(store.open_report_count())
    except Exception:                                   # noqa: BLE001
        return 0


def _render(request: Request, template: str, session, **ctx):
    # A temp-password holder sees exactly one screen until they set their own.
    if (session and template != "password.html"
            and (session.get("_user") or {}).get("must_change")):
        return RedirectResponse(url="/admin/password", status_code=303)
    base = {"request": request, "session": session, "base_url": BASE_URL,
            "csrf": auth.csrf_token(session), "nav": ctx.pop("nav", ""),
            "maker_checker": MAKER_CHECKER,
            "open_reports": _open_reports() if session else 0}
    base.update(ctx)
    return _templates.TemplateResponse("admin/" + template, base)


def _is_admin(session) -> bool:
    return bool(session) and session.get("r") == "admin"


def _login_redirect():
    return RedirectResponse(url="/admin/login", status_code=303)


def _login_page(request: Request, error=None, status_code=200):
    return _templates.TemplateResponse("admin/login.html", {
        "request": request, "session": None, "error": error,
        # The one-click shortcut renders only in a review build. The route it
        # posts to checks the same flag, so a cached page cannot revive it.
        "dev_login": auth.PREVIEW_BUILD,
    }, status_code=status_code)


def _signed_in(request: Request, username: str, role: str, sv: int = 1,
               to: str = "/admin/pages"):
    """The single place a session cookie is minted, so the preview shortcut
    cannot drift into a weaker session than a typed password produces."""
    resp = RedirectResponse(url=to, status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.make_session(username, role, sv),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        # Path=/admin: the browser never attaches this to a public page request,
        # so the credential is simply absent from anonymous traffic.
        path=auth.SESSION_PATH,
        # Production builds mint Secure cookies unconditionally: behind a
        # TLS-terminating proxy the app-side scheme reads "http", and keying
        # off it would silently drop the flag exactly where it matters.
        secure=(not auth.PREVIEW_BUILD) or request.url.scheme == "https")
    return resp


def _lines(value):
    """Textarea -> list, blank lines dropped."""
    return [ln.strip() for ln in str(value or "").splitlines() if ln.strip()]


def _figures(value):
    """'Years in business | 12' per line -> [{label, value}]."""
    out = []
    for ln in _lines(value):
        if "|" in ln:
            label, _, val = ln.partition("|")
            out.append({"label": label.strip()[:40], "value": val.strip()[:20]})
    return out


# maps.app.goo.gl is kept: it is Google Maps' own share format (what the Maps app
# produces) and only ever resolves to Maps content. Generic goo.gl is dropped —
# it is a deprecated general-purpose shortener whose destination is arbitrary,
# so it is an open-redirect vector on a lender-branded page.
MAP_HOSTS = ("google.com", "www.google.com", "maps.google.com",
             "maps.app.goo.gl", "www.google.co.in", "google.co.in")


def _clean_map_url(value):
    """An unchecked link on a lender-branded page is an open redirect for
    phishing, so only Google Maps hosts are accepted."""
    from urllib.parse import urlparse
    v = str(value or "").strip()
    if not v:
        return None
    u = urlparse(v)
    if u.scheme not in ("http", "https") or u.hostname not in MAP_HOSTS:
        return None
    return v[:400]


def _clean_phones(value):
    """Two numbers max, each a 10-digit Indian national number, stored as
    "+91 XXXXXXXXXX". The server is the real gate — the two form inputs enforce
    10 digits in the browser, but a scripted post or the CSV importer must not be
    able to store a malformed number. A +91 or leading-0 prefix is stripped to
    the 10 national digits; anything that is not exactly 10 digits is dropped."""
    out = []
    for ln in _lines(value)[:2]:
        digits = "".join(c for c in ln if c.isdigit())
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10:
            out.append("+91 " + digits)
    return out


def _web_address(typed, state_code, branch_slug):
    """'TN/vellore/velan-steel', 'vellore/velan-steel' or 'velan-steel'
    -> (state, branch, slug). A blank field returns slug None, meaning "derive
    the last segment from the business name" — what happened before this field
    existed, and still the default.

    One, two or three parts are all accepted, but a short one is COMPLETED from
    the State and Branch fields rather than kept short: the public service
    routes /{state}/{branch}/{slug} and nothing else, so a two-part address
    would be a page that publishes and then 404s for the customer.

    Raises ValueError carrying a sentence fit to put on the form."""
    if not str(typed or "").strip():
        return state_code, branch_slug, None
    segments = [store.slugify(s) for s in str(typed).split("/")]
    segments = [s for s in segments if s]     # collapses //, leading and trailing /
    if not segments:
        raise ValueError("That web address is empty once accents and "
                         "punctuation are removed.")
    if len(segments) > 3:
        raise ValueError("A web address is at most three parts, like "
                         "TN/vellore/velan-steel.")
    slug = segments[-1]
    branch = segments[-2] if len(segments) >= 2 else store.slugify(branch_slug)
    state = segments[0] if len(segments) == 3 else store.slugify(state_code)
    # The last segment is checked against RESERVED_SLUGS inside store; the other
    # two never reach that check, and /static/... or /admin/... typed here would
    # mint a page URL that a real route already answers.
    for seg in (state, branch):
        if seg in store.RESERVED_SLUGS:
            raise ValueError("'{}' is reserved and cannot be part of a page "
                             "address.".format(seg))
    return state, branch, slug


def _store_accepts_typed_address() -> bool:
    """store.create_page takes the address an admin typed; a stand-in store —
    the JSON-file double that replaces Mongo in preview builds — may not yet.
    Probing beats calling and hoping: a store that ignored the argument would
    create the page at a DIFFERENT address, and an address is permanent."""
    import inspect
    try:
        return "name_slug" in inspect.signature(store.create_page).parameters
    except (TypeError, ValueError):
        return False


# The only standings the badge may show. Free text here could smuggle repayment
# or bureau language onto a public page about a named borrower; a fixed list
# cannot. Anything else is dropped, which (with the template) hides the tier.
TIER_STATUSES = {"active", "inactive", "lapsed"}

# How a customer's consent to publish can have been given. First publish
# requires one of these plus a free-text reference saying where the evidence
# lives — the difference between "an admin clicked publish" and something that
# stands up when a customer disputes the page.
CONSENT_METHODS = {"written", "whatsapp", "verbal"}


def _clean_tier_status(value):
    v = str(value or "").strip().lower()
    return v.capitalize() if v in TIER_STATUSES else None


def _this_year():
    import datetime as _dt
    # IST year; a founding year in the future is a data error, not a valid input.
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).year


def _accepted_photo(p):
    """The photo_url worth STORING, or None.

    Two rejections, for two different reasons:

      not own-host        an external image on a lender-branded page can be
                          swapped after review and leaks every visitor's IP to
                          whoever serves it.
      cannot survive here a /static/img/uploads/ path is a container-filesystem
                          reference. On a database-backed deployment that disk
                          is wiped by the next deploy, so storing one is storing
                          a URL already known to be dead.

    The second is the prevention half of the missing-photo fix: hiding a dead
    reference at render time still leaves it in the row, and the editor posts it
    straight back on the next save. Refusing it on write is what stops the fault
    coming back."""
    if not p.startswith(("/static/", "/photo/")):
        return None
    import main
    if p.startswith(main.DISK_PHOTO_PREFIX) and not main.disk_photos_persist():
        return None
    return p


def _page_payload(form):
    year = str(form.get("established_year", "")).strip()
    return {
        "business_name": form.get("business_name", "").strip()[:120],
        "owner_name": form.get("owner_name", "").strip()[:80] or None,
        "category": form.get("category", "").strip()[:60] or None,
        # Own-host photos only (/static bundled, /photo database-backed). An
        # external image on a lender-branded page can change after review and
        # leaks every visitor's IP to whoever hosts it; the upload flow exists
        # precisely so nothing needs hot-linking.
        #
        # A reference the deployment cannot keep is also refused HERE, at the
        # write boundary, not merely hidden at render time. Rendering around bad
        # data leaves the bad data in place: the editor's hidden photo_url field
        # carries the dead path back on every save, so an old page re-persists
        # it forever and the fault survives every fix downstream. Dropping it on
        # write means the next save of any affected page cleans it, and no page
        # can acquire the state again.
        "photo_url": _accepted_photo(form.get("photo_url", "").strip()[:400]),
        "locality": form.get("locality", "").strip()[:60] or None,
        "district": form.get("district", "").strip()[:60] or None,
        "state_name": form.get("state_name", "").strip()[:60] or None,
        "established_year": (int(year) if year.isdigit() and 1800 < int(year) <= _this_year() else None),
        "summary": form.get("summary", "").strip()[:400] or None,
        "about": _lines(form.get("about"))[:6],
        "offerings": _lines(form.get("offerings"))[:8],
        "figures": _figures(form.get("figures"))[:4],
        "hours": form.get("hours", "").strip()[:80] or None,
        "languages": _lines(form.get("languages"))[:4],
        "phones": _clean_phones(form.get("phones")),
        "map_url": _clean_map_url(form.get("map_url")),
        "indexed": form.get("indexed") == "on",
        "tier": (form.get("tier") or "").strip().lower() or None,
        "tier_status": _clean_tier_status(form.get("tier_status")),
    }


# ---- auth -----------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if _session(request):
        return RedirectResponse(url="/admin/pages", status_code=303)
    return _login_page(request)


@router.post("/login", response_class=HTMLResponse)
async def login(request: Request):
    form = await _form(request)
    username = (form.get("username") or "").strip().lower()[:60]
    password = form.get("password") or ""

    wait = auth.locked_out(username)
    if wait:
        return _login_page(
            request, status_code=429,
            error="Too many attempts. Try again in {} minutes.".format(max(1, wait // 60)))

    user = store.user_by_name(username)
    # Always run one PBKDF2, even when the user is unknown or inactive: verifying
    # against a fixed dummy hash keeps the response time constant, so it cannot be
    # used to tell a real username from a fake one (the error text is already
    # identical). The dummy result is discarded.
    if user and user.get("active", True):
        ok = auth.verify_password(password, user.get("password", ""))
    else:
        auth.verify_password(password, auth.DUMMY_HASH)
        ok = False
    if not ok:
        auth.note_failure(username)
        # One message for "no such user" and "wrong password" alike: telling
        # them apart hands an attacker a way to enumerate valid usernames.
        return _login_page(request, error="Wrong username or password.",
                           status_code=401)

    auth.clear_failures(username)
    return _signed_in(request, user["username"], user.get("role", "admin"),
                      sv=int(user.get("sv", 1)),
                      to="/admin/password" if user.get("must_change") else "/admin/pages")


@router.post("/dev-login")
async def dev_login(request: Request):
    """One-click sign-in, preview builds only.

    Not a back door with a hidden button: in production PBN_SHOW_SWITCHER is off,
    and then this route answers 404 like a URL that was never registered, while
    the button that posts to it is not rendered either. Both halves check the
    same flag, so neither a cached login page nor a hand-crafted POST gets in.

    The session it mints is an ordinary one — same signature, expiry, cookie
    scope and CSRF derivation — because a shortcut down a different code path
    would stop exercising the thing being previewed. It signs in as its own
    identity rather than a real account, so the audit trail says plainly which
    rows were made by the shortcut."""
    if not auth.PREVIEW_BUILD:
        return Response("Not found", status_code=404)
    return _signed_in(request, auth.DEV_USERNAME, auth.DEV_ROLE)


@router.post("/logout")
async def logout(request: Request):
    # CSRF on logout too: without it, a cross-site auto-submitting form could
    # force-log-out a signed-in admin. The button already carries the token.
    session = _session(request)
    form = await _form(request)
    if session and not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    resp = RedirectResponse(url="/admin/login", status_code=303)
    resp.delete_cookie(auth.SESSION_COOKIE, path=auth.SESSION_PATH)
    return resp


# ---- pages ----------------------------------------------------------------
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def admin_root(request: Request):
    return RedirectResponse(url="/admin/pages", status_code=303)


# Sort orders offered on the Pages screen: (row key, reverse). A whitelist
# rather than a passthrough — the value arrives in a query string, and an
# unknown one falls back to the default instead of reaching the data layer.
PAGE_SORTS = {
    "updated_desc": ("updated_at", True),
    "updated_asc": ("updated_at", False),
    "created_desc": ("created_at", True),
    "created_asc": ("created_at", False),
    "name_asc": ("business_name", False),
    "name_desc": ("business_name", True),
}
PAGE_SORT_LABELS = [
    ("updated_desc", "Recently updated"),
    ("updated_asc", "Longest untouched"),
    ("created_desc", "Newest page"),
    ("created_asc", "Oldest page"),
    ("name_asc", "Name A–Z"),
    ("name_desc", "Name Z–A"),
]
DEFAULT_PAGE_SORT = "updated_desc"


def _clean_date(value):
    """'YYYY-MM-DD' or None. Timestamps are stored as IST strings in exactly
    this shape, so a validated date can be compared as text — no parsing of
    every row just to filter a list."""
    import datetime as _dt
    v = str(value or "").strip()[:10]
    try:
        _dt.datetime.strptime(v, "%Y-%m-%d")
        return v
    except ValueError:
        return None


@router.get("/pages", response_class=HTMLResponse)
def pages_list(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    p = request.query_params

    status = (p.get("status") or "").strip().lower()
    if status not in store.PAGE_STATUSES:
        status = ""
    q = (p.get("q") or "").strip()[:80]
    date_from = _clean_date(p.get("from"))
    date_to = _clean_date(p.get("to"))
    sort = p.get("sort") if p.get("sort") in PAGE_SORTS else DEFAULT_PAGE_SORT

    # ONE fetch, then count and filter here. The tiles must total the WHOLE
    # book while the table shows a single slice of it; counting with a second
    # round-trip would let the headline numbers drift from the rows on screen.
    rows = store.list_pages(limit=2000)
    counts = {"total": len(rows), "live": 0, "draft": 0, "removed": 0}
    for r in rows:
        if r.get("status") in counts:
            counts[r["status"]] += 1

    sel = rows
    if status:
        sel = [r for r in sel if r.get("status") == status]
    if q:
        needle = q.lower()
        sel = [r for r in sel
               if needle in " ".join(str(r.get(k) or "") for k in
                                     ("business_name", "owner_name", "path")).lower()]
    if date_from:
        sel = [r for r in sel if str(r.get("updated_at") or "")[:10] >= date_from]
    if date_to:
        sel = [r for r in sel if str(r.get("updated_at") or "")[:10] <= date_to]

    key, reverse = PAGE_SORTS[sort]
    sel = sorted(sel, key=lambda r: str(r.get(key) or "").lower(), reverse=reverse)

    # Every filter except the status tile, pre-encoded: the tiles are links, so
    # clicking one must keep the search, dates and sort the user already set.
    keep = urlencode([(k, v) for k, v in (
        ("q", q), ("from", date_from), ("to", date_to),
        ("sort", sort if sort != DEFAULT_PAGE_SORT else "")) if v])

    return _render(request, "pages.html", session, nav="pages",
                   pages=sel, counts=counts, status=status, q=q,
                   date_from=date_from or "", date_to=date_to or "",
                   sort=sort, sort_labels=PAGE_SORT_LABELS, keep=keep,
                   filtered=bool(status or q or date_from or date_to
                                 or sort != DEFAULT_PAGE_SORT),
                   statuses=store.PAGE_STATUSES)


@router.get("/pages/new", response_class=HTMLResponse)
def page_new(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    return _render(request, "page_form.html", session, nav="pages",
                   page=None, error=None, events=[])


@router.post("/pages/new", response_class=HTMLResponse)
async def page_create(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    data = _page_payload(form)
    state = (form.get("state_code") or "").strip().upper()[:3]
    branch = (form.get("branch_slug") or "").strip()[:40]
    typed = (form.get("web_address") or "").strip()[:200]
    if not data["business_name"] or not state or not branch:
        return _render(request, "page_form.html", session, nav="pages", page=None,
                       events=[], form=form,
                       error="Business name, state and branch are all required.")
    try:
        state, branch, slug = _web_address(typed, state, branch)
        if slug:
            if not _store_accepts_typed_address():
                raise ValueError("This deployment's storage cannot set a chosen "
                                 "web address. Leave the field blank to derive "
                                 "one from the business name.")
            page = store.create_page(data, session["u"], state, branch, name_slug=slug)
        else:
            page = store.create_page(data, session["u"], state, branch)
    except ValueError as exc:
        # A typed address is never adjusted into a free one — the admin gets the
        # form back with what they typed still in it, because the whole reason
        # to type an address is that this exact link is going somewhere.
        return _render(request, "page_form.html", session, nav="pages", page=None,
                       events=[], form=form, error=str(exc))
    return RedirectResponse(url="/admin/pages/{}".format(page["id"]), status_code=303)


@router.get("/pages/{page_id}", response_class=HTMLResponse)
def page_edit(request: Request, page_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    page = store.page_by_id(page_id)
    if not page:
        return Response("Not found", status_code=404)
    error = None
    if request.query_params.get("err") == "consent":
        error = ("To publish, record the customer's consent: pick how it was "
                 "given and add a reference — where the signed form is kept, "
                 "the WhatsApp message date, or who took it verbally and when.")
    elif request.query_params.get("err") == "approval":
        error = "Publishing needs an admin: ask one to review this page and take it live."
    # A photo saved before uploads moved into the database points at the old
    # container filesystem, which no deploy survives. The public page now hides
    # such a reference rather than rendering a broken frame, so without this the
    # photo would simply vanish with no explanation of where it went.
    import main
    photo_lost = bool((page.get("photo_url") or "").strip()) and not main.real_photo(page)
    return _render(request, "page_form.html", session, nav="pages",
                   page=page, error=error, events=store.page_events(page_id),
                   photo_lost=photo_lost,
                   views=store.views_for(page_id) if page.get("status") == "live" else 0)


@router.post("/pages/{page_id}", response_class=HTMLResponse)
async def page_update(request: Request, page_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    # update_page writes the FULL editable field set, so a partial POST would
    # blank every field it did not send. The browser form always carries the
    # sentinel; a script that hand-rolls a partial body is refused instead of
    # silently wiping a live page.
    if form.get("form_complete") != "1":
        return Response("Rejected: partial form. Updates must send every field "
                        "(missing ones would be erased).", status_code=400)
    store.update_page(page_id, _page_payload(form), session["u"])
    return RedirectResponse(url="/admin/pages/{}?saved=1".format(page_id), status_code=303)


@router.post("/pages/{page_id}/status", response_class=HTMLResponse)
async def page_status(request: Request, page_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    status = form.get("status")
    if status not in store.PAGE_STATUSES:
        return Response("Bad status", status_code=400)
    consent_method = None
    consent_ref = None
    if status == store.PAGE_LIVE:
        # Maker-checker: staff prepare, an admin takes it live.
        if MAKER_CHECKER and not _is_admin(session):
            return RedirectResponse(
                url="/admin/pages/{}?err=approval".format(page_id), status_code=303)
        page = store.page_by_id(page_id)
        if page and not page.get("consent"):
            # First publish: consent evidence is REQUIRED, not defaulted. A
            # publish that cannot say how the customer agreed goes back to the
            # form instead of going live.
            consent_method = (form.get("consent_method") or "").strip().lower()
            consent_ref = (form.get("consent_ref") or "").strip()[:200]
            if consent_method not in CONSENT_METHODS or not consent_ref:
                return RedirectResponse(
                    url="/admin/pages/{}?err=consent".format(page_id), status_code=303)
    store.set_page_status(page_id, status, session["u"], form.get("note") or None,
                          consent_method=consent_method, consent_ref=consent_ref)
    return RedirectResponse(url="/admin/pages/{}".format(page_id), status_code=303)


# ---- live preview + photo upload ------------------------------------------
def _preview_page_dict(form):
    """Draft form values -> a page dict the real variant template can render.
    Deliberately reuses _page_payload so the preview validates and shapes fields
    exactly as a save would — a preview that accepted what a save rejects would
    lie."""
    data = _page_payload(form)
    state = (form.get("state_code") or "TN").strip().upper()[:3] or "TN"
    branch = (form.get("branch_slug") or "branch").strip()[:40] or "branch"
    typed = (form.get("web_address") or "").strip()[:200]
    try:
        state, branch, slug = _web_address(typed, state, branch)
    except ValueError:
        state, branch, slug = state, store.slugify(branch), None
    slug = slug or store.slugify(data.get("business_name")) or "preview"
    data["path"] = store.build_path(state, branch, slug)
    return data


@router.post("/preview", response_class=HTMLResponse)
async def live_preview(request: Request):
    """Render the current draft as the customer would see it. Auth + CSRF like
    every admin POST — this reads session-only tooling and must not be drivable
    by an unauthenticated cross-site fetch."""
    session = _session(request)
    if not session:
        return Response("", status_code=401)
    raw = await request.body()
    if len(raw) > MAX_PREVIEW_BODY:
        return Response("Draft too large to preview.", status_code=413)
    form = {k: v[0] for k, v in
            parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True).items()}
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    try:
        variant = int(form.get("variant") or 0)
    except (TypeError, ValueError):
        variant = 0
    import main                                   # lazy: main imports admin
    html = main.render_variant_html(_preview_page_dict(form), variant or None)
    # This HTML is dropped into a same-origin iframe via srcdoc; it inherits the
    # admin page's CSP, so it needs no policy header of its own.
    return Response(html, media_type="text/html",
                    headers={"Cache-Control": "no-store"})


@router.post("/upload")
async def upload_photo(request: Request):
    """Accept a base64 image (JSON), store it in the git-backed image repo, and
    return its public URL. JSON rather than multipart so this process keeps its
    no-python-multipart property; the admin JS reads the file and base64-encodes
    it before posting."""
    session = _session(request)
    if not session:
        return Response(json.dumps({"error": "Not signed in."}),
                        status_code=401, media_type="application/json")
    raw = await request.body()
    if len(raw) > MAX_UPLOAD_BODY:
        return Response(json.dumps({"error": "That image is too large."}),
                        status_code=413, media_type="application/json")
    try:
        body = json.loads(raw.decode("utf-8", "replace"))
    except ValueError:
        return Response(json.dumps({"error": "Malformed upload."}),
                        status_code=400, media_type="application/json")
    if not auth.csrf_ok(session, body.get("csrf")):
        return Response(json.dumps({"error": "Session expired. Reload and try again."}),
                        status_code=403, media_type="application/json")
    # Per-admin daily cap: a leaked session must not be able to fill the disk.
    if not store.reserve_lead_slot(200, scope="upload:" + session["u"]):
        return Response(json.dumps({"error": "Daily upload limit reached for "
                                             "this account."}),
                        status_code=429, media_type="application/json")
    data_url = body.get("data") or ""
    # strip an optional "data:image/png;base64," prefix
    if "," in data_url and data_url.strip().lower().startswith("data:"):
        data_url = data_url.split(",", 1)[1]
    try:
        blob = base64.b64decode(data_url, validate=True)
    except (ValueError, TypeError):
        return Response(json.dumps({"error": "Could not read that file."}),
                        status_code=400, media_type="application/json")
    try:
        result = uploads.save_image(blob, by=session["u"])
    except uploads.UploadError as exc:
        return Response(json.dumps({"error": str(exc)}),
                        status_code=422, media_type="application/json")
    return Response(json.dumps(result), media_type="application/json",
                    headers={"Cache-Control": "no-store"})


# ---- leads ----------------------------------------------------------------
@router.get("/leads", response_class=HTMLResponse)
def leads_list(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    status = request.query_params.get("status") or None
    q = request.query_params.get("q") or None
    leads = store.list_leads(status=status, q=q)
    # Age annotation so a NEW lead that has waited past the "we call within one
    # working day" promise is visibly on fire, not just another row.
    from datetime import datetime as _dt
    now = _dt.now(store.IST).replace(tzinfo=None)
    for lead in leads:
        try:
            t0 = _dt.strptime(lead.get("at", ""), "%Y-%m-%d %H:%M:%S")
            lead["age_hours"] = max(0, int((now - t0).total_seconds() // 3600))
        except ValueError:
            lead["age_hours"] = None
    # Whole-book counts, not counts of the current view — see reports_list.
    every = store.list_leads()
    waiting = 0
    for r in every:
        if r.get("status") != "NEW":
            continue
        try:
            t0 = _dt.strptime(r.get("at", ""), "%Y-%m-%d %H:%M:%S")
            if (now - t0).total_seconds() >= 24 * 3600:
                waiting += 1
        except ValueError:
            pass
    counts = {"total": len(every),
              "new": sum(1 for r in every if r.get("status") == "NEW"),
              "working": sum(1 for r in every
                             if r.get("status") in ("CONTACTED", "INTERESTED")),
              "overdue": waiting}
    return _render(request, "leads.html", session, nav="leads",
                   leads=leads, counts=counts,
                   status=status or "", q=q or "",
                   statuses=store.LEAD_STATUSES)


@router.post("/leads/{lead_id}", response_class=HTMLResponse)
async def lead_update(request: Request, lead_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    store.update_lead(lead_id,
                      status=form.get("status") or None,
                      note=form.get("cs_notes"),
                      branch=form.get("assigned_branch") or None,
                      by=session["u"])
    back = "/admin/leads"
    if form.get("status_filter"):
        back += "?status=" + form["status_filter"]
    return RedirectResponse(url=back, status_code=303)


def _user_book():
    """The user list plus whole-book counts for the tiles.

    Four routes render users.html (list, create, the create-failed path, and
    every row action), so the counting lives here rather than being repeated
    and drifting between them."""
    us = store.list_users()
    return us, {"total": len(us),
                "active": sum(1 for u in us if u.get("active")),
                "suspended": sum(1 for u in us if not u.get("active")),
                "admins": sum(1 for u in us if u.get("role") == "admin")}


# ---- users (admin role only) -------------------------------------------------
def _temp_password() -> str:
    """Readable enough to dictate over the phone, random enough to matter for
    the minutes it lives — the holder must change it at first sign-in."""
    return secrets.token_urlsafe(9)


@router.get("/users", response_class=HTMLResponse)
def users_list(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    if not _is_admin(session):
        return Response("Managing users needs the admin role.", status_code=403)
    _ub = _user_book()
    return _render(request, "users.html", session, nav="users",
                   users=_ub[0], counts=_ub[1], temp_pw=None, temp_user=None,
                   error=None, roles=store.USER_ROLES)


@router.post("/users/new", response_class=HTMLResponse)
async def user_create(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    if not _is_admin(session):
        return Response("Managing users needs the admin role.", status_code=403)
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    username = (form.get("username") or "").strip().lower()[:60]
    role = form.get("role") if form.get("role") in store.USER_ROLES else "staff"
    error = None
    if not username or not username.replace("-", "").replace(".", "").isalnum():
        error = "Username: letters, numbers, dots and hyphens only."
    elif store.user_by_name(username):
        error = "That username already exists."
    if error:
        _ub = _user_book()
        return _render(request, "users.html", session, nav="users",
                       users=_ub[0], counts=_ub[1], temp_pw=None, temp_user=None,
                       error=error, roles=store.USER_ROLES)
    temp = _temp_password()
    store.create_user(username, auth.hash_password(temp), role=role,
                      by=session["u"], must_change=True)
    # The temp password is rendered ONCE, on this no-store response, and never
    # persisted anywhere in the clear.
    _ub = _user_book()
    return _render(request, "users.html", session, nav="users",
                   users=_ub[0], counts=_ub[1], temp_pw=temp, temp_user=username,
                   error=None, roles=store.USER_ROLES)


@router.post("/users/{user_id}", response_class=HTMLResponse)
async def user_action(request: Request, user_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    if not _is_admin(session):
        return Response("Managing users needs the admin role.", status_code=403)
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    target = store.user_by_id(user_id)
    if not target:
        return Response("No such user", status_code=404)
    action = form.get("action")
    temp_pw = None
    if action == "suspend":
        if target["username"] == session["u"]:
            return Response("You cannot suspend your own account.", status_code=400)
        store.update_user(user_id, by=session["u"], bump_sv=True, active=False)
    elif action == "activate":
        store.update_user(user_id, by=session["u"], active=True)
    elif action == "reset":
        temp_pw = _temp_password()
        store.update_user(user_id, by=session["u"], bump_sv=True,
                          password=auth.hash_password(temp_pw), must_change=True)
    elif action == "role":
        role = form.get("role")
        if role not in store.USER_ROLES:
            return Response("Bad role", status_code=400)
        if target["username"] == session["u"] and role != "admin":
            return Response("You cannot demote your own account.", status_code=400)
        store.update_user(user_id, by=session["u"], bump_sv=True, role=role)
    else:
        return Response("Bad action", status_code=400)
    _ub = _user_book()
    return _render(request, "users.html", session, nav="users",
                   users=_ub[0], counts=_ub[1], temp_pw=temp_pw,
                   temp_user=target["username"] if temp_pw else None,
                   error=None, roles=store.USER_ROLES)


# ---- own password -------------------------------------------------------------
@router.get("/password", response_class=HTMLResponse)
def password_form(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    return _render(request, "password.html", session, nav="",
                   error=None, done=False,
                   forced=bool((session.get("_user") or {}).get("must_change")))


@router.post("/password", response_class=HTMLResponse)
async def password_change(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    user = store.user_by_name(session["u"])
    if not user:
        return Response("The preview sign-in has no password to change.",
                        status_code=400)
    current = form.get("current") or ""
    new = form.get("new") or ""
    confirm = form.get("confirm") or ""
    error = None
    if not auth.verify_password(current, user.get("password", "")):
        error = "Current password is wrong."
    elif len(new) < 8:
        error = "New password must be at least 8 characters."
    elif new == current:
        error = "The new password must be different."
    elif new != confirm:
        error = "The two copies of the new password do not match."
    if error:
        return _render(request, "password.html", session, nav="",
                       error=error, done=False,
                       forced=bool(user.get("must_change")))
    updated = store.update_user(user["id"], by=session["u"], bump_sv=True,
                                password=auth.hash_password(new), must_change=False)
    # bump_sv killed every session including THIS one — mint a fresh cookie so
    # the user changing their password is the one person not logged out by it.
    return _signed_in(request, user["username"], user.get("role", "admin"),
                      sv=int(updated.get("sv", 1)))


# ---- page reports (takedown / correction intake) ----------------------------
@router.get("/reports", response_class=HTMLResponse)
def reports_list(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    status = request.query_params.get("status") or None
    # Counts come from the UNFILTERED book, like the page tiles: a count taken
    # from the filtered list tells you "0 still open" the moment you filter to
    # DONE, which is the opposite of what the number is for.
    every = store.list_reports()
    counts = {"total": len(every),
              "open": sum(1 for r in every if r.get("status") == "OPEN"),
              "done": sum(1 for r in every if r.get("status") == "DONE")}
    return _render(request, "reports.html", session, nav="reports",
                   reports=store.list_reports(status=status), counts=counts,
                   status=status or "", statuses=store.REPORT_STATUSES)


@router.post("/reports/{report_id}", response_class=HTMLResponse)
async def report_update(request: Request, report_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
    status = form.get("status")
    if status not in store.REPORT_STATUSES:
        return Response("Bad status", status_code=400)
    store.update_report(report_id, status, session["u"], form.get("note"))
    back = "/admin/reports"
    if form.get("status_filter"):
        back += "?status=" + form["status_filter"]
    return RedirectResponse(url=back, status_code=303)


def _csv_safe(value):
    """Neutralise spreadsheet formula injection. A lead name like
    =HYPERLINK(...) or +cmd would execute when the exported CSV is opened in
    Excel/Sheets; the name comes from the anonymous public form, so it is
    untrusted. Prefixing a leading =,+,-,@ (or tab/CR) with an apostrophe makes
    the cell render as literal text."""
    t = "" if value is None else str(value)
    if t and t[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + t
    return t


@router.get("/leads.csv")
def leads_csv(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    # Reading one lead and walking out with the whole book are different acts:
    # the bulk export is admin-only.
    if not _is_admin(session):
        return Response("Exporting the full lead book needs the admin role.",
                        status_code=403)
    rows = store.list_leads(status=request.query_params.get("status") or None,
                            limit=20000)
    buf = io.StringIO()
    cols = ["id", "at", "name", "mobile", "pincode", "referrer_business_name",
            "source_path", "via", "status", "assigned_branch", "routed_at",
            "called_by", "called_at", "cs_notes"]
    w = csv.writer(buf)
    w.writerow(cols)
    for r in rows:
        w.writerow([_csv_safe(r.get(c, "")) for c in cols])
    # A bulk copy of customer phone numbers leaving the system is a different
    # act from reading one record, so it is audited on its own.
    store.log_lead_export(len(rows), session["u"])
    return Response(
        buf.getvalue(), media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="pbn-leads.csv"',
                 "Cache-Control": "no-store"})
