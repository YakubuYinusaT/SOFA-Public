"""Which providers a subscriber is connected to (their bank, and later others).

A link is set up by default for partner services. Having a link does not authorise anything: protected services still
verify the caller before they act (step 4). A caller with no link can ask SOFA to connect them: that creates a pending
link and passes the request to the provider, who activates it on their side.
"""

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ServiceLink


def _find(db: Session, customer_id, domain: str, provider: str) -> ServiceLink | None:
    return db.scalar(select(ServiceLink).where(ServiceLink.customer_id == customer_id, ServiceLink.domain == domain,
                                               ServiceLink.provider == provider))


def with_status(db: Session, customer_id, domain: str, status: str) -> list[ServiceLink]:
    return list(db.scalars(select(ServiceLink).where(ServiceLink.customer_id == customer_id, ServiceLink.domain == domain,
                                                     ServiceLink.status == status).order_by(ServiceLink.created_at)))


def active(db: Session, customer_id, domain: str) -> list[ServiceLink]:
    return with_status(db, customer_id, domain, "active")


def get(db: Session, customer_id, domain: str, provider: str) -> ServiceLink | None:
    return _find(db, customer_id, domain, provider)


def _as_epoch(value: datetime | None) -> float:
    if not value:
        return 0.0
    return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()


def locked(row: ServiceLink, now: float) -> bool:
    return _as_epoch(row.locked_until) > now


def record_failure(row: ServiceLink, now: float, max_attempts: int, lockout_minutes: int) -> None:
    """A wrong PIN or code. After `max_attempts` in a row the link is locked for a while, across calls."""
    row.failed_attempts = (row.failed_attempts or 0) + 1
    if row.failed_attempts >= max_attempts:
        row.locked_until = datetime.fromtimestamp(now, timezone.utc) + timedelta(minutes=lockout_minutes)


def clear_failures(row: ServiceLink, now: float) -> None:
    row.failed_attempts, row.locked_until = 0, None
    row.last_verified_at = datetime.fromtimestamp(now, timezone.utc)


def link(db: Session, customer_id, domain: str, provider: str, status: str = "active") -> ServiceLink:
    """Create or update a link. This is also what a provider's activation calls."""
    row = _find(db, customer_id, domain, provider)
    if not row:
        row = ServiceLink(customer_id=customer_id, domain=domain, provider=provider, status=status)
        db.add(row)
    row.status = status
    if status == "active" and not row.linked_at:
        row.linked_at = datetime.now(timezone.utc)
    db.flush()
    return row


def request_link(db: Session, customer_id, domain: str, provider: str) -> tuple[ServiceLink, bool]:
    """A caller asked to be connected. Returns (link, created): created is False if they had already asked."""
    row = _find(db, customer_id, domain, provider)
    if row and row.status != "revoked":
        return row, False
    return link(db, customer_id, domain, provider, status="pending"), True
