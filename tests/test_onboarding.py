"""Opening an account by phone: the journey, the secrets that never get kept, and picking an application back up."""

import logging

import pytest
from sqlalchemy import select, text

from sofa.gateway import links, mockbank
from sofa.gateway.onboarding import age, parse_dob
from sofa.models import Base, CallTurn, Customer, Handoff, MockBankApplication, ServiceLink
from tests.test_bank_ops import said
from tests.test_verification import AMINA, OTP, PIN, Caller, clock, stored_text, v_app, v_client, v_db  # noqa: F401  (fixtures and helpers)

NEW = "+2348055550999"
BVN = "22334455667"


@pytest.fixture
def sms(v_app):
    sent = []

    async def capture(to, message):
        sent.append((to, message))
        return "mock_sent", None

    v_app.state.svc.sms.send = capture
    return sent


def application(db):
    db.expire_all()
    return db.scalar(select(MockBankApplication))


def to_name_question(call):
    call.say("I want to open an account")
    call.say("yes")
    call.type(OTP)


def to_dob(call):
    to_name_question(call)
    call.say("Amina Yusuf")
    call.say("yes")


def to_bvn(call):
    to_dob(call)
    call.type("12031990")
    call.say("yes")


def finish(call):
    to_bvn(call)
    call.type(BVN)
    call.say("12 Allen Avenue Ikeja")


# ---- the whole journey ---------------------------------------------------------------------------------------

def test_a_new_caller_opens_an_account_start_to_finish(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("I want to open an account")
    assert "I can help you open a Demo Bank account" in call.reply and "I do not keep your BVN" in call.reply
    call.say("yes")
    assert call.keypad and "sent a code by SMS to the number you are calling from" in call.reply
    call.type(OTP)
    assert "What is your full name, as it is written on your ID?" in call.reply and not call.keypad
    call.say("Amina Yusuf")
    assert "I have Amina Yusuf. Is that right?" in call.reply
    call.say("yes")
    assert call.keypad and "date of birth on your keypad" in call.reply
    call.type("12031990")
    assert "I have 12 March 1990. Is that right?" in call.reply and not call.keypad
    call.say("yes")
    assert call.keypad and "11 digit BVN" in call.reply
    call.type(BVN)
    assert "What is your home address?" in call.reply
    call.say("12 Allen Avenue Ikeja")
    assert "Your application is with Demo Bank, reference APP" in call.reply and "secure link" in call.reply
    app = application(v_db)
    assert (app.status, app.full_name, app.dob, app.address, app.bvn_given) == ("awaiting_identity_verification", "Amina Yusuf", "1990-03-12",
                                                                                "12 Allen Avenue Ikeja", True)
    link_sms = [m for to, m in sms if "finish opening your account" in m]
    assert len(link_sms) == 1 and app.reference in link_sms[0]
    v_db.expire_all()
    mine = v_db.scalar(select(Customer).where(Customer.phone == NEW))
    assert v_db.scalar(select(ServiceLink).where(ServiceLink.customer_id == mine.id)).status == "pending"  # linked once the bank approves


def test_no_pin_is_asked_while_opening_an_account(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    finish(call)
    spoken = " ".join((t.reply_text or "") for t in v_db.scalars(select(CallTurn)))
    assert "PIN on your keypad" not in spoken  # there is no PIN yet; the phone code and the bank's own check do that job


def test_the_bvn_is_never_stored_or_logged(v_client, v_db, sms, caplog):
    caplog.set_level(logging.DEBUG)
    call = Caller(v_client, NEW)
    finish(call)
    dump = stored_text(v_db)
    assert BVN not in dump and OTP not in dump and BVN not in caplog.text
    assert not any(BVN in m for _, m in sms)  # nor texted anywhere


def test_the_keypad_steps_record_no_audio_and_no_digits(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_bvn(call)
    call.type(BVN)
    v_db.expire_all()
    keypad = [t for t in v_db.scalars(select(CallTurn)) if t.asr_model == "keypad"]
    assert len(keypad) == 3 and all(t.transcript is None and t.audio_path is None for t in keypad)  # code, date of birth, BVN
    assert [t.llm_json for t in keypad] == [{"onboarding": "otp", "ok": True}, {"onboarding": "dob", "ok": True}, {"onboarding": "bvn", "ok": True}]


def test_speech_during_the_bvn_step_is_dropped_unheard(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_bvn(call)
    assert call.keypad
    call.say("my bvn is two two three three")
    assert call.keypad and "Never say your PIN or code out loud" in call.reply
    turn = v_db.scalars(select(CallTurn).order_by(CallTurn.seq.desc())).first()
    assert turn.transcript is None and turn.audio_path is None and turn.recording_url is None


# ---- the checks along the way ------------------------------------------------------------------------------

def test_a_wrong_phone_code_can_be_retried_and_three_wrong_stop_the_journey(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("I want to open an account")
    call.say("yes")
    call.type("111111")
    assert call.keypad and "code was not correct" in call.reply
    call.type("222222")
    call.type("333333")
    assert not call.keypad and "I could not confirm this number" in call.reply
    assert application(v_db) is None  # nothing was started


def test_declining_at_the_start_sends_no_code(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("I want to open an account")
    call.say("no")
    assert "No problem" in call.reply and not [m for _, m in sms if "code" in m]


def test_a_name_that_is_not_right_is_asked_again(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_name_question(call)
    call.say("Amina Yusef")
    call.say("no")
    assert "let us try that again" in call.reply
    call.say("Amina Yusuf")
    assert "I have Amina Yusuf. Is that right?" in call.reply


def test_an_impossible_date_is_asked_again(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_dob(call)
    call.type("31022000")
    assert call.keypad and "not a date I can use" in call.reply
    call.type("12031990")
    assert "12 March 1990" in call.reply


def born_years_ago(years: int) -> str:
    from datetime import date

    return f"1203{date.today().year - years}"  # 12 March, `years` or `years + 1` years ago: clear of the birthday edge for these tests


def test_under_sixteen_cannot_use_banking(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_dob(call)
    call.type(born_years_ago(14))
    assert not call.keypad and "at least 16" in call.reply and "<Hangup" not in call.last.text and "<Record" in call.last.text


def test_under_twelve_is_not_served_at_all_and_the_call_ends(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_dob(call)
    call.type(born_years_ago(10))
    assert "aged 12 and above" in call.reply and "<Record" not in call.last.text


def test_sixteen_is_old_enough_for_banking(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_dob(call)
    call.type(born_years_ago(17))
    assert "Is that right?" in call.reply


def test_a_bvn_of_the_wrong_length_is_asked_again(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_bvn(call)
    call.type("1234")
    assert call.keypad and "not 11 digits" in call.reply
    call.type(BVN)
    assert "home address" in call.reply


def test_nothing_typed_leaves_the_application_saved(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_bvn(call)
    call.type("")
    assert "finish later" in call.reply and not call.keypad
    assert application(v_db).status == "draft" and application(v_db).full_name == "Amina Yusuf"


def test_changing_the_subject_mid_journey_keeps_the_application(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    to_name_question(call)
    call.say("actually I want to buy something on CI Store")
    assert "CI Store" in call.reply
    assert application(v_db).status == "draft"


# ---- picking it back up ---------------------------------------------------------------------------------------

def test_an_unfinished_application_is_resumed_where_it_stopped(v_client, v_db, sms):
    first = Caller(v_client, NEW)
    to_dob(first)  # name given; hung up at the date of birth
    ref = application(v_db).reference
    second = Caller(v_client, NEW)
    second.say("I started opening an account but could not finish")
    assert second.keypad and "sent a code" in second.reply
    second.type(OTP)
    assert f"Your application {ref} is not finished yet" in said(v_db) and "date of birth" in said(v_db)
    assert second.keypad
    second.type("12031990")
    second.say("yes")
    second.type(BVN)
    second.say("12 Allen Avenue Ikeja")
    assert f"reference {ref}" in second.reply  # the same application, not a second one
    v_db.expire_all()
    assert len(list(v_db.scalars(select(MockBankApplication)))) == 1


def test_status_while_waiting_for_the_identity_check_can_resend_the_link(v_client, v_db, sms):
    finish(Caller(v_client, NEW))
    call = Caller(v_client, NEW)
    call.say("what is my application status")
    assert call.keypad
    call.type(OTP)
    assert "waiting for your identity check. Shall I send the secure link again?" in call.reply
    call.say("yes")
    assert "sent the secure link again" in call.reply
    assert len([m for _, m in sms if "finish opening your account" in m]) == 2


def test_status_while_the_bank_reviews(v_client, v_db, sms):
    finish(Caller(v_client, NEW))
    mockbank.complete_identity_check(v_db, application(v_db).reference)
    v_db.commit()
    call = Caller(v_client, NEW)
    call.say("what is my application status")
    call.type(OTP)
    assert "is being reviewed by Demo Bank" in call.reply


def test_once_approved_the_new_customer_banks_like_any_other(v_client, v_db, sms):
    finish(Caller(v_client, NEW))
    app = application(v_db)
    row = mockbank.approve(v_db, app.reference)
    links.link(v_db, row.customer_id, "banking", "demobank")  # what the bank's approval tells SOFA to do
    v_db.commit()
    status = Caller(v_client, NEW)
    status.say("what is my application status")
    status.type(OTP)
    assert "Your Demo Bank account is ready" in status.reply
    bank = Caller(v_client, NEW)
    bank.say("my bank balance please")
    bank.say("yes")
    assert bank.keypad and "Before I open your Demo Bank account" in bank.reply  # now the PIN rules apply
    bank.type(PIN)
    bank.type(OTP)
    assert "Your Demo Bank balance is 0 naira" in said(v_db)


def test_a_rejected_application_goes_to_the_bank_with_the_reason(v_client, v_db, sms):
    finish(Caller(v_client, NEW))
    mockbank.reject(v_db, application(v_db).reference, "ID photo unclear")
    v_db.commit()
    call = Caller(v_client, NEW)
    call.say("what is my application status")
    call.type(OTP)
    assert "could not approve your application" in call.reply and "follow up" in call.reply
    handoff = v_db.scalar(select(Handoff))
    assert (handoff.reason, handoff.provider) == ("application_rejected", "demobank") and "ID photo unclear" in handoff.summary


def test_asking_about_an_application_that_does_not_exist_offers_to_open_one(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("what is my application status")
    call.type(OTP)
    assert "do not see an application under your number. Would you like to open an account?" in call.reply


# ---- the rest ----------------------------------------------------------------------------------------------------

def test_what_is_needed_is_explained_without_any_code(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("what do I need to open an account")
    assert not call.keypad and "your BVN" in call.reply and "a selfie" in call.reply


def test_someone_who_already_has_the_account_is_told_so(v_client, v_db, sms):
    call = Caller(v_client, AMINA)
    call.say("I want to open an account")
    assert "already have a Demo Bank account connected" in call.reply


def test_with_several_banks_available_the_caller_chooses(v_client, v_app, v_db, sms):
    v_app.state.svc.settings.onboarding_banks = "demobank,gtbank"
    call = Caller(v_client, NEW)
    call.say("I want to open an account")
    assert "Demo Bank or GT Bank" in call.reply
    call.say("GT bank please")
    assert "open a GT Bank account" in call.reply


def test_a_bank_that_cannot_open_accounts_by_phone_is_said_plainly(v_client, v_db, sms):
    call = Caller(v_client, NEW)
    call.say("I want to open an account with Access Bank")
    assert "cannot open an account with Access Bank by phone yet" in call.reply


def test_the_account_number_for_funding_goes_by_sms_not_aloud(v_client, v_db, sms):
    call = Caller(v_client, AMINA)
    call.verified()
    call.say("how do I fund my account")
    spoken = said(v_db)
    assert "sent your account details to your phone by SMS" in spoken
    number = [m for _, m in sms if "to fund your account" in m]
    assert number and not any(digit_run in spoken for digit_run in ("0" * 3,)) and number[0].split()[-1].strip(".").isdigit()


# ---- small pieces --------------------------------------------------------------------------------------------------

def test_dates_typed_on_the_keypad():
    assert parse_dob("12031990").isoformat() == "1990-03-12"
    for bad in ("31022000", "1203199", "abcdefgh", "12132000", "01012999"):
        assert parse_dob(bad) is None


def test_age_counts_birthdays():
    from datetime import date
    assert age(date(2008, 6, 15), today=date(2026, 6, 14)) == 17 and age(date(2008, 6, 15), today=date(2026, 6, 15)) == 18
