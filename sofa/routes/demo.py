"""The live page (/demo): one screen that shows SOFA working, and how to try it.

It shows the live calls (who said what, what Sofa did, how long it took), the three people's views of the same service (the caller, the bank
desk, the shop owner), what is switched on, and the logins for the demo portals.

PRIVACY: it is public and read-only when switched on, and it shows what callers said. So it is OFF unless DEMO_PAGE_ENABLED=true, it masks
phone numbers down to the last four digits, and it hides any run of six or more digits in what was said. Use it for the demo deployment
only, with demo callers, never with real customers' calls.
"""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select

from ..gateway.hub import enabled_services
from ..web import ui
from ..models import (Call, CallTurn, Customer, Handoff, LateReply, MockBankApplication, MockBankTransaction, Order, Product, ShopAdvice)
from .admin import db_dep
from .pages import ATTRIBUTION, COMPANY, templates

router = APIRouter(prefix="/demo")

LONG_DIGITS = re.compile(r"\d{6,}")


def mask_phone(number: str) -> str:
    return ("*" * max(0, len(number) - 4)) + number[-4:] if number else ""


def scrub(text: str | None) -> str:
    return LONG_DIGITS.sub("[hidden]", text or "")


def enabled(request: Request) -> None:
    if not request.app.state.svc.settings.demo_page_enabled:
        raise HTTPException(404)


@router.get("")
def demo(request: Request, _=Depends(enabled), db=Depends(db_dep)):
    svc = request.app.state.svc
    s = svc.settings
    since = datetime.now(timezone.utc) - timedelta(days=1)

    def count(q) -> int:
        return db.scalar(q) or 0

    side = ui.profile(s)  # naic: the shop side only. demobank: the bank side only. all: both
    wants = {"naic": {"shop"}, "demobank": {"bank"}, "all": {"shop", "bank"}}[side]
    passed_to = {"naic": "the shop", "demobank": "the bank", "all": "a bank or shop"}[side]
    stats = [(label, n) for kind, label, n in [
        ("both", "calls in the last 24 hours", count(select(func.count(Call.id)).where(Call.created_at >= since))),
        ("shop", "orders placed", count(select(func.count(Order.id)).where(Order.status != "draft"))),
        ("bank", "bank transfers and payments", count(select(func.count(MockBankTransaction.id)))),
        ("bank", "account applications", count(select(func.count(MockBankApplication.id)))),
        ("both", f"follow-ups passed to {passed_to}", count(select(func.count(Handoff.id)))),
        ("both", "late answers called back", count(select(func.count(LateReply.id)).where(LateReply.status.in_(("answered", "simulated", "calling", "queued"))))),
    ] if kind == "both" or kind in wants]
    calls = []
    for c in db.scalars(select(Call).order_by(Call.created_at.desc()).limit(6)):
        turns = list(db.scalars(select(CallTurn).where(CallTurn.call_id == c.id).order_by(CallTurn.seq)))
        calls.append({
            "when": c.created_at, "from": mask_phone(c.from_number), "domain": c.service_domain or "front desk", "language": c.language or "",
            "outcome": (c.outcome or "").replace("_", " "),
            "turns": [{"said": "(typed on the keypad)" if t.asr_model == "keypad" else scrub(t.transcript), "reply": scrub(t.reply_text),
                       "action": (t.action_taken or "").split("|")[0], "ms": (t.latency_ms or {}).get("total")} for t in turns],
        })
    shop = db.scalar(select(func.count(Product.id)))
    logins = [(n, p, t, k) for n, p, t, k in [("Bank service desk", "/provider", s.provider_token, "bank"), ("Shop owner (CII Store)", "/vendor", s.vendor_token, "shop")] if k in wants]
    ledes = {"naic": "A caller speaks; Sofa does the shopping or the search, and tells them what it did.",
             "demobank": "A caller speaks; Sofa does the banking, and tells them what it did.",
             "all": "A caller speaks; Sofa does the banking, the shopping or the search, and tells them what it did."}
    return templates.TemplateResponse(request, "demo.html", {
        "request": request, "brand": "SOFA live demo", "stats": stats, "calls": calls, "refresh": 10, "side": side, "lede": ledes[side],
        "footer_kind": {"naic": "naic", "demobank": "demobank", "all": "internal"}[side], "home_url": "/demo",
        "services": enabled_services(s), "llm_provider": s.llm_provider, "llm_model": s.llm_model, "bank_backend": s.bank_backend,
        "gateway_number": s.gateway_number or "(not set)", "products": shop, "advice": count(select(func.count(ShopAdvice.id))),
        "logins": [(n, p, t) for n, p, t, k in logins],
        "show_logins": all(t.startswith("dev-") for _, _, t, _ in logins),
        "try_these": [(t, h) for k, t, h in TRY_THESE if k in wants], "base": s.public_base_url.rstrip("/"),
    })


TRY_THESE = [
    ("bank", "Banking", "Call from +2348055550101 (Amina). Say: \"I want to send the usual to Mama\". Sofa reads it back, you type the PIN on the keypad (sample PIN 1234), and the money moves."),
    ("bank", "Connect an account", "Call from +2348055550104 (Kemi), who banks with Demo Bank but has not used Sofa. Say: \"I want my Demo Bank balance\", then follow the steps."),
    ("bank", "Open an account", "Call from any other number and say \"I want to open an account\". Then open the bank service desk to approve the application."),
    ("shop", "Farm shop", "Say: \"How much is urea?\", \"Give me two bags of NPK\", or ask \"When should I apply urea on my maize?\" and the shop owner's own advice is spoken."),
    ("shop", "Something Sofa cannot answer", "Ask the shop \"How should I use a sprayer on my cassava?\". It is passed to the shop owner, who can write the answer up as advice in the vendor portal."),
]
