"""The same name at two banks: SOFA never guesses between them, and always says the bank."""

import pytest
from sqlalchemy import select

from sofa.gateway import mockbank
from sofa.models import Customer, MockBankAccount, MockBankTransaction
from tests.test_bank_ops import begin, enter, said
from tests.test_verification import AMINA, PIN, Caller, clock, v_app, v_client, v_db  # noqa: F401  (fixtures and helpers)

WHICH = ("Which one do you mean: Hauwa Bello at Access Bank, account ending 4 3 2 1 "
         "or Hauwa Bello at GT Bank, account ending 6 7 8 9?")


@pytest.fixture
def two_mamas(v_app):
    """Mama has an account at GT Bank (the fixture's) and now one at Access Bank too, both called mama."""
    with v_app.state.svc.session_factory() as db:
        me = db.scalar(select(Customer).where(Customer.phone == AMINA))
        mockbank.add_beneficiary(db, me, "demobank", "Hauwa Bello", "mama", 5_000, "Access Bank", "0987654321")
        db.commit()


def last_transaction(db):
    db.expire_all()
    return db.scalars(select(MockBankTransaction)).first()


def balance(db):
    db.expire_all()
    return db.scalar(select(MockBankAccount)).balance_kobo


def test_the_read_back_always_names_the_bank(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    assert "That is 2,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?" in said(v_db)


def test_two_accounts_with_the_same_name_are_asked_about_never_guessed(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send money to mama")
    enter(call)
    assert WHICH in said(v_db)
    assert balance(v_db) == 4_250_000 and last_transaction(v_db) is None


def test_the_caller_can_pick_by_bank_name(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send money to mama")
    enter(call)
    call.say("the Access one")
    assert "How much would you like to send?" in call.reply
    call.say("3000")
    assert "That is 3,000 naira to Hauwa Bello at Access Bank. Shall I go ahead?" in call.reply
    call.say("yes")
    call.type(PIN)
    assert "3,000 naira sent to Hauwa Bello at Access Bank" in call.reply
    assert last_transaction(v_db).counterparty == "Hauwa Bello at Access Bank" and balance(v_db) == 4_250_000 - 300_000


def test_the_caller_can_pick_by_the_last_digits(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send 1000 to mama")
    enter(call)
    call.say("the one ending 6 7 8 9")
    assert "That is 1,000 naira to Hauwa Bello at GT Bank." in call.reply


def test_the_caller_can_pick_by_place(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send 1000 to mama")
    enter(call)
    call.say("the first one")
    assert "That is 1,000 naira to Hauwa Bello at Access Bank." in call.reply


def test_the_usual_follows_the_account_that_was_picked(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    assert WHICH in said(v_db)
    call.say("GT bank")
    assert "That is 10,000 naira to Hauwa Bello at GT Bank, the same as last time. Shall I go ahead?" in call.reply


def test_a_nickname_that_fits_only_one_account_needs_no_question(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send 1000 to mum")  # only the GT Bank entry is called mum
    enter(call)
    assert "That is 1,000 naira to Hauwa Bello at GT Bank." in said(v_db)


def test_an_answer_that_never_picks_one_cancels_without_sending(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send 1000 to mama")
    enter(call)
    call.say("hmm")
    assert "Which one do you mean" in call.reply
    call.say("ehh")
    assert "I have not done anything" in call.reply
    assert last_transaction(v_db) is None


def test_changing_the_subject_at_the_which_question_drops_the_transfer(v_client, v_db, two_mamas):
    call = Caller(v_client)
    begin(call, "send 1000 to mama")
    enter(call)
    call.say("actually I want to buy something on CI Store")
    assert "CI Store" in call.reply and last_transaction(v_db) is None
