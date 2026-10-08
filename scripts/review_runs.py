"""Open the saved test calls in the dashboard, one dashboard for each run, to listen and review later.

    python -m scripts.review_runs            # start every run in review_runs/ (ports 8101, 8102, ...)
    python -m scripts.review_runs 4          # start only the run whose folder begins with 4_

Each folder in review_runs/ is one run of the same English call (database, caller recordings, spoken replies, orders). They are read as they
were saved; nothing is deleted. Sign in at /admin with the admin password of your .env (or dev-admin-token when none is set).
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "review_runs"
BASE_PORT = 8100


def main() -> None:
    folders = sorted(p for p in RUNS.iterdir() if p.is_dir() and (p / "sofa.db").exists())
    if len(sys.argv) > 1:
        folders = [p for p in folders if p.name.startswith(sys.argv[1] + "_")]
    if not folders:
        raise SystemExit("no saved runs found in review_runs/")
    procs = []
    for folder in folders:
        port = BASE_PORT + int(folder.name.split("_")[0])
        env = dict(os.environ, STORAGE_DIR=str(folder), DATABASE_URL=f"sqlite:///{folder.as_posix()}/sofa.db", ASR_URL="inprocess://mock",
                   PAYSTACK_SECRET_KEY="", ADMIN_TOKEN=os.environ.get("ADMIN_TOKEN") or "dev-admin-token")
        procs.append(subprocess.Popen([sys.executable, "-m", "uvicorn", "sofa.main:create_app", "--factory", "--host", "127.0.0.1", "--port", str(port)],
                                      cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        print(f"{folder.name}: http://127.0.0.1:{port}/admin/calls")
    print("Press Ctrl+C to stop them all.")
    try:
        for p in procs:
            p.wait()
    except KeyboardInterrupt:
        for p in procs:
            p.terminate()


if __name__ == "__main__":
    main()
