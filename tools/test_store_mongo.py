"""Exercise store.py against a REAL MongoDB — the Mongo half of the storage
contract is otherwise never executed (every E2E runs on filestore).

Runs in CI against the mongo service; locally it SKIPS cleanly when no Mongo
is reachable. Uses its own throwaway database and drops it afterwards.

    MONGO_URI=mongodb://localhost:27017 PBN_MONGO_DB=pbn_ci \
    python3 tools/test_store_mongo.py
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ.setdefault("PBN_MONGO_DB", "pbn_ci")

import store  # noqa: E402

fails = []


def check(label, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + label
          + (("  -> " + str(detail)) if detail and not cond else ""))
    if not cond:
        fails.append(label)


try:
    store.db_admin().client.admin.command("ping")
except Exception as exc:                                 # noqa: BLE001
    print("SKIP: no MongoDB reachable ({})".format(type(exc).__name__))
    raise SystemExit(0)

if store.MONGO_DB == store.SHERLOCK_DB:
    raise SystemExit("refusing to test against Sherlock's database")


def wipe():
    """dropDatabase needs privileges a properly scoped Atlas user lacks —
    fall back to dropping the collections one by one."""
    db = store.db_admin()
    try:
        db.client.drop_database(store.MONGO_DB)
    except Exception:                                    # noqa: BLE001
        for name in db.list_collection_names():
            db.drop_collection(name)


wipe()

print("\n-- indexes --")
store.ensure_indexes()
names = store.db_admin().business_pages.index_information()
check("unique path index exists",
      any(i.get("unique") for i in names.values()
          if i.get("key") == [("path", 1)]), names)

print("\n-- pages --")
data = {"business_name": "Mongo Traders", "phones": ["+91 9000000001"]}
page = store.create_page(dict(data), "ci", "TN", "vellore")
check("create derives the slug", page["path"] == "/TN/vellore/mongo-traders", page["path"])
again = store.create_page(dict(data), "ci", "TN", "vellore")
check("collision suffixes -2", again["path"].endswith("-2"), again["path"])
try:
    store.create_page(dict(data), "ci", "TN", "vellore", name_slug="mongo-traders")
    check("explicit taken slug refused", False)
except ValueError:
    check("explicit taken slug refused", True)

updated = store.update_page(page["id"], {"category": "Wholesale"}, "ci")
check("update patches and audits", updated["category"] == "Wholesale")
live = store.set_page_status(page["id"], "live", "ci",
                             consent_method="written", consent_ref="form at branch")
check("publish records consent evidence",
      live["consent"]["method"] == "written" and live["consent"]["ref"])
found, canonical = store.page_by_path("/TN/vellore/mongo-traders")
check("page_by_path finds it live", found and found["status"] == "live" and not canonical)

print("\n-- leads / reports / counters --")
check("quota reserve works", store.reserve_lead_slot(2, scope="ci") is True)
store.reserve_lead_slot(2, scope="ci")
check("quota cap enforced", store.reserve_lead_slot(2, scope="ci") is False)
lead = store.insert_lead("CI Person", "9111111111", "600001",
                         page["path"], "v1", "hash", via="wa")
check("lead carries via + attribution",
      lead["via"] == "wa" and lead["referrer_business_name"] == "Mongo Traders")
report = store.insert_report(page["path"], "remove", "please remove this page", None, "hash")
check("report stored OPEN", report["status"] == "OPEN")
done = store.update_report(report["id"], "DONE", "ci", note="removed")
check("report closes with handler", done["handled_by"] == "ci")
store.count_view(page["id"])
store.count_view(page["id"])
check("views counted", store.views_for(page["id"]) == 2, store.views_for(page["id"]))

print("\n-- users --")
user = store.create_user("ci.staff", "pbkdf2$1$x$y", role="staff", must_change=True)
check("user starts sv=1 must_change", user["sv"] == 1 and user["must_change"])
bumped = store.update_user(user["id"], bump_sv=True, active=False)
check("suspend bumps sv", bumped["sv"] == 2 and bumped["active"] is False)

wipe()
print("\n" + ("ALL PASS" if not fails else "FAILURES: " + ", ".join(fails)))
raise SystemExit(1 if fails else 0)
