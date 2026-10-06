"""Paystack charge.success handling: match to the oldest open invoice of the same amount,
otherwise record an unmatched payment and open a handoff."""

import json
import logging

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ..models import Customer, Handoff, Invoice, Merchant, Order, Payment, VirtualAccount, WebhookEvent
from ..textutil import naira
from . import orders as order_svc
from .notify import notify_owners, send_sms
from .outbound import schedule_callback

log = logging.getLogger("sofa.payments")


async def get_or_create_account(db: Session, services, customer: Customer, merchant: Merchant) -> VirtualAccount:
    acct = db.scalar(
        select(VirtualAccount).where(VirtualAccount.customer_id == customer.id, VirtualAccount.merchant_id == merchant.id)
    )
    if acct:
        return acct
    info = await services.paystack.create_dedicated_account(customer.phone, customer.name, merchant.paystack_subaccount_code)
    acct = VirtualAccount(
        customer_id=customer.id, merchant_id=merchant.id, account_number=info["account_number"],
        bank_name=info["bank_name"], provider_customer_code=info.get("customer_code"),
    )
    db.add(acct)
    db.flush()
    return acct


async def handle_paystack_event(db: Session, services, payload: dict) -> str:
    """Idempotent. Returns a short status string (also used by tests)."""
    event, data = payload.get("event"), payload.get("data", {})
    event_id = str(data.get("id") or data.get("reference") or "")
    if event != "charge.success":
        log.info("ignored paystack event %s", event)
        return "ignored"
    try:
        db.add(WebhookEvent(provider="paystack", event_id=event_id, event_type=event))
        db.flush()
    except IntegrityError:
        db.rollback()
        return "duplicate"

    auth = data.get("authorization", {})
    if auth.get("channel") != "dedicated_nuban":
        return "ignored_channel"
    account_number = auth.get("receiver_bank_account_number")
    amount = int(data.get("amount", 0))
    acct = db.scalar(select(VirtualAccount).where(VirtualAccount.account_number == account_number))
    payment = Payment(
        provider_reference=str(data.get("reference") or event_id), amount_kobo=amount,
        sender_name=auth.get("sender_name"), channel="dedicated_nuban", account_number=account_number,
    )
    db.add(payment)
    invoice = None
    if acct:
        invoice = db.scalar(
            select(Invoice)
            .where(Invoice.customer_id == acct.customer_id, Invoice.merchant_id == acct.merchant_id,
                   Invoice.status == "open", Invoice.amount_kobo == amount)
            .order_by(Invoice.created_at)
        )
    if invoice:
        payment.invoice_id, payment.match_status = invoice.id, "auto"
        await settle_invoice(db, services, invoice, "paystack", amount)
        return "matched"

    # short payment, overpayment, or two open invoices: a human resolves it in the admin page
    merchant_id = acct.merchant_id if acct else None
    payment.match_status = "unmatched"
    if merchant_id:
        db.add(Handoff(merchant_id=merchant_id, reason="unmatched_payment",
                       summary=f"{naira(amount)} received into {account_number} from {auth.get('sender_name')}, no exact invoice match."))
    db.flush()
    return "unmatched"


def parse_event(body: bytes) -> dict:
    return json.loads(body.decode("utf-8"))


async def settle_invoice(db: Session, services, invoice: Invoice, actor: str, amount_kobo: int) -> None:
    """Mark an invoice paid: order -> paid, text the customer and the owner, queue the payment callback."""
    invoice.status = "paid"
    order = db.get(Order, invoice.order_id)
    order_svc.transition(db, order, "paid", actor)
    customer, merchant = db.get(Customer, invoice.customer_id), db.get(Merchant, invoice.merchant_id)
    await send_sms(
        db, services, to=customer.phone, template="payment_received", merchant_id=merchant.id,
        order_id=order.id, customer_id=customer.id,
        body=f"{merchant.name}: payment of {naira(amount_kobo)} received for {invoice.reference}. Your order is being prepared.",
    )
    await notify_owners(db, services, merchant.id, f"Paid: {invoice.reference}, {naira(amount_kobo)} from {customer.phone}.", "merchant_paid", order.id)
    schedule_callback(db, order, "payment_received")
    db.flush()


def paid_total(db: Session, invoice: Invoice) -> int:
    return sum(p.amount_kobo for p in db.scalars(select(Payment).where(Payment.invoice_id == invoice.id)))


async def apply_payment(db: Session, services, payment: Payment, invoice: Invoice, actor: str = "admin") -> str:
    """Staff attach a payment to an invoice (manual entry or an unmatched webhook payment). The invoice
    is settled once its payments add up to the amount. Returns 'paid', 'part_paid' or 'overpaid'.
    Only call this after seeing the funds in the bank or Paystack dashboard, never on a screenshot alone."""
    if invoice.status != "open":
        raise ValueError(f"invoice {invoice.reference} is {invoice.status}, not open")
    payment.invoice_id, payment.match_status = invoice.id, "manual"
    db.flush()
    total = paid_total(db, invoice)
    if total < invoice.amount_kobo:
        return "part_paid"
    await settle_invoice(db, services, invoice, actor, total)
    return "overpaid" if total > invoice.amount_kobo else "paid"
