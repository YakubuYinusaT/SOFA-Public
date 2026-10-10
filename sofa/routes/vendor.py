"""The shop owner's portal (/vendor): how a vendor runs their shop on SOFA without the Connected Intelligence team.

A vendor sees their own stock and prices, their orders, the questions callers asked that Sofa could not answer, and the advice Sofa speaks
for them (an agriculture store writes its fertilizer and spraying guidance here, and Sofa says it as written).

One password (VENDOR_TOKEN), one shop (VENDOR_MERCHANT, default "CI Store"). A login and a scope per merchant are the next step.
"""

import asyncio
import logging
import re
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..models import Call, Customer, Handoff, Merchant, Order, Product, ShopAdvice
from ..services import advice as advice_svc
from ..services import catalog
from ..services import orders as order_svc
from ..web import auth, ui
from .admin import db_dep
from .pages import ATTRIBUTION, COMPANY, form_of, go, templates

log = logging.getLogger("sofa.vendor")


def _only_when_shopping_is_on(request: Request) -> None:
    if not ui.console_enabled(request.app.state.svc.settings, "vendor"):
        raise HTTPException(404)  # a build without shopping does not serve the shop portal at all


router = APIRouter(prefix="/vendor", dependencies=[Depends(_only_when_shopping_is_on)])
portal = auth.vendor
NAV = [("dashboard", "Today", "/vendor"), ("orders", "Orders", "/vendor/orders"), ("stock", "Stock and prices", "/vendor/stock"),
       ("advice", "Advice Sofa gives", "/vendor/advice"), ("questions", "Customer questions", "/vendor/questions")]
LOW_STOCK = 20
STOP_WORDS = {"the", "and", "for", "how", "what", "when", "should", "can", "you", "your", "with", "that", "this", "have", "need", "want", "please",
              "caller", "asked", "advice", "much", "many", "does", "about", "from", "will", "would", "could", "there", "into", "use"}
_tasks: set = set()


def merchant_of(request: Request, db) -> Merchant:
    m = db.scalar(select(Merchant).where(Merchant.name == request.app.state.svc.settings.vendor_merchant))
    if not m:
        raise HTTPException(404, "the vendor's shop has not been set up: run python -m scripts.seed_agro")
    return m


def page(request: Request, name: str, sess: dict, active: str, m: Merchant, status_code: int = 200, **ctx):
    base = dict(request=request, csrf=sess["csrf"], active=active, attribution=ATTRIBUTION, company=COMPANY, m=m,
                brand="Shop owner portal", nav_items=NAV, logout_url="/vendor/logout", base_path="/vendor", footer_kind="naic", home_url="/vendor",
                msg=request.query_params.get("msg"), err=request.query_params.get("err"),
                open_portal=portal.is_open(request),
                default_password=request.app.state.svc.settings.vendor_token == "dev-vendor-token" and not portal.is_open(request))
    base.update(ctx)
    return templates.TemplateResponse(request, name, base, status_code=status_code)


def keywords_from(text: str) -> str:
    """Starting words for a new advice entry, taken from the question a caller asked. The vendor edits them."""
    words = []
    for w in re.findall(r"[a-zA-Z]{3,}", text.lower()):
        if w not in STOP_WORDS and w not in words:
            words.append(w)
    return ", ".join(words[:10])


def warm_voice(request: Request, text: str) -> None:
    """Make the audio for new advice now, so the first caller who needs it hears it at once instead of waiting for the voice model."""
    svc = request.app.state.svc

    async def run():
        try:
            await svc.audio.speak(text, "en")
        except Exception as exc:  # the advice is saved either way; it will be spoken, only slower the first time
            log.warning("could not prepare advice audio: %s", exc)

    task = asyncio.get_running_loop().create_task(run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


# ---- login ------------------------------------------------------------------------------------


def login_page(request: Request, status_code: int = 200, **ctx):
    ctx.update(request=request, csrf="", active="", attribution=ATTRIBUTION, company=COMPANY, brand="Shop owner portal", base_path="/vendor", who="a shop owner's portal", footer_kind="naic", home_url="/vendor")
    return templates.TemplateResponse(request, "portal_login.html", ctx, status_code=status_code)


@router.get("/login")
def login_form(request: Request):
    target = request.query_params.get("next", "/vendor")
    if portal.logged_in(request):  # already in, with its own password or as the admin: no second password
        return RedirectResponse(target if target.startswith("/vendor") and not target.startswith("//") else "/vendor", status_code=303)
    return login_page(request, next=target)


@router.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/vendor")):
    if portal.too_many_failures(request):
        return login_page(request, 429, err="Too many attempts. Wait 15 minutes.", next=next)
    if not portal.check_password(request, password):
        portal.record_failure(request)
        return login_page(request, 401, err="Wrong password.", next=next)
    response = RedirectResponse(next if next.startswith("/vendor") and not next.startswith("//") else "/vendor", status_code=303)
    portal.set_cookie(request, response, portal.new_session_cookie(request))
    return response


@router.post("/logout")
async def logout(request: Request, sess=Depends(portal.verified_post)):
    response = RedirectResponse("/vendor/login", status_code=303)
    response.delete_cookie(portal.cookie, path=portal.path)
    return response


# ---- today ------------------------------------------------------------------------------------


@router.get("")
def dashboard(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    m = merchant_of(request, db)
    since = datetime.now(timezone.utc) - timedelta(days=1)
    live = ("confirmed", "awaiting_payment", "paid", "dispatched")
    orders_open = list(db.scalars(select(Order).where(Order.merchant_id == m.id, Order.status.in_(live)).order_by(Order.created_at.desc()).limit(8)))
    new_today = db.scalar(select(func.count(Order.id)).where(Order.merchant_id == m.id, Order.status != "draft", Order.created_at >= since)) or 0
    sales = sum(o.total_kobo for o in db.scalars(select(Order).where(Order.merchant_id == m.id, Order.status.in_(("paid", "dispatched", "delivered")),
                                                                      Order.created_at >= since)))
    low = list(db.scalars(select(Product).where(Product.merchant_id == m.id, Product.active.is_(True), Product.stock_qty <= LOW_STOCK).order_by(Product.stock_qty)))
    questions = db.scalar(select(func.count(Handoff.id)).where(Handoff.merchant_id == m.id, Handoff.status == "open")) or 0
    calls = db.scalar(select(func.count(Call.id)).where(Call.merchant_id == m.id, Call.created_at >= since)) or 0
    return page(request, "vendor/today.html", sess, "dashboard", m, orders_open=orders_open, new_today=new_today, sales=sales, low=low,
                questions=questions, calls=calls, customer=lambda o: db.get(Customer, o.customer_id), items=order_svc.describe_items)


# ---- orders -----------------------------------------------------------------------------------


@router.get("/orders")
def orders(request: Request, status: str = "", sess=Depends(portal.session), db=Depends(db_dep)):
    m = merchant_of(request, db)
    q = select(Order).where(Order.merchant_id == m.id)
    q = q.where(Order.status == status) if status else q.where(Order.status != "draft")
    rows = [{"o": o, "customer": db.get(Customer, o.customer_id), "lines": order_svc.describe_items(o) if o.items else ""}
            for o in db.scalars(q.order_by(Order.created_at.desc()).limit(100))]
    return page(request, "vendor/orders.html", sess, "orders", m, rows=rows, status=status, statuses=list(order_svc.TRANSITIONS))


@router.post("/orders/{order_id}/status")
async def order_status(order_id: uuid.UUID, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    o = db.get(Order, order_id)
    if not o or o.merchant_id != m.id:
        raise HTTPException(404)
    to = (await form_of(request)).get("status", "")
    try:
        await order_svc.advance_order(db, request.app.state.svc, o, to, "vendor")
        db.commit()
    except order_svc.InvalidTransition as exc:
        db.rollback()
        return go("/vendor/orders", err=f"That is not a step this order can take now ({exc}).")
    return go("/vendor/orders", msg=f"Order {str(o.id)[:8]} is now {to.replace('_', ' ')}. The customer is told.")


# ---- stock and prices -------------------------------------------------------------------------


@router.get("/stock")
def stock(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    return _stock_page(request, sess, db, merchant_of(request, db))


def _stock_page(request, sess, db, m, err=None, draft=None, status_code=200):
    items = list(db.scalars(select(Product).where(Product.merchant_id == m.id).order_by(Product.category, Product.name)))
    extra = {"err": err} if err else {}
    return page(request, "vendor/stock.html", sess, "stock", m, status_code=status_code, items=items, low=LOW_STOCK, unit_options=order_svc.unit_options,
                draft=draft or {}, **extra)


@router.post("/stock")
async def stock_add(request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    f = await form_of(request)
    try:
        catalog.add_product(db, m.id, name=f.get("name", ""), unit=f.get("unit", ""), price_naira=f.get("price_naira") or "x", stock_qty=f.get("stock_qty") or 0,
                            category=f.get("category"), aliases=f.get("aliases", ""))
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _stock_page(request, sess, db, m, err=str(exc), draft=f, status_code=422)  # shown again with what was typed
    return go("/vendor/stock", msg="Product added. Callers can now ask for it by name.")


@router.post("/stock/{product_id}")
async def stock_update(product_id: uuid.UUID, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    p = catalog.get_product(db, m.id, product_id)
    if not p:
        raise HTTPException(404)
    f = await form_of(request)
    try:
        if (f.get("price_naira") or "").strip():
            catalog.set_price(p, f["price_naira"], f.get("unit") or None)
        catalog.set_stock(db, p, int(f.get("stock_qty") or 0), source="vendor")
        p.active = f.get("active") == "on"
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go("/vendor/stock", err=f"{p.name}: {exc}")
    return go("/vendor/stock", msg=f"{p.name} saved. Sofa uses the new price and stock on the next call.")


# ---- the advice Sofa gives ----------------------------------------------------------------------


@router.get("/advice")
def advice_list(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    return _advice_page(request, sess, db, merchant_of(request, db))


def _advice_page(request, sess, db, m, err=None, draft=None, status_code=200):
    rows = list(db.scalars(select(ShopAdvice).where(ShopAdvice.merchant_id == m.id).order_by(ShopAdvice.created_at, ShopAdvice.id)))
    products = [p.name for p in db.scalars(select(Product).where(Product.merchant_id == m.id, Product.active.is_(True)).order_by(Product.name))]
    extra = {"err": err} if err else {}
    return page(request, "vendor/advice.html", sess, "advice", m, status_code=status_code, rows=rows, products=products, draft=draft or {}, **extra)


def _save_advice(db, m: Merchant, entry: ShopAdvice | None, f: dict) -> ShopAdvice:
    topic, answer = (f.get("topic") or "").strip(), (f.get("answer") or "").strip()
    keywords = ", ".join(w.strip() for w in re.split(r"[,\n]", f.get("keywords") or "") if w.strip())
    if not topic or not answer:
        raise ValueError("Write what the advice is about, and the answer Sofa should say.")
    if len([k for k in keywords.split(",") if k.strip()]) < 3:
        raise ValueError("Add at least three words a caller would use for this (for example: maize, fertilizer, urea, apply). Sofa needs two of them to pick this advice.")
    if len(answer) > 900:
        raise ValueError("Keep the answer under 900 characters: it is read out loud, and a caller will not listen to more than about a minute.")
    product = (f.get("product_name") or "").strip() or None
    if product and not db.scalar(select(Product).where(Product.merchant_id == m.id, Product.name == product)):
        raise ValueError(f"{product} is not one of your products.")
    entry = entry or ShopAdvice(merchant_id=m.id, topic=topic, answer=answer)
    entry.topic, entry.keywords, entry.answer, entry.product_name, entry.active = topic, keywords, answer, product, f.get("active", "on") == "on"
    db.add(entry)
    return entry


@router.post("/advice")
async def advice_add(request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    f = await form_of(request)
    try:
        entry = _save_advice(db, m, None, f)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return _advice_page(request, sess, db, m, err=str(exc), draft=f, status_code=422)  # shown again with what was typed, never wiped
    warm_voice(request, entry.answer)
    return go("/vendor/advice", msg="Advice added. Sofa will say it when a caller asks about it.")


@router.post("/advice/{advice_id}")
async def advice_update(advice_id: uuid.UUID, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    entry = db.get(ShopAdvice, advice_id)
    if not entry or entry.merchant_id != m.id:
        raise HTTPException(404)
    f = await form_of(request)
    if f.get("delete"):
        db.delete(entry)
        db.commit()
        return go("/vendor/advice", msg="Advice deleted.")
    try:
        before = entry.answer
        _save_advice(db, m, entry, f)
        db.commit()
    except ValueError as exc:
        db.rollback()
        return go("/vendor/advice", err=str(exc))
    if entry.answer != before:
        warm_voice(request, entry.answer)
    return go("/vendor/advice", msg="Advice saved.")


# ---- questions Sofa could not answer ------------------------------------------------------------------


@router.get("/questions")
def questions(request: Request, status: str = "open", sess=Depends(portal.session), db=Depends(db_dep)):
    return _questions_page(request, sess, db, merchant_of(request, db), status)


def _questions_page(request, sess, db, m, status="open", err=None, drafts=None, status_code=200):
    rows = []
    for h in db.scalars(select(Handoff).where(Handoff.merchant_id == m.id, Handoff.status == status).order_by(Handoff.created_at.desc()).limit(100)):
        call = db.get(Call, h.call_id) if h.call_id else None
        rows.append({"h": h, "phone": call.from_number if call else "", "words": keywords_from(h.summary.split(":", 1)[-1]) if h.reason == "advice_question" else ""})
    products = [p.name for p in db.scalars(select(Product).where(Product.merchant_id == m.id, Product.active.is_(True)).order_by(Product.name))]
    extra = {"err": err} if err else {}
    return page(request, "vendor/questions.html", sess, "questions", m, status_code=status_code, rows=rows, status=status, products=products, drafts=drafts or {}, **extra)


@router.post("/questions/{handoff_id}")
async def question_update(handoff_id: uuid.UUID, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    m = merchant_of(request, db)
    h = db.get(Handoff, handoff_id)
    if not h or h.merchant_id != m.id:
        raise HTTPException(404)
    f = await form_of(request)
    stamp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    if f.get("action") == "reopen":
        h.status, h.resolved_by = "open", None
        db.commit()
        return go("/vendor/questions", msg="Reopened.")
    if f.get("action") == "advise":  # turn the question into advice Sofa will give from now on
        try:
            entry = _save_advice(db, m, None, {"topic": f.get("topic") or "Customer question", "keywords": f.get("keywords", ""), "answer": f.get("answer", ""),
                                               "product_name": f.get("product_name", "")})
        except ValueError as exc:
            db.rollback()
            return _questions_page(request, sess, db, m, "open", err=str(exc), drafts={str(h.id): f}, status_code=422)
        h.status, h.resolved_by = "resolved", "vendor"
        h.summary = f"{h.summary}\n[{stamp} vendor: written up as advice '{entry.topic}']"
        db.commit()
        warm_voice(request, entry.answer)
        return go("/vendor/questions", msg="Resolved, and Sofa will give this advice from now on.")
    h.status, h.resolved_by = "resolved", "vendor"
    if (f.get("note") or "").strip():
        h.summary = f"{h.summary}\n[{stamp} vendor: {f['note'].strip()[:300]}]"
    db.commit()
    return go("/vendor/questions", msg="Marked as handled.")
