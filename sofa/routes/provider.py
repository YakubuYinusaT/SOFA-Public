"""The bank's service desk (/provider): what a bank sees of the people SOFA could not finish helping.

SOFA is a medium, not the customer-care desk. When a request cannot be finished (a failed account link, a rejected application, a complaint,
"let me speak to someone") SOFA tells the caller the bank will follow up, and the details land here. This is a demo of that desk, and it
also plays the part of the bank's back office: it can move an account application on, approve or reject it, which is what a real bank
does through its own systems and then tells SOFA.

One password (PROVIDER_TOKEN), one bank (PROVIDER_BANK, default demobank). Per-user accounts and roles are the next step.
"""

import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select

from ..gateway import banks, links, mockbank
from ..models import Call, Customer, Handoff, MockBankApplication, MockBankComplaint, Notification
from ..web import auth, ui
from .admin import db_dep
from .pages import form_of, go, templates, ATTRIBUTION, COMPANY



def _only_when_banking_is_on(request: Request) -> None:
    if not ui.console_enabled(request.app.state.svc.settings, "provider"):
        raise HTTPException(404)  # a build without banking does not serve the bank desk at all


router = APIRouter(prefix="/provider", dependencies=[Depends(_only_when_banking_is_on)])
portal = auth.provider

REASONS = {
    "link_failed": "Could not connect an existing account",
    "link_request": "Asked to be connected to the bank",
    "application_rejected": "Application rejected",
    "application_incomplete": "Application incomplete",
    "asked_for_person": "Asked for a person",
    "three_unknown_turns": "Sofa could not understand the request",
    "asr_low_confidence": "Sofa could not hear the caller",
}
NAV = [("dashboard", "Desk", "/provider"), ("handoffs", "Follow-ups", "/provider/handoffs"),
       ("applications", "Account applications", "/provider/applications"), ("complaints", "Complaints", "/provider/complaints")]


def bank_of(request: Request):
    return banks.BANKS[request.app.state.svc.settings.provider_bank]


def page(request: Request, name: str, sess: dict, active: str, status_code: int = 200, **ctx):
    bank = bank_of(request)
    ctx.update(request=request, csrf=sess["csrf"], active=active, attribution=ATTRIBUTION, company=COMPANY, bank=bank,
               brand="Bank service desk", nav_items=NAV, logout_url="/provider/logout", base_path="/provider", footer_kind="demobank", home_url="/provider",
               msg=request.query_params.get("msg"), err=request.query_params.get("err"),
               default_password=request.app.state.svc.settings.provider_token == "dev-provider-token")
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def caller_of(db, call_id) -> str:
    call = db.get(Call, call_id) if call_id else None
    return call.from_number if call else ""


# ---- login ------------------------------------------------------------------------------------


@router.get("/login")
def login_form(request: Request):
    target = request.query_params.get("next", "/provider")
    if portal.logged_in(request):  # already in, with its own password or as the admin: no second password
        return RedirectResponse(target if target.startswith("/provider") and not target.startswith("//") else "/provider", status_code=303)
    return page(request, "portal_login.html", {"csrf": ""}, "", next=target, who="the bank desk")


@router.post("/login")
async def login(request: Request, password: str = Form(""), next: str = Form("/provider")):
    if portal.too_many_failures(request):
        return page(request, "portal_login.html", {"csrf": ""}, "", 429, err="Too many attempts. Wait 15 minutes.", next=next, who="the bank desk")
    if not portal.check_password(request, password):
        portal.record_failure(request)
        return page(request, "portal_login.html", {"csrf": ""}, "", 401, err="Wrong password.", next=next, who="the bank desk")
    response = RedirectResponse(next if next.startswith("/provider") and not next.startswith("//") else "/provider", status_code=303)
    portal.set_cookie(request, response, portal.new_session_cookie(request))
    return response


@router.post("/logout")
async def logout(request: Request, sess=Depends(portal.verified_post)):
    response = RedirectResponse("/provider/login", status_code=303)
    response.delete_cookie(portal.cookie, path=portal.path)
    return response


# ---- the desk ---------------------------------------------------------------------------------


@router.get("")
def desk(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    bank = bank_of(request)
    count = lambda q: db.scalar(q) or 0  # noqa: E731
    open_handoffs = count(select(func.count(Handoff.id)).where(Handoff.provider == bank.code, Handoff.status.in_(("open", "contacted"))))
    waiting = {s: count(select(func.count(MockBankApplication.id)).where(MockBankApplication.provider == bank.code, MockBankApplication.status == s))
               for s in ("awaiting_identity_verification", "under_review", "approved", "rejected")}
    complaints = count(select(func.count(MockBankComplaint.id)).where(MockBankComplaint.provider == bank.code))
    recent = list(db.scalars(select(Handoff).where(Handoff.provider == bank.code).order_by(Handoff.created_at.desc()).limit(5)))
    return page(request, "provider/desk.html", sess, "dashboard", open_handoffs=open_handoffs, waiting=waiting, complaints=complaints, recent=recent,
                reasons=REASONS, caller_of=lambda h: caller_of(db, h.call_id))


@router.get("/handoffs")
def handoffs(request: Request, status: str = "open", sess=Depends(portal.session), db=Depends(db_dep)):
    bank = bank_of(request)
    wanted = ("open", "contacted") if status == "open" else (status,)
    rows = list(db.scalars(select(Handoff).where(Handoff.provider == bank.code, Handoff.status.in_(wanted)).order_by(Handoff.created_at.desc()).limit(200)))
    return page(request, "provider/handoffs.html", sess, "handoffs", rows=rows, status=status, reasons=REASONS, caller_of=lambda h: caller_of(db, h.call_id))


@router.post("/handoffs/{handoff_id}")
async def handoff_update(handoff_id: uuid.UUID, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    bank = bank_of(request)
    h = db.get(Handoff, handoff_id)
    if not h or h.provider != bank.code:
        raise HTTPException(404)
    f = await form_of(request)
    action, note, who = f.get("action", ""), (f.get("note") or "").strip()[:300], (f.get("by") or "").strip()[:60] or "desk"
    stamp = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    if action == "contacted":
        h.status = "contacted"
    elif action == "resolved":
        h.status, h.resolved_by = "resolved", who
    elif action == "reopen":
        h.status, h.resolved_by = "open", None
    else:
        raise HTTPException(400, "unknown action")
    if note or action in ("contacted", "resolved"):
        h.summary = f"{h.summary}\n[{stamp} {who}: {action}{' - ' + note if note else ''}]"
    db.commit()
    return go("/provider/handoffs", msg={"contacted": "Marked as contacted.", "resolved": "Marked as resolved.", "reopen": "Reopened."}[action])


# ---- account applications: the bank's back office -------------------------------------------------------


@router.get("/applications")
def applications(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    bank = bank_of(request)
    rows = []
    for a in db.scalars(select(MockBankApplication).where(MockBankApplication.provider == bank.code).order_by(MockBankApplication.created_at.desc()).limit(100)):
        customer = db.get(Customer, a.customer_id)
        rows.append({"a": a, "phone": customer.phone if customer else ""})
    return page(request, "provider/applications.html", sess, "applications", rows=rows)


@router.post("/applications/{reference}")
async def application_update(reference: str, request: Request, sess=Depends(portal.verified_post), db=Depends(db_dep)):
    svc = request.app.state.svc
    bank = bank_of(request)
    row = db.scalar(select(MockBankApplication).where(MockBankApplication.reference == reference, MockBankApplication.provider == bank.code))
    if not row:
        raise HTTPException(404)
    f = await form_of(request)
    action = f.get("action", "")
    customer = db.get(Customer, row.customer_id)
    if action == "identity_ok" and row.status == "awaiting_identity_verification":
        mockbank.complete_identity_check(db, reference)
        msg = "Identity check passed: the application is under review."
    elif action == "approve" and row.status in ("under_review", "awaiting_identity_verification"):
        mockbank.approve(db, reference)
        links.link(db, customer.id, "banking", bank.code, status="active")  # the bank tells SOFA: the account is connected
        acct = mockbank.MockBank(svc.settings).account(db, customer.id, bank.code)
        text = f"{bank.name}: your account is open. Your account number is {acct.account_number}. You can now call Sofa to use it."
        await svc.sms.send(customer.phone, text)
        db.add(Notification(channel="sms", template="account_approved", to_number=customer.phone, customer_id=customer.id, body=text, status="sent"))
        msg = f"Approved. Account {acct.account_number} opened, the account is connected to Sofa and the customer is texted."
    elif action == "reject" and row.status in ("under_review", "awaiting_identity_verification"):
        reason = (f.get("reason") or "").strip()[:200] or "could not verify the details"
        mockbank.reject(db, reference, reason)
        msg = "Rejected. If the customer calls Sofa, they will be told and passed back to this desk."
    else:
        db.rollback()
        return go("/provider/applications", err="That action does not fit the application's status.")
    db.commit()
    return go("/provider/applications", msg=msg)


@router.get("/complaints")
def complaints(request: Request, sess=Depends(portal.session), db=Depends(db_dep)):
    bank = bank_of(request)
    rows = []
    for c in db.scalars(select(MockBankComplaint).where(MockBankComplaint.provider == bank.code).order_by(MockBankComplaint.created_at.desc()).limit(100)):
        customer = db.get(Customer, c.customer_id)
        rows.append({"c": c, "phone": customer.phone if customer else ""})
    return page(request, "provider/complaints.html", sess, "complaints", rows=rows)
