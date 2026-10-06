"""Draft orders, the status machine, stock reservation and invoices."""

import secrets
import string
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Invoice, Order, OrderEvent, OrderItem, Product, StockMovement, utcnow
from ..textutil import naira, plural_unit

TRANSITIONS = {
    "draft": {"confirmed", "cancelled"},
    "confirmed": {"awaiting_payment", "dispatched", "cancelled"},
    "awaiting_payment": {"paid", "cancelled"},
    "paid": {"dispatched"},
    "dispatched": {"delivered"},
    "delivered": {"paid"},  # pay-on-delivery orders are paid last
    "cancelled": set(),
}
REF_ALPHABET = string.ascii_uppercase.replace("O", "").replace("I", "") + "23456789"


class InvalidTransition(Exception):
    pass


def transition(db: Session, order: Order, to: str, actor: str) -> None:
    if to not in TRANSITIONS.get(order.status, set()):
        raise InvalidTransition(f"{order.status} -> {to}")
    db.add(OrderEvent(order_id=order.id, from_status=order.status, to_status=to, actor=actor))
    order.status = to


def unit_options(product: Product) -> list[tuple[str, int, int]]:
    """[(unit, price_kobo, qty_per_unit)] with the default unit always present."""
    opts = [(u.unit, u.price_kobo, u.qty_per_unit) for u in product.units]
    if not any(o[0] == product.default_unit for o in opts):
        opts.insert(0, (product.default_unit, product.price_kobo, 1))
    return opts


def unit_price(product: Product, unit: str) -> int | None:
    return next((price for u, price, _ in unit_options(product) if u == unit), None)


def base_qty(product: Product, unit: str, qty: int) -> int:
    per = next((q for u, _, q in unit_options(product) if u == unit), 1)
    return per * qty


def get_draft(db: Session, merchant_id, customer_id, call_id=None) -> Order:
    order = db.scalar(
        select(Order).where(Order.merchant_id == merchant_id, Order.customer_id == customer_id, Order.status == "draft")
    )
    if not order:
        order = Order(merchant_id=merchant_id, customer_id=customer_id, source_call_id=call_id)
        db.add(order)
        db.flush()
    return order


def recompute(order: Order) -> None:
    order.total_kobo = sum(i.qty * i.unit_price_kobo for i in order.items)


def add_item(db: Session, order: Order, product: Product, unit: str, qty: int, spoken: str | None) -> OrderItem:
    price = unit_price(product, unit)
    existing = next((i for i in order.items if i.product_id == product.id and i.unit == unit), None)
    if existing:
        existing.qty += qty
        item = existing
    else:
        item = OrderItem(
            order_id=order.id, product_id=product.id, unit=unit, qty=qty,
            unit_price_kobo=price, spoken_name=spoken, product=product,
        )
        order.items.append(item)
    recompute(order)
    db.flush()
    return item


def set_qty(order: Order, item: OrderItem, qty: int) -> None:
    item.qty = qty
    recompute(order)


def remove_item(db: Session, order: Order, item: OrderItem) -> None:
    order.items.remove(item)
    db.delete(item)
    recompute(order)


def describe_item(item: OrderItem) -> str:
    return f"{item.qty} {plural_unit(item.unit, item.qty)} of {item.product.name}"


def describe_items(order: Order, lang: str = "en") -> str:
    from ..dialogue.templates import join_list

    return join_list([describe_item(i) for i in order.items], lang)


def committed_base_qty(order: Order) -> dict:
    out: dict = {}
    for i in order.items:
        out[i.product_id] = out.get(i.product_id, 0) + base_qty(i.product, i.unit, i.qty)
    return out


def reserve_stock(db: Session, order: Order, source: str = "voice") -> None:
    for i in order.items:
        need = base_qty(i.product, i.unit, i.qty)
        i.product.stock_qty -= need
        db.add(StockMovement(merchant_id=order.merchant_id, product_id=i.product_id, delta=-need, reason="order", source=source))
    order.stock_reserved = True


def release_stock(db: Session, order: Order, source: str = "voice") -> None:
    if not order.stock_reserved:
        return
    for i in order.items:
        back = base_qty(i.product, i.unit, i.qty)
        i.product.stock_qty += back
        db.add(StockMovement(merchant_id=order.merchant_id, product_id=i.product_id, delta=back, reason="adjustment", source=source))
    order.stock_reserved = False


def new_reference(db: Session) -> str:
    while True:
        ref = "SOFA-" + "".join(secrets.choice(REF_ALPHABET) for _ in range(4))
        if not db.scalar(select(Invoice.id).where(Invoice.reference == ref)):
            return ref


def confirm_order(db: Session, order: Order, address: str | None, payment_mode: str, actor: str) -> Invoice | None:
    """draft -> confirmed, reserve stock; transfer-first orders go on to awaiting_payment with an invoice."""
    order.delivery_address = address
    order.payment_mode = payment_mode
    order.confirmed_at = utcnow()
    transition(db, order, "confirmed", actor)
    reserve_stock(db, order)
    invoice = None
    if payment_mode == "transfer_first":
        transition(db, order, "awaiting_payment", "system")
        invoice = Invoice(
            order_id=order.id, merchant_id=order.merchant_id, customer_id=order.customer_id,
            amount_kobo=order.total_kobo, reference=new_reference(db),
            due_at=datetime.now(timezone.utc) + timedelta(days=1),
        )
        db.add(invoice)
    db.flush()
    return invoice


def cancel_order(db: Session, order: Order, actor: str) -> None:
    release_stock(db, order)
    transition(db, order, "cancelled", actor)


def latest_order(db: Session, merchant_id, customer_id, statuses: tuple[str, ...] | None = None) -> Order | None:
    q = select(Order).where(Order.merchant_id == merchant_id, Order.customer_id == customer_id)
    q = q.where(Order.status.in_(statuses)) if statuses else q.where(Order.status != "draft")
    return db.scalar(q.order_by(Order.created_at.desc()))


def copy_into_draft(db: Session, source: Order, draft: Order) -> None:
    for i in list(draft.items):
        remove_item(db, draft, i)
    for i in source.items:
        add_item(db, draft, i.product, i.unit, i.qty, i.spoken_name)


def invoice_sms(merchant_name: str, order: Order, invoice: Invoice, account: dict) -> str:
    return (
        f"{merchant_name}: {describe_items(order)}. Total {naira(invoice.amount_kobo)}. "
        f"Pay to {account['bank_name']} {account['account_number']}. Ref {invoice.reference}. "
        f"This account is created for your payments to {merchant_name}."
    )


async def advance_order(db: Session, svc, order: Order, status: str, actor: str) -> None:
    """Admin action: move an order along the status machine. Dispatch and delivery text the customer
    and queue the callback; cancelling releases the reserved stock and voids the open invoice."""
    from ..models import Customer, Merchant
    from .notify import send_sms
    from .outbound import schedule_callback

    if status == "cancelled":
        cancel_order(db, order, actor)
        for inv in db.scalars(select(Invoice).where(Invoice.order_id == order.id, Invoice.status == "open")):
            inv.status = "void"
        return
    transition(db, order, status, actor)
    if status in ("dispatched", "delivered"):
        customer, merchant = db.get(Customer, order.customer_id), db.get(Merchant, order.merchant_id)
        text = {"dispatched": "your order is on its way.", "delivered": "your order has been delivered."}[status]
        await send_sms(db, svc, to=customer.phone, body=f"{merchant.name}: {text}", template=status,
                       merchant_id=merchant.id, order_id=order.id, customer_id=customer.id)
        schedule_callback(db, order, status)
