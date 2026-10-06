"""Outbound status calls. A callback is a queued row (schedule_callback); the scheduler places it through
Africa's Talking with the caller ID set to the merchant's own number, outside quiet hours. The SMS version of
the same news always goes out too (see payments.py and orders.py), so a missed call loses nothing.

Life of a row:  queued -> calling -> answered
                                  -> queued again (no answer, up to outbound_max_attempts, outbound_retry_minutes apart)
                                  -> no_answer | failed | expired | skipped_limit
In mock mode (no AT_API_KEY) a row ends as `simulated` and nothing is dialled.
"""

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Customer, Merchant, Order, OutboundCall, PhoneNumber
from .notify import in_quiet_hours

log = logging.getLogger("sofa.outbound")

# After the message the customer may answer (a Record follows) only for these triggers; the others just say it and hang up.
ASK_AFTER = {"delivered"}
COUNTED = ("calling", "answered", "no_answer", "failed")  # rows that used up a phone call to this customer


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def schedule_callback(db: Session, order: Order, trigger: str) -> OutboundCall:
    job = OutboundCall(order_id=order.id, trigger=trigger)
    db.add(job)
    db.flush()
    return job


def retry_or_fail(job: OutboundCall, settings, reason: str, now: datetime | None = None, final_status: str = "no_answer") -> None:
    """A call that did not connect: try again later, or give up after outbound_max_attempts."""
    now = now or datetime.now(timezone.utc)
    job.last_error = reason[:200]
    if job.attempt >= settings.outbound_max_attempts:
        job.status = final_status
    else:
        job.status = "queued"
        job.not_before = now + timedelta(minutes=settings.outbound_retry_minutes)


def sweep_unanswered(db: Session, settings, now: datetime) -> int:
    """A call the provider never reported on: after the ring time we treat it as unanswered."""
    n = 0
    for job in db.scalars(select(OutboundCall).where(OutboundCall.status == "calling")):
        if job.not_before is not None and _aware(job.not_before) <= now:
            retry_or_fail(job, settings, "no answer", now)
            n += 1
    return n


def due_jobs(db: Session, settings, now: datetime | None = None) -> list[OutboundCall]:
    """Queued callbacks that may be placed now. Quiet hours hold everything until morning."""
    now = now or datetime.now(timezone.utc)
    if in_quiet_hours(settings.quiet_hours, now):
        return []
    due = []
    for job in db.scalars(select(OutboundCall).where(OutboundCall.status == "queued")):
        if now - _aware(job.created_at) > timedelta(hours=settings.outbound_expire_hours):
            job.status, job.last_error = "expired", "too old to call"
        elif job.not_before is None or _aware(job.not_before) <= now:
            due.append(job)
    return due


def _calls_today(db: Session, customer_id, now: datetime, skip_id) -> int:
    since = now - timedelta(hours=24)
    return db.scalar(
        select(func.count(OutboundCall.id)).join(Order, Order.id == OutboundCall.order_id)
        .where(Order.customer_id == customer_id, OutboundCall.status.in_(COUNTED), OutboundCall.id != skip_id,
               OutboundCall.updated_at > since)
    ) or 0


async def place_due_calls(db: Session, svc, now: datetime | None = None) -> int:
    """Dial every callback that is due. Returns how many calls were placed (or simulated)."""
    s = svc.settings
    now = now or datetime.now(timezone.utc)
    if not s.outbound_calls_enabled:
        return 0
    sweep_unanswered(db, s, now)
    placed = 0
    for job in due_jobs(db, s, now):
        order = db.get(Order, job.order_id)
        customer = db.get(Customer, order.customer_id) if order else None
        merchant = db.get(Merchant, order.merchant_id) if order else None
        number = db.scalar(select(PhoneNumber).where(PhoneNumber.merchant_id == order.merchant_id, PhoneNumber.active.is_(True))) if order else None
        if not (customer and merchant and number):
            job.status, job.last_error = "failed", "no customer or shop number"
            continue
        if order.status == "cancelled":
            job.status, job.last_error = "expired", "order cancelled"
            continue
        if _calls_today(db, customer.id, now, job.id) >= s.outbound_max_per_customer_per_day:
            job.status, job.last_error = "skipped_limit", "daily call limit for this customer"
            continue
        job.attempt += 1
        job.status = "calling"
        job.not_before = now + timedelta(minutes=s.outbound_ring_minutes)
        db.commit()  # claimed: a second worker will not dial the same row
        result = await svc.voice.call(number.e164, customer.phone, str(job.id))
        if result.simulated:
            job.status = "simulated"
        elif result.ok:
            job.provider_session_id = result.session_id
        else:
            log.warning("outbound call to %s failed: %s", customer.phone, result.error)
            retry_or_fail(job, s, result.error or "rejected", now, final_status="failed")
        db.commit()
        placed += 1
    db.commit()
    return placed
