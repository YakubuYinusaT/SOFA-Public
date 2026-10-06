"""The shop owner's advice library, and finding the right piece for a caller's question.

For a shop that also advises (an agriculture store: which fertilizer for which crop, how much, when, how to spray safely) the owner writes
the advice once, and Sofa speaks it as written. Sofa never makes farming advice up: a model that is wrong about a dose is worse than
no answer. A question with no matching entry is passed to the owner, who follows up.

Matching is plain and checkable: each entry lists the words a caller would use, and the entry with the most of them in what was said wins.
At least two must match, so a single word such as "maize" does not pick an answer about spraying.
"""

import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ShopAdvice
from ..textutil import clean

MIN_MATCHES = 2


def keywords_of(entry: ShopAdvice) -> list[str]:
    return [clean(k) for k in re.split(r"[,\n]", entry.keywords or "") if clean(k)]


def score(entry: ShopAdvice, texts: list[str]) -> int:
    """How many of the entry's words appear in what the caller said (whole words or whole phrases, accents ignored)."""
    heard = " " + " ".join(clean(t) for t in texts) + " "
    return sum(1 for k in keywords_of(entry) if f" {k} " in heard)


def find(db: Session, merchant_id, texts: list[str]) -> tuple[ShopAdvice | None, int]:
    """The best entry for the question, or None when nothing matches well enough."""
    best, best_score = None, 0
    for entry in db.scalars(select(ShopAdvice).where(ShopAdvice.merchant_id == merchant_id, ShopAdvice.active.is_(True))
                            .order_by(ShopAdvice.created_at, ShopAdvice.id)):
        s = score(entry, texts)
        if s > best_score:
            best, best_score = entry, s
    return (best, best_score) if best_score >= MIN_MATCHES else (None, best_score)


def topics(db: Session, merchant_id, limit: int = 20) -> list[str]:
    """What the shop can advise on, for the model's prompt (it only has to recognise that a question is a request for advice)."""
    return [a.topic for a in db.scalars(select(ShopAdvice).where(ShopAdvice.merchant_id == merchant_id, ShopAdvice.active.is_(True))
                                        .order_by(ShopAdvice.created_at, ShopAdvice.id).limit(limit))]
