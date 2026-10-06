"""Server-rendered admin console (/admin): onboarding, catalog, orders, handoffs, calls, labelling,
validation metrics and translations. Cookie login + CSRF (sofa/web/auth.py); Jinja2 autoescapes."""

import re
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select

from ..dialogue import templates as reply_templates
from ..dialogue.translation_io import LANG_NAMES, export_xlsx, import_sheet
from ..models import (Call, CallTurn, Customer, DailyReport, Handoff, Invoice, Merchant, MerchantUser, MissedDemand, Order, OutboundCall,
                      OrderEvent, Payment, PhoneNumber, Product, VirtualAccount)
from ..services import catalog, labelling, merchants, metrics, reports, scheduler
from ..services import orders as order_svc
from ..services.payments import apply_payment
from ..textutil import naira
from ..web import auth, ui
from .admin import db_dep

router = APIRouter(prefix="/admin")
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "web" / "templates"))
ATTRIBUTION = ("N-ATLaS is an initiative of the Federal Ministry of Communications, Innovation and Digital "
               "Economy, and powered by Awarri Technologies.")
COMPANY = "Connected Intelligence"
MAX_UPLOAD = 2 * 1024 * 1024


def _lagos(value) -> str:
    if not value:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")


templates.env.filters["naira"] = lambda k: naira(int(k or 0))
templates.env.filters["lagos"] = _lagos
templates.env.filters["short"] = lambda v: str(v)[:8]
ui.install(templates)


def page(request: Request, name: str, sess: dict, active: str, status_code: int = 200, **ctx):
    svc = request.app.state.svc
    ctx.update(request=request, csrf=sess["csrf"], active=active, attribution=ATTRIBUTION, company=COMPANY,
               msg=request.query_params.get("msg"), err=request.query_params.get("err"),
               default_password=svc.settings.admin_token == "dev-admin-token")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def go(url: str, msg: str | None = None, err: str | None = None) -> RedirectResponse:
    sep = "&" if "?" in url else "?"
    if msg:
        url += f"{sep}msg={quote(msg)}"
    elif err:
        url += f"{sep}err={quote(err)}"
    return RedirectResponse(url, status_code=303)


async def form_of(request: Request) -> dict:
    return {k: (v if isinstance(v, str) else v) for k, v in (await request.form()).multi_items()}


def _to_translate(request: Request) -> dict[str, str]:
    """Enabled languages other than English, {code: name}: the ones that need a translator."""
    return {l: LANG_NAMES[l] for l in request.app.state.svc.settings.languages if l in LANG_NAMES}


def s_langs(request: Request) -> tuple[str, ...]:
    return request.app.state.svc.settings.languages


def _merchant(db, merchant_id) -> Merchant:
    m = db.get(Merchant, merchant_id)
    if not m:
        raise HTTPException(404, "merchant not found")
    return m


# ---- login ------------------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    sess = {"csrf": ""}
    return page(request, "login.html", sess, "", next=request.query_params.get("next", "/admin"))


@router.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/admin")):
    if auth.too_many_failures(request):
        return page(request, "login.html", {"csrf": ""}, "", 429, err="Too many attempts. Wait 15 minutes.", next=next)
    if not auth.check_password(request, password):
        auth.record_failure(request)
        return page(request, "login.html", {"csrf": ""}, "", 401, err="Wrong password.", next=next)
    target = next if next.startswith("/admin") and not next.startswith("//") else "/admin"
    response = RedirectResponse(target, status_code=303)
    auth.set_cookie(request, response, auth.new_session_cookie(request))
    return response


@router.post("/logout")
async def logout(request: Request, sess=Depends(auth.verified_post)):
    response = RedirectResponse("/admin/login", status_code=303)
    response.delete_cookie(auth.COOKIE, path=auth.admin.path)
    return response


# ---- dashboard --------------------------------------------------------------------------------


@router.get("")
def dashboard(request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    s = request.app.state.svc.settings
    real = metrics.real_calls(db)
    n_orders, value = metrics.order_totals(db)
    labelled, total = labelling.progress(db)
    demand: dict[str, int] = {}
    for spoken, n in db.execute(select(MissedDemand.spoken_name, func.sum(MissedDemand.count_on_day)).group_by(MissedDemand.spoken_name)):
        demand[spoken] = int(n or 0)
    return page(
        request, "dashboard.html", sess, "dashboard",
        real=len(real), unique=len({c.from_number for c in real}), orders=n_orders, value=value,
        open_handoffs=db.scalar(select(func.count(Handoff.id)).where(Handoff.status == "open")),
        unmatched=db.scalar(select(func.count(Payment.id)).where(Payment.match_status == "unmatched")),
        labelled=labelled, label_total=total,
        recent=list(db.scalars(select(Call).order_by(Call.started_at.desc()).limit(8))),
        merchants={m.id: m.name for m in db.scalars(select(Merchant))},
        demand=sorted(demand.items(), key=lambda kv: -kv[1])[:8],
        coverage={name: reply_templates.coverage(l)[:2] for l, name in _to_translate(request).items()},
        modes={"SMS": "mock (logged, not sent)" if s.sms_is_mock else "live",
               "Paystack": "mock accounts" if s.paystack_is_mock else "live",
               "ASR": "built-in mock" if s.asr_url.startswith("inprocess") else s.asr_url,
               "LLM": "built-in mock" if s.llm_url.startswith("inprocess") else s.llm_url,
               "TTS": "built-in mock" if s.tts_url.startswith("inprocess") else s.tts_url},
    )


# ---- merchants ---------------------------------------------------------------------------------


@router.get("/merchants")
def merchant_list(request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    rows = []
    for m in db.scalars(select(Merchant).order_by(Merchant.created_at)):
        rows.append({
            "m": m, "numbers": [n.e164 for n in db.scalars(select(PhoneNumber).where(PhoneNumber.merchant_id == m.id))],
            "products": db.scalar(select(func.count(Product.id)).where(Product.merchant_id == m.id)),
            "owner": db.scalar(select(MerchantUser).where(MerchantUser.merchant_id == m.id, MerchantUser.role == "owner")),
        })
    return page(request, "merchants.html", sess, "merchants", rows=rows, languages=list(s_langs(request)),
                modes=merchants.PAYMENT_MODES)


@router.post("/merchants")
async def merchant_create(request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    f = await form_of(request)
    try:
        m = merchants.create_merchant(
            db, name=f.get("name", ""), category=f.get("category", ""), owner_name=f.get("owner_name", ""),
            owner_phone=f.get("owner_phone", ""), number=f.get("number", ""),
            default_language=f.get("default_language", "en"), payment_mode=f.get("payment_mode", "transfer_first"),
            allowed_languages=s_langs(request))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go("/admin/merchants", err=str(exc))
    return go(f"/admin/merchants/{m.id}", msg="Merchant created. Next: add the catalog.")


@router.get("/merchants/{merchant_id}")
def merchant_detail(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    m = _merchant(db, merchant_id)
    return page(
        request, "merchant.html", sess, "merchants", m=m,
        users=list(db.scalars(select(MerchantUser).where(MerchantUser.merchant_id == m.id))),
        numbers=list(db.scalars(select(PhoneNumber).where(PhoneNumber.merchant_id == m.id))),
        n_products=db.scalar(select(func.count(Product.id)).where(Product.merchant_id == m.id)),
        n_orders=db.scalar(select(func.count(Order.id)).where(Order.merchant_id == m.id, Order.status != "draft")),
        languages=list(s_langs(request)), modes=merchants.PAYMENT_MODES,
        report_preview=reports.sms_text(reports.compute_report(db, m, datetime.now(timezone.utc), include_test=True)),
        recent_reports=list(db.scalars(select(DailyReport).where(DailyReport.merchant_id == m.id).order_by(DailyReport.day.desc()).limit(7))),
    )


@router.post("/merchants/{merchant_id}")
async def merchant_update(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    m, f = _merchant(db, merchant_id), await form_of(request)
    try:
        merchants.check_settings(f.get("default_language", m.default_language), f.get("payment_mode", m.payment_mode), s_langs(request))
        if not f.get("name", "").strip():
            raise ValueError("business name is required")
        m.name, m.category = f["name"].strip(), f.get("category", m.category).strip()
        m.default_language, m.payment_mode = f["default_language"], f["payment_mode"]
        m.status = f.get("status", m.status) if f.get("status") in ("active", "paused") else m.status
        m.settlement_bank_code = f.get("settlement_bank_code", "").strip() or None
        m.settlement_account_number = f.get("settlement_account_number", "").strip() or None
        m.paystack_subaccount_code = f.get("paystack_subaccount_code", "").strip() or None
        report_time = f.get("report_time", m.report_time).strip()
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", report_time):
            raise ValueError("report time must look like 19:00 (24-hour, Lagos time)")
        m.report_enabled, m.report_time = f.get("report_enabled") == "on", report_time
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}", err=str(exc))
    return go(f"/admin/merchants/{merchant_id}", msg="Saved.")


@router.post("/merchants/{merchant_id}/report/send")
async def merchant_send_sample_report(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    """Text today's report to the owners right now. Includes team test calls, so a demo shows something; the
    scheduled 7 PM report never counts them. Not recorded as the day's report, so the real one still goes out."""
    m = _merchant(db, merchant_id)
    body = reports.sms_text(reports.compute_report(db, m, datetime.now(timezone.utc), include_test=True))
    statuses = await scheduler.send_report(db, request.app.state.svc, m, body, datetime.now(timezone.utc))
    db.commit()
    if not statuses:
        return go(f"/admin/merchants/{merchant_id}", err="This merchant has no owner phone to text.")
    ok = sum(1 for x in statuses if x in scheduler.SENT_OK)
    if ok < len(statuses):
        return go(f"/admin/merchants/{merchant_id}", err=f"Only {ok} of {len(statuses)} texts went out. See Notifications in the logs.")
    mock = " (SMS is in mock mode: it was logged, not sent)" if request.app.state.svc.settings.sms_is_mock else ""
    return go(f"/admin/merchants/{merchant_id}", msg=f"Sample report sent to {len(statuses)} owner phone(s){mock}.")


@router.post("/merchants/{merchant_id}/users")
async def merchant_add_user(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    _merchant(db, merchant_id)
    f = await form_of(request)
    try:
        merchants.add_user(db, merchant_id, name=f.get("name", ""), phone=f.get("phone", ""), role=f.get("role", "staff"),
                           can_update_stock=f.get("can_update_stock") == "on")
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}", err=str(exc))
    return go(f"/admin/merchants/{merchant_id}", msg="Person added. They can update stock by calling from that number.")


@router.post("/merchants/{merchant_id}/numbers")
async def merchant_add_number(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    _merchant(db, merchant_id)
    try:
        merchants.add_number(db, merchant_id, (await form_of(request)).get("number", ""))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}", err=str(exc))
    return go(f"/admin/merchants/{merchant_id}", msg="Number added. Point its callback URL at /voice/inbound/<secret>.")


# ---- catalog -------------------------------------------------------------------------------------


@router.get("/sample.csv")
def sample_csv(sess=Depends(auth.session)):
    return Response(catalog.SAMPLE_CSV, media_type="text/csv", headers={"Content-Disposition": 'attachment; filename="catalog_sample.csv"'})


@router.get("/merchants/{merchant_id}/products")
def products(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    m = _merchant(db, merchant_id)
    items = list(db.scalars(select(Product).where(Product.merchant_id == m.id).order_by(Product.category, Product.name)))
    return page(request, "products.html", sess, "merchants", m=m, items=items, unit_options=order_svc.unit_options)


@router.post("/merchants/{merchant_id}/products")
async def product_add(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    _merchant(db, merchant_id)
    f = await form_of(request)
    try:
        catalog.add_product(db, merchant_id, name=f.get("name", ""), unit=f.get("unit", ""), price_naira=f.get("price_naira") or "x",
                            stock_qty=f.get("stock_qty") or 0, brand=f.get("brand"), size_label=f.get("size_label"),
                            category=f.get("category"), aliases=f.get("aliases", ""), units_spec=f.get("units", ""))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}/products", err=str(exc))
    return go(f"/admin/merchants/{merchant_id}/products", msg="Product added.")


@router.post("/merchants/{merchant_id}/products/import")
async def product_import(merchant_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    _merchant(db, merchant_id)
    upload = (await request.form()).get("file")
    if not upload or not getattr(upload, "filename", ""):
        return go(f"/admin/merchants/{merchant_id}/products", err="Choose a CSV file first.")
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        return go(f"/admin/merchants/{merchant_id}/products", err="File is too large (2 MB limit).")
    try:
        n = catalog.import_products_csv(db, merchant_id, data.decode("utf-8-sig"))
        db.commit()
    except (ValueError, UnicodeDecodeError) as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}/products", err=f"Nothing imported. {exc}")
    return go(f"/admin/merchants/{merchant_id}/products", msg=f"Imported {n} products.")


@router.post("/merchants/{merchant_id}/products/{product_id}")
async def product_update(merchant_id: uuid.UUID, product_id: uuid.UUID, request: Request,
                         sess=Depends(auth.verified_post), db=Depends(db_dep)):
    p = catalog.get_product(db, merchant_id, product_id)
    if not p:
        raise HTTPException(404)
    f = await form_of(request)
    try:
        if (f.get("price_naira") or "").strip():  # blank means keep the current prices
            catalog.set_price(p, f["price_naira"], f.get("unit") or None)
        catalog.set_stock(db, p, int(f.get("stock_qty") or 0), source="web")
        p.active = f.get("active") == "on"
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go(f"/admin/merchants/{merchant_id}/products", err=f"{p.name}: {exc}")
    return go(f"/admin/merchants/{merchant_id}/products", msg=f"{p.name} saved.")


@router.post("/merchants/{merchant_id}/products/{product_id}/aliases")
async def alias_add(merchant_id: uuid.UUID, product_id: uuid.UUID, request: Request,
                    sess=Depends(auth.verified_post), db=Depends(db_dep)):
    p = catalog.get_product(db, merchant_id, product_id)
    if not p:
        raise HTTPException(404)
    phrase = (await form_of(request)).get("phrase", "")
    if not catalog.add_alias(db, p, phrase, "merchant_correction"):
        return go(f"/admin/merchants/{merchant_id}/products", err="Type the words customers use for this item.")
    db.commit()
    return go(f"/admin/merchants/{merchant_id}/products", msg=f"Alias added to {p.name}.")


# ---- orders ------------------------------------------------------------------------------------------


@router.get("/orders")
def order_list(request: Request, status: str = "", merchant: str = "", sess=Depends(auth.session), db=Depends(db_dep)):
    q = select(Order)
    q = q.where(Order.status == status) if status else q.where(Order.status != "draft")
    if merchant:
        q = q.where(Order.merchant_id == uuid.UUID(merchant))
    rows = []
    for o in db.scalars(q.order_by(Order.created_at.desc()).limit(200)):
        rows.append({"o": o, "customer": db.get(Customer, o.customer_id), "merchant": db.get(Merchant, o.merchant_id),
                     "lines": order_svc.describe_items(o) if o.items else ""})
    return page(request, "orders.html", sess, "orders", rows=rows, status=status, merchant=merchant,
                statuses=list(order_svc.TRANSITIONS), all_merchants=list(db.scalars(select(Merchant))))


@router.get("/orders/{order_id}")
def order_detail(order_id: uuid.UUID, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404)
    invoices = list(db.scalars(select(Invoice).where(Invoice.order_id == o.id)))
    open_invoice = next((i for i in invoices if i.status == "open"), None)
    nxt = sorted(order_svc.TRANSITIONS[o.status] - ({"paid"} if open_invoice else set()))
    return page(
        request, "order.html", sess, "orders", o=o, customer=db.get(Customer, o.customer_id),
        merchant=db.get(Merchant, o.merchant_id), invoices=invoices, open_invoice=open_invoice, next_states=nxt,
        payments=list(db.scalars(select(Payment).where(Payment.invoice_id.in_([i.id for i in invoices] or [None])))),
        events=list(db.scalars(select(OrderEvent).where(OrderEvent.order_id == o.id).order_by(OrderEvent.created_at))),
        calls_out=list(db.scalars(select(OutboundCall).where(OutboundCall.order_id == o.id).order_by(OutboundCall.created_at))),
        paid_so_far=sum(p.amount_kobo for p in db.scalars(select(Payment).where(Payment.invoice_id == (open_invoice.id if open_invoice else None)))) if open_invoice else 0,
    )


@router.post("/orders/{order_id}/status")
async def order_status(order_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    o = db.get(Order, order_id)
    if not o:
        raise HTTPException(404)
    to = (await form_of(request)).get("status", "")
    try:
        await order_svc.advance_order(db, request.app.state.svc, o, to, "admin")
        db.commit()
    except order_svc.InvalidTransition as exc:
        db.rollback()
        return go(f"/admin/orders/{order_id}", err=f"Cannot move the order that way ({exc}).")
    return go(f"/admin/orders/{order_id}", msg=f"Order is now {to.replace('_', ' ')}.")


@router.post("/invoices/{invoice_id}/payments")
async def invoice_payment(invoice_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    inv = db.get(Invoice, invoice_id)
    if not inv:
        raise HTTPException(404)
    f = await form_of(request)
    back = f"/admin/orders/{inv.order_id}"
    try:
        amount = int(round(float(f.get("amount_naira", "")) * 100))
        if amount <= 0 or f.get("confirmed") != "on":
            raise ValueError("Enter the amount and tick the box to confirm you saw the money in the bank or Paystack dashboard.")
        payment = Payment(provider="manual", amount_kobo=amount, sender_name=f.get("sender_name") or None, channel="manual", match_status="manual")
        db.add(payment)
        result = await apply_payment(db, request.app.state.svc, payment, inv, "admin")
        db.commit()
    except (ValueError, order_svc.InvalidTransition) as exc:
        db.rollback()
        return go(back, err=str(exc))
    text = {"paid": "Invoice paid. The customer is texted and the callback is queued.",
            "overpaid": "Invoice paid, but MORE than the amount was received. Refund the difference outside SOFA.",
            "part_paid": "Part payment recorded. The invoice stays open until the balance arrives."}[result]
    return go(back, msg=text)


# ---- handoffs and unmatched payments -------------------------------------------------------------------


@router.get("/handoffs")
def handoffs(request: Request, status: str = "open", sess=Depends(auth.session), db=Depends(db_dep)):
    rows = list(db.scalars(select(Handoff).where(Handoff.status == status).order_by(Handoff.created_at.desc()).limit(200)))
    pay_rows = []
    for p in db.scalars(select(Payment).where(Payment.match_status == "unmatched").order_by(Payment.created_at.desc())):
        acct = db.scalar(select(VirtualAccount).where(VirtualAccount.account_number == p.account_number))
        candidates = list(db.scalars(select(Invoice).where(
            Invoice.status == "open", Invoice.customer_id == acct.customer_id, Invoice.merchant_id == acct.merchant_id))) if acct else []
        pay_rows.append({"p": p, "customer": db.get(Customer, acct.customer_id) if acct else None, "candidates": candidates})
    return page(request, "handoffs.html", sess, "handoffs", rows=rows, status=status, pay_rows=pay_rows,
                merchants={m.id: m.name for m in db.scalars(select(Merchant))})


@router.post("/handoffs/{handoff_id}/resolve")
async def handoff_resolve(handoff_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    h = db.get(Handoff, handoff_id)
    if not h:
        raise HTTPException(404)
    f = await form_of(request)
    h.status, h.resolved_by = ("open", None) if f.get("reopen") else ("resolved", f.get("resolved_by", "").strip() or "admin")
    db.commit()
    return go("/admin/handoffs", msg="Handoff resolved." if h.status == "resolved" else "Handoff reopened.")


@router.post("/payments/{payment_id}/apply")
async def payment_apply(payment_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    payment = db.get(Payment, payment_id)
    f = await form_of(request)
    inv = db.get(Invoice, uuid.UUID(f["invoice_id"])) if f.get("invoice_id") else None
    if not payment or not inv:
        return go("/admin/handoffs", err="Choose an invoice to apply the payment to.")
    try:
        result = await apply_payment(db, request.app.state.svc, payment, inv, "admin")
        db.commit()
    except (ValueError, order_svc.InvalidTransition) as exc:
        db.rollback()
        return go("/admin/handoffs", err=str(exc))
    return go("/admin/handoffs", msg={"paid": "Applied: invoice paid.", "overpaid": "Applied: invoice paid, but more than the amount was received; refund the difference.",
                                      "part_paid": "Applied as a part payment; the invoice stays open."}[result])


# ---- calls and audio ---------------------------------------------------------------------------------------


@router.get("/calls")
def calls(request: Request, tests: str = "", outcome: str = "", sess=Depends(auth.session), db=Depends(db_dep)):
    q = select(Call)
    if not tests:
        q = q.where(Call.is_test.is_(False))
    if outcome:
        q = q.where(Call.outcome == outcome)
    rows = list(db.scalars(q.order_by(Call.started_at.desc()).limit(200)))
    return page(request, "calls.html", sess, "calls", rows=rows, tests=tests, outcome=outcome,
                merchants={m.id: m.name for m in db.scalars(select(Merchant))},
                outcomes=[o for (o,) in db.execute(select(Call.outcome).distinct()) if o])


@router.get("/calls/{call_id}")
def call_detail(call_id: uuid.UUID, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    c = db.get(Call, call_id)
    if not c:
        raise HTTPException(404)
    return page(request, "call.html", sess, "calls", c=c, merchant=db.get(Merchant, c.merchant_id) if c.merchant_id else None,
                customer=db.get(Customer, c.customer_id) if c.customer_id else None,
                handoff=db.get(Handoff, c.handoff_id) if c.handoff_id else None)


@router.get("/audio/{turn_id}/{kind}")
def audio(turn_id: uuid.UUID, kind: str, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    """Caller or reply audio for one turn, behind the login. Refuses any path outside the storage folder."""
    t = db.get(CallTurn, turn_id)
    path = {"caller": t.audio_path, "reply": t.reply_audio_path}.get(kind) if t else None
    root = Path(request.app.state.svc.settings.storage_dir).resolve()
    if not path or not Path(path).resolve().is_relative_to(root) or not Path(path).exists():
        raise HTTPException(404)
    return FileResponse(path, media_type="audio/wav")


# ---- labelling -----------------------------------------------------------------------------------------------


@router.get("/label")
def label_page(request: Request, turn: str = "", lang: str = "", tests: str = "", sess=Depends(auth.session), db=Depends(db_dep)):
    t = db.get(CallTurn, uuid.UUID(turn)) if turn else labelling.next_unlabelled(db, include_test=bool(tests), language=lang or None)
    done, total = labelling.progress(db, include_test=bool(tests))
    ctx = dict(done=done, total=total, lang=lang, tests=tests, intents=labelling.INTENTS, flags=labelling.FLAGS, t=t,
               langs=list(s_langs(request)))
    if t:
        call = db.get(Call, t.call_id)
        ctx.update(call=call, matches=labelling.system_matches(t), intent=(t.llm_json or {}).get("intent"),
                   product_names=[p.name for p in db.scalars(select(Product).where(Product.merchant_id == call.merchant_id).order_by(Product.name))],
                   label=t.label or {})
    return page(request, "label.html", sess, "label", **ctx)


@router.post("/label/{turn_id}")
async def label_save(turn_id: uuid.UUID, request: Request, sess=Depends(auth.verified_post), db=Depends(db_dep)):
    t = db.get(CallTurn, turn_id)
    if not t:
        raise HTTPException(404)
    form = await request.form()
    n = int(form.get("n_matches", "0") or 0)
    labelling.apply_label(db, t, {
        "transcript": form.get("transcript", ""), "intent": form.get("intent", ""), "language": form.get("language", ""),
        "notes": form.get("notes", ""), "flags": form.getlist("flags"),
        "product_matches": [{"spoken": form.get(f"spoken_{i}", ""), "system_choice": form.get(f"system_{i}") or None,
                             "correct_product": form.get(f"correct_{i}", "")} for i in range(n)],
    }, "admin")
    db.commit()
    keep = f"?lang={form.get('filter_lang', '')}&tests={form.get('filter_tests', '')}"
    return go(f"/admin/label{keep}", msg="Saved. Next turn.")


# ---- metrics and exports ------------------------------------------------------------------------------------------


@router.get("/metrics")
def metrics_page(request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
    return page(request, "metrics.html", sess, "metrics", m=metrics.validation_metrics(db))


@router.get("/export/{name}")
def export(name: str, request: Request, sess=Depends(auth.session), db=Depends(db_dep)):
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


# ---- languages -------------------------------------------------------------------------------------------------------


@router.get("/languages")
def languages(request: Request, sess=Depends(auth.session)):
    rows = []
    for lang, name in _to_translate(request).items():
        done, total, missing = reply_templates.coverage(lang)
        rows.append({"lang": lang, "name": name, "done": done, "total": total, "missing": missing[:12], "n_missing": len(missing)})
    return page(request, "languages.html", sess, "languages", rows=rows, report=None)


@router.get("/translation-sheet.xlsx")
def translation_sheet(request: Request, sess=Depends(auth.session)):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sofa_translation_sheet.xlsx"
        export_xlsx(path, reply_templates.TRANSLATIONS, tuple(_to_translate(request)))
        data = path.read_bytes()
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="sofa_translation_sheet.xlsx"'})


@router.post("/languages/import")
async def languages_import(request: Request, sess=Depends(auth.verified_post)):
    form = await request.form()
    upload = form.get("file")
    if not upload or not getattr(upload, "filename", ""):
        return go("/admin/languages", err="Choose the filled-in sheet (.xlsx or .csv).")
    data = await upload.read()
    if len(data) > MAX_UPLOAD:
        return go("/admin/languages", err="File is too large (2 MB limit).")
    suffix = ".csv" if upload.filename.lower().endswith(".csv") else ".xlsx"
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / f"sheet{suffix}"
        path.write_bytes(data)
        try:
            res = import_sheet(path, allow_partial=form.get("allow_partial") == "on", dry_run=form.get("dry_run") == "on")
        except Exception as exc:  # unreadable workbook, missing sheet or header
            return go("/admin/languages", err=f"Could not read that file: {exc}")
    rows = []
    for lang, name in _to_translate(request).items():
        done, total, missing = reply_templates.coverage(lang)
        rows.append({"lang": lang, "name": name, "done": done, "total": total, "missing": missing[:12], "n_missing": len(missing)})
    wrote = not form.get("dry_run") and (not res.errors or form.get("allow_partial") == "on")
    return page(request, "languages.html", sess, "languages", rows=rows,
                report={"errors": res.errors, "warnings": res.warnings, "wrote": wrote, "dry": form.get("dry_run") == "on"})
