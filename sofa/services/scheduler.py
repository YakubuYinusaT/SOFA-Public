"""Background jobs, run inside the API process: the 7 PM merchant report and the outbound status calls.

Idempotent by design: a daily_reports row (unique per merchant and Lagos day) is claimed before
anything is sent, so two workers, a restart, or a slow tick can never text an owner twice. A failed
send is retried up to 3 times, at least 10 minutes apart. Quiet hours do not apply to this SMS:
it goes at the merchant's own report time, which the merchant chose.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import DailyReport, Merchant, MerchantUser
from . import latereply, outbound, reports
from .notify import send_sms

log = logging.getLogger("sofa.scheduler")
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(minutes=10)
SENT_OK = ("sent", "mock_sent")


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def owner_phones(db: Session, merchant: Merchant) -> list[str]:
    return [u.phone for u in db.scalars(select(MerchantUser).where(MerchantUser.merchant_id == merchant.id, MerchantUser.role == "owner"))]


async def send_report(db: Session, svc, merchant: Merchant, body: str, now: datetime) -> list[str]:
    """Text the report to the merchant's owners. Returns the delivery status of each SMS."""
    statuses = []
    for phone in owner_phones(db, merchant):
        note = await send_sms(db, svc, to=phone, body=body, template="daily_report", merchant_id=merchant.id)
        statuses.append(note.status)
    return statuses


async def run_due_reports(svc, now: datetime | None = None) -> list[str]:
    """Send every report that is due now. Returns the names of the merchants texted (for logs and tests)."""
    now = now or datetime.now(timezone.utc)
    lagos = reports.to_lagos(now)
    hhmm = lagos.strftime("%H:%M")
    texted: list[str] = []
    with svc.session_factory() as db:
        merchants = list(db.scalars(select(Merchant).where(Merchant.status == "active", Merchant.report_enabled.is_(True))))
        for m in merchants:
            if hhmm < (m.report_time or "19:00"):
                continue
            day = reports.day_bounds(now)[2]
            rec = db.scalar(select(DailyReport).where(DailyReport.merchant_id == m.id, DailyReport.day == day))
            if rec and (rec.status in ("sent", "skipped_empty", "no_recipient") or rec.attempts >= MAX_ATTEMPTS):
                continue
            if rec and rec.status == "failed" and rec.last_attempt_at and now - _aware(rec.last_attempt_at) < RETRY_AFTER:
                continue
            if not rec:
                rec = DailyReport(merchant_id=m.id, day=day, status="pending", attempts=0)
                db.add(rec)
                try:
                    db.commit()  # claim today's report before doing anything else
                except IntegrityError:
                    db.rollback()  # another worker claimed it
                    continue
            report = reports.compute_report(db, m, now)
            if report.is_empty:
                rec.status = "skipped_empty"  # nothing to say: do not text an owner about a quiet day
                db.commit()
                continue
            body = reports.sms_text(report)
            rec.body, rec.attempts, rec.last_attempt_at = body, rec.attempts + 1, now
            statuses = await send_report(db, svc, m, body, now)
            if not statuses:
                rec.status = "no_recipient"
            elif all(s in SENT_OK for s in statuses):
                rec.status, rec.sent_at = "sent", now
                texted.append(m.name)
            else:
                rec.status = "failed"
                log.warning("daily report for %s: SMS failed (attempt %d)", m.name, rec.attempts)
            db.commit()
    return texted


async def loop(svc) -> None:
    """Tick forever; one bad tick must never stop the scheduler."""
    interval = svc.settings.scheduler_interval_seconds
    while True:
        try:
            texted = await run_due_reports(svc)
            if texted:
                log.info("daily report sent to: %s", ", ".join(texted))
            with svc.session_factory() as db:
                placed = await outbound.place_due_calls(db, svc)
            if placed:
                log.info("placed %d outbound status call(s)", placed)
            with svc.session_factory() as db:
                late = await latereply.place_due(db, svc)
            if late:
                log.info("called back %d caller(s) with a late answer", late)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("scheduler tick failed")
        await asyncio.sleep(interval)
