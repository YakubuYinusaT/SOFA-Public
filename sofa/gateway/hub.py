"""The gateway's wiring: the registered adapters and the tool registry built from them."""

import time

from .adapters import ServiceAdapter
from .adapters.banking import BankingAdapter
from .adapters.commerce import CommerceAdapter
from .adapters.lookup import LookupAdapter
from .tools import ToolRegistry


class Gateway:
    def __init__(self, adapters: list[ServiceAdapter], policy=None, clock=time.time):
        self.policy = policy  # VerificationPolicy; without one a protected tool simply needs a `verified` flag
        self.clock = clock    # seconds; tests replace it to move time
        self.tools = ToolRegistry()
        self._adapters: dict[str, ServiceAdapter] = {}
        for adapter in adapters:
            self._adapters[adapter.domain] = adapter
            for tool in adapter.tools():
                self.tools.register(tool)

    def domains(self) -> list[str]:
        return list(self._adapters)

    def adapter(self, domain: str | None) -> ServiceAdapter | None:
        return self._adapters.get(domain) if domain else None

    def authorize(self, tool_id: str, st: dict, params: dict | None = None):
        """May this call run this tool right now? `st` is the gateway session state."""
        return self.tools.authorize(tool_id, st.get("auth"), params, policy=self.policy, now=self.clock(), provider=st.get("bank"))

    def challenge(self, tool_id: str, st: dict):
        """What must the caller do before this tool runs? None if nothing. (Parameters are collected separately.)"""
        tool = self.tools.get(tool_id)
        return self.policy.required(tool, st.get("auth"), self.clock(), st.get("bank")) if tool and self.policy else None


ADAPTERS = {"commerce": CommerceAdapter, "banking": BankingAdapter, "lookup": LookupAdapter}


def enabled_services(settings=None) -> list[str]:
    """The services switched on, in the order they are registered."""
    wanted = [x.strip() for x in (getattr(settings, "services_enabled", None) or ",".join(ADAPTERS)).split(",") if x.strip()]
    return [k for k in ADAPTERS if k in wanted]


def build_gateway(settings=None) -> Gateway:
    return Gateway([ADAPTERS[k]() for k in enabled_services(settings)])
