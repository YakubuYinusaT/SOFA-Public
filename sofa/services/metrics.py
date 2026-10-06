"""Validation metrics and anonymised exports: the NAIC "real-world validation" evidence.

Definitions follow the spec: an interaction is a completed non-test call with at least one
understood (non-unknown) request. WER, intent accuracy and match accuracy come from turns a
human has labelled, so they are only as good as the labelled sample: always show the sample size.
"""

import csv
import io
import json
import statistics
from collections import defaultdict
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Call, CallTurn, Customer, Merchant, Order
from ..textutil import wer
from .redact import Redactor, pseudonym

COMPLETED = ("order_confirmed", "stock_updated", "answered")


def _intent(t: CallTurn) -> str | None:
    return (t.llm_json or {}).get("intent")


def _understood(c: Call) -> bool:
    return any(_intent(t) not in (None, "unknown") for t in c.turns)


def _median(values: list[float]):
    return statistics.median(values) if values else None


def _rate(num: int, den: int):
    return round(num / den, 3) if den else None


def clarification_counts(calls: list[Call]) -> tuple[int, int]:
    """(items that needed a clarifying question, items matched at all) from the logged match steps."""
    asked = total = 0
    for c in calls:
        for t in c.turns:
            for m in t.match_candidates or []:
                if m.get("step") in ("clarification", "empty") or m.get("status") not in ("match", "ambiguous", "none"):
                    continue
                total += 1
                asked += m.get("status") == "ambiguous"
    return asked, total


def call_group_stats(calls: list[Call]) -> dict:
    handoffs = sum(1 for c in calls if c.handoff_id)
    done = sum(1 for c in calls if not c.handoff_id and c.outcome in COMPLETED)
    asked, total = clarification_counts(calls)
    latencies = [t.latency_ms["total"] for c in calls for t in c.turns if t.latency_ms and "total" in t.latency_ms]
    return {
        "calls": len(calls),
        "task_completion_rate": _rate(done, len(calls)),
        "handoff_rate": _rate(handoffs, len(calls)),
        "clarification_rate": _rate(asked, total),
        "median_latency_ms": _median(latencies),
    }


def real_calls(db: Session) -> list[Call]:
    calls = db.scalars(select(Call).where(Call.is_test.is_(False)).order_by(Call.started_at))
    return [c for c in calls if _understood(c)]


def order_totals(db: Session) -> tuple[int, int]:
    test_ids = {c.id for c in db.scalars(select(Call).where(Call.is_test.is_(True)))}
    orders = [o for o in db.scalars(select(Order).where(Order.status != "draft", Order.status != "cancelled"))
              if o.source_call_id not in test_ids]
    return len(orders), sum(o.total_kobo for o in orders)


def labelled_turns(db: Session) -> list[tuple[CallTurn, Call]]:
    rows = db.execute(select(CallTurn, Call).join(Call, Call.id == CallTurn.call_id)
                      .where(CallTurn.labelled_at.is_not(None), Call.is_test.is_(False)))
    return [(t, c) for t, c in rows if t.label]


def language_breakdown(db: Session) -> list[dict]:
    groups: dict[str, list[tuple[CallTurn, Call]]] = defaultdict(list)
    for t, c in labelled_turns(db):
        groups[(t.label.get("language") or c.language or "?")].append((t, c))
    out = []
    for lang, rows in sorted(groups.items()):
        wers = [wer(t.label["transcript"], t.transcript or "") for t, _ in rows if t.label.get("transcript")]
        intents = [(t.label["intent"], _intent(t)) for t, _ in rows if t.label.get("intent")]
        matches = [m for t, _ in rows for m in t.label.get("product_matches", []) if m.get("system_choice")]
        out.append({
            "language": lang, "labelled_turns": len(rows),
            "wer_mean": round(statistics.mean(wers), 3) if wers else None, "wer_sample": len(wers),
            "intent_accuracy": _rate(sum(1 for a, b in intents if a == b), len(intents)), "intent_sample": len(intents),
            "match_accuracy": _rate(sum(1 for m in matches if m.get("correct_product") == m["system_choice"]), len(matches)),
            "match_sample": len(matches),
            "code_mixed": sum(1 for t, _ in rows if "code-switching" in t.label.get("flags", [])),
        })
    return out


def early_vs_late(db: Session, n: int = 20) -> dict | None:
    """The 'system improves' evidence: first calls against last calls of the pilot."""
    calls = real_calls(db)
    k = min(n, len(calls) // 2)
    if k < 3:
        return None
    return {"window": k, "first": call_group_stats(calls[:k]), "last": call_group_stats(calls[-k:]),
            "note": None if k == n else f"fewer than {2 * n} real calls so far: comparing the first {k} with the last {k}"}


def per_day(db: Session) -> list[dict]:
    days: dict[str, int] = defaultdict(int)
    for c in real_calls(db):
        days[(c.started_at.date() if c.started_at else None).isoformat()] += 1
    return [{"date": d, "real_interactions": n} for d, n in sorted(days.items())]


def validation_metrics(db: Session) -> dict:
    all_calls = list(db.scalars(select(Call).where(Call.is_test.is_(False))))
    real = real_calls(db)
    stats = call_group_stats(all_calls)
    n_orders, value = order_totals(db)
    labelled = labelled_turns(db)
    return {
        "real_interactions": len(real),
        "unique_callers": len({c.from_number for c in real}),
        "task_completion_rate": stats["task_completion_rate"] if all_calls else None,
        "handoff_rate": stats["handoff_rate"] if all_calls else None,
        "clarification_rate": stats["clarification_rate"],
        "median_latency_ms": stats["median_latency_ms"],
        "confirmed_orders": n_orders,
        "order_value_naira": value / 100,
        "labelled_turns": len(labelled),
        "code_mixed_rate": _rate(sum(1 for t, _ in labelled if "code-switching" in t.label.get("flags", [])), len(labelled)),
        "by_language": language_breakdown(db),
        "early_vs_late": early_vs_late(db),
        "per_day": per_day(db),
    }


# ---- anonymised exports -----------------------------------------------------------------------------


def _csv(rows: list[dict], fields: list[str]) -> str:
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=fields)
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def export_calls_csv(db: Session, secret: str) -> str:
    rows = []
    for c in db.scalars(select(Call).where(Call.is_test.is_(False)).order_by(Call.started_at)):
        rows.append({
            "call": pseudonym(str(c.id), secret, "call"), "caller": pseudonym(c.from_number, secret, "caller"),
            "merchant": db.get(Merchant, c.merchant_id).name if c.merchant_id else "gateway", "language": c.language, "started_at": c.started_at,
            "duration_s": c.duration_s, "turns": len(c.turns), "understood": _understood(c),
            "outcome": c.outcome, "handoff": bool(c.handoff_id),
        })
    return _csv(rows, ["call", "caller", "merchant", "language", "started_at", "duration_s", "turns", "understood", "outcome", "handoff"])


def export_turns_csv(db: Session, secret: str) -> str:
    redact, rows = Redactor(db), []
    for c in db.scalars(select(Call).where(Call.is_test.is_(False)).order_by(Call.started_at)):
        for t in c.turns:
            matches = [m.get("status") for m in (t.match_candidates or [])]
            rows.append({
                "call": pseudonym(str(c.id), secret, "call"), "seq": t.seq, "language": c.language,
                "transcript": redact(t.transcript), "asr_model": t.asr_model,
                "asr_confidence": None if t.asr_confidence is None else round(t.asr_confidence, 3),
                "intent": _intent(t), "match_status": "|".join(str(m) for m in matches),
                "action": (t.action_taken or "").split("|")[0], "reply": redact(t.reply_text),
                "latency_ms": (t.latency_ms or {}).get("total"), "error": bool(t.error),
            })
    return _csv(rows, ["call", "seq", "language", "transcript", "asr_model", "asr_confidence", "intent",
                       "match_status", "action", "reply", "latency_ms", "error"])


def _consented(db: Session, call: Call) -> bool:
    customer = db.get(Customer, call.customer_id) if call.customer_id else None
    return bool(customer and customer.consent_recorded_at)


def export_speech_jsonl(db: Session, storage_dir: str) -> str:
    """audio + corrected transcript, labelled turns only, where the customer heard the recording notice."""
    redact, lines = Redactor(db), []
    for t, c in labelled_turns(db):
        if not (t.audio_path and t.label.get("transcript") and _consented(db, c)):
            continue
        try:
            rel = str(Path(t.audio_path).resolve().relative_to(Path(storage_dir).resolve()))
        except ValueError:
            rel = Path(t.audio_path).name
        lines.append(json.dumps({
            "audio": rel, "language": t.label.get("language") or c.language, "text": redact(t.label["transcript"]),
            "asr_text": redact(t.transcript), "flags": t.label.get("flags", []),
        }, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")


def export_understanding_jsonl(db: Session) -> str:
    """transcript -> corrected intent JSON, personal fields removed."""
    redact, lines = Redactor(db), []
    for t, c in labelled_turns(db):
        if not (t.label.get("transcript") and _consented(db, c)):
            continue
        output = dict(t.llm_json or {})
        output.pop("say", None)
        output["customer_name"], output["delivery_note"] = None, None
        if t.label.get("intent"):
            output["intent"] = t.label["intent"]
        output["items"] = [{**i, "spoken_name": redact(i.get("spoken_name"))} for i in output.get("items", [])]
        lines.append(json.dumps({"language": t.label.get("language") or c.language, "input": redact(t.label["transcript"]),
                                 "output": output}, ensure_ascii=False))
    return "\n".join(lines) + ("\n" if lines else "")
