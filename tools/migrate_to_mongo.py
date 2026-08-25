"""One-shot migrator: JSON file-store -> MongoDB.

The pilot's pages and leads live in $PBN_DATA_DIR as JSON (filestore.py). The
moment Atlas exists this carries them over — without it, "switch to Mongo"
means retyping every page and silently dropping the lead history, which for an
NBFC is thrown-away consent and audit evidence.

DRY-RUN BY DEFAULT: prints what it would insert. Pass --apply to write.

    PBN_DATA_DIR=/tmp/pbn-data \
    PBN_MONGO_URI_ADMIN='mongodb+srv://…' PBN_MONGO_DB=pbn \
    python3 tools/migrate_to_mongo.py [--apply]

Safety rails:
  * Refuses to run against Sherlock's database name.
  * Refuses --apply when the target business_pages is non-empty — this is a
    first-fill tool, not a sync; a partial merge would corrupt id sequences.
  * Creates the hardening indexes while it is here: UNIQUE path on
    business_pages, unique username on pbn_users, unique id everywhere.
"""
import argparse
import json
import os
import sys

DATA_DIR = os.getenv("PBN_DATA_DIR", "/tmp/pbn-data")
MONGO_DB = os.getenv("PBN_MONGO_DB", "pbn")
URI = os.getenv("PBN_MONGO_URI_ADMIN") or os.getenv("PBN_MONGO_URI_RW") or ""
SHERLOCK_DB = "dpd_early_warning"


def _load(name, key="rows"):
    path = os.path.join(DATA_DIR, name)
    if not os.path.exists(path):
        return []
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh).get(key, [])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    args = ap.parse_args()

    if MONGO_DB == SHERLOCK_DB:
        sys.exit("REFUSED: PBN_MONGO_DB is Sherlock's database ({}). "
                 "PBN gets its own — set PBN_MONGO_DB=pbn.".format(SHERLOCK_DB))
    if not URI:
        sys.exit("Set PBN_MONGO_URI_ADMIN (the admin-scoped connection string).")

    pages = _load("pages.json")
    leads = _load("leads.json")
    users = _load("users.json")
    reports = _load("reports.json")
    with open(os.path.join(DATA_DIR, "events.json"), "r", encoding="utf-8") as fh:
        ev = json.load(fh)
    page_events = ev.get("page_events", [])
    lead_events = ev.get("lead_events", [])
    counters = {}
    cpath = os.path.join(DATA_DIR, "counters.json")
    if os.path.exists(cpath):
        with open(cpath, "r", encoding="utf-8") as fh:
            counters = json.load(fh).get("counters", {})
    # Daily quota keys are ephemeral; carrying them over would randomly cap the
    # first production day. Sequence counters must travel or ids would collide.
    counters = {k: v for k, v in counters.items() if not k.startswith("lead_quota:")}

    print("source: {}".format(DATA_DIR))
    for label, rows in (("pages", pages), ("leads", leads), ("users", users),
                        ("reports", reports), ("page_events", page_events),
                        ("lead_events", lead_events)):
        print("  {:<12} {}".format(label, len(rows)))
    print("  {:<12} {}".format("counters", len(counters)))
    if not args.apply:
        print("\nDRY RUN — nothing written. Re-run with --apply.")
        return

    from pymongo import ASCENDING, MongoClient
    db = MongoClient(URI, serverSelectionTimeoutMS=8000)[MONGO_DB]

    if db.business_pages.count_documents({}) > 0:
        sys.exit("REFUSED: {}.business_pages is not empty. This tool only "
                 "fills a fresh database.".format(MONGO_DB))

    if pages:
        db.business_pages.insert_many(pages)
    if leads:
        db.site_leads.insert_many(leads)
    if users:
        db.pbn_users.insert_many(users)
    if reports:
        db.page_reports.insert_many(reports)
    if page_events:
        db.business_page_events.insert_many(page_events)
    if lead_events:
        db.lead_events.insert_many(lead_events)
    for key, value in counters.items():
        db.counters.update_one({"_id": key}, {"$set": {"v": int(value)}}, upsert=True)

    # Hardening indexes (part of the deferred-until-Atlas trio).
    db.business_pages.create_index([("id", ASCENDING)], unique=True)
    db.business_pages.create_index([("path", ASCENDING)], unique=True)
    db.business_pages.create_index([("aliases", ASCENDING)])
    db.site_leads.create_index([("id", ASCENDING)], unique=True)
    db.pbn_users.create_index([("username", ASCENDING)], unique=True)
    db.page_reports.create_index([("id", ASCENDING)], unique=True)
    db.business_page_events.create_index([("page_id", ASCENDING)])
    db.lead_events.create_index([("lead_id", ASCENDING)])

    print("\nDONE. Wrote to {}.{{business_pages,site_leads,pbn_users,"
          "page_reports,events,counters}} and created unique indexes.".format(MONGO_DB))
    print("Now run the service against this DB and spot-check /admin/pages.")


if __name__ == "__main__":
    main()
