"""Turn labelling: staff play a turn, correct the transcript, intent and product match.
Labels are the validation evidence (WER, intent and match accuracy) and the training set."""

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clients.llm import Intent
from ..models import Call, CallTurn, Product
from .catalog import add_alias

INTENTS = list(Intent.__args__)
FLAGS = ["background noise", "accent or dialect", "code-switching", "unclear audio", "caller speaking to someone else"]


def next_unlabelled(db: Session, *, include_test: bool = False, language: str | None = None,
                    after: CallTurn | None = None) -> CallTurn | None:
    q = select(CallTurn).where(CallTurn.labelled_at.is_(None), CallTurn.transcript.is_not(None), CallTurn.transcript != "")
    q = q.join(Call, Call.id == CallTurn.call_id)
    if not include_test:
        q = q.where(Call.is_test.is_(False))
    if language:
        q = q.where(Call.language == language)
    if after is not None:
        q = q.where(CallTurn.id != after.id)
    return db.scalar(q.order_by(Call.started_at, CallTurn.seq))


def progress(db: Session, include_test: bool = False) -> tuple[int, int]:
    """(labelled, total) turns that have a transcript."""
    base = select(CallTurn).where(CallTurn.transcript.is_not(None), CallTurn.transcript != "").join(Call, Call.id == CallTurn.call_id)
    if not include_test:
        base = base.where(Call.is_test.is_(False))
    turns = list(db.scalars(base))
    return sum(1 for t in turns if t.labelled_at), len(turns)


def system_matches(turn: CallTurn) -> list[dict]:
    """What the system decided for each spoken item this turn: [{spoken, chosen, status}]."""
    return [
        {"spoken": m.get("spoken", ""), "chosen": m.get("chosen"), "status": m.get("status"), "step": m.get("step")}
        for m in (turn.match_candidates or []) if m.get("step") != "clarification" or m.get("chosen")
    ]


def apply_label(db: Session, turn: CallTurn, data: dict, who: str) -> None:
    """Save a label. Where staff correct a product match, the phrase is taught as a merchant_correction
    alias (these outrank aliases learned from confirmed calls)."""
    call = db.get(Call, turn.call_id)
    matches = []
    for m in data.get("product_matches", []):
        correct = (m.get("correct_product") or "").strip() or None
        matches.append({"spoken": m.get("spoken", ""), "system_choice": m.get("system_choice"), "correct_product": correct})
        if correct and correct != m.get("system_choice") and m.get("spoken"):
            product = db.scalar(select(Product).where(Product.merchant_id == call.merchant_id, Product.name == correct))
            if product:
                add_alias(db, product, m["spoken"], "merchant_correction", call.language)
    turn.label = {
        "transcript": (data.get("transcript") or "").strip(),
        "intent": data.get("intent") or None,
        "language": data.get("language") or call.language,
        "product_matches": matches,
        "flags": [f for f in data.get("flags", []) if f in FLAGS],
        "notes": (data.get("notes") or "").strip(),
    }
    turn.labelled_by, turn.labelled_at = who, datetime.now(timezone.utc)
