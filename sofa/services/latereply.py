"""A reply that came too late for the call. If the answer arrives after the caller was told "this is taking too long", it is not
thrown away: it is saved here and SOFA phones the caller back with it.

Life of a row:  queued -> calling -> answered
                                  -> queued again (no answer, up to late_reply_max_attempts, late_reply_retry_minutes apart)
                                  -> no_answer | failed | expired
The same news is never worth a call a day later, so a row older than late_reply_expire_minutes is dropped. In mock mode nothing
is dialled and the row ends as `simulated`.
"""

import html
import json
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from ..models import Customer, LateReply

log = logging.getLogger("sofa.latereply")

PLAY = re.compile(r'<Play url="([^"]*)"')
PREFIX = "late-"  # clientRequestId prefix: how the answered call is matched back to its row


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def audio_of(body: str) -> list[str]:
    """The audio the late reply would have played."""
    return [html.unescape(u) for u in PLAY.findall(body)]


def sensitive(st: dict | None) -> bool:
    """An answer about a bank account is never phoned out: the call back could be answered by someone else, and a money move may or
    may not have gone through. Those callers are told to phone in after a while instead."""
    return bool(st) and st.get("domain") == "banking"


def park(svc, task, session_id: str) -> None:
    """The caller was told it is taking too long: when the work finishes anyway, queue a call back with its result."""
    st = svc.sessions.get(session_id)
    if not st or not st.get("customer_id"):
        return

    def finished(t) -> None:
        try:
            result = t.result()
        except BaseException:
            return  # it failed: nothing to say
        body, text = result[0], result[1]
        urls = audio_of(body)
        if not urls or "<GetDigits" in body:
            return  # a keypad prompt for a caller who has gone is no news
        try:
            with svc.session_factory() as db:
                db.add(LateReply(customer_id=uuid.UUID(st["customer_id"]), session_id=session_id, text=text, audio=json.dumps(urls),
                                 lang=st.get("lang", "en"), ends="<Record" not in body))
                db.commit()
        except Exception:
            log.exception("could not save a late reply")

    task.add_done_callback(finished)


def due(db, settings, now: datetime) -> list[LateReply]:
    rows = []
    for row in db.scalars(select(LateReply).where(LateReply.status == "queued")):
        if now - _aware(row.created_at) > timedelta(minutes=settings.late_reply_expire_minutes):
            row.status, row.last_error = "expired", "too old to call"
        elif row.not_before is None or _aware(row.not_before) <= now:
            rows.append(row)
    return rows


def sweep(db, settings, now: datetime) -> None:
    for row in db.scalars(select(LateReply).where(LateReply.status == "calling")):
        if row.not_before is not None and _aware(row.not_before) <= now:
            retry_or_fail(row, settings, "no answer", now)


def retry_or_fail(row: LateReply, settings, reason: str, now: datetime, final_status: str = "no_answer") -> None:
    row.last_error = reason[:200]
    if row.attempt >= settings.late_reply_max_attempts:
        row.status = final_status
    else:
        row.status, row.not_before = "queued", now + timedelta(minutes=settings.late_reply_retry_minutes)


async def place_due(db, svc, now: datetime | None = None) -> int:
    s = svc.settings
    now = now or datetime.now(timezone.utc)
    if not s.outbound_calls_enabled or not s.gateway_number.strip():
        return 0
    sweep(db, s, now)
    placed = 0
    for row in due(db, s, now):
        customer = db.get(Customer, row.customer_id)
        if not customer:
            row.status, row.last_error = "failed", "no customer"
            continue
        row.attempt += 1
        row.status, row.not_before = "calling", now + timedelta(minutes=s.outbound_ring_minutes)
        db.commit()  # claimed: a second worker will not dial the same row
        result = await svc.voice.call(s.gateway_number, customer.phone, PREFIX + str(row.id))
        if result.simulated:
            row.status = "simulated"
        elif result.ok:
            row.provider_session_id = result.session_id
        else:
            log.warning("late-reply call to %s failed: %s", customer.phone, result.error)
            retry_or_fail(row, s, result.error or "rejected", now, final_status="failed")
        db.commit()
        placed += 1
    db.commit()
    return placed
