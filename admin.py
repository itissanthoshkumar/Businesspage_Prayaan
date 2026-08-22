"""The /admin back-office.

Server-rendered forms with NO JavaScript. That is not nostalgia: the public
pages ship a Content-Security-Policy of script-src 'none', and keeping the whole
service honest to that means the policy needs no per-route exception and no
nonce plumbing. A CRUD admin is exactly the kind of thing HTML forms were for.

Bodies are parsed straight from urlencoded text rather than through FastAPI's
Form(...), which would pull in python-multipart — a dependency this process
does not otherwise need.
"""
import base64
import csv
import io
import json
import os
from urllib.parse import parse_qs

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


def _session(request: Request):
    return auth.read_session(request.cookies.get(auth.SESSION_COOKIE, ""))


def _render(request: Request, template: str, session, **ctx):
    base = {"request": request, "session": session, "base_url": BASE_URL,
            "csrf": auth.csrf_token(session), "nav": ctx.pop("nav", "")}
    base.update(ctx)
    return _templates.TemplateResponse("admin/" + template, base)


def _login_redirect():
    return RedirectResponse(url="/admin/login", status_code=303)


def _login_page(request: Request, error=None, status_code=200):
    return _templates.TemplateResponse("admin/login.html", {
        "request": request, "session": None, "error": error,
        # The one-click shortcut renders only in a review build. The route it
        # posts to checks the same flag, so a cached page cannot revive it.
        "dev_login": auth.PREVIEW_BUILD,
    }, status_code=status_code)


def _signed_in(request: Request, username: str, role: str):
    """The single place a session cookie is minted, so the preview shortcut
    cannot drift into a weaker session than a typed password produces."""
    resp = RedirectResponse(url="/admin/pages", status_code=303)
    resp.set_cookie(
        auth.SESSION_COOKIE, auth.make_session(username, role),
        max_age=auth.SESSION_MAX_AGE, httponly=True, samesite="lax",
        # Path=/admin: the browser never attaches this to a public page request,
        # so the credential is simply absent from anonymous traffic.
        path=auth.SESSION_PATH,
        secure=request.url.scheme == "https")
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
    out = []
    for ln in _lines(value)[:2]:            # two is the cap the design honours
        digits = "".join(c for c in ln if c.isdigit() or c == "+")
        if len(digits) >= 8:
            out.append(ln.strip()[:20])
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


def _clean_tier_status(value):
    v = str(value or "").strip().lower()
    return v.capitalize() if v in TIER_STATUSES else None


def _this_year():
    import datetime as _dt
    # IST year; a founding year in the future is a data error, not a valid input.
    return (_dt.datetime.utcnow() + _dt.timedelta(hours=5, minutes=30)).year


def _page_payload(form):
    year = str(form.get("established_year", "")).strip()
    return {
        "business_name": form.get("business_name", "").strip()[:120],
        "owner_name": form.get("owner_name", "").strip()[:80] or None,
        "category": form.get("category", "").strip()[:60] or None,
        "photo_url": form.get("photo_url", "").strip()[:400] or None,
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
    return _signed_in(request, user["username"], user.get("role", "admin"))


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


@router.get("/pages", response_class=HTMLResponse)
def pages_list(request: Request):
    session = _session(request)
    if not session:
        return _login_redirect()
    status = request.query_params.get("status") or None
    q = request.query_params.get("q") or None
    return _render(request, "pages.html", session, nav="pages",
                   pages=store.list_pages(status=status, q=q),
                   status=status or "", q=q or "",
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
    return _render(request, "page_form.html", session, nav="pages",
                   page=page, error=None, events=store.page_events(page_id))


@router.post("/pages/{page_id}", response_class=HTMLResponse)
async def page_update(request: Request, page_id: int):
    session = _session(request)
    if not session:
        return _login_redirect()
    form = await _form(request)
    if not auth.csrf_ok(session, form.get("csrf")):
        return Response("Bad CSRF token", status_code=403)
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
    store.set_page_status(page_id, status, session["u"], form.get("note") or None)
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
    return _render(request, "leads.html", session, nav="leads",
                   leads=store.list_leads(status=status, q=q),
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
    rows = store.list_leads(status=request.query_params.get("status") or None,
                            limit=20000)
    buf = io.StringIO()
    cols = ["id", "at", "name", "mobile", "pincode", "referrer_business_name",
            "source_path", "status", "assigned_branch", "routed_at",
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
