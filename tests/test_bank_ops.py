"""Banking operations through the gateway: ask for what is missing, read back, verify, run at the bank, speak its answer."""

import pytest
from sqlalchemy import select

from sofa.models import CallTurn, Handoff, MockBankAccount, MockBankComplaint, MockBankTransaction, Notification
from tests.test_verification import AMINA, OTP, PIN, Caller, clock, v_app, v_client, v_db  # noqa: F401  (fixtures and helpers)


def said(db):
    """Everything SOFA said on the last turn (the header only carries the first clip)."""
    db.expire_all()
    return db.scalars(select(CallTurn).order_by(CallTurn.created_at.desc(), CallTurn.seq.desc())).first().reply_text


def balance(db):
    db.expire_all()
    return db.scalar(select(MockBankAccount)).balance_kobo


def transactions(db):
    db.expire_all()
    return list(db.scalars(select(MockBankTransaction)))


def begin(call, words):
    """Say what is wanted and confirm the bank (the caller has one bank linked, so SOFA checks which)."""
    call.say(words)
    call.say("yes")


def enter(call):
    """PIN, then the SMS code."""
    call.type(PIN)
    call.type(OTP)


def test_send_the_usual_to_mama(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    assert call.keypad and "Before I open your" in call.reply  # nothing is read back before the caller is verified
    call.type(PIN)
    call.type(OTP)
    assert "That is 10,000 naira to Hauwa Bello at GT Bank, the same as last time. Shall I go ahead?" in said(v_db)
    assert transactions(v_db) == [] and balance(v_db) == 4_250_000  # nothing has moved yet
    call.say("yes")
    assert call.keypad and "To authorise this, please enter your PIN" in call.reply  # the PIN from opening the account does not cover this
    assert transactions(v_db) == [] and balance(v_db) == 4_250_000  # nothing moves before it
    call.type(PIN)
    assert not call.keypad
    assert "Done. 10,000 naira sent to Hauwa Bello at GT Bank. Your balance is now 32,500 naira." in call.reply
    assert balance(v_db) == 3_250_000 and len(transactions(v_db)) == 1


def test_every_money_move_has_its_own_pin_after_its_own_read_back(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)  # the first transfer
    call.say("send the usual to mama")
    assert not call.keypad and "Shall I go ahead" in call.reply  # already verified for the account: details first
    call.say("yes")
    assert call.keypad and "To authorise this, please enter your PIN" in call.reply
    assert len(transactions(v_db)) == 1  # the second has not moved yet
    call.type(PIN)
    assert len(transactions(v_db)) == 2


def test_someone_else_saying_yes_after_the_account_was_opened_cannot_move_money(v_client, v_db):
    """Amina verified a moment ago and passes the phone on. A bare yes is not enough: it only leads to the PIN prompt."""
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)  # Amina's PIN and code open the account for this transfer
    assert "Shall I go ahead" in said(v_db)
    call.say("yes")
    assert call.keypad and transactions(v_db) == []
    call.type("0000")  # the other person does not know the PIN
    assert call.keypad and "PIN was not correct" in call.reply
    assert transactions(v_db) == [] and balance(v_db) == 4_250_000


def test_three_wrong_pins_at_the_authorisation_stop_the_transfer_and_tell_the_bank(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    call.say("yes")
    for wrong in ("0000", "1111", "2222"):
        call.type(wrong)
    assert not call.keypad and "I will let Demo Bank know" in call.reply
    assert transactions(v_db) == []
    assert v_db.scalar(select(Handoff)).reason == "verification_failed"


def test_typing_nothing_at_the_authorisation_cancels_the_transfer(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    call.say("yes")
    call.type("")
    assert not call.keypad and "No problem, I will leave it there" in call.reply
    assert transactions(v_db) == []


def test_looking_things_up_needs_no_authorising_pin(v_client, v_db):
    call = Caller(v_client)
    call.verified()
    call.say("my balance again please")
    assert not call.keypad and "Your Demo Bank balance is 42,500 naira" in call.reply  # only money moves are authorised each time


def test_a_missing_amount_is_asked_for_in_words(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send money to my mum")
    enter(call)
    assert "How much would you like to send?" in said(v_db)
    call.say("five thousand naira")
    assert "That is 5,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?" in call.reply
    call.say("yes")
    call.type(PIN)
    assert "5,000 naira sent to Hauwa Bello at GT Bank" in call.reply
    assert balance(v_db) == 4_250_000 - 500_000


def test_amount_and_recipient_said_together(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    assert "That is 2,000 naira to Hauwa Bello at GT Bank. Shall I go ahead?" in said(v_db)


def test_saying_no_at_the_read_back_does_nothing(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    call.say("no")
    assert "I have not done anything" in call.reply
    assert transactions(v_db) == [] and balance(v_db) == 4_250_000


def test_nothing_runs_without_a_spoken_yes(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    call.say("hmm")  # not an answer
    assert transactions(v_db) == []


def test_not_enough_money_is_the_banks_answer(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 50000 to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)
    assert "your balance of 42,500 naira is not enough for 50,000 naira" in call.reply
    assert transactions(v_db) == [] and balance(v_db) == 4_250_000


def test_the_banks_limit_is_respected(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 150000 to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)
    assert "above the 100,000 naira limit" in call.reply
    assert transactions(v_db) == []


def test_a_recipient_who_is_not_saved_cannot_be_added_by_phone(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to chidi")
    enter(call)
    assert "cannot find chidi in your saved recipients" in said(v_db)
    assert transactions(v_db) == []


def test_buying_airtime(v_client, v_db):
    call = Caller(v_client)
    begin(call, "I want to buy airtime")
    enter(call)
    assert "How much airtime would you like?" in said(v_db)
    call.say("500")
    assert "That is 500 naira airtime for this number" in call.reply
    call.say("yes")
    call.type(PIN)
    assert "Done. 500 naira airtime has been sent to this number. Your balance is now 42,000 naira." in call.reply


def test_paying_a_bill(v_client, v_db):
    call = Caller(v_client)
    begin(call, "pay my electricity bill")
    enter(call)
    assert "How much is the bill?" in said(v_db)
    call.say("3000")
    assert "That is 3,000 naira to electricity" in call.reply
    call.say("yes")
    call.type(PIN)
    assert "3,000 naira paid to electricity" in call.reply and balance(v_db) == 4_250_000 - 300_000


def test_the_last_transaction_is_reported_from_the_bank(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)  # the authorising PIN, after the spoken yes
    call.say("did my payment go through")
    assert call.keypad and "different part of your account" in call.reply  # account information is not the transfers resource
    call.type(PIN)
    assert "Your last transaction was 10,000 naira to Hauwa Bello at GT Bank, and it was successful" in said(v_db)


def test_a_statement_goes_to_the_phone_by_sms_and_is_not_kept(v_client, v_db, v_app):
    sent = []

    async def capture(to, message):
        sent.append((to, message))
        return "mock_sent", None

    v_app.state.svc.sms.send = capture
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)  # the authorising PIN, after the spoken yes
    call.say("send me my statement")
    assert call.keypad and "different part of your account" in call.reply
    call.type(PIN)
    assert "last 1 transactions to your phone by SMS" in said(v_db)
    texts = [m for to, m in sent if "transactions" in m]
    assert texts and "10,000 naira to Hauwa Bello at GT Bank" in texts[0]
    v_db.expire_all()
    note = v_db.scalar(select(Notification).where(Notification.template == "statement"))
    assert note.body == "[statement: 1 transactions]"  # the audit row says it was sent, not what it said


def test_blocking_a_card_needs_a_yes_and_a_new_pin(v_client, v_db):
    call = Caller(v_client)
    call.verified()  # balance first: a different resource comes next
    call.say("block my card")
    assert call.keypad and "different part of your account" in call.reply
    call.type(PIN)
    assert "I will block your card. Shall I go ahead?" in said(v_db)
    call.say("yes")
    call.type(PIN)  # the authorising PIN, after the spoken yes
    assert "Your card ending 4 3 2 1 is blocked" in call.reply
    v_db.expire_all()
    assert v_db.scalar(select(MockBankAccount)).card_blocked is True


def test_a_complaint_is_logged_with_the_bank_and_handed_over(v_client, v_db):
    call = Caller(v_client)
    begin(call, "I want to complain")
    enter(call)
    assert "Please tell me what happened" in said(v_db)
    call.say("my transfer failed but I was debited")
    v_db.expire_all()
    complaint = v_db.scalar(select(MockBankComplaint))
    assert complaint.details == "my transfer failed but I was debited"
    assert f"reference {complaint.reference}" in call.reply and "follow up" in call.reply
    handoff = v_db.scalar(select(Handoff))
    assert (handoff.reason, handoff.provider) == ("complaint", "demobank") and complaint.reference in handoff.summary


def test_account_information_is_never_spoken_before_verification(v_client, v_db):
    call = Caller(v_client)
    spoken = [call.say("my bank balance please"), call.say("yes")]
    assert not any("42,500" in s or "Hauwa" in s for s in spoken)
    assert call.keypad


def test_changing_the_subject_at_the_read_back_drops_the_transfer(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send 2000 to mama")
    enter(call)
    call.say("actually I want to buy something on CI Store")
    assert "CI Store" in call.reply
    assert transactions(v_db) == []


def test_a_bank_that_fails_is_handed_the_details_and_the_call_goes_on(v_client, v_db, v_app):
    async def broken(*a, **k):
        raise TimeoutError("bank did not answer")

    v_app.state.svc.bank.balance = broken
    call = Caller(v_client)
    call.verified()
    assert "could not complete that with Demo Bank" in said(v_db) and "follow up" in said(v_db)
    handoff = v_db.scalar(select(Handoff))
    assert (handoff.reason, handoff.provider) == ("backend_error", "demobank")
    assert "CI Store" in call.say("I want to buy something")  # the call carried on


def test_a_caller_the_bank_does_not_know_is_handed_to_the_bank(v_client, v_db, v_app):
    from sofa.gateway import links
    from sofa.models import Customer
    with v_app.state.svc.session_factory() as db:
        c = Customer(phone="+2348055550777")
        db.add(c)
        db.flush()
        links.link(db, c.id, "banking", "demobank")  # linked, but the bank has no account for them
        db.commit()
    call = Caller(v_client, "+2348055550777")
    call.say("I want my bank balance")
    begin(call, "yes")
    enter(call)
    assert "cannot find an account for you at Demo Bank" in said(v_db)
    assert v_db.scalar(select(Handoff)).reason == "no_account"


def test_the_pin_and_figures_stay_out_of_the_turn_records(v_client, v_db):
    call = Caller(v_client)
    begin(call, "send the usual to mama")
    enter(call)
    call.say("yes")
    call.type(PIN)
    v_db.expire_all()
    for turn in v_db.scalars(select(CallTurn)):
        assert PIN not in (turn.reply_text or "") and OTP not in (turn.reply_text or "")
        assert turn.llm_json is None or PIN not in str(turn.llm_json)
