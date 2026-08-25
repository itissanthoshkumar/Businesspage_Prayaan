"""Durable JSON-file store — the same surface as store.py, without MongoDB.

WHY THIS EXISTS
    run_preview.py used to monkeypatch store.* with a dict held in memory, so
    every page an admin created died with the process. Demoing the back-office
    then meant re-typing the customer before every walkthrough. This module is
    the same double, except it writes to disk, so a restart keeps what was
    created and does NOT resurrect what was removed.

WHAT IT IS NOT
    Not a database. One process, one directory, whole-file rewrites. That is
    the right shape for a preview/demo deployment of a few hundred pages and it
    is honestly all this needs; the moment there is a real MongoDB, store.py is
    already the implementation and this file simply stops being installed.

DURABILITY
    Every mutation rewrites the affected file completely, into a temp file in
    the SAME directory, fsync'd, then os.replace()'d over the target. os.replace
    is atomic within a filesystem, so a reader (or the next boot) sees either
    the whole old file or the whole new one — never a truncated one. A
    half-written pages.json would take the service down on boot, which is the
    failure this pattern exists to rule out. The directory is fsync'd after the
    rename so the rename itself survives a power loss, not just the bytes.

CONCURRENCY
    uvicorn runs sync route handlers in a threadpool, so two admins can save at
    the same instant. Every read-modify-write below holds a single re-entrant
    lock, so the second save reads the first one's result instead of stomping
    it.

DEPENDENCIES
    Standard library only: json, os, tempfile, threading, datetime. Ordering,
    slugging and path normalisation are reused from store.py (pure functions,
    no database), and password hashing from auth.py.

LAYOUT   $PBN_DATA_DIR (default /tmp/pbn-data)
    pages.json     {"v":1,"rows":[<business page>, ...]}
    leads.json     {"v":1,"rows":[<lead>, ...]}
    events.json    {"v":1,"page_events":[...],"lead_events":[...]}
    users.json     {"v":1,"rows":[<user incl. password hash>, ...]}
    counters.json  {"v":1,"counters":{"business_pages":7,"lead_quota:...":3}}
"""
import json
import os
import tempfile
import threading
from datetime import datetime, timedelta, timezone

import auth
import store

# Project convention: everything is stored and compared in IST. No UTC anywhere.
IST = timezone(timedelta(hours=5, minutes=30))

FORMAT_VERSION = 1
DEFAULT_DATA_DIR = "/tmp/pbn-data"
DATA_DIR = os.getenv("PBN_DATA_DIR", DEFAULT_DATA_DIR)

_FILENAMES = {
    "pages": "pages.json",
    "leads": "leads.json",
    "events": "events.json",
    "users": "users.json",
    "counters": "counters.json",
    "reports": "reports.json",
}

# store.hash_ip is captured BEFORE install() replaces it, so the delegation
# below can never recurse into itself.
_hash_ip_impl = store.hash_ip

_LOCK = threading.RLock()               # re-entrant: mutations call _next_id()

_PAGES = []
_LEADS = []
_PAGE_EVENTS = []
_LEAD_EVENTS = []
_USERS = []
_REPORTS = []
_COUNTERS = {}
_BY_PATH = {}
_BY_ALIAS = {}
_READY = False


# ---------------------------------------------------------------------------
# demo fixtures — moved here from run_preview.py so seeding owns them
# ---------------------------------------------------------------------------
DEMO_PAGES = [
    {
        "id": 1, "tier": "gold", "tier_status": "Active", "path": "/TN/vellore/santhosh-enterprise",
        "aliases": ["/TN/ranipet/santhosh-enterprise"], "status": "live", "indexed": True,
        "phones": ["+91 98400 12345", "+91 416 227 8890"],
        "map_url": "https://maps.google.com/?q=Katpadi,+Vellore,+Tamil+Nadu",
        "business_name": "Santhosh Enterprise", "owner_name": "S. Santhosh",
        "category": "Hardware & Building Materials",
        "locality": "Katpadi", "district": "Vellore", "state_name": "Tamil Nadu",
        "established_year": 2013, "photo_url": "/static/img/biz-hardware.jpg",
        "summary": "Hardware and building materials shop in Katpadi, Vellore — pipes, "
                   "paints, fittings and tools for contractors and households since 2013.",
        "about": [
            "Santhosh Enterprise has served builders, plumbers and households around Katpadi "
            "for more than a decade. The shop stocks PVC and GI pipes, paints, sanitary "
            "fittings, hand tools and fasteners, with most fast-moving items available off "
            "the shelf.",
            "The team handles small contractor orders the same day and delivers within "
            "Katpadi and the nearby streets. Regular customers include local masons, "
            "electricians and apartment maintenance teams who place standing orders every "
            "month.",
        ],
        "offerings": ["PVC & GI pipes", "Paints and primers", "Sanitary fittings",
                      "Hand tools", "Fasteners & hardware", "Contractor supply"],
        "figures": [{"label": "Years in business", "value": "12"},
                    {"label": "People employed", "value": "8"},
                    {"label": "Regular customers", "value": "300+"}],
        "hours": "Mon–Sat, 8:30 am – 8:30 pm", "languages": ["Tamil", "English"],
        "updated_at": "2026-08-01 10:00:00",
    },
    {
        "id": 2, "tier": "bronze", "tier_status": "Active", "path": "/TN/vellore/amman-garments", "aliases": [],
        "status": "live", "indexed": True,
        "phones": ["+91 90031 44520"],
        "map_url": "https://maps.google.com/?q=Gudiyatham,+Vellore,+Tamil+Nadu",
        "business_name": "Amman Garments", "owner_name": "R. Kalaiselvi",
        "category": "Garment Manufacturing",
        "locality": "Gudiyatham", "district": "Vellore", "state_name": "Tamil Nadu",
        "established_year": 2016, "photo_url": "/static/img/biz-garments.jpg",
        "summary": "Garment stitching unit in Gudiyatham, Vellore — school uniforms, "
                   "innerwear and job-work stitching for wholesale buyers.",
        "about": [
            "Amman Garments runs a stitching floor of eighteen machines in Gudiyatham, "
            "taking job-work from wholesalers in Vellore, Ambur and Chennai. The unit "
            "specialises in school uniforms and cotton innerwear.",
            "The owner trains women from the surrounding streets on the machines, and most "
            "of the team has been with the unit since it opened. Orders are quoted per piece "
            "with sample approval before the run begins.",
        ],
        "offerings": ["School uniforms", "Cotton innerwear", "Job-work stitching",
                      "Bulk cutting", "Sample development"],
        "figures": [{"label": "Machines on floor", "value": "18"},
                    {"label": "People employed", "value": "22"},
                    {"label": "Pieces per month", "value": "14,000"}],
        "hours": "Mon–Sat, 9:00 am – 6:00 pm", "languages": ["Tamil"],
        "updated_at": "2026-08-02 10:00:00",
    },
    {
        "id": 3, "tier": "gold", "tier_status": "Active", "path": "/TN/coimbatore/sri-lakshmi-traders", "aliases": [],
        "status": "live", "indexed": True,
        "phones": ["+91 94430 11278", "+91 422 268 4410"],
        "map_url": "https://maps.google.com/?q=Sulur,+Coimbatore,+Tamil+Nadu",
        "business_name": "Sri Lakshmi Traders", "owner_name": "M. Ramesh",
        "category": "Wholesale Provisions",
        "locality": "Sulur", "district": "Coimbatore", "state_name": "Tamil Nadu",
        "established_year": 2008, "photo_url": "/static/img/biz-traders.jpg",
        "summary": "Wholesale provisions trader in Sulur, Coimbatore — rice, pulses, "
                   "oils and grocery supply to retail shops and messes.",
        "about": [
            "Sri Lakshmi Traders supplies rice, pulses, edible oils and packaged groceries to "
            "retail shops, messes and small caterers across Sulur and the Avinashi road belt.",
            "The business buys directly from mills in Erode and Tiruchirappalli and moves "
            "stock on its own tempo. Retailers on a weekly route get a fixed delivery day, "
            "which keeps their working capital cycle predictable.",
        ],
        "offerings": ["Rice & pulses", "Edible oils", "Packaged groceries",
                      "Route delivery", "Mess & catering supply"],
        "figures": [{"label": "Years in business", "value": "17"},
                    {"label": "Retail shops served", "value": "120"},
                    {"label": "Delivery vehicles", "value": "3"}],
        "hours": "Mon–Sat, 7:00 am – 7:00 pm", "languages": ["Tamil", "English"],
        "updated_at": "2026-08-03 10:00:00",
    },
    {
        "id": 4, "tier": "silver", "tier_status": "Active", "path": "/TN/madurai/kpm-engineering-works", "aliases": [],
        "status": "live", "indexed": True,
        "phones": ["+91 98942 60117", "+91 452 248 9903"],
        "map_url": "https://maps.google.com/?q=Thirupparankundram,+Madurai,+Tamil+Nadu",
        "business_name": "KPM Engineering Works", "owner_name": "K. Pandiaraj",
        "category": "Precision Engineering & Fabrication",
        "locality": "Thirupparankundram", "district": "Madurai", "state_name": "Tamil Nadu",
        "established_year": 2011, "photo_url": "/static/img/biz-workshop.jpg",
        "summary": "Engineering workshop in Thirupparankundram, Madurai — lathe turning, "
                   "fabrication and machined components for pump and auto units.",
        "about": [
            "KPM Engineering Works machines components for pump assemblers and automobile "
            "workshops around Madurai. The shed runs four lathes, a milling machine and a "
            "fabrication bay.",
            "The workshop takes drawings from customers and turns samples within two days, "
            "with repeat batch work quoted per component. Most customers have worked with the "
            "owner since he ran a single lathe in 2011.",
        ],
        "offerings": ["Lathe turning", "Milling", "Sheet fabrication",
                      "Pump components", "Sample & batch work"],
        "figures": [{"label": "Years in business", "value": "14"},
                    {"label": "People employed", "value": "11"},
                    {"label": "Machines in use", "value": "6"}],
        "hours": "Mon–Sat, 9:00 am – 7:00 pm", "languages": ["Tamil", "English"],
        "updated_at": "2026-08-04 10:00:00",
    },
]

PREVIEW_ADMIN_USER = "admin"
PREVIEW_ADMIN_PASSWORD = "prayaan"


# ---------------------------------------------------------------------------
# disk primitives
# ---------------------------------------------------------------------------
def _path_for(name) -> str:
    return os.path.join(DATA_DIR, _FILENAMES[name])


def _now() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")


def _today() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d")


def _clone(value):
    """Hand callers their own copy.

    The Mongo implementation returns freshly decoded documents, so a caller that
    mutates a returned row cannot corrupt the store. A dict-backed double that
    returned live references would quietly differ, and the difference would only
    surface in production. json round-trip is also a free assertion that every
    row we hold is serialisable."""
    if value is None:
        return None
    return json.loads(json.dumps(value))


def _fsync_dir(directory):
    """The rename is only durable once the DIRECTORY entry is flushed."""
    try:
        fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write(path, payload):
    """Temp file in the same directory, fsync, os.replace. Never a partial file."""
    directory = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".pbn-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)                    # atomic within the filesystem
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(directory)


def _read_file(name, default):
    path = _path_for(name)
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default
    except ValueError as exc:
        # Atomic writes make this close to impossible. If it happens anyway,
        # say so loudly: silently starting empty would erase real customer
        # records and nobody would notice until a page 404'd.
        raise RuntimeError(
            "{} is not valid JSON ({}). Move it aside to start fresh; do not "
            "delete it if it holds real data.".format(path, exc))


def _payload(name):
    if name == "pages":
        return {"v": FORMAT_VERSION, "rows": _PAGES}
    if name == "leads":
        return {"v": FORMAT_VERSION, "rows": _LEADS}
    if name == "events":
        return {"v": FORMAT_VERSION,
                "page_events": _PAGE_EVENTS, "lead_events": _LEAD_EVENTS}
    if name == "users":
        return {"v": FORMAT_VERSION, "rows": _USERS}
    if name == "counters":
        return {"v": FORMAT_VERSION, "counters": _COUNTERS}
    if name == "reports":
        return {"v": FORMAT_VERSION, "rows": _REPORTS}
    raise KeyError(name)


def _save(*names):
    """Caller MUST hold _LOCK."""
    for name in names:
        _atomic_write(_path_for(name), _payload(name))


def _next_id(counter):
    with _LOCK:
        value = int(_COUNTERS.get(counter, 0)) + 1
        _COUNTERS[counter] = value
        return value


# ---------------------------------------------------------------------------
# boot: load, seed, index
# ---------------------------------------------------------------------------
def _reindex():
    _BY_PATH.clear()
    _BY_ALIAS.clear()
    for row in _PAGES:
        _BY_PATH[row["path"]] = row
        for alias in row.get("aliases") or []:
            _BY_ALIAS[store.norm_path(alias)] = row


def _seed_pages():
    """Only ever called when pages.json is ABSENT. A removed demo page must
    stay removed across restarts, and an edited one must keep its edits."""
    highest_page, highest_event = 0, 0
    for demo in DEMO_PAGES:
        row = _clone(demo)
        parts = [p for p in row["path"].split("/") if p]
        row.setdefault("state_code", parts[0].upper())
        row.setdefault("branch_slug", parts[1])
        row.setdefault("name_slug", parts[2])
        row.setdefault("created_at", row.get("updated_at") or _now())
        row.setdefault("created_by", "seed")
        row.setdefault("consent", None)          # fixtures assert no consent
        _PAGES.append(row)
        highest_page = max(highest_page, int(row["id"]))
        highest_event += 1
        _PAGE_EVENTS.append({
            "id": highest_event, "at": row["created_at"], "page_id": row["id"],
            "event": "CREATED", "by": "seed",
            "changes": {"path": [None, row["path"]]}, "note": "demo fixture",
        })
    _COUNTERS["business_pages"] = max(int(_COUNTERS.get("business_pages", 0)), highest_page)
    _COUNTERS["business_page_events"] = max(
        int(_COUNTERS.get("business_page_events", 0)), highest_event)


def _seed_admin_user():
    """Only ever called when users.json is ABSENT."""
    _USERS.append({
        "id": _next_id("pbn_users"),
        "username": PREVIEW_ADMIN_USER,
        "password": auth.hash_password(PREVIEW_ADMIN_PASSWORD),
        "role": "admin", "active": True,
        "created_at": _now(), "created_by": "seed",
    })


def _prune_quota(today):
    """Daily lead-quota keys would otherwise accumulate one row per IP per day
    forever. Today's survive, so a restart does not hand a flooder a fresh
    allowance. Page-view counters keep a 60-day window (the admin shows 14)."""
    cutoff = (datetime.now(IST) - timedelta(days=60)).strftime("%Y-%m-%d")
    stale = [k for k in _COUNTERS
             if k.startswith("lead_quota:") and k.rsplit(":", 1)[-1] != today]
    stale += [k for k in _COUNTERS
              if k.startswith("pv:") and k.rsplit(":", 1)[-1] < cutoff]
    for key in stale:
        _COUNTERS.pop(key, None)
    return bool(stale)


def init(data_dir=None):
    """Load the directory into memory, seeding anything not there yet."""
    global DATA_DIR, _READY
    with _LOCK:
        DATA_DIR = data_dir or os.getenv("PBN_DATA_DIR", DEFAULT_DATA_DIR)
        os.makedirs(DATA_DIR, exist_ok=True)

        had_pages = os.path.exists(_path_for("pages"))
        had_users = os.path.exists(_path_for("users"))

        for bucket in (_PAGES, _LEADS, _PAGE_EVENTS, _LEAD_EVENTS, _USERS, _REPORTS):
            del bucket[:]
        _COUNTERS.clear()

        _COUNTERS.update(_read_file("counters", {}).get("counters", {}))
        _PAGES.extend(_read_file("pages", {}).get("rows", []))
        _LEADS.extend(_read_file("leads", {}).get("rows", []))
        events = _read_file("events", {})
        _PAGE_EVENTS.extend(events.get("page_events", []))
        _LEAD_EVENTS.extend(events.get("lead_events", []))
        _USERS.extend(_read_file("users", {}).get("rows", []))
        _REPORTS.extend(_read_file("reports", {}).get("rows", []))

        dirty = set()
        if not had_pages:
            _seed_pages()
            dirty.update(("pages", "events", "counters"))
        if not had_users:
            _seed_admin_user()
            dirty.update(("users", "counters"))
        if _prune_quota(_today()):
            dirty.add("counters")
        # Materialise every file on first boot, so the directory shows its whole
        # shape rather than growing a leads.json only once someone submits one.
        dirty.update(n for n in _FILENAMES if not os.path.exists(_path_for(n)))

        _reindex()
        if dirty:
            _save(*sorted(dirty))
        _READY = True
    return DATA_DIR


def stats():
    """For the launcher banner — nothing in the app depends on this."""
    with _LOCK:
        return {"dir": DATA_DIR, "pages": len(_PAGES),
                "live": len([p for p in _PAGES if p.get("status") == store.PAGE_LIVE]),
                "leads": len(_LEADS), "users": len(_USERS)}


# ---------------------------------------------------------------------------
# public read/insert half  (mirrors the top of store.py)
# ---------------------------------------------------------------------------
def page_by_path(path):
    """(page, canonical_path_or_None) — second value set when an alias matched."""
    p = store.norm_path(path)
    with _LOCK:
        row = _BY_PATH.get(p)
        if row:
            return _clone(row), None
        row = _BY_ALIAS.get(p)
        if row:
            return _clone(row), row.get("path")
    return None, None


LIVE_PROJECTION = ("path", "updated_at", "indexed",
                   "business_name", "locality", "district", "category",
                   "photo_url", "owner_name", "tier", "tier_status")


def live_pages(limit=50000):
    with _LOCK:
        rows = sorted((r for r in _PAGES if r.get("status") == store.PAGE_LIVE),
                      key=lambda r: int(r.get("id") or 0))[:int(limit)]
        out = []
        for r in rows:
            projected = {k: r.get(k) for k in LIVE_PROJECTION}
            projected["indexed"] = r.get("indexed", True)
            out.append(projected)
        return out


def reserve_lead_slot(cap, scope="global", day=None) -> bool:
    """Conditional increment under the lock — the file-store equivalent of the
    single-round-trip Mongo counter. A check-then-insert would be a spam
    amplifier under concurrency."""
    key = "lead_quota:{}:{}".format(scope, day or _today())
    with _LOCK:
        used = int(_COUNTERS.get(key, 0))
        if used >= int(cap):
            return False
        _COUNTERS[key] = used + 1
        _save("counters")
        return True


def hash_ip(ip, salt=None) -> str:
    """Same daily-rotating salted hash the real store uses — the raw IP is never
    stored. Delegates to the implementation captured before install()."""
    return _hash_ip_impl(ip, salt)


def insert_lead(name, mobile, pincode, source_path, consent_version, ip_hash,
                via=None):
    page, _ = page_by_path(source_path) if source_path else (None, None)
    with _LOCK:
        lid = _next_id("site_leads")
        row = {
            "id": lid, "at": _now(),
            "name": str(name or "").strip()[:120],
            "mobile": str(mobile or "").strip()[:15],
            "pincode": str(pincode or "").strip()[:6],
            "source_path": store.norm_path(source_path) if source_path else None,
            "source_page_id": (page or {}).get("id"),
            "referrer_business_name": (page or {}).get("business_name"),
            "via": via,
            "status": "NEW",
            "assigned_branch": None, "routed_at": None,
            "cs_notes": None, "called_by": None, "called_at": None,
            "consent_version": consent_version, "ip_hash": ip_hash,
        }
        _LEADS.append(row)
        _LEAD_EVENTS.append({
            "id": _next_id("lead_events"), "at": row["at"], "lead_id": lid,
            "event": "RECEIVED", "by": None,
            "changes": {"source_path": [None, row["source_path"]]}, "note": None,
        })
        _save("leads", "events", "counters")
        return _clone(row)


# ---------------------------------------------------------------------------
# admin half  (mirrors the ADMIN DATA LAYER section of store.py)
# ---------------------------------------------------------------------------
def _page_event(page_id, event, by, changes=None, note=None):
    """Caller MUST hold _LOCK; the caller also saves."""
    _PAGE_EVENTS.append({
        "id": _next_id("business_page_events"), "at": _now(),
        "page_id": page_id, "event": event, "by": by,
        "changes": changes or {}, "note": note,
    })


def _unique_name_slug(base, state_code, branch_slug, explicit=False):
    """Never reassigns an address that has ever been used — including one held
    by a removed page, and including an alias. A link already shared in a
    WhatsApp thread must not one day open a different business."""
    slug = store.slugify(base) or "business"
    if slug in store.RESERVED_SLUGS:
        if explicit:
            raise ValueError("'{}' is a reserved address".format(slug))
        slug = slug + "-1"
    n, candidate = 1, slug
    while True:
        path = store.build_path(state_code, branch_slug, candidate)
        if path not in _BY_PATH and path not in _BY_ALIAS:
            return candidate
        n += 1
        candidate = "{}-{}".format(slug, n)


def _find_page(page_id):
    pid = int(page_id)
    return next((r for r in _PAGES if int(r.get("id")) == pid), None)


def create_page(data, by, state_code, branch_slug, name_slug=None):
    """name_slug, when given, is a slug the admin CHOSE — it is honoured exactly
    or refused (ValueError), never silently adjusted to a free one. Parity with
    store.create_page; without it every create that sends a slug would fail on
    this backend."""
    with _LOCK:
        if name_slug:
            wanted = store.slugify(name_slug)
            if not wanted:
                raise ValueError("That web address is empty once cleaned up.")
            if wanted in store.RESERVED_SLUGS:
                raise ValueError("'{}' is a reserved address".format(wanted))
            path = store.build_path(state_code, branch_slug, wanted)
            if path in _BY_PATH or path in _BY_ALIAS:
                raise ValueError("The address {} is already taken by another "
                                 "page.".format(path))
            name_slug = wanted
        else:
            name_slug = _unique_name_slug(data.get("business_name"), state_code, branch_slug)

        pid = _next_id("business_pages")
        now = _now()
        row = {
            "id": pid,
            "state_code": str(state_code).upper(),
            "branch_slug": store.slugify(branch_slug),
            "name_slug": name_slug,
            "path": store.build_path(state_code, branch_slug, name_slug),
            "aliases": [],
            "status": store.PAGE_DRAFT,        # nothing publishes by accident
            "indexed": True,
            "created_at": now, "updated_at": now, "created_by": by,
            "consent": None,
        }
        for k in store.PAGE_EDITABLE:
            if k in data:
                row[k] = data[k]
        _PAGES.append(row)
        _reindex()
        _page_event(pid, "CREATED", by, {"path": [None, row["path"]]})
        _save("pages", "events", "counters")
        return _clone(row)


def update_page(page_id, data, by):
    with _LOCK:
        row = _find_page(page_id)
        if not row:
            return None
        changes = {}
        for k in store.PAGE_EDITABLE:
            if k in data and data[k] != row.get(k):
                changes[k] = [row.get(k), data[k]]
                row[k] = data[k]
        if not changes:
            return _clone(row)
        row["updated_at"] = _now()
        _reindex()
        _page_event(int(page_id), "EDITED", by, changes)
        _save("pages", "events", "counters")
        return _clone(row)


def set_page_status(page_id, status, by, note=None,
                    consent_method=None, consent_ref=None):
    """publish / remove / restore. THE PATH IS NEVER TOUCHED. First publish
    records consent evidence (method + reference) exactly like store.py."""
    if status not in store.PAGE_STATUSES:
        raise ValueError("bad status")
    with _LOCK:
        row = _find_page(page_id)
        if not row:
            return None
        was = row.get("status")
        row["status"] = status
        row["updated_at"] = _now()
        if status == store.PAGE_LIVE and not row.get("consent"):
            row["consent"] = {"recorded_by": by, "at": _now(),
                              "method": consent_method or "unrecorded",
                              "ref": consent_ref or None, "note": note}
        event = {"live": "PUBLISHED", "removed": "REMOVED", "draft": "UNPUBLISHED"}[status]
        if status == store.PAGE_LIVE and was == store.PAGE_REMOVED:
            event = "RESTORED"
        _page_event(int(page_id), event, by, {"status": [was, status]}, note)
        _save("pages", "events", "counters")
        return _clone(row)


def page_by_id(page_id):
    with _LOCK:
        return _clone(_find_page(page_id))


def _matches(row, q, fields):
    """Case-insensitive substring — exactly what the Mongo side's re.escape'd
    case-insensitive regex does, without needing re."""
    needle = str(q).lower()
    return any(needle in str(row.get(f) or "").lower() for f in fields)


def list_pages(status=None, q=None, limit=500):
    with _LOCK:
        rows = list(_PAGES)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if q:
            rows = [r for r in rows if _matches(r, q, ("business_name", "owner_name", "path"))]
        rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        return _clone(rows[:int(limit)])


def page_events(page_id, limit=50):
    pid = int(page_id)
    with _LOCK:
        rows = [e for e in _PAGE_EVENTS if int(e.get("page_id") or 0) == pid]
        rows.sort(key=lambda e: int(e.get("id") or 0), reverse=True)
        return _clone(rows[:int(limit)])


# ---- leads ----------------------------------------------------------------
def _find_lead(lead_id):
    lid = int(lead_id)
    return next((r for r in _LEADS if int(r.get("id")) == lid), None)


def list_leads(status=None, q=None, limit=500):
    with _LOCK:
        rows = list(_LEADS)
        if status:
            rows = [r for r in rows if r.get("status") == status]
        if q:
            rows = [r for r in rows if _matches(r, q, ("name", "mobile", "referrer_business_name"))]
        rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        return _clone(rows[:int(limit)])


def lead_by_id(lead_id):
    with _LOCK:
        return _clone(_find_lead(lead_id))


def update_lead(lead_id, status=None, note=None, branch=None, by=None):
    with _LOCK:
        row = _find_lead(lead_id)
        if not row:
            return None
        patch, changes = {}, {}
        if status and status in store.LEAD_STATUSES and status != row.get("status"):
            patch["status"] = status
            changes["status"] = [row.get("status"), status]
            if status == "CONTACTED" and not row.get("called_at"):
                patch["called_at"] = _now()
                patch["called_by"] = by
            if status == "SENT_TO_BRANCH":
                patch["routed_at"] = _now()
        if note is not None and note != row.get("cs_notes"):
            patch["cs_notes"] = note
            changes["cs_notes"] = ["<redacted>", "<redacted>"]   # notes may carry PII
        if branch is not None and branch != row.get("assigned_branch"):
            patch["assigned_branch"] = branch
            changes["assigned_branch"] = [row.get("assigned_branch"), branch]
        if not patch:
            return _clone(row)
        row.update(patch)
        _LEAD_EVENTS.append({
            "id": _next_id("lead_events"), "at": _now(), "lead_id": int(lead_id),
            "event": "UPDATED", "by": by, "changes": changes, "note": None})
        _save("leads", "events", "counters")
        return _clone(row)


def log_lead_export(count, by):
    """A bulk copy of customer phone numbers leaving the system is a different
    act from reading one record, so it is audited on its own."""
    with _LOCK:
        _LEAD_EVENTS.append({
            "id": _next_id("lead_events"), "at": _now(), "lead_id": None,
            "event": "EXPORTED", "by": by, "changes": {"rows": [None, int(count)]},
            "note": None})
        _save("events", "counters")


# ---- users ----------------------------------------------------------------
def user_by_name(username):
    name = str(username or "").lower()
    with _LOCK:
        return _clone(next((u for u in _USERS if u.get("username") == name), None))


def user_by_id(user_id):
    uid = int(user_id)
    with _LOCK:
        return _clone(next((u for u in _USERS if int(u.get("id")) == uid), None))


def create_user(username, password_hash, role="admin", by=None, must_change=False):
    with _LOCK:
        row = {"id": _next_id("pbn_users"), "username": str(username).lower(),
               "password": password_hash, "role": role, "active": True,
               "sv": 1, "must_change": bool(must_change),
               "created_at": _now(), "created_by": by}
        _USERS.append(row)
        _save("users", "counters")
        return _clone(row)


def update_user(user_id, by=None, bump_sv=False, **fields):
    allowed = {k: v for k, v in fields.items()
               if k in ("role", "active", "password", "must_change")}
    with _LOCK:
        uid = int(user_id)
        row = next((u for u in _USERS if int(u.get("id")) == uid), None)
        if not row:
            return None
        if bump_sv:
            allowed["sv"] = int(row.get("sv", 1)) + 1
        row.update(allowed)
        _save("users")
        return _clone(row)


def list_users():
    with _LOCK:
        rows = sorted(_USERS, key=lambda u: int(u.get("id") or 0))
        return [{k: v for k, v in _clone(u).items() if k != "password"} for u in rows]


# ---- page views (attribution) ----------------------------------------------
def count_view(page_id, day=None):
    key = "pv:{}:{}".format(int(page_id), day or _today())
    with _LOCK:
        _COUNTERS[key] = int(_COUNTERS.get(key, 0)) + 1
        _save("counters")


def views_for(page_id, days=14) -> int:
    with _LOCK:
        total = 0
        for d in range(int(days)):
            day = (datetime.now(IST) - timedelta(days=d)).strftime("%Y-%m-%d")
            total += int(_COUNTERS.get("pv:{}:{}".format(int(page_id), day), 0))
        return total


# ---- page reports (takedown / correction intake) ---------------------------
def insert_report(page_path, request_type, details, contact, ip_hash):
    with _LOCK:
        rid = _next_id("page_reports")
        cleaned = str(page_path or "").strip()
        row = {
            "id": rid, "at": _now(),
            "page_path": store.norm_path(cleaned) if cleaned.startswith("/") else (cleaned[:200] or None),
            "request_type": request_type if request_type in store.REPORT_TYPES else "other",
            "details": str(details or "").strip()[:1000],
            "contact": str(contact or "").strip()[:200] or None,
            "status": "OPEN",
            "handled_by": None, "handled_at": None, "handled_note": None,
            "ip_hash": ip_hash,
        }
        _REPORTS.append(row)
        _save("reports", "counters")
        return _clone(row)


def list_reports(status=None, limit=500):
    with _LOCK:
        rows = [r for r in _REPORTS if not status or r.get("status") == status]
        rows.sort(key=lambda r: int(r.get("id") or 0), reverse=True)
        return _clone(rows[:int(limit)])


def open_report_count():
    with _LOCK:
        return len([r for r in _REPORTS if r.get("status") == "OPEN"])


def update_report(report_id, status, by, note=None):
    if status not in store.REPORT_STATUSES:
        raise ValueError("bad status")
    with _LOCK:
        rid = int(report_id)
        row = next((r for r in _REPORTS if int(r.get("id")) == rid), None)
        if not row:
            return None
        row["status"] = status
        if status == "DONE":
            row["handled_by"] = by
            row["handled_at"] = _now()
        if note is not None and str(note).strip():
            row["handled_note"] = str(note).strip()[:500]
        _save("reports")
        return _clone(row)


# ---------------------------------------------------------------------------
# installation
# ---------------------------------------------------------------------------
INSTALLS = (
    "page_by_path", "live_pages", "create_page", "update_page", "set_page_status",
    "page_by_id", "list_pages", "page_events", "insert_lead", "list_leads",
    "lead_by_id", "update_lead", "log_lead_export", "user_by_name", "user_by_id",
    "create_user", "update_user", "list_users", "reserve_lead_slot", "hash_ip",
    "insert_report", "list_reports", "open_report_count", "update_report",
    "count_view", "views_for",
)


def install(data_dir=None):
    """Point store.* at this module. Everything else in store.py — norm_path,
    slugify, build_path, PAGE_EDITABLE, the status tuples — is pure and stays."""
    init(data_dir)
    for name in INSTALLS:
        setattr(store, name, globals()[name])
    return DATA_DIR
