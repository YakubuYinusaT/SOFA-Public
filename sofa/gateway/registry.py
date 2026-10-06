"""The services SOFA can route a caller to.

The caller never picks one from a menu: they say what they need and the first model call (the router) names the service
domain. Each descriptor also says how sensitive the service is, which the authentication step (to come) reads: `open`
services need no PIN, `protected` ones always do.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ServiceDomain:
    key: str
    label: str          # how Sofa names it aloud (English; translated with the other phrases later)
    hint: str           # what callers say, shown to the router model
    sensitivity: str    # open | protected
    provider_label: str | None = None  # how Sofa names whoever follows up on an unresolved request; None = nobody to hand to


SERVICES: dict[str, ServiceDomain] = {
    s.key: s for s in (
        ServiceDomain("banking", "your bank", "bank account, balance, transfers, airtime, bills, statements, cards, opening an account", "protected", "your bank"),
        ServiceDomain("commerce", "shopping", "buying something, prices, orders, tracking an order, paying for an order", "open", "the shop"),
        ServiceDomain("lookup", "a search", "looking up information, searching, asking a general question", "open"),
    )
}

CHAT = "chat"  # a greeting, thanks or small talk with no request in it: answered warmly, never counted as not understood
HUMAN, UNCLEAR = "human", "unclear"  # HUMAN: the caller asked for a person. SOFA has none to offer, so it is a request to answer, not a route
DOMAIN_CHOICES = [*SERVICES, HUMAN, CHAT, UNCLEAR]


