"""Merchant onboarding rules shared by the admin API and the admin pages."""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import LANGUAGES
from ..models import Merchant, MerchantUser, PhoneNumber
from ..textutil import e164

PAYMENT_MODES = ("transfer_first", "pay_on_delivery", "per_customer")
NG_NUMBER = re.compile(r"^\+234[789]\d{9}$")


def clean_phone(value: str, what: str = "phone number") -> str:
    number = e164(value or "")
    if not NG_NUMBER.match(number):
        raise ValueError(f"{what} '{value}' is not a Nigerian mobile number (expected like 0803 123 4567 or +2348031234567)")
    return number


def check_settings(language: str, payment_mode: str, allowed: tuple[str, ...] = LANGUAGES) -> None:
    if language not in allowed:
        raise ValueError(f"language must be one of {', '.join(allowed)}")
    if payment_mode not in PAYMENT_MODES:
        raise ValueError(f"payment mode must be one of {', '.join(PAYMENT_MODES)}")


def add_number(db: Session, merchant_id, number: str) -> PhoneNumber:
    """The number customers dial. E.164 voice numbers from Africa's Talking are not always mobile-shaped,
    so only the E.164 form is required here."""
    e = e164(number or "")
    if not re.match(r"^\+\d{8,15}$", e):
        raise ValueError(f"dial-in number '{number}' is not a valid phone number")
    if db.scalar(select(PhoneNumber.id).where(PhoneNumber.e164 == e)):
        raise ValueError(f"{e} is already assigned to a merchant")
    row = PhoneNumber(merchant_id=merchant_id, e164=e)
    db.add(row)
    return row


def create_merchant(db: Session, *, name: str, category: str, owner_name: str, owner_phone: str,
                    number: str, default_language: str = "en", payment_mode: str = "transfer_first",
                    allowed_languages: tuple[str, ...] = LANGUAGES) -> Merchant:
    if not name.strip() or not owner_name.strip():
        raise ValueError("business name and owner name are required")
    check_settings(default_language, payment_mode, allowed_languages)
    owner = clean_phone(owner_phone, "owner phone")
    m = Merchant(name=name.strip(), category=(category or "provisions").strip(), default_language=default_language,
                 payment_mode=payment_mode)
    db.add(m)
    db.flush()
    db.add(MerchantUser(merchant_id=m.id, name=owner_name.strip(), phone=owner, role="owner", can_update_stock=True))
    add_number(db, m.id, number)
    db.flush()
    return m


def add_user(db: Session, merchant_id, *, name: str, phone: str, role: str = "staff", can_update_stock: bool = False) -> MerchantUser:
    if role not in ("owner", "staff"):
        raise ValueError("role must be owner or staff")
    if not name.strip():
        raise ValueError("name is required")
    u = MerchantUser(merchant_id=merchant_id, name=name.strip(), phone=clean_phone(phone), role=role,
                     can_update_stock=can_update_stock)
    db.add(u)
    return u
