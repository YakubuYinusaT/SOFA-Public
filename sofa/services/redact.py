"""Strip personal data from text that leaves the system (training exports, evidence call logs).

Heuristic, not a guarantee: it removes phone numbers, known customer/merchant-user names and the
delivery addresses on file. Spoken audio cannot be redacted this way (a voice is personal data),
so audio only ever leaves as a file path for labelled turns whose customer was told about recording.
"""

import hashlib
import hmac
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Customer, CustomerProfile, MerchantUser

PHONE = re.compile(r"(?:\+?234|\b0)[\s\-]?[789]\d(?:[\s\-]?\d){7,9}|\b\d{10,}\b")


class Redactor:
    def __init__(self, db: Session):
        names: set[str] = set()
        for n in list(db.scalars(select(Customer.name))) + list(db.scalars(select(MerchantUser.name))):
            names.update(part for part in (n or "").split() if len(part) >= 3)
        addresses = [a for a in db.scalars(select(CustomerProfile.delivery_address)) if a and len(a) >= 6]
        self._names = sorted(names, key=len, reverse=True)
        self._addresses = sorted(set(addresses), key=len, reverse=True)

    def __call__(self, text: str | None) -> str:
        if not text:
            return text or ""
        out = PHONE.sub("<PHONE>", text)
        for address in self._addresses:
            out = re.sub(re.escape(address), "<ADDRESS>", out, flags=re.I)
        for name in self._names:
            out = re.sub(rf"\b{re.escape(name)}\b", "<NAME>", out, flags=re.I)
        return out


def pseudonym(value: str, secret: str, prefix: str = "id") -> str:
    """Stable pseudonym so repeat callers stay countable without exposing the number."""
    digest = hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()[:10]
    return f"{prefix}_{digest}"
