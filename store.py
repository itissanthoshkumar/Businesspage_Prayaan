"""Data access for the public Business Pages service.

Deliberately NOT Sherlock's mongostore. This process is internet-facing, so it
gets its own connection with narrowly scoped credentials:

  PBN_MONGO_URI_RO  — may read business_pages only
  PBN_MONGO_URI_RW  — may insert into site_leads and bump counters, nothing else

Falling back to a single MONGO_URI is supported for local development only. In
production boot_checks() below — run at startup by main.py — refuses to start
when the URIs are shared, the database name collides with Sherlock's, or the
write user can read the lead book.
"""
import hashlib
import os
from datetime import datetime, timedelta, timezone

# Project convention: everything is stored and compared in IST. No UTC anywhere.
IST = timezone(timedelta(hours=5, minutes=30))

# PBN's OWN database. Deliberately no fallback to the shared MONGO_DB env or
# Sherlock's database name: on a box that carries Sherlock's environment, a
# fallback would silently merge PBN's pages and leads into Sherlock's data.
MONGO_DB = os.getenv("PBN_MONGO_DB", "pbn")
URI_RO = os.getenv("PBN_MONGO_URI_RO") or os.getenv("MONGO_URI", "mongodb://127.0.0.1:27017")
URI_RW = os.getenv("PBN_MONGO_URI_RW") or URI_RO

PAGE_LIVE = "live"

_ro = None
_rw = None


def _now():
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _client(uri):
    from pymongo import MongoClient
    return MongoClient(uri, serverSelectionTimeoutMS=int(os.getenv("MONGO_TIMEOUT_MS", "6000")),
                       appname="pbn-public")


def db_ro():
    global _ro
    if _ro is None:
        _ro = _client(URI_RO)[MONGO_DB]
    return _ro


def db_rw():
    global _rw
    if _rw is None:
        _rw = _client(URI_RW)[MONGO_DB]
    return _rw


def norm_path(path) -> str:
    """Mirror of mongostore._norm_path — leading slash, upper state, lower rest."""
    parts = [p for p in str(path or "").strip().split("/") if p]
    if len(parts) != 3:
        return "/" + "/".join(parts)
    return "/{}/{}/{}".format(parts[0].upper(), parts[1].lower(), parts[2].lower())


def page_by_path(path):
    """(page, canonical_path_or_None). The second value is set when the request
    matched an alias, so the caller can 301 to the canonical URL."""
    p = norm_path(path)
    row = db_ro().business_pages.find_one({"path": p}, {"_id": 0})
    if row:
        return row, None
    row = db_ro().business_pages.find_one({"aliases": p}, {"_id": 0})
    if row:
        return row, row.get("path")
    return None, None


def live_pages(limit=50000):
    """Live pages. business_name/locality are carried for the internal review
    gallery; the sitemap simply ignores them."""
    return list(db_ro().business_pages.find(
        {"status": PAGE_LIVE},
        {"_id": 0, "path": 1, "updated_at": 1, "indexed": 1,
         "business_name": 1, "locality": 1, "district": 1, "category": 1,
         "photo_url": 1, "owner_name": 1, "tier": 1, "tier_status": 1}
    ).sort("id", 1).limit(int(limit)))


def _seq(name):
    from pymongo import ReturnDocument
    doc = db_rw().counters.find_one_and_update(
        {"_id": name}, {"$inc": {"v": 1}}, upsert=True,
        return_document=ReturnDocument.AFTER)
    return doc["v"]


def reserve_lead_slot(cap, scope="global", day=None) -> bool:
    """Atomically reserve one submission under a daily cap. A check-then-insert
    would be a spam amplifier under concurrency, so this is a single round-trip
    conditional increment — the same pattern Sherlock uses for vendor call caps.

    expireAt + the TTL index from ensure_indexes() reap the day's keys two days
    on, so quota rows do not accumulate one-per-IP-per-day forever."""
    from pymongo import ReturnDocument
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    key = "lead_quota:{}:{}".format(scope, day)
    db_rw().counters.update_one(
        {"_id": key},
        {"$setOnInsert": {"v": 0, "expireAt": datetime.now(IST) + timedelta(days=2)}},
        upsert=True)
    doc = db_rw().counters.find_one_and_update(
        {"_id": key, "v": {"$lt": int(cap)}}, {"$inc": {"v": 1}},
        return_document=ReturnDocument.AFTER)
    return doc is not None


def hash_ip(ip, salt=None) -> str:
    """Daily-rotating KEYED hash. The raw IP is never stored — it is only needed
    to spot one source flooding the form, which a per-day hash answers.

    Keyed with a server-side secret via HMAC, not a plain SHA of the date. The
    IPv4 space is only ~4.3e9, so a hash salted with the public date could be
    brute-forced back to the exact IP in seconds by anyone who saw the lead DB;
    HMAC with a secret the attacker does not have closes that."""
    import hmac as _hmac
    secret = os.getenv("PBN_SECRET_KEY", "") or "dev-only-not-for-production"
    day = salt or datetime.now(IST).strftime("%Y-%m-%d")
    msg = "{}|{}".format(day, str(ip or "")).encode()
    return _hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()[:32]


def insert_lead(name, mobile, pincode, source_path, consent_version, ip_hash,
                via=None):
    """Insert an inbound loan lead.

    source_path comes from the request URL, never from a form field — otherwise a
    scripted post could attribute leads to any customer it chose and poison the
    referral numbers. The page lookup uses the read-only connection.

    via: which share channel brought the visitor ('wa' = the page's WhatsApp
    share link, 'qr' = a printed code). Whitelisted upstream; this is the only
    attribution beyond the page itself, so keep it."""
    page, _ = page_by_path(source_path) if source_path else (None, None)
    lid = _seq("site_leads")
    row = {
        "id": lid, "at": _now(),
        "name": str(name or "").strip()[:120],
        "mobile": str(mobile or "").strip()[:15],
        "pincode": str(pincode or "").strip()[:6],
        "source_path": norm_path(source_path) if source_path else None,
        "source_page_id": (page or {}).get("id"),
        "referrer_business_name": (page or {}).get("business_name"),
        "via": via,
        "status": "NEW",
        "assigned_branch": None, "routed_at": None,
        "cs_notes": None, "called_by": None, "called_at": None,
        "consent_version": consent_version, "ip_hash": ip_hash,
    }
    db_rw().site_leads.insert_one(row)
    db_rw().lead_events.insert_one({
        "id": _seq("lead_events"), "at": _now(), "lead_id": lid,
        "event": "RECEIVED", "by": None,
        "changes": {"source_path": [None, row["source_path"]]}, "note": None,
    })
    row.pop("_id", None)
    return row


# ===========================================================================
# ADMIN DATA LAYER
#
# A THIRD credential, deliberately distinct from the two above. The public
# process reads pages with URI_RO and inserts leads with URI_RW; neither may
# edit a page or read the lead book. Admin work needs both, so it gets its own
# connection rather than widening either public one — the property worth keeping
# is that a flaw in the anonymous request path cannot reach customer records.
# ===========================================================================
URI_ADMIN = os.getenv("PBN_MONGO_URI_ADMIN") or URI_RW

PAGE_DRAFT = "draft"
PAGE_REMOVED = "removed"
PAGE_STATUSES = (PAGE_DRAFT, PAGE_LIVE, PAGE_REMOVED)

LEAD_STATUSES = ("NEW", "CONTACTED", "INTERESTED", "NOT_INTERESTED",
                 "SENT_TO_BRANCH", "CLOSED")

# Segments that would collide with a route or impersonate the brand. An
# auto-derived slug that lands here is SUFFIXED so a business genuinely called
# "API" can still be imported; an explicitly supplied one is rejected.
RESERVED_SLUGS = {
    "api", "admin", "static", "assets", "preview", "privacy", "grievance",
    "report", "referral-terms", "robots.txt", "sitemap.xml", "favicon.ico",
    "healthz", "prayaan", "prayaancapital", "prayaan-capital", "login", "logout",
}

_admin = None


def db_admin():
    global _admin
    if _admin is None:
        _admin = _client(URI_ADMIN)[MONGO_DB]
    return _admin


def _seq_admin(name):
    from pymongo import ReturnDocument
    doc = db_admin().counters.find_one_and_update(
        {"_id": name}, {"$inc": {"v": 1}}, upsert=True,
        return_document=ReturnDocument.AFTER)
    return doc["v"]


def slugify(text) -> str:
    """ASCII, lowercase, hyphenated.

    Tamil business names are TRANSLITERATED upstream, never percent-encoded — a
    URL full of %E0%AE%95 reads as spam in a WhatsApp message, and these links
    live in chat histories for years."""
    import re
    import unicodedata
    s = unicodedata.normalize("NFKD", str(text or ""))
    s = s.encode("ascii", "ignore").decode("ascii").lower()
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return re.sub(r"-{2,}", "-", s)[:60]


def unique_name_slug(base, state_code, branch_slug, explicit=False):
    """Append -2, -3 ... until the path is free. Never reassigns a path that has
    ever been used, including by a removed page: a link already shared must not
    one day open a different business.

    explicit=True means a person TYPED this address instead of it being derived
    from the business name, and then nothing is adjusted — a reserved word or a
    collision raises. Someone who types an address is about to print it on a
    signboard or paste it into a WhatsApp broadcast, so quietly handing back
    velan-steel-2 would publish the wrong link; better to say no on the form."""
    slug = slugify(base)
    if not slug:
        if explicit:
            raise ValueError("That web address is empty once accents and "
                             "punctuation are removed.")
        slug = "business"
    if slug in RESERVED_SLUGS:
        if explicit:
            raise ValueError("'{}' is a reserved address".format(slug))
        slug = slug + "-1"
    n, candidate = 1, slug
    while True:
        path = build_path(state_code, branch_slug, candidate)
        taken = db_admin().business_pages.find_one(
            {"$or": [{"path": path}, {"aliases": path}]}, {"_id": 1})
        if not taken:
            return candidate
        if explicit:
            raise ValueError("{} already belongs to another page.".format(path))
        n += 1
        candidate = "{}-{}".format(slug, n)


def build_path(state_code, branch_slug, name_slug) -> str:
    """Every segment is slugified here, not only the ones this module derives:
    admin-typed segments reach this function too, and one stray space or
    apostrophe would mint an address that no browser reproduces byte-for-byte —
    which for a link that lives in chat histories is a broken page forever."""
    parts = [slugify(state_code), slugify(branch_slug), slugify(name_slug)]
    if not all(parts):
        # A short path would still resolve here but not on the public side,
        # which routes /{state}/{branch}/{slug} and nothing else.
        raise ValueError("A web address needs a state, a branch and a name.")
    return norm_path("/{}/{}/{}".format(*parts))


def _page_event(page_id, event, by, changes=None, note=None):
    db_admin().business_page_events.insert_one({
        "id": _seq_admin("business_page_events"), "at": _now(),
        "page_id": page_id, "event": event, "by": by,
        "changes": changes or {}, "note": note,
    })


# fields an editor may set; anything else in a submitted form is ignored
PAGE_EDITABLE = (
    "business_name", "owner_name", "category", "photo_url",
    "locality", "district", "state_name", "established_year", "summary",
    "about", "offerings", "figures", "hours", "languages",
    "phones", "map_url", "indexed", "tier", "tier_status",
)


def create_page(data, by, state_code, branch_slug, name_slug=None):
    """name_slug is the last URL segment an admin TYPED. Leave it None — as
    every existing caller does — and the address is derived from the business
    name exactly as before. Passing one switches the address rules from
    forgiving to strict (see unique_name_slug), so a collision or a reserved
    word comes back as a ValueError the form can show instead of the page
    quietly opening at an address nobody asked for.

    Raises ValueError; it is the caller's job to put that on the form."""
    explicit = bool(name_slug)
    if name_slug:
        name_slug = unique_name_slug(name_slug, state_code, branch_slug, explicit=True)
    else:
        name_slug = unique_name_slug(data.get("business_name"), state_code, branch_slug)
    pid = _seq_admin("business_pages")
    row = {
        "id": pid,
        "state_code": slugify(state_code).upper(),
        "branch_slug": slugify(branch_slug),
        "name_slug": name_slug,
        "path": build_path(state_code, branch_slug, name_slug),
        "aliases": [],
        "status": PAGE_DRAFT,             # nothing publishes by accident
        "indexed": True,
        "created_at": _now(), "updated_at": _now(), "created_by": by,
        "consent": None,
    }
    for k in PAGE_EDITABLE:
        if k in data:
            row[k] = data[k]
    # The unique index on path (ensure_indexes) is the real gate against two
    # admins racing the same address; unique_name_slug is only the fast path.
    # On a collision the derived slug is re-drawn; a typed one is refused —
    # its whole point is that THIS exact link is going somewhere.
    for _ in range(3):
        try:
            db_admin().business_pages.insert_one(row)
            break
        except Exception as exc:                        # noqa: BLE001
            if type(exc).__name__ != "DuplicateKeyError":
                raise
            row.pop("_id", None)
            if explicit:
                raise ValueError("{} already belongs to another page.".format(row["path"]))
            name_slug = unique_name_slug(data.get("business_name"), state_code, branch_slug)
            row["name_slug"] = name_slug
            row["path"] = build_path(state_code, branch_slug, name_slug)
    _page_event(pid, "CREATED", by, {"path": [None, row["path"]]})
    row.pop("_id", None)
    return row


def update_page(page_id, data, by):
    before = db_admin().business_pages.find_one({"id": int(page_id)}, {"_id": 0})
    if not before:
        return None
    changes, patch = {}, {}
    for k in PAGE_EDITABLE:
        if k in data and data[k] != before.get(k):
            changes[k] = [before.get(k), data[k]]
            patch[k] = data[k]
    if not patch:
        return before
    patch["updated_at"] = _now()
    db_admin().business_pages.update_one({"id": int(page_id)}, {"$set": patch})
    _page_event(int(page_id), "EDITED", by, changes)
    return db_admin().business_pages.find_one({"id": int(page_id)}, {"_id": 0})


def set_page_status(page_id, status, by, note=None,
                    consent_method=None, consent_ref=None):
    """publish / remove / restore.

    The PATH IS NEVER TOUCHED here. A removed page keeps its address so the same
    link can be restored to the same business, and so the address is never handed
    to anyone else.

    First publish records consent EVIDENCE, not just an assertion: the admin
    layer requires a method (how the customer agreed) and a reference (where
    the proof lives — form location, message date, who took it verbally) and
    they are stored verbatim. In a dispute, "an admin clicked publish" and
    "signed form of 12 Aug, kept at Vellore branch" are different answers."""
    if status not in PAGE_STATUSES:
        raise ValueError("bad status")
    before = db_admin().business_pages.find_one({"id": int(page_id)}, {"_id": 0})
    if not before:
        return None
    patch = {"status": status, "updated_at": _now()}
    if status == PAGE_LIVE and not before.get("consent"):
        patch["consent"] = {"recorded_by": by, "at": _now(),
                            "method": consent_method or "unrecorded",
                            "ref": consent_ref or None, "note": note}
    db_admin().business_pages.update_one({"id": int(page_id)}, {"$set": patch})
    event = {"live": "PUBLISHED", "removed": "REMOVED", "draft": "UNPUBLISHED"}[status]
    if status == PAGE_LIVE and before.get("status") == PAGE_REMOVED:
        event = "RESTORED"
    _page_event(int(page_id), event, by, {"status": [before.get("status"), status]}, note)
    return db_admin().business_pages.find_one({"id": int(page_id)}, {"_id": 0})


def page_by_id(page_id):
    return db_admin().business_pages.find_one({"id": int(page_id)}, {"_id": 0})


def list_pages(status=None, q=None, limit=500):
    query = {}
    if status:
        query["status"] = status
    if q:
        import re as _re
        rx = _re.compile(_re.escape(str(q)), _re.I)
        query["$or"] = [{"business_name": rx}, {"owner_name": rx}, {"path": rx}]
    return list(db_admin().business_pages.find(query, {"_id": 0})
                .sort("id", -1).limit(int(limit)))


def page_events(page_id, limit=50):
    return list(db_admin().business_page_events.find({"page_id": int(page_id)}, {"_id": 0})
                .sort("id", -1).limit(int(limit)))


# ---- leads ----------------------------------------------------------------
def list_leads(status=None, q=None, limit=500):
    query = {}
    if status:
        query["status"] = status
    if q:
        import re as _re
        rx = _re.compile(_re.escape(str(q)), _re.I)
        query["$or"] = [{"name": rx}, {"mobile": rx}, {"referrer_business_name": rx}]
    return list(db_admin().site_leads.find(query, {"_id": 0}).sort("id", -1).limit(int(limit)))


def lead_by_id(lead_id):
    return db_admin().site_leads.find_one({"id": int(lead_id)}, {"_id": 0})


def update_lead(lead_id, status=None, note=None, branch=None, by=None):
    before = db_admin().site_leads.find_one({"id": int(lead_id)}, {"_id": 0})
    if not before:
        return None
    patch, changes = {}, {}
    if status and status in LEAD_STATUSES and status != before.get("status"):
        patch["status"] = status
        changes["status"] = [before.get("status"), status]
        if status == "CONTACTED" and not before.get("called_at"):
            patch["called_at"] = _now()
            patch["called_by"] = by
        if status == "SENT_TO_BRANCH":
            patch["routed_at"] = _now()
    if note is not None and note != before.get("cs_notes"):
        patch["cs_notes"] = note
        changes["cs_notes"] = ["<redacted>", "<redacted>"]   # notes may carry PII
    if branch is not None and branch != before.get("assigned_branch"):
        patch["assigned_branch"] = branch
        changes["assigned_branch"] = [before.get("assigned_branch"), branch]
    if not patch:
        return before
    db_admin().site_leads.update_one({"id": int(lead_id)}, {"$set": patch})
    db_admin().lead_events.insert_one({
        "id": _seq_admin("lead_events"), "at": _now(), "lead_id": int(lead_id),
        "event": "UPDATED", "by": by, "changes": changes, "note": None})
    return db_admin().site_leads.find_one({"id": int(lead_id)}, {"_id": 0})


def log_lead_export(count, by):
    """Export is audited separately from viewing: a bulk copy of customer phone
    numbers leaving the system is a different act from reading one record."""
    db_admin().lead_events.insert_one({
        "id": _seq_admin("lead_events"), "at": _now(), "lead_id": None,
        "event": "EXPORTED", "by": by, "changes": {"rows": [None, int(count)]},
        "note": None})


# ---- users ----------------------------------------------------------------
USER_ROLES = ("admin", "staff")


def user_by_name(username):
    return db_admin().pbn_users.find_one({"username": str(username or "").lower()}, {"_id": 0})


def user_by_id(user_id):
    return db_admin().pbn_users.find_one({"id": int(user_id)}, {"_id": 0})


def create_user(username, password_hash, role="admin", by=None, must_change=False):
    row = {"id": _seq_admin("pbn_users"), "username": str(username).lower(),
           "password": password_hash, "role": role, "active": True,
           # sv (session version) travels into every cookie at login and is
           # re-checked per request; bumping it revokes all existing sessions.
           "sv": 1, "must_change": bool(must_change),
           "created_at": _now(), "created_by": by}
    db_admin().pbn_users.insert_one(row)
    row.pop("_id", None)
    return row


def update_user(user_id, by=None, bump_sv=False, **fields):
    """Patch role/active/password/must_change. bump_sv=True kills every live
    session for the user (suspension, password change, role change)."""
    allowed = {k: v for k, v in fields.items()
               if k in ("role", "active", "password", "must_change")}
    before = db_admin().pbn_users.find_one({"id": int(user_id)}, {"_id": 0})
    if not before:
        return None
    if bump_sv:
        allowed["sv"] = int(before.get("sv", 1)) + 1
    if allowed:
        db_admin().pbn_users.update_one({"id": int(user_id)}, {"$set": allowed})
    return db_admin().pbn_users.find_one({"id": int(user_id)}, {"_id": 0})


def list_users():
    return list(db_admin().pbn_users.find({}, {"_id": 0, "password": 0}).sort("id", 1))


# ---- page views (attribution) ----------------------------------------------
def count_view(page_id, day=None):
    """One daily counter per page — enough to tell a customer 'your page was
    seen N times' and to see whether sharing works at all. No cookies, no UA,
    no per-visitor anything: it is a tally, not tracking."""
    day = day or datetime.now(IST).strftime("%Y-%m-%d")
    key = "pv:{}:{}".format(int(page_id), day)
    db_rw().counters.update_one(
        {"_id": key},
        {"$inc": {"v": 1},
         "$setOnInsert": {"expireAt": datetime.now(IST) + timedelta(days=60)}},
        upsert=True)


def views_for(page_id, days=14) -> int:
    keys = ["pv:{}:{}".format(int(page_id),
            (datetime.now(IST) - timedelta(days=d)).strftime("%Y-%m-%d"))
            for d in range(int(days))]
    total = 0
    for row in db_admin().counters.find({"_id": {"$in": keys}}, {"v": 1}):
        total += int(row.get("v", 0))
    return total


def ensure_indexes():
    """Idempotent; run at startup on Mongo deployments. The unique path index
    is a data-integrity guarantee the application loop can only approximate;
    the TTL index reaps daily quota/view counters via their expireAt."""
    from pymongo import ASCENDING
    db = db_admin()
    db.business_pages.create_index([("id", ASCENDING)], unique=True)
    db.business_pages.create_index([("path", ASCENDING)], unique=True)
    db.business_pages.create_index([("aliases", ASCENDING)])
    db.site_leads.create_index([("id", ASCENDING)], unique=True)
    db.pbn_users.create_index([("username", ASCENDING)], unique=True)
    db.page_reports.create_index([("id", ASCENDING)], unique=True)
    db.business_page_events.create_index([("page_id", ASCENDING)])
    db.lead_events.create_index([("lead_id", ASCENDING)])
    db.counters.create_index("expireAt", expireAfterSeconds=0)


# ---- page reports (public takedown / correction intake) --------------------
# Takedown-on-request is the legal basis for publishing without prior consent,
# so /report must actually reach someone. Rows are inserted by the PUBLIC
# process (the RW credential needs insert on page_reports) and worked in the
# back-office.
REPORT_STATUSES = ("OPEN", "DONE")
REPORT_TYPES = ("remove", "correct", "other")


def insert_report(page_path, request_type, details, contact, ip_hash):
    """page_path is normalised for matching but kept even when nothing matches —
    a reporter may describe a page that was already taken down, or paste a
    mangled link. The report is the signal; resolution happens in the admin."""
    rid = _seq("page_reports")
    row = {
        "id": rid, "at": _now(),
        "page_path": norm_path(page_path) if str(page_path or "").strip().startswith("/") else (str(page_path or "").strip()[:200] or None),
        "request_type": request_type if request_type in REPORT_TYPES else "other",
        "details": str(details or "").strip()[:1000],
        "contact": str(contact or "").strip()[:200] or None,
        "status": "OPEN",
        "handled_by": None, "handled_at": None, "handled_note": None,
        "ip_hash": ip_hash,
    }
    db_rw().page_reports.insert_one(row)
    row.pop("_id", None)
    return row


def list_reports(status=None, limit=500):
    query = {"status": status} if status else {}
    return list(db_admin().page_reports.find(query, {"_id": 0})
                .sort("id", -1).limit(int(limit)))


def open_report_count():
    return db_admin().page_reports.count_documents({"status": "OPEN"})


def update_report(report_id, status, by, note=None):
    if status not in REPORT_STATUSES:
        raise ValueError("bad status")
    patch = {"status": status}
    if status == "DONE":
        patch["handled_by"] = by
        patch["handled_at"] = _now()
    if note is not None and str(note).strip():
        patch["handled_note"] = str(note).strip()[:500]
    db_admin().page_reports.update_one({"id": int(report_id)}, {"$set": patch})
    return db_admin().page_reports.find_one({"id": int(report_id)}, {"_id": 0})


# ---- uploaded photos (database-backed) --------------------------------------
# On ephemeral hosts (Render/serverless) a disk upload dies with the container
# — Murugan Stores' photo lasted exactly one redeploy. Content-addressed rows
# in Mongo survive every deploy; ~250KB per photo against Atlas's free 512MB
# is thousands of photos. Served by GET /photo/<id>.<ext> with immutable
# caching (the id IS the content hash).
# NOTE for the scoped-users setup: the RO role needs `find` on pbn.photos.
def save_photo(photo_id, ext, data, by=None):
    from bson import Binary
    db_admin().photos.update_one(
        {"_id": str(photo_id)},
        {"$setOnInsert": {"ext": str(ext), "data": Binary(bytes(data)),
                          "size": len(data), "at": _now(), "by": by}},
        upsert=True)


def get_photo(photo_id):
    return db_ro().photos.find_one({"_id": str(photo_id)})


# ---- boot checks ------------------------------------------------------------
SHERLOCK_DB = "dpd_early_warning"


def boot_checks():
    """Run at startup by main.py for production Mongo deployments (preview
    builds and the file store skip it). Refuses to start rather than run in a
    shape that silently loses a security property; the message names the exact
    env var to fix. A Mongo that is down raises its own error here — a service
    that cannot reach its store should not come up either."""
    problems = []
    if MONGO_DB == SHERLOCK_DB:
        problems.append(
            "PBN_MONGO_DB is '{}' — Sherlock's database. PBN must use its own; "
            "set PBN_MONGO_DB (e.g. 'pbn').".format(SHERLOCK_DB))
    if os.getenv("PBN_ALLOW_SHARED_DB_USER", "").lower() in ("1", "true", "yes"):
        # Sanctioned, SHOUTED bypass for free-tier pilots that have not yet
        # created the three scoped Atlas users. The guard's job is that the
        # property is never lost SILENTLY — this is explicit and logged, and
        # the scoped users remain the bar for real customer volume.
        import logging
        logging.getLogger("pbn").warning(
            "PBN_ALLOW_SHARED_DB_USER is set: running with ONE shared DB user. "
            "A flaw in the public request path can read customer records. "
            "Create the scoped RO/RW users before real customer volume.")
        if problems:
            raise RuntimeError("PBN refuses to start:\n  - " + "\n  - ".join(problems))
        return
    if not (os.getenv("PBN_MONGO_URI_RO") and os.getenv("PBN_MONGO_URI_RW")
            and os.getenv("PBN_MONGO_URI_ADMIN")):
        problems.append(
            "PBN_MONGO_URI_RO / _RW / _ADMIN must all be set explicitly in "
            "production — the single-URI fallback is for local development only.")
    elif len({URI_RO, URI_RW, URI_ADMIN}) != 3:
        problems.append(
            "PBN_MONGO_URI_RO / _RW / _ADMIN must be three DIFFERENT users — "
            "the point is that the anonymous request path cannot read customer "
            "records, and a shared user erases that property silently.")
    else:
        # The write user must NOT be able to read the lead book. An
        # OperationFailure here is the GOOD outcome.
        try:
            db_rw().site_leads.find_one({}, {"_id": 1})
            problems.append(
                "The PBN_MONGO_URI_RW user CAN READ site_leads. It must be "
                "insert-only on leads/reports/counters; fix its role in Atlas.")
        except Exception as exc:                        # noqa: BLE001
            if type(exc).__name__ != "OperationFailure":
                raise                                   # Mongo unreachable etc.
    if problems:
        raise RuntimeError("PBN refuses to start:\n  - " + "\n  - ".join(problems))
