"""Service adapters: how SOFA's common voice layer talks to one kind of service (banking, commerce, ...).

An adapter declares its tools (so the registry knows what exists and how risky each is) and, for a service that holds a
conversation of its own, takes over the turns once the caller is inside it. The gateway never reaches into a provider
directly: it goes through the adapter, and the adapter's tools go through the registry.
"""

from ..tools import Tool


class ServiceAdapter:
    domain: str = ""

    def tools(self) -> list[Tool]:
        return []

    def in_dialogue(self, st: dict) -> bool:
        """Mid-conversation (an order being built): this turn belongs to the service, no routing needed."""
        return False

    def holds_session(self, st: dict) -> bool:
        """The caller is already inside this service, so a turn the router cannot place is probably meant for it."""
        return False

    async def turn(self, gw, transcript: str, confidence: float, alternatives: list[str] | None):
        raise NotImplementedError

    async def resolve_pending(self, gw, pending: dict, transcript: str, alternatives: list[str] | None):
        return None
