"""Shared rendering helpers for the consoles, following the Connected Intelligence design system.

StateChip maps every state to one of four colours, and the mapping is fixed because the question a reader is really asking is whether money
or service is exposed. The chip always shows the state's own name; colour alone never carries the meaning.

  confirmed  the evidence agrees: it is done and it is real
  pending    waiting on evidence, or on a person
  exposed    money or service is at risk, or something failed
  inert      nothing is owed and nothing is running
"""

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

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
                           *([("Live", demo)] if demo else []),
                           *([("About", [("Overview", "/overview"), ("Technology", "/overview/technology"), ("Use cases", "/overview/use-cases"),
                                         ("What we found", "/overview/findings")])] if settings.overview_enabled else [])],
                "lines": [COMPANY_LINE, "ConnectedCI Ltd", NAIC_DISCLAIMER]}
    on = services_on(settings)
    consoles = [("Admin home", "/admin/home"), ("Team console", "/admin")]
    if "commerce" in on:
        consoles.append(("Shop owner portal", "/vendor"))
    if "banking" in on:
        consoles.append(("Bank service desk", "/provider"))
    return {"groups": [("Consoles", consoles), ("Live", [("Live demo page", "/demo"), ("System health", "/health"), *([("API reference", "/docs")] if profile(settings) == "all" else [])])],
            "lines": [COMPANY_LINE, "ConnectedCI Ltd. Internal console."]}


DECK_SLIDE = re.compile(r"\d{1,3}\.jpg")


def deck_dir(settings) -> Path:
    return Path(settings.storage_dir).resolve() / "overview" / "deck"


def deck_slides(settings) -> list[str]:
    """The slide pictures (01.jpg, 02.jpg, ...) the team put in the deck folder on the server, in order. The deck is shown as pictures only, so a file
    that could be edited or reused (a PowerPoint, a PDF) is never sent to a visitor."""
    folder = deck_dir(settings)
    if not folder.is_dir():
        return []
    return sorted((p.name for p in folder.iterdir() if p.is_file() and DECK_SLIDE.fullmatch(p.name)), key=lambda n: int(n.split(".")[0]))


def site_nav(kind: str, request) -> list[tuple[str, str, bool]]:
    """The links across the top of every page that is not inside a signed-in console: where else a reader can go. Only pages of the page's own side are
    listed (the shop pages never link to the bank pages, nor the bank pages to the shop pages). Each entry is (label, address, whether it is this page)."""
    settings = request.app.state.svc.settings
    path = request.url.path
    items: list[tuple[str, str]] = []
    if kind == "naic":
        if settings.overview_enabled:
            items += [("Overview", "/overview"), ("Technology", "/overview/technology"), ("Use cases", "/overview/use-cases"), ("What we found", "/overview/findings")]
            if deck_slides(settings):
                items.append(("Pitch deck", "/overview/deck"))
            elif settings.hub_deck_url:
                items.append(("Pitch deck", settings.hub_deck_url))
        if settings.demo_page_enabled:
            items.append(("Live demo", "/demo"))
        if console_enabled(settings, "vendor"):
            items.append(("Shop owner portal", "/vendor"))
    elif kind == "demobank":
        if settings.demo_page_enabled:
            items.append(("Live demo", "/demo"))
        if console_enabled(settings, "provider"):
            items.append(("Bank service desk", "/provider"))

    def here(href: str) -> bool:
        if href == "/overview":
            return path == "/overview"
        return href.startswith("/") and (path == href or path.startswith(href + "/"))
    return [(label, href, here(href)) for label, href in items]


def site_home(kind: str, request) -> tuple[str, str] | None:
    """The 'back to the site' link inside a signed-in console: the first page of the site menu."""
    nav = site_nav(kind, request)
    return (f"Back to {nav[0][0].lower()}", nav[0][1]) if nav else None


def install(templates) -> None:
    templates.env.filters["state_class"] = state_class
    templates.env.globals["as_of"] = as_of
    templates.env.globals["footer_for"] = footer_for
    templates.env.globals["site_nav"] = site_nav
    templates.env.globals["site_home"] = site_home
    static = Path(__file__).resolve().parent / "static"
    # the stylesheet and script carry their last-changed time in the address, so a visitor's browser fetches the new file after an update instead of keeping the old one
    templates.env.globals["asset_v"] = str(int(max((static / n).stat().st_mtime for n in ("ci.css", "ci.js") if (static / n).exists())))
