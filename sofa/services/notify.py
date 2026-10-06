"""Every outbound message is a notifications row (channel: sms | voice). WhatsApp is out of scope."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import MerchantUser, Notification


async def send_sms(
    db: Session, services, *, to: str, body: str, template: str,
    merchant_id=None, order_id=None, customer_id=None,
) -> Notification:
    note = Notification(
        merchant_id=merchant_id, order_id=order_id, customer_id=customer_id,
        channel="sms", template=template, to_number=to, body=body,
    )
    db.add(note)
    db.flush()
    status, message_id = await services.sms.send(to, body)
    note.status, note.provider_message_id = status, message_id
    return note


async def notify_owners(db: Session, services, merchant_id, body: str, template: str, order_id=None) -> None:
    owners = db.scalars(select(MerchantUser).where(MerchantUser.merchant_id == merchant_id, MerchantUser.role == "owner"))
    for owner in owners:
        await send_sms(db, services, to=owner.phone, body=body, template=template, merchant_id=merchant_id, order_id=order_id)


def in_quiet_hours(quiet: str, now: datetime | None = None) -> bool:
    """quiet like '21:00-07:00', evaluated in Africa/Lagos (UTC+1, no DST)."""
    start, end = quiet.split("-")
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    minutes = (now.hour * 60 + now.minute + 60) % (24 * 60)
    to_min = lambda s: int(s[:2]) * 60 + int(s[3:])  # noqa: E731
    s, e = to_min(start), to_min(end)
    return (s <= minutes or minutes < e) if s > e else (s <= minutes < e)
