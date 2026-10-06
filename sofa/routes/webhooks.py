"""Provider webhooks: verify first, return 200 fast, process in the background, idempotent."""

import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select

from ..clients.paystack import verify_signature
from ..models import Notification
from ..services.payments import handle_paystack_event, parse_event

log = logging.getLogger("sofa.webhooks")
router = APIRouter()


async def _process_paystack(svc, payload: dict) -> None:
    with svc.session_factory() as db:
        try:
            result = await handle_paystack_event(db, svc, payload)
            db.commit()
            log.info("paystack event processed: %s", result)
        except Exception:
            db.rollback()
            log.exception("paystack event failed")


@router.post("/webhooks/paystack")
async def paystack(request: Request, background: BackgroundTasks):
    svc = request.app.state.svc
    body = await request.body()
    if not verify_signature(svc.paystack.webhook_secret, body, request.headers.get("x-paystack-signature")):
        log.warning("paystack webhook rejected: bad signature")
        raise HTTPException(status_code=401, detail="bad signature")
    background.add_task(_process_paystack, svc, parse_event(body))
    return {"ok": True}


@router.post("/webhooks/sms/{secret}")
async def sms_events(secret: str, request: Request):
    """Africa's Talking delivery reports (and inbound messages, which become handoffs later)."""
    svc = request.app.state.svc
    if secret != svc.settings.at_callback_secret:
        raise HTTPException(status_code=404)
    form = await request.form()
    message_id, status = form.get("id"), form.get("status")
    if message_id and status:
        with svc.session_factory() as db:
            note = db.scalar(select(Notification).where(Notification.provider_message_id == str(message_id)))
            if note:
                note.status = str(status).lower()
                db.commit()
    return {"ok": True}
