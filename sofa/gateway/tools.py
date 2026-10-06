"""The tool registry: the one list of things SOFA is allowed to do for a caller.

Every operation a service offers is registered here with its risk and what it needs before it can run. The model
only ever picks an operation; nothing outside this list can run, and a protected one will not run until the caller
is verified (the verification step plugs into `authorize`). This is how SOFA stays a medium: the provider's system
does the work and stays the source of truth.
"""

from dataclasses import dataclass

RISKS = ("informational", "account", "service", "sensitive", "restricted")


@dataclass(frozen=True)
class Tool:
    tool_id: str                  # service.operation, for example bank.balance (the service prefix can be shorter than the domain)
    domain: str
    description: str
    risk: str = "informational"   # one of RISKS
    protected: bool = False       # True: the caller must be verified before it runs
    required: tuple[str, ...] = ()  # parameters the caller must give before it can run
    confirm: bool = False         # read back and get a spoken yes before running
    resource: str = ""            # the part of the account it touches; moving to another asks for the PIN again (default: itself)

    def __post_init__(self):
        if not self.resource:
            object.__setattr__(self, "resource", self.tool_id)
        if self.risk not in RISKS:
            raise ValueError(f"{self.tool_id}: unknown risk {self.risk!r}")
        if "." not in self.tool_id:
            raise ValueError(f"{self.tool_id}: a tool id looks like service.operation")


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""  # unregistered | missing:<parameter> | needs_verification
    challenge: object = None  # when verification is needed and a policy is in place: what to ask for


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        if tool.tool_id in self._tools:
            raise ValueError(f"tool {tool.tool_id} is already registered")
        self._tools[tool.tool_id] = tool

    def get(self, tool_id: str) -> Tool | None:
        return self._tools.get(tool_id)

    def for_domain(self, domain: str) -> list[Tool]:
        return [t for t in self._tools.values() if t.domain == domain]

    def authorize(self, tool_id: str, auth: dict | None = None, params: dict | None = None, *, policy=None,
                  now: float = 0.0, provider: str | None = None) -> Decision:
        """May this operation run now? `auth` is the caller's verification state for this call; with a `policy` the answer
        also says what the caller has to do (PIN, code) before it can."""
        tool = self.get(tool_id)
        if not tool:
            return Decision(False, "unregistered")
        for name in tool.required:
            if (params or {}).get(name) in (None, ""):
                return Decision(False, f"missing:{name}")
        if tool.protected:
            if policy:
                challenge = policy.required(tool, auth, now, provider)
                if challenge:
                    return Decision(False, "needs_verification", challenge)
            elif not (auth or {}).get("verified"):
                return Decision(False, "needs_verification")
        return Decision(True)
