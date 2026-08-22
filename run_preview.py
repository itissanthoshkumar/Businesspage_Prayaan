"""Preview launcher — real app, real templates, no MongoDB.

LIVES IN THE REPO ON PURPOSE. This file used to exist only under
/tmp/pbn-public-preview, so every periodic /tmp cleanup destroyed it and the
preview had to be reconstructed from scratch. Keep it here; copy it out.

Run the preview:
    rsync -a ~/Documents/Claude/pbn-public/ /tmp/pbn-public-preview/
    python3 /tmp/pbn-public-preview/run_preview.py

It runs from a /tmp MIRROR because the preview sandbox cannot read ~/Documents.

Storage is filestore.py: JSON files under $PBN_DATA_DIR (default /tmp/pbn-data),
seeded with the four demo businesses and the admin user on first boot only.
Pages created in the back-office therefore SURVIVE a restart, and pages removed
in the back-office stay removed. Point PBN_DATA_DIR somewhere else for a
throwaway copy; delete the directory to start from the fixtures again.

Env:
    PBN_PORT       listen port (default 8797)
    PBN_DATA_DIR   storage directory (default /tmp/pbn-data)
"""
import os
import sys

SVC = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(SVC, "vendor"))
sys.path.insert(0, SVC)

PORT = int(os.getenv("PBN_PORT", "8797"))
os.environ.setdefault("PBN_BASE_URL", "http://localhost:{}".format(PORT))

import filestore  # noqa: E402

filestore.install()          # store.* now reads and writes JSON files

import main  # noqa: E402
import uvicorn  # noqa: E402

if __name__ == "__main__":
    s = filestore.stats()
    print("storage:     {}  ({} pages, {} live, {} leads, {} users)".format(
        s["dir"], s["pages"], s["live"], s["leads"], s["users"]))
    print("preview:     http://localhost:{}/preview".format(PORT))
    print("back-office: http://localhost:{}/admin  (admin / prayaan)".format(PORT))
    uvicorn.run(main.app, host="127.0.0.1", port=PORT, log_level="warning")
