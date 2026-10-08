import hashlib
import hmac
import logging

import httpx

from .http import tls_context

log = logging.getLogger("sofa.paystack")

MOCK_WEBHOOK_SECRET = "dev-paystack-secret"


def verify_signature(secret: str, body: bytes, signature: str | None) -> bool:
    if not signature:
        return False
    expected = hmac.new(secret.encode(), body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, signature)


class PaystackClient:
    """Dedicated virtual accounts with merchant subaccounts. Mock mode when no secret key."""

    def __init__(self, settings):
        self.s = settings

    @property
    def webhook_secret(self) -> str:
        return self.s.paystack_secret_key or MOCK_WEBHOOK_SECRET

    async def create_dedicated_account(self, phone: str, name: str | None, subaccount: str | None) -> dict:
        """Returns {account_number, bank_name, customer_code}."""
        if self.s.paystack_is_mock:
            digits = "".join(ch for ch in phone if ch.isdigit())[-10:].rjust(10, "0")
            return {"account_number": digits, "bank_name": "Test Bank (mock)", "customer_code": f"CUS_mock_{digits}"}
        headers = {"Authorization": f"Bearer {self.s.paystack_secret_key}"}
        email = f"{phone.lstrip('+')}@customers.sofa.ciit.africa"  # never mailed
        async with httpx.AsyncClient(base_url="https://api.paystack.co", headers=headers, timeout=20, verify=tls_context()) as http:
            cust = await http.post("/customer", json={"email": email, "first_name": name or "Customer", "phone": phone})
            cust.raise_for_status()
            code = cust.json()["data"]["customer_code"]
            body = {"customer": code, "preferred_bank": self.s.paystack_preferred_bank}
            if subaccount:
                body["subaccount"] = subaccount
            acct = await http.post("/dedicated_account", json=body)
            acct.raise_for_status()
            data = acct.json()["data"]
        return {
            "account_number": data["account_number"],
            "bank_name": data["bank"]["name"],
            "customer_code": code,
        }
