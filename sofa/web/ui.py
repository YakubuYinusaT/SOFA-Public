"""Shared rendering helpers for the consoles, following the Connected Intelligence design system.

StateChip maps every state to one of four colours, and the mapping is fixed because the question a reader is really asking is whether money
or service is exposed. The chip always shows the state's own name; colour alone never carries the meaning.

  confirmed  the evidence agrees: it is done and it is real
  pending    waiting on evidence, or on a person
  exposed    money or service is at risk, or something failed
  inert      nothing is owed and nothing is running
"""

from datetime import datetime, timedelta, timezone

STATE_CLASS = {
    # confirmed
    "paid": "confirmed", "delivered": "confirmed", "resolved": "confirmed", "approved": "confirmed", "answered": "confirmed",
    "verified": "confirmed", "successful": "confirmed", "active": "confirmed", "live": "confirmed", "on": "confirmed", "in_stock": "confirmed",
    "sent": "confirmed", "linked": "confirmed",
    # pending
    "draft": "pending", "confirmed": "pending", "awaiting_payment": "pending", "dispatched": "pending", "open": "pending", "contacted": "pending",
    "queued": "pending", "calling": "pending", "under_review": "pending", "awaiting_identity_verification": "pending", "low": "pending",
    "part_paid": "pending", "pending": "pending", "prototype": "pending", "pilot": "pending", "beta": "pending", "login_required": "pending",
    # exposed
    "cancelled": "exposed", "rejected": "exposed", "failed": "exposed", "no_answer": "exposed", "out": "exposed", "out_of_stock": "exposed",
    "overpaid": "exposed", "unmatched": "exposed", "mismatch": "exposed",
    # inert
    "closed": "inert", "expired": "inert", "skipped_limit": "inert", "simulated": "inert", "inactive": "inert", "off": "inert", "not_for_sale": "inert",
}


def state_class(state: str | None) -> str:
    return STATE_CLASS.get((state or "").strip().lower().replace(" ", "_"), "inert")


def lagos_now() -> str:
    """The as-of time that every figure on a page carries (Africa/Lagos, UTC+1, no daylight saving)."""
    return (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%H:%M")


def as_of() -> str:
    return f"as of {lagos_now()} Lagos"


# ---- the pages a visitor can reach ---------------------------------------------------------------------------------

NAIC_DISCLAIMER = "N-ATLaS is an initiative of the Federal Ministry of Communications, Innovation and Digital Economy, and powered by Awarri Technologies."

COMPANY_LINE = "SOFA (System Of Functional Assistance) is a product of Connected Intelligence."


def services_on(settings) -> set[str]:
    return {x.strip() for x in (settings.services_enabled or "").split(",") if x.strip()}


def profile(settings) -> str:
    """naic: shopping and search only. demobank: banking only. all: everything."""
    on = services_on(settings)
    if "banking" not in on:
        return "naic"
    if not on & {"commerce", "lookup"}:
        return "demobank"
    return "all"


def console_enabled(settings, console: str) -> bool:
    """Is this console part of the build? The shop portal needs shopping; the bank desk needs banking. A build without it does not serve it at all."""
    on = services_on(settings)
    return {"vendor": "commerce" in on, "provider": "banking" in on}.get(console, True)


def footer_for(kind: str, request) -> dict:
    """The footer for a page: its groups of links (only related pages), and its lines of small print."""
    settings = request.app.state.svc.settings
    demo = [("Live demo", "/demo")] if settings.demo_page_enabled and kind != "internal" else []
    if kind == "naic":
        return {"groups": [("Shop owner portal", [("Today", "/vendor"), ("Orders", "/vendor/orders"), ("Stock and prices", "/vendor/stock"),
                                                    ("Advice Sofa gives", "/vendor/advice"), ("Customer questions", "/vendor/questions")]),
                           *([("Live", demo)] if demo else [])],
                "lines": [COMPANY_LINE, "ConnectedCI Ltd", NAIC_DISCLAIMER]}
    on = services_on(settings)
    consoles = [("Admin home", "/admin/home"), ("Team console", "/admin")]
    if "commerce" in on:
        consoles.append(("Shop owner portal", "/vendor"))
    if "banking" in on:
        consoles.append(("Bank service desk", "/provider"))
    return {"groups": [("Consoles", consoles), ("Live", [("Live demo page", "/demo"), ("System health", "/health"), *([("API reference", "/docs")] if profile(settings) == "all" else [])])],
            "lines": [COMPANY_LINE, "ConnectedCI Ltd. Internal console."]}


def install(templates) -> None:
    templates.env.filters["state_class"] = state_class
    templates.env.globals["as_of"] = as_of
    templates.env.globals["footer_for"] = footer_for
