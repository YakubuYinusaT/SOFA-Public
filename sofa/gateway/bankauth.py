"""How a bank checks a PIN and a one-time code.

In production the bank does both: SOFA passes the typed digits through and never keeps them. The built-in check below lets the whole verification conversation run and be tested: its PIN comes from settings and its code is texted by SOFA.
"""

import hashlib
import hmac
import secrets


class MockBankAuth:
    def __init__(self, settings, sms):
        self.s, self.sms = settings, sms
        self._codes: dict[tuple[str, str], tuple[str, float]] = {}  # (bank, phone) -> (sha256 of code, expiry)

    def _new_code(self) -> str:
        return "".join(secrets.choice("0123456789") for _ in range(self.s.verify_otp_length))

    async def verify_pin(self, bank: str, phone: str, pin: str) -> bool:
        return hmac.compare_digest(pin, self.s.mock_bank_pin)

    async def issue_otp(self, bank: str, phone: str, bank_name: str, now: float) -> None:
        code = self._new_code()
        self._codes[(bank, phone)] = (hashlib.sha256(code.encode()).hexdigest(), now + self.s.verify_otp_ttl_seconds)
        await self.sms.send(phone, f"{bank_name}: your verification code is {code}. Never share it with anyone.")

    async def verify_otp(self, bank: str, phone: str, code: str, now: float) -> bool:
        stored = self._codes.get((bank, phone))
        if not stored or now > stored[1]:
            return False
        return hmac.compare_digest(stored[0], hashlib.sha256(code.encode()).hexdigest())
