"""Admin REST API. Authenticated with a static bearer token; roles (admin / owner / staff) scoped to the caller's merchant_id are the next step."""

import hmac
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, File, Header, HTTPException, Request, Response, UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from ..models import (Call, CallTurn, Handoff, Invoice, Merchant, MerchantUser, Order, Payment, PhoneNumber,
                      Product)
from ..services import catalog, metrics
from ..services import orders as order_svc
from ..services.payments import apply_payment
from ..textutil import e164, naira

router = APIRouter(prefix="/api")


def require_admin(request: Request, authorization: str = Header(default="")):
    if not hmac.compare_digest(authorization, f"Bearer {request.app.state.svc.settings.admin_token}"):
        raise HTTPException(status_code=401, detail="admin token required")


def db_dep(request: Request):
    with request.app.state.svc.session_factory() as db:
        yield db


Auth = [Depends(require_admin)]


class MerchantIn(BaseModel):
    name: str
    category: str = "provisions"
    owner_name: str
    owner_phone: str
    number: str  # the number customers dial (E.164)
    default_language: str = "en"
    payment_mode: str = "transfer_first"


@router.post("/merchants", dependencies=Auth)
def create_merchant(body: MerchantIn, db=Depends(db_dep)):
    m = Merchant(name=body.name, category=body.category, default_language=body.default_language, payment_mode=body.payment_mode)
    db.add(m)
    db.flush()
    db.add(MerchantUser(merchant_id=m.id, name=body.owner_name, phone=e164(body.owner_phone), role="owner", can_update_stock=True))
    db.add(PhoneNumber(merchant_id=m.id, e164=e164(body.number)))
    db.commit()
    return {"id": str(m.id)}


@router.patch("/merchants/{merchant_id}", dependencies=Auth)
def update_merchant(merchant_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    m = db.get(Merchant, merchant_id) or _404()
    for k in ("name", "category", "status", "default_language", "payment_mode", "settlement_bank_code",
              "settlement_account_number", "paystack_subaccount_code"):
        if k in body:
            setattr(m, k, body[k])
    db.commit()
    return {"ok": True}


@router.post("/merchants/{merchant_id}/users", dependencies=Auth)
def add_user(merchant_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    u = MerchantUser(merchant_id=merchant_id, name=body["name"], phone=e164(body["phone"]),
                     role=body.get("role", "staff"), can_update_stock=body.get("can_update_stock", False))
    db.add(u)
    db.commit()
    return {"id": str(u.id)}


@router.post("/merchants/{merchant_id}/products/import", dependencies=Auth)
async def import_products(merchant_id: uuid.UUID, file: UploadFile = File(...), db=Depends(db_dep)):
    """CSV columns: name, brand, size_label, category, unit, price_naira, stock_qty, aliases, units.
    aliases = 'big indomie|indomie big one'; units = 'carton:40:7000|pack:1:350' (unit:qty_per_default_unit:price_naira).
    All-or-nothing: a bad row rejects the whole file and names the row."""
    try:
        created = catalog.import_products_csv(db, merchant_id, (await file.read()).decode("utf-8-sig"))
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc))
    db.commit()
    return {"created": created}


@router.get("/merchants/{merchant_id}/products", dependencies=Auth)
def list_products(merchant_id: uuid.UUID, db=Depends(db_dep)):
    return [{"id": str(p.id), "name": p.name, "price": naira(p.price_kobo), "unit": p.default_unit,
             "stock_qty": p.stock_qty, "active": p.active, "aliases": [a.phrase for a in p.aliases]}
            for p in db.scalars(select(Product).where(Product.merchant_id == merchant_id))]


@router.patch("/merchants/{merchant_id}/products/{product_id}", dependencies=Auth)
def patch_product(merchant_id: uuid.UUID, product_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    p = catalog.get_product(db, merchant_id, product_id) or _404()
    try:
        if "price_naira" in body:
            catalog.set_price(p, body["price_naira"])
        if "stock_qty" in body:
            catalog.set_stock(db, p, int(body["stock_qty"]), source="web")  # always leaves a stock_movements row
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    for k in ("active", "name"):
        if k in body:
            setattr(p, k, body[k])
    db.commit()
    return {"ok": True}


@router.post("/products/{product_id}/aliases", dependencies=Auth)
def add_alias(product_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    p = db.get(Product, product_id) or _404()
    catalog.add_alias(db, p, body["phrase"], body.get("source", "merchant_correction"), body.get("language"))
    db.commit()
    return {"ok": True}


@router.get("/merchants/{merchant_id}/orders", dependencies=Auth)
def list_orders(merchant_id: uuid.UUID, status: str | None = None, db=Depends(db_dep)):
    q = select(Order).where(Order.merchant_id == merchant_id)
    if status:
        q = q.where(Order.status == status)
    return [{"id": str(o.id), "status": o.status, "total": naira(o.total_kobo), "address": o.delivery_address,
             "items": order_svc.describe_items(o) if o.items else "", "customer_id": str(o.customer_id)}
            for o in db.scalars(q.order_by(Order.created_at.desc()))]


class StatusIn(BaseModel):
    status: str


@router.patch("/orders/{order_id}/status", dependencies=Auth)
async def set_status(order_id: uuid.UUID, body: StatusIn, request: Request, db=Depends(db_dep)):
    o = db.get(Order, order_id) or _404()
    try:
        await order_svc.advance_order(db, request.app.state.svc, o, body.status, "admin")
    except order_svc.InvalidTransition as exc:
        raise HTTPException(409, str(exc))
    db.commit()
    return {"status": o.status}


@router.post("/invoices/{invoice_id}/payments", dependencies=Auth)
async def manual_payment(invoice_id: uuid.UUID, body: dict, request: Request, db=Depends(db_dep)):
    """Staff mark an invoice paid only after seeing the funds in the bank or Paystack dashboard.
    Payments add up: a short payment leaves the invoice open until the balance arrives."""
    inv = db.get(Invoice, invoice_id) or _404()
    payment = Payment(provider="manual", amount_kobo=int(round(float(body["amount_naira"]) * 100)),
                      sender_name=body.get("sender_name"), channel="manual", match_status="manual")
    db.add(payment)
    try:
        result = await apply_payment(db, request.app.state.svc, payment, inv, "admin")
    except (ValueError, order_svc.InvalidTransition) as exc:
        db.rollback()
        raise HTTPException(409, str(exc))
    db.commit()
    return {"invoice_status": inv.status, "result": result}


@router.get("/handoffs", dependencies=Auth)
def list_handoffs(status: str | None = "open", db=Depends(db_dep)):
    q = select(Handoff).order_by(Handoff.created_at.desc())
    if status:
        q = q.where(Handoff.status == status)
    return [{"id": str(h.id), "reason": h.reason, "summary": h.summary, "status": h.status, "call_id": str(h.call_id) if h.call_id else None}
            for h in db.scalars(q)]


@router.patch("/handoffs/{handoff_id}", dependencies=Auth)
def resolve_handoff(handoff_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    h = db.get(Handoff, handoff_id) or _404()
    h.status, h.resolved_by = body.get("status", "resolved"), body.get("resolved_by")
    db.commit()
    return {"ok": True}


@router.get("/calls/{call_id}", dependencies=Auth)
def get_call(call_id: uuid.UUID, db=Depends(db_dep)):
    c = db.get(Call, call_id) or _404()
    return {
        "id": str(c.id), "from": c.from_number, "to": c.to_number, "language": c.language, "outcome": c.outcome,
        "is_test": c.is_test, "duration_s": c.duration_s,
        "turns": [{"id": str(t.id), "seq": t.seq, "transcript": t.transcript, "asr_confidence": t.asr_confidence,
                   "asr_model": t.asr_model, "llm_json": t.llm_json, "match_candidates": t.match_candidates,
                   "action": t.action_taken, "reply": t.reply_text, "latency_ms": t.latency_ms, "error": t.error,
                   "audio_path": t.audio_path, "label": t.label} for t in c.turns],
    }


@router.post("/turns/{turn_id}/label", dependencies=Auth)
def label_turn(turn_id: uuid.UUID, body: dict, db=Depends(db_dep)):
    """Correct transcript, intent or product match: the training labels."""
    t = db.get(CallTurn, turn_id) or _404()
    t.label, t.labelled_by, t.labelled_at = body, body.get("labelled_by", "admin"), datetime.now(timezone.utc)
    db.commit()
    return {"ok": True}


@router.get("/metrics/validation", dependencies=Auth)
def validation_metrics(db=Depends(db_dep)):
    """Counts and rates for the NAIC evidence; see services/metrics.py for definitions."""
    return metrics.validation_metrics(db)


@router.get("/export/{name}", dependencies=Auth)
def export(name: str, request: Request, db=Depends(db_dep)):
    """Anonymised exports: calls.csv, turns.csv, speech.jsonl, understanding.jsonl."""
    s = request.app.state.svc.settings
    makers = {
        "calls.csv": (lambda: metrics.export_calls_csv(db, s.admin_token), "text/csv"),
        "turns.csv": (lambda: metrics.export_turns_csv(db, s.admin_token), "text/csv"),
        "speech.jsonl": (lambda: metrics.export_speech_jsonl(db, s.storage_dir), "application/x-ndjson"),
        "understanding.jsonl": (lambda: metrics.export_understanding_jsonl(db), "application/x-ndjson"),
    }
    if name not in makers:
        raise HTTPException(404)
    body, media = makers[name]
    return Response(body(), media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"'})


def _404():
    raise HTTPException(status_code=404)
