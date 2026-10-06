"""Banking at the front desk: finding which bank the caller means from the banks connected to their number."""

from sqlalchemy import select

from sofa.gateway import links
from sofa.models import Customer, Handoff, ServiceLink
from tests.test_gateway import gw_app, gw_call, gw_client, gw_db  # noqa: F401  (fixtures)

AMINA, TUNDE, NOBODY = "+2348055550101", "+2348055550102", "+2348055550103"


def subscriber(db, phone, banks=(), name=None):
    c = Customer(phone=phone, name=name)
    db.add(c)
    db.flush()
    for code in banks:
        links.link(db, c.id, "banking", code)
    db.commit()
    return c


def test_no_bank_connected_offers_to_connect_one(gw_call, gw_db):
    subscriber(gw_db, NOBODY)
    replies = gw_call(["I want to check my bank balance", "yes", "Demo Bank"], caller=NOBODY)
    assert "could not find any bank under your number" in replies[1]
    assert "Demo Bank, GT Bank or Access Bank" in replies[2]
    # an existing account is connected by checking details with the bank, not by passing the request on
    assert "To connect your Demo Bank account" in replies[3] and "date of birth, your BVN and your account number" in replies[3]
    assert gw_db.scalar(select(ServiceLink)) is None and gw_db.scalar(select(Handoff)) is None  # nothing is linked until the bank has checked


def test_naming_a_bank_that_is_not_connected_offers_that_bank(gw_call, gw_db):
    subscriber(gw_db, NOBODY)
    replies = gw_call(["I want my Demo Bank balance", "yes please"], caller=NOBODY)
    assert "could not find a Demo Bank account" in replies[1]
    assert "To connect your Demo Bank account" in replies[2]  # named already, so no "which one?"


def test_declining_the_offer_to_connect(gw_call, gw_db):
    subscriber(gw_db, NOBODY)
    replies = gw_call(["bank balance", "no thanks"], caller=NOBODY)
    assert "No problem" in replies[2]
    assert gw_db.scalar(select(ServiceLink)) is None and gw_db.scalar(select(Handoff)) is None


def test_one_bank_is_confirmed_by_name(gw_call, gw_db):
    subscriber(gw_db, AMINA, ["demobank"])
    replies = gw_call(["I want to send money", "yes"], caller=AMINA)
    assert "I found your Demo Bank account" in replies[1]
    assert "Before I open your Demo Bank account, please enter your PIN" in replies[2]  # "send money" was already said


def test_one_bank_the_caller_says_no_to(gw_call, gw_db):
    subscriber(gw_db, AMINA, ["demobank"])
    replies = gw_call(["bank transfer", "no"], caller=AMINA)
    assert "No problem" in replies[2]


def test_several_banks_ask_which_one(gw_call, gw_db):
    subscriber(gw_db, TUNDE, ["demobank", "gtbank"])
    replies = gw_call(["my bank balance please", "GT bank please"], caller=TUNDE)
    assert "Demo Bank or GT Bank" in replies[1]
    assert "Before I open your GT Bank account" in replies[2]


def test_naming_a_connected_bank_skips_the_questions(gw_call, gw_db):
    subscriber(gw_db, TUNDE, ["demobank", "gtbank"])
    replies = gw_call(["my Demo Bank balance"], caller=TUNDE)
    assert "Before I open your Demo Bank account" in replies[1]


def test_changing_the_subject_while_choosing_a_bank(gw_call, gw_db):
    subscriber(gw_db, TUNDE, ["demobank", "gtbank"])
    replies = gw_call(["bank balance", "actually I want to buy something"], caller=TUNDE)
    assert "CI Store" in replies[2]


def test_a_bank_choice_that_never_lands_is_dropped_politely(gw_call, gw_db):
    subscriber(gw_db, TUNDE, ["demobank", "gtbank"])
    replies = gw_call(["bank balance", "hmm", "ehh"], caller=TUNDE)
    assert "Which one" in replies[2] and "No problem" in replies[3]


def test_other_peoples_banks_are_never_used(gw_call, gw_db):
    subscriber(gw_db, AMINA, ["demobank"])
    subscriber(gw_db, NOBODY)
    replies = gw_call(["bank balance"], caller=NOBODY)
    assert "could not find any bank" in replies[1]


def test_unresolved_banking_request_goes_to_the_chosen_bank(gw_call, gw_db):
    subscriber(gw_db, AMINA, ["demobank"])
    replies = gw_call(["I want my bank", "yes", "hmm", "mmm", "uhh"], caller=AMINA)
    assert "pass the details to Demo Bank" in replies[5]
    assert gw_db.scalar(select(Handoff)).provider == "demobank"


def test_the_bank_chosen_stays_for_the_rest_of_the_call(gw_call, gw_db):
    subscriber(gw_db, AMINA, ["demobank"])
    replies = gw_call(["I want my bank", "yes", "send money to my mum"], caller=AMINA)
    assert "We are on Demo Bank" in replies[2]
    assert "Before I open your Demo Bank account" in replies[3]  # no need to name the bank again
