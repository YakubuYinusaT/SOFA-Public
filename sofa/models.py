"""Data model from the spec. Money is kobo (int), phones are E.164, every merchant-owned
row carries merchant_id and every query filters on it."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class Row:
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


def fk(table: str, nullable: bool = False):
    return mapped_column(Uuid, ForeignKey(f"{table}.id"), nullable=nullable, index=True)


# ---- merchants ---------------------------------------------------------------------------


class Merchant(Row, Base):
    __tablename__ = "merchants"
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(100), default="provisions")
    status: Mapped[str] = mapped_column(String(20), default="active")
    default_language: Mapped[str] = mapped_column(String(2), default="en")
    # transfer_first | pay_on_delivery | per_customer
    payment_mode: Mapped[str] = mapped_column(String(20), default="transfer_first")
    settlement_bank_code: Mapped[str | None] = mapped_column(String(20))
    settlement_account_number: Mapped[str | None] = mapped_column(String(20))
    paystack_subaccount_code: Mapped[str | None] = mapped_column(String(50))
    report_enabled: Mapped[bool] = mapped_column(Boolean, default=True)  # daily owner report by SMS
    report_time: Mapped[str] = mapped_column(String(5), default="19:00")  # HH:MM, Africa/Lagos


class MerchantUser(Row, Base):
    __tablename__ = "merchant_users"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    name: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(20), index=True)
    role: Mapped[str] = mapped_column(String(10), default="owner")  # owner | staff
    can_update_stock: Mapped[bool] = mapped_column(Boolean, default=True)


class PhoneNumber(Row, Base):
    __tablename__ = "phone_numbers"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    e164: Mapped[str] = mapped_column(String(20), unique=True)
    provider: Mapped[str] = mapped_column(String(20), default="africastalking")
    active: Mapped[bool] = mapped_column(Boolean, default=True)


# ---- catalog -----------------------------------------------------------------------------


class Product(Row, Base):
    __tablename__ = "products"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    name: Mapped[str] = mapped_column(String(200))
    brand: Mapped[str | None] = mapped_column(String(100))
    size_label: Mapped[str | None] = mapped_column(String(50))
    category: Mapped[str | None] = mapped_column(String(100))
    default_unit: Mapped[str] = mapped_column(String(20), default="piece")
    price_kobo: Mapped[int] = mapped_column(BigInteger)  # per default unit
    stock_qty: Mapped[int] = mapped_column(Integer, default=0)  # in default units
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    units: Mapped[list["ProductUnit"]] = relationship(back_populates="product", lazy="selectin")
    aliases: Mapped[list["ProductAlias"]] = relationship(back_populates="product", lazy="selectin")


class ShopAdvice(Row, Base):
    """A piece of advice the shop owner has written for callers (for an agriculture store: how and when to use a product, safety, pests).
    Sofa speaks the owner's words as written: it never makes farming advice up. A question with no matching entry goes to the owner."""

    __tablename__ = "shop_advice"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    topic: Mapped[str] = mapped_column(String(200))
    keywords: Mapped[str] = mapped_column(Text, default="")  # comma separated: words a caller would use that point to this advice
    answer: Mapped[str] = mapped_column(Text)  # what Sofa says, as the owner wrote it
    product_name: Mapped[str | None] = mapped_column(String(200))  # a product Sofa then offers to add to the order
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class ProductUnit(Row, Base):
    __tablename__ = "product_units"
    product_id: Mapped[uuid.UUID] = fk("products")
    unit: Mapped[str] = mapped_column(String(20))
    qty_per_unit: Mapped[int] = mapped_column(Integer, default=1)  # in default units
    price_kobo: Mapped[int] = mapped_column(BigInteger)
    product: Mapped[Product] = relationship(back_populates="units")


class ProductAlias(Row, Base):
    __tablename__ = "product_aliases"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    product_id: Mapped[uuid.UUID] = fk("products")
    phrase: Mapped[str] = mapped_column(String(200), index=True)
    language: Mapped[str | None] = mapped_column(String(2))
    # seed | confirmed_call | merchant_correction | global
    source: Mapped[str] = mapped_column(String(30), default="seed")
    confirmations: Mapped[int] = mapped_column(Integer, default=0)
    product: Mapped[Product] = relationship(back_populates="aliases")


class Unit(Row, Base):
    __tablename__ = "units"
    canonical: Mapped[str] = mapped_column(String(20), unique=True)
    variants: Mapped[list] = mapped_column(JSON, default=list)


class StockMovement(Row, Base):
    __tablename__ = "stock_movements"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    product_id: Mapped[uuid.UUID] = fk("products")
    delta: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(20))  # order | restock | adjustment
    source: Mapped[str] = mapped_column(String(10), default="voice")  # voice | web | sms
    call_turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)


# ---- customers ---------------------------------------------------------------------------


class Customer(Row, Base):
    __tablename__ = "customers"
    phone: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str | None] = mapped_column(String(200))
    language: Mapped[str | None] = mapped_column(String(2))
    consent_recorded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CustomerProfile(Row, Base):
    __tablename__ = "customer_profiles"
    __table_args__ = (UniqueConstraint("customer_id", "merchant_id"),)
    customer_id: Mapped[uuid.UUID] = fk("customers")
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    delivery_address: Mapped[str | None] = mapped_column(Text)
    landmark: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(Text)
    payment_mode_override: Mapped[str | None] = mapped_column(String(20))


class VirtualAccount(Row, Base):
    __tablename__ = "virtual_accounts"
    __table_args__ = (UniqueConstraint("customer_id", "merchant_id"),)
    customer_id: Mapped[uuid.UUID] = fk("customers")
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    provider: Mapped[str] = mapped_column(String(20), default="paystack")
    account_number: Mapped[str] = mapped_column(String(20), index=True)
    bank_name: Mapped[str] = mapped_column(String(100))
    provider_customer_code: Mapped[str | None] = mapped_column(String(50))


# ---- calls -------------------------------------------------------------------------------


class Call(Row, Base):
    __tablename__ = "calls"
    # None for a gateway call: the merchant (or bank) is chosen after the caller says what they need.
    merchant_id: Mapped[uuid.UUID | None] = fk("merchants", nullable=True)
    customer_id: Mapped[uuid.UUID | None] = fk("customers", nullable=True)
    direction: Mapped[str] = mapped_column(String(10), default="inbound")
    provider_session_id: Mapped[str] = mapped_column(String(100), index=True)
    from_number: Mapped[str] = mapped_column(String(20))
    to_number: Mapped[str] = mapped_column(String(20))
    language: Mapped[str | None] = mapped_column(String(2))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(30))
    handoff_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    duration_s: Mapped[int | None] = mapped_column(Integer)
    cost: Mapped[str | None] = mapped_column(String(30))
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    service_domain: Mapped[str | None] = mapped_column(String(20))  # gateway calls: last service the caller asked for
    turns: Mapped[list["CallTurn"]] = relationship(back_populates="call", order_by="CallTurn.seq")


class CallTurn(Row, Base):
    __tablename__ = "call_turns"
    call_id: Mapped[uuid.UUID] = fk("calls")
    seq: Mapped[int] = mapped_column(Integer)
    recording_url: Mapped[str | None] = mapped_column(Text)
    audio_path: Mapped[str | None] = mapped_column(Text)
    asr_model: Mapped[str | None] = mapped_column(String(100))
    transcript: Mapped[str | None] = mapped_column(Text)
    asr_confidence: Mapped[float | None] = mapped_column(Float)
    asr_alternatives: Mapped[list | None] = mapped_column(JSON)  # other models' readings: [{language, model, text, confidence}]
    llm_json: Mapped[dict | None] = mapped_column(JSON)
    match_candidates: Mapped[list | None] = mapped_column(JSON)
    action_taken: Mapped[str | None] = mapped_column(String(50))
    reply_text: Mapped[str | None] = mapped_column(Text)
    reply_audio_path: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[dict | None] = mapped_column(JSON)  # per stage + total
    error: Mapped[str | None] = mapped_column(Text)
    # labelling (admin screen)
    labelled_by: Mapped[str | None] = mapped_column(String(100))
    labelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    label: Mapped[dict | None] = mapped_column(JSON)
    call: Mapped[Call] = relationship(back_populates="turns")


# ---- orders ------------------------------------------------------------------------------


class Order(Row, Base):
    __tablename__ = "orders"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    customer_id: Mapped[uuid.UUID] = fk("customers")
    # draft confirmed awaiting_payment paid dispatched delivered cancelled
    status: Mapped[str] = mapped_column(String(20), default="draft", index=True)
    total_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    delivery_address: Mapped[str | None] = mapped_column(Text)
    channel: Mapped[str] = mapped_column(String(10), default="voice")
    source_call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    payment_mode: Mapped[str] = mapped_column(String(20), default="transfer_first")
    stock_reserved: Mapped[bool] = mapped_column(Boolean, default=False)
    items: Mapped[list["OrderItem"]] = relationship(back_populates="order", lazy="selectin", cascade="all, delete-orphan")


class OrderItem(Row, Base):
    __tablename__ = "order_items"
    order_id: Mapped[uuid.UUID] = fk("orders")
    product_id: Mapped[uuid.UUID] = fk("products")
    unit: Mapped[str] = mapped_column(String(20))
    qty: Mapped[int] = mapped_column(Integer)
    unit_price_kobo: Mapped[int] = mapped_column(BigInteger)
    spoken_name: Mapped[str | None] = mapped_column(String(200))  # kept for alias learning
    order: Mapped[Order] = relationship(back_populates="items")
    product: Mapped[Product] = relationship(lazy="selectin")


class OrderEvent(Row, Base):
    __tablename__ = "order_events"
    order_id: Mapped[uuid.UUID] = fk("orders")
    from_status: Mapped[str | None] = mapped_column(String(20))
    to_status: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(50))


class Invoice(Row, Base):
    __tablename__ = "invoices"
    order_id: Mapped[uuid.UUID] = fk("orders")
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    customer_id: Mapped[uuid.UUID] = fk("customers")
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    reference: Mapped[str] = mapped_column(String(20), unique=True)
    sent_via: Mapped[list] = mapped_column(JSON, default=list)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | paid | void


class Payment(Row, Base):
    __tablename__ = "payments"
    invoice_id: Mapped[uuid.UUID | None] = fk("invoices", nullable=True)
    provider: Mapped[str] = mapped_column(String(20), default="paystack")
    provider_reference: Mapped[str | None] = mapped_column(String(100))
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    sender_name: Mapped[str | None] = mapped_column(String(200))
    channel: Mapped[str | None] = mapped_column(String(30))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    match_status: Mapped[str] = mapped_column(String(10), default="unmatched")  # auto | manual | unmatched
    account_number: Mapped[str | None] = mapped_column(String(20))


class Notification(Row, Base):
    __tablename__ = "notifications"
    merchant_id: Mapped[uuid.UUID | None] = fk("merchants", nullable=True)
    order_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    channel: Mapped[str] = mapped_column(String(10), default="sms")  # sms | voice
    template: Mapped[str] = mapped_column(String(50))
    to_number: Mapped[str] = mapped_column(String(20))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="queued")
    provider_message_id: Mapped[str | None] = mapped_column(String(100))


class OutboundCall(Row, Base):
    __tablename__ = "outbound_calls"
    order_id: Mapped[uuid.UUID] = fk("orders")
    trigger: Mapped[str] = mapped_column(String(30))  # payment_received | dispatched | delivered
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    # queued -> calling -> answered | (queued again) | no_answer | failed | expired | skipped_limit | simulated
    status: Mapped[str] = mapped_column(String(20), default="queued")
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    # queued: do not call before this time. calling: if still ringing after this time, count it as unanswered.
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_session_id: Mapped[str | None] = mapped_column(String(100), index=True)
    last_error: Mapped[str | None] = mapped_column(String(200))


class LateReply(Row, Base):
    """The answer to a request that took so long the caller was told to try again; SOFA phones back with it (services/latereply.py)."""
    __tablename__ = "late_replies"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    session_id: Mapped[str] = mapped_column(String(100))  # the call it belongs to, whose state a call back carries on from
    text: Mapped[str] = mapped_column(Text, default="")
    audio: Mapped[str] = mapped_column(Text, default="[]")  # JSON list of the reply's audio urls
    lang: Mapped[str] = mapped_column(String(2), default="en")
    ends: Mapped[bool] = mapped_column(Boolean, default=False)  # the reply said goodbye: do not keep the line open
    # queued -> calling -> answered | (queued again) | no_answer | failed | expired | simulated
    status: Mapped[str] = mapped_column(String(20), default="queued")
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pin_attempts: Mapped[int] = mapped_column(Integer, default=0)  # wrong PINs typed on the call back
    provider_session_id: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(String(200))


class Handoff(Row, Base):
    __tablename__ = "handoffs"
    call_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    merchant_id: Mapped[uuid.UUID | None] = fk("merchants", nullable=True)
    reason: Mapped[str] = mapped_column(String(50))
    service_domain: Mapped[str | None] = mapped_column(String(20))  # gateway handoffs: the service whose provider follows up
    provider: Mapped[str | None] = mapped_column(String(30))  # which provider (a bank code, later others)
    summary: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(10), default="open")  # open | resolved
    resolved_by: Mapped[str | None] = mapped_column(String(100))


class MissedDemand(Row, Base):
    __tablename__ = "missed_demand"
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    spoken_name: Mapped[str] = mapped_column(String(200))
    call_turn_id: Mapped[uuid.UUID | None] = mapped_column(Uuid)
    count_on_day: Mapped[int] = mapped_column(Integer, default=1)


class ServiceLink(Row, Base):
    """A subscriber's connection to a provider, for example their Demo Bank account. Set up by default for partner
    services; a caller who has none can ask SOFA to connect them, which creates a pending link the provider activates.
    A link only says the provider is theirs: it never authorises an action (protected services verify the caller)."""

    __tablename__ = "service_links"
    __table_args__ = (UniqueConstraint("customer_id", "domain", "provider"),)
    customer_id: Mapped[uuid.UUID] = fk("customers")  # the subscriber: one row per phone number
    domain: Mapped[str] = mapped_column(String(20))  # banking | commerce | ...
    provider: Mapped[str] = mapped_column(String(30))  # bank code, for example demobank
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | pending | revoked
    linked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # last successful PIN check
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)  # wrong PINs or codes since the last success
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # no verification attempts before this


class MockBankAccount(Row, Base):
    """The bank's own system (see gateway/mockbank.py): a connected bank holds this."""

    __tablename__ = "mockbank_accounts"
    __table_args__ = (UniqueConstraint("customer_id", "provider"),)
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    account_number: Mapped[str] = mapped_column(String(20))
    balance_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    card_last4: Mapped[str | None] = mapped_column(String(4))
    card_blocked: Mapped[bool] = mapped_column(Boolean, default=False)


class MockBankIdentity(Row, Base):
    """The bank's record of a customer who already has an account with it. The BVN is held hashed, the way the bank
    would hold it; SOFA never has it. Used to check the BVN, date of birth and account number a caller types to connect their account."""

    __tablename__ = "mockbank_identities"
    __table_args__ = (UniqueConstraint("provider", "account_number"),)
    provider: Mapped[str] = mapped_column(String(30))
    account_number: Mapped[str] = mapped_column(String(10))
    full_name: Mapped[str] = mapped_column(String(200))
    dob: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD
    bvn_hash: Mapped[str] = mapped_column(String(64))
    balance_kobo: Mapped[int] = mapped_column(BigInteger, default=0)
    card_last4: Mapped[str | None] = mapped_column(String(4))


class MockBankLinkCheck(Row, Base):
    """One attempt to connect an existing account. The bank holds what was typed (BVN hashed) until it decides."""

    __tablename__ = "mockbank_link_checks"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(20), unique=True)
    dob: Mapped[str | None] = mapped_column(String(10))
    bvn_hash: Mapped[str | None] = mapped_column(String(64))
    account_number: Mapped[str | None] = mapped_column(String(10))
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | verified | mismatch | not_found


class MockBankBeneficiary(Row, Base):
    """A saved recipient. `nicknames` is how the account holder says it ("mama, mum")."""

    __tablename__ = "mockbank_beneficiaries"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    nicknames: Mapped[str] = mapped_column(String(200), default="")
    bank_name: Mapped[str] = mapped_column(String(100), default="")  # which bank this recipient's account is at
    account_number: Mapped[str] = mapped_column(String(10), default="")
    last_amount_kobo: Mapped[int | None] = mapped_column(BigInteger)  # "the usual"


class MockBankTransaction(Row, Base):
    """The bank's transaction ledger."""

    __tablename__ = "mockbank_transactions"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    kind: Mapped[str] = mapped_column(String(20))  # transfer | airtime | bill
    amount_kobo: Mapped[int] = mapped_column(BigInteger)
    counterparty: Mapped[str] = mapped_column(String(200))
    reference: Mapped[str] = mapped_column(String(20), unique=True)
    status: Mapped[str] = mapped_column(String(20), default="successful")
    balance_after_kobo: Mapped[int] = mapped_column(BigInteger)


class MockBankComplaint(Row, Base):
    """The bank's complaints desk."""

    __tablename__ = "mockbank_complaints"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    details: Mapped[str] = mapped_column(Text)
    reference: Mapped[str] = mapped_column(String(20), unique=True)


class MockBankApplication(Row, Base):
    """The bank's account-opening workflow. The bank sees the BVN; this model checks it and
    keeps only that it was given."""

    __tablename__ = "mockbank_applications"
    customer_id: Mapped[uuid.UUID] = fk("customers")
    provider: Mapped[str] = mapped_column(String(30))
    reference: Mapped[str] = mapped_column(String(20), unique=True)
    # draft -> awaiting_identity_verification -> under_review -> approved | rejected
    status: Mapped[str] = mapped_column(String(40), default="draft")
    full_name: Mapped[str | None] = mapped_column(String(200))
    dob: Mapped[str | None] = mapped_column(String(10))  # YYYY-MM-DD
    address: Mapped[str | None] = mapped_column(Text)
    bvn_given: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str | None] = mapped_column(String(200))  # why it was rejected


class WebhookEvent(Row, Base):
    """Idempotency: every provider event id is stored once."""

    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("provider", "event_id"),)
    provider: Mapped[str] = mapped_column(String(20))
    event_id: Mapped[str] = mapped_column(String(100))
    event_type: Mapped[str | None] = mapped_column(String(50))


class DailyReport(Row, Base):
    """One row per merchant per Lagos day: makes the 7 PM send idempotent and keeps what was sent."""

    __tablename__ = "daily_reports"
    __table_args__ = (UniqueConstraint("merchant_id", "day"),)
    merchant_id: Mapped[uuid.UUID] = fk("merchants")
    day: Mapped[str] = mapped_column(String(10))  # YYYY-MM-DD, Lagos
    status: Mapped[str] = mapped_column(String(20), default="pending")  # sent | failed | skipped_empty | no_recipient
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    body: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
