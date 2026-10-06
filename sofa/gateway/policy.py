"""When must a caller prove it is still them?

Having a bank linked to a phone number does not authorise anything. For a protected tool the caller types their PIN
on the keypad (never says it), and the first time in a call also a one-time code sent by SMS. After that the PIN is
asked again:
  - when it has been a few minutes since the last one,
  - when the caller moves to a different sensitive resource (statements to transfers, say),
  - (money-moving actions are not decided here: each one is authorised by its own PIN, typed after the read-back and the
    caller's spoken yes, so a PIN given earlier, for opening the account, never covers a transfer: see bankops.advance_op)
  - and now and then at random, so a stolen phone cannot count on a predictable pattern.
The random roll happens here on the server, never in the model, and never right after a PIN was just entered.
"""

import random
from dataclasses import dataclass

from .tools import Tool


@dataclass(frozen=True)
class Challenge:
    kind: str    # pin | pin_otp
    reason: str  # entry | stale | new_resource | fresh | random


class VerificationPolicy:
    def __init__(self, settings, rng: random.Random | None = None):
        self.s = settings
        self.rng = rng or random.SystemRandom()

    def required(self, tool: Tool, auth: dict | None, now: float, provider: str | None) -> Challenge | None:
        """None means the tool may run now; otherwise what the caller has to do first."""
        if not tool.protected:
            return None
        auth = auth or {}
        if auth.get("bank") != provider or not auth.get("verified_at"):
            return Challenge("pin_otp", "entry")
        if now - auth.get("last_pin_at", 0) > self.s.verify_reverify_seconds:
            return Challenge("pin", "stale")
        if tool.resource not in auth.get("resources", []):
            return Challenge("pin", "new_resource")
        if now - auth.get("last_pin_at", 0) > 30 and self.rng.random() < self.s.verify_random_rate:
            return Challenge("pin", "random")
        return None
