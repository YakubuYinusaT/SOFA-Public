"""Daily merchant report: what happened today, in one short SMS or read out over the phone.

Same numbers in both. The SMS must stay inside ONE segment (160 GSM-7 characters): a long text is
billed per segment and any character outside the basic GSM set (for example the naira sign) drops
the limit to 70. So the SMS writes "N184,500", uses plain ASCII, and drops the least important
parts until it fits.
"""

import re
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Call, CallTurn, Handoff, Merchant, MissedDemand, Order
from ..textutil import normalize_phrase

SMS_LIMIT = 160  # one GSM-7 segment
LAGOS = timedelta(hours=1)  # Africa/Lagos is UTC+1 all year


def to_lagos(now: datetime) -> datetime:
    """Lagos wall-clock time as a naive datetime."""
    return (now.astimezone(timezone.utc) + LAGOS).replace(tzinfo=None)


def day_bounds(now: datetime) -> tuple[datetime, datetime, str]:
    """(start, end) of the Lagos day containing `now`, as UTC datetimes, plus its YYYY-MM-DD label."""
    lagos = to_lagos(now)
    start_lagos = lagos.replace(hour=0, minute=0, second=0, microsecond=0)
    start = (start_lagos - LAGOS).replace(tzinfo=timezone.utc)
    return start, start + timedelta(days=1), start_lagos.date().isoformat()


@dataclass
class Report:
    merchant: str
    day: str  # YYYY-MM-DD, Lagos
    orders: int = 0
    value_kobo: int = 0
    unpaid: int = 0  # orders waiting for payment, whatever day they were placed
    missed: list[tuple[str, int]] = field(default_factory=list)  # (what callers asked for, how many), most asked first
    callbacks: int = 0  # open handoffs: callers waiting for the owner to call back

    @property
    def is_empty(self) -> bool:
        return not (self.orders or self.missed or self.callbacks)


def compute_report(db: Session, merchant: Merchant, now: datetime, include_test: bool = False) -> Report:
    start, end, day = day_bounds(now)
    test_calls = {c for (c,) in db.execute(select(Call.id).where(Call.is_test.is_(True)))} if not include_test else set()

    def real_order(o: Order) -> bool:
        return o.source_call_id not in test_calls

    todays = [o for o in db.scalars(select(Order).where(
        Order.merchant_id == merchant.id, Order.confirmed_at >= start, Order.confirmed_at < end,
        Order.status.not_in(("draft", "cancelled")))) if real_order(o)]
    awaiting = [o for o in db.scalars(select(Order).where(Order.merchant_id == merchant.id, Order.status == "awaiting_payment"))
                if real_order(o)]

    missed: Counter = Counter()
    rows = db.execute(select(MissedDemand.spoken_name, MissedDemand.count_on_day, Call.is_test)
                      .outerjoin(CallTurn, CallTurn.id == MissedDemand.call_turn_id)
                      .outerjoin(Call, Call.id == CallTurn.call_id)
                      .where(MissedDemand.merchant_id == merchant.id, MissedDemand.created_at >= start, MissedDemand.created_at < end))
    for spoken, n, is_test in rows:
        if is_test and not include_test:
            continue
        name = normalize_phrase(spoken)
        if name:
            missed[name] += n or 1

    handoffs = [h for h in db.scalars(select(Handoff).where(Handoff.merchant_id == merchant.id, Handoff.status == "open"))
                if h.call_id not in test_calls]
    return Report(merchant=merchant.name, day=day, orders=len(todays), value_kobo=sum(o.total_kobo for o in todays),
                  unpaid=len(awaiting), missed=missed.most_common(), callbacks=len(handoffs))


# ---- the SMS -----------------------------------------------------------------------------------------

_KEEP = re.compile(r"[^A-Za-z0-9 .,:;!?'()/+\-x%&=#\n]")


def to_gsm7(text: str) -> str:
    """Plain ASCII that is safe in a single GSM-7 segment: accents dropped, everything unusual removed."""
    text = unicodedata.normalize("NFKD", text).replace("’", "'").replace("‘", "'")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r" +", " ", _KEEP.sub("", text)).strip()


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def _short_date(day: str) -> str:
    d = datetime.strptime(day, "%Y-%m-%d")
    return f"{d.day}{d.strftime('%b')}"


def sms_text(r: Report) -> str:
    """One short SMS. Priority when space runs out: orders and value, unpaid, callbacks due, missed items."""
    name = to_gsm7(r.merchant)[:18].strip() or "Shop"
    head = f"{name} {_short_date(r.day)}: " + (
        f"{_plural(r.orders, 'order')}, N{r.value_kobo // 100:,}" if r.orders else "no orders")
    unpaid = f"{r.unpaid} unpaid" if r.unpaid else ""
    callbacks = f"{r.callbacks} {'callback' if r.callbacks == 1 else 'callbacks'} due" if r.callbacks else ""
    missed = [(to_gsm7(n)[:22].title(), c) for n, c in r.missed]

    def assemble(k: int) -> str:
        parts = [head + (f", {unpaid}" if unpaid else "") + "."]
        if k:
            parts.append("Missed: " + ", ".join(f"{n} x{c}" for n, c in missed[:k]) + ".")
        if callbacks:
            parts.append(callbacks[0].upper() + callbacks[1:] + ".")
        return " ".join(parts)

    for k in range(len(missed), -1, -1):  # keep as many missed items as still fit
        text = assemble(k)
        if len(text) <= SMS_LIMIT:
            return text
    return head[:SMS_LIMIT]  # a merchant name this long is truncated above, so this is a last resort


# ---- the spoken version --------------------------------------------------------------------------------


def spoken_missed(r: Report, limit: int = 3) -> list[str]:
    return [name.title() for name, _ in r.missed[:limit]]
