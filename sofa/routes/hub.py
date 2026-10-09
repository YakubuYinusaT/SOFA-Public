"""The admin home (/admin/home): every console and every page in one place, so the team never has to remember an address.

It is for the team only (it needs the admin login) because it lists every console. The public entry point (/) takes a visitor straight to their own side.

Type what you want to do ("change a price", "approve an account") and the list narrows; Enter opens the first match, and "/" jumps to the box.
Each console shows whether you are logged in, and a link to a console you are not logged into goes through its login and then on to the page you wanted.
It holds no customer data: only addresses, what each page is for, and (while the passwords are still the demo ones) the demo passwords.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse

from ..gateway.hub import enabled_services
from ..web import auth, ui
from .pages import ATTRIBUTION, COMPANY, templates

router = APIRouter()

# key, name, who it is for, what it does, base path, login portal (None: no login), password setting, links: (label, path, what you do there, extra words to find it by)
CONSOLES = [
    ("vendor", "Shop owner portal", "For the shop owner (CII Store)", "Run the shop: orders, stock and prices, the advice Sofa speaks, and the questions callers asked that Sofa could not answer.",
     "/vendor", auth.vendor, "vendor_token", [
         ("Today", "/vendor", "Sales, orders to deal with, stock running low", "dashboard sales summary overview"),
         ("Orders", "/vendor/orders", "Move an order to dispatched or delivered", "dispatch deliver cancel customer order status"),
         ("Stock and prices", "/vendor/stock", "Change a price, restock, add a product", "price restock inventory quantity product fertilizer add"),
         ("Advice Sofa gives", "/vendor/advice", "Write what Sofa says about using a product", "agronomy consulting how to apply farming guidance"),
         ("Customer questions", "/vendor/questions", "Answer what Sofa could not, and turn it into advice", "farmer follow up unanswered handoff"),
     ]),
    ("provider", "Bank service desk", "For the bank (Demo Bank, demo)", "What Sofa passes to the bank: people to phone back, account applications to approve or reject, and complaints.",
     "/provider", auth.provider, "provider_token", [
         ("Desk", "/provider", "Everything waiting for the bank", "dashboard summary overview"),
         ("Follow-ups", "/provider/handoffs", "People Sofa could not finish helping: phone them back", "handoff call back phone customer contact"),
         ("Account applications", "/provider/applications", "Approve or reject a new account", "approve reject open account kyc identity bvn"),
         ("Complaints", "/provider/complaints", "Complaints callers logged by phone", "complaint card atm issue"),
     ]),
    ("admin", "Team console", "For the Connected Intelligence team", "Everything behind the product: shops, orders, calls, accuracy checks and translations.",
     "/admin", auth.admin, "admin_token", [
         ("Dashboard", "/admin", "How the product is doing", "overview summary"),
         ("Merchants", "/admin/merchants", "Add a shop, its owner and its phone number", "shop onboarding number owner stock import"),
         ("Orders", "/admin/orders", "Every order, and recording a payment", "payment invoice transfer status"),
         ("Handoffs", "/admin/handoffs", "Requests Sofa passed on, and payments that did not match", "unmatched payment follow up"),
         ("Calls", "/admin/calls", "What callers said and how Sofa replied, turn by turn", "transcript recording conversation review listen audio"),
         ("Label", "/admin/label", "Correct what Sofa heard, to measure accuracy", "labelling transcript accuracy wer"),
         ("Metrics", "/admin/metrics", "Accuracy and reliability numbers", "validation word error rate latency"),
         ("Languages", "/admin/languages", "Translate Sofa's phrases (Yoruba), import a finished sheet", "translation yoruba hausa igbo phrases"),
         ("Translation sheet", "/admin/translation-sheet.xlsx", "Download the sheet for the translator", "excel xlsx download translator"),
     ]),
    ("demo", "Live demo page", "For judges and visitors", "One public screen of Sofa working: live calls, what is switched on, and how to try each scenario. Off unless it is switched on for a demo.",
     "/demo", None, "", [
         ("Live demo", "/demo", "Watch calls appear as they happen", "judges watch live calls show"),
     ]),
]

TASKS = [
    ("Change a price or restock a product", "/vendor/stock", "Shop owner", "price restock stock inventory quantity fertilizer urea npk"),
    ("See new orders and move them on to dispatched or delivered", "/vendor/orders", "Shop owner", "order dispatch deliver cancel customer"),
    ("Write what Sofa tells customers about using a product", "/vendor/advice", "Shop owner", "advice farming agronomy consulting guidance maize"),
    ("Answer a customer's question that Sofa could not", "/vendor/questions", "Shop owner", "question farmer unanswered follow up handoff"),
    ("See today's sales and what is running low", "/vendor", "Shop owner", "sales today dashboard low stock"),
    ("Approve or reject a new bank account application", "/provider/applications", "Bank desk", "approve reject account open application kyc"),
    ("Phone back a customer Sofa passed to the bank", "/provider/handoffs", "Bank desk", "follow up handoff call back customer"),
    ("See complaints callers logged", "/provider/complaints", "Bank desk", "complaint card atm"),
    ("Review what callers said and how Sofa replied", "/admin/calls", "Team", "calls transcript conversation review audio listen"),
    ("Add a shop and its phone number", "/admin/merchants", "Team", "merchant shop onboarding number owner"),
    ("Record a payment against an order", "/admin/orders", "Team", "payment invoice transfer"),
    ("Correct what Sofa heard, to measure accuracy", "/admin/label", "Team", "label accuracy transcript"),
    ("Translate Sofa's phrases into Yoruba", "/admin/languages", "Team", "translation yoruba language phrases sheet"),
    ("Watch Sofa live, as a judge or visitor", "/demo", "Judges", "demo live judges watch"),
    ("Check that the system is running", "/health", "Developers", "health status up running check"),
    ("Read the developer API reference", "/docs", "Developers", "api docs openapi swagger developer"),
]


def _status(request: Request, portal, key: str) -> tuple[str, str]:
    """The status chip for a console: the state, and the words under it."""
    settings = request.app.state.svc.settings
    if key == "demo":
        return ("on", "on") if settings.demo_page_enabled else ("off", "off")
    found = portal.access(request)
    if found:
        return ("active", "logged in" if found[0] == "own" else "open as admin")  # the admin login opens the shop and bank consoles too
    return ("login_required", "login required")


@router.get("/", include_in_schema=False)
def entry(request: Request):
    """The public front door. A visitor goes straight to the pages of the service this build runs; a build with several services goes to the admin home."""
    s = request.app.state.svc.settings
    side = ui.profile(s)
    if side == "all":
        return RedirectResponse("/admin/home", status_code=307)
    if s.overview_enabled and side == "naic":
        return RedirectResponse("/overview", status_code=307)  # the front door of the public build: the story first, with the demo and the tests one click away
    if s.demo_page_enabled:
        return RedirectResponse("/demo", status_code=307)
    return RedirectResponse("/vendor" if side == "naic" else "/provider", status_code=307)


@router.get("/admin/home")
def master(request: Request, sess=Depends(auth.admin.session)):
    svc = request.app.state.svc
    s = svc.settings
    consoles = []
    for key, name, who, what, base, portal, token_attr, links in CONSOLES:
        if key in ("vendor", "provider") and not ui.console_enabled(s, key):
            continue
        state, label = _status(request, portal, key)
        token = getattr(s, token_attr) if token_attr else ""
        demo_default = token in ("dev-admin-token", "dev-provider-token", "dev-vendor-token")
        consoles.append({"key": key, "name": name, "who": who, "what": what, "base": base, "links": links, "state": state, "label": label,
                         "password": token if demo_default else None, "login": (portal.login_url if portal else None)})
    return templates.TemplateResponse(request, "hub.html", {
        "request": request, "brand": "SOFA admin", "csrf": sess["csrf"], "active": "home", "company": COMPANY, "consoles": consoles,
        "tasks": [t for t in TASKS if not (t[1].startswith("/vendor") and not ui.console_enabled(s, "vendor"))
                  and not (t[1].startswith("/provider") and not ui.console_enabled(s, "provider"))
                  and not (t[1] == "/docs" and ui.profile(s) != "all")],
        "services": ", ".join(enabled_services(s)), "llm_provider": s.llm_provider, "llm_model": s.llm_model, "bank_backend": s.bank_backend,
        "number": s.gateway_number or "not set", "api_docs": ui.profile(s) == "all", "shop_number": "see the Merchants page", "demo_on": s.demo_page_enabled,
    })
