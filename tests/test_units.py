import pytest

from sofa.dialogue.validator import check_say, fill
from sofa.services.notify import in_quiet_hours
from sofa.textutil import e164, naira, normalize_phrase, similarity
from datetime import datetime, timezone


def test_fill_and_reject_unknown_placeholder():
    facts = {"p1": {"name": "Super Pack", "price": "350 naira"}, "total": "700 naira"}
    assert fill("{p1.name} at {p1.price}", facts) == "Super Pack at 350 naira"
    with pytest.raises(KeyError):
        fill("{p9.name}", facts)


def test_validator_accepts_placeholder_only_facts():
    facts = {"p1": {"name": "Super Pack", "price": "350 naira"}, "p2": {"name": "Hungry Man", "price": "500 naira"}}
    say = "We have {p1.name} at {p1.price}, or {p2.name} at {p2.price}. Which one?"
    assert check_say(say, facts) == "We have Super Pack at 350 naira, or Hungry Man at 500 naira. Which one?"


@pytest.mark.parametrize("say", [
    "We have Super Pack at 350 naira. Which one?",         # invented digit
    "We have Super Pack for three hundred. Which one?",    # spelled-out number
    "We have {p9.name}. Which one?",                        # unknown placeholder
    "We have {p1.name}.",                                   # no question
    "Is it {p1.name}? Or something else? Tell me more?",    # too many questions/sentences
    "",
])
def test_validator_rejects(say):
    assert check_say(say, {"p1": {"name": "Super Pack", "price": "350 naira"}}) is None


def test_money_and_phone_formatting():
    assert naira(1400000) == "14,000 naira"
    assert naira(123450) == "1,234.50 naira"
    assert e164("0803 111 2222") == "+2348031112222"
    assert e164("2348031112222") == "+2348031112222"


def test_normalize_and_similarity():
    assert normalize_phrase("Please give me two cartons of Indomie") == "two cartons indomie"
    assert similarity("indomie big", "indomie big") == 1.0
    assert similarity("peak milk", "golden penny") < 0.3


def test_quiet_hours_use_lagos_time():
    # 21:30 UTC = 22:30 Lagos: quiet. 08:00 UTC = 09:00 Lagos: allowed.
    assert in_quiet_hours("21:00-07:00", datetime(2026, 10, 5, 21, 30, tzinfo=timezone.utc))
    assert not in_quiet_hours("21:00-07:00", datetime(2026, 10, 5, 8, 0, tzinfo=timezone.utc))
