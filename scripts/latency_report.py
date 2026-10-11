"""Where does the time go on a call turn? Reads the per-stage times SOFA saved for every turn and prints a short report.

    python -m scripts.latency_report [--hours 48] [--exclude-test] [--db sqlite:///path/sofa.db]

On the server (reads the database named in its .env; prints no phone numbers and no words anyone said):

    cd /opt/sofa/src && sudo -u sofa /opt/sofa/venv/bin/python -m scripts.latency_report --hours 48

The times are the server's share only: from the moment Africa's Talking posts the recording link to the moment the reply is ready. The wait for
silence at the end of the caller's turn, the upload of the recording and the download of the reply by Africa's Talking are not in these numbers.
"""

import argparse
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from sofa.config import get_settings
from sofa.db import make_engine, make_session_factory
from sofa.models import Call, CallTurn

STAGES = [
    ("audio_fetch", "download recording + resample"),
    ("asr", "speech to text"),
    ("understand_and_act", "language model + dialogue"),
    ("tts", "voice (reply audio)"),
    ("total", "TOTAL (server share)"),
]


def pct(values: list[int], p: float) -> int:
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(p * len(ordered)))]


def table(title: str, rows: list[dict]) -> None:
    print(f"\n{title}: {len(rows)} turns")
    if not rows:
        return
    print(f"  {'stage':<34}{'n':>5}{'median':>9}{'p90':>8}{'max':>8}   (seconds)")
    for key, label in STAGES:
        vals = [r[key] for r in rows if key in r]
        if vals:
            print(f"  {label:<34}{len(vals):>5}{pct(vals, 0.5) / 1000:>9.1f}{pct(vals, 0.9) / 1000:>8.1f}{max(vals) / 1000:>8.1f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hours", type=float, default=0, help="only turns from the last N hours (default: all)")
    ap.add_argument("--exclude-test", action="store_true", help="leave out calls from the test numbers")
    ap.add_argument("--db", default="", help="database URL (default: DATABASE_URL from .env)")
    args = ap.parse_args()

    settings = get_settings()
    session_factory = make_session_factory(make_engine(args.db or settings.database_url))
    query = select(CallTurn, Call).join(Call, CallTurn.call_id == Call.id).where(CallTurn.latency_ms.is_not(None)).order_by(CallTurn.created_at)
    if args.hours:
        query = query.where(CallTurn.created_at >= datetime.now(timezone.utc) - timedelta(hours=args.hours))
    if args.exclude_test:
        query = query.where(Call.is_test.is_(False))

    with session_factory() as db:
        turns = [(t.seq, t.created_at, dict(t.latency_ms), t.action_taken or "", bool(t.error), c.id) for t, c in db.execute(query)]
    turns = [x for x in turns if "total" in x[2]]
    if not turns:
        print("no timed turns found")
        return

    calls = {x[5] for x in turns}
    print(f"{len(turns)} timed turns in {len(calls)} calls, {turns[0][1]:%Y-%m-%d %H:%M} to {turns[-1][1]:%Y-%m-%d %H:%M} UTC")
    table("All turns", [x[2] for x in turns])
    table("First turn of a call", [x[2] for x in turns if x[0] == 1])
    table("Later turns", [x[2] for x in turns if x[0] > 1])

    totals = [x[2]["total"] for x in turns]
    hold = settings.filler_after_seconds * 1000
    slow = sum(1 for v in totals if v > hold)
    print(f"\nTurns where the caller heard a holding message (server share over {settings.filler_after_seconds:g} s): {slow} of {len(totals)} ({100 * slow // len(totals)}%)")
    print("Turns by server time:", ", ".join(f"{label} {sum(1 for v in totals if lo <= v < hi)}" for label, lo, hi in
                                              [("under 2 s", 0, 2000), ("2 to 4 s", 2000, 4000), ("4 to 8 s", 4000, 8000), ("8 to 15 s", 8000, 15000), ("over 15 s", 15000, 10**9)]))
    print(f"Replies where the voice was too slow and the stored wording was spoken instead: {sum(1 for x in turns if 'template_fallback' in x[3])}")
    print(f"Turns that failed with an error: {sum(1 for x in turns if x[4])}")

    print("\nSlowest 5 turns (stage times in seconds):")
    for seq, created, lat, action, _, _ in sorted(turns, key=lambda x: -x[2]["total"])[:5]:
        parts = " ".join(f"{k}={lat[k] / 1000:.1f}" for k, _ in STAGES[:-1] if k in lat)
        print(f"  {created:%m-%d %H:%M} turn {seq}: total={lat['total'] / 1000:.1f}  {parts}  [{action.split('|')[0]}]")


if __name__ == "__main__":
    main()
