from sqlalchemy.orm import Session

from ..models import Call, Handoff
from .notify import notify_owners


async def open_handoff(db: Session, services, call: Call, reason: str, summary: str, draft_desc: str = "",
                       provider: str | None = None) -> Handoff:
    h = Handoff(call_id=call.id, merchant_id=call.merchant_id, service_domain=call.service_domain, provider=provider,
                reason=reason, summary=summary)
    db.add(h)
    db.flush()
    call.handoff_id = h.id
    call.outcome = "handoff"
    body = f"SOFA handoff ({reason}). Caller {call.from_number}. {summary}"
    if draft_desc:
        body += f" Draft order: {draft_desc}."
    if call.merchant_id:  # a gateway call has no shop owner to text; the admin handoff queue is where it is worked
        await notify_owners(db, services, call.merchant_id, body, "handoff")
    return h
