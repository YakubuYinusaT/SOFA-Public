"""Connecting an account that already exists: the phone code, then date of birth, BVN and account number, all checked by the bank."""

import pytest
from sqlalchemy import select

from sofa.gateway import links, mockbank
from sofa.models import Customer, Handoff, MockBankAccount, MockBankLinkCheck, ServiceLink
from tests.test_onboarding import born_years_ago, sms, stored_text  # noqa: F401  (fixtures and helpers)
from tests.test_verification import OTP, Caller, clock, v_app, v_client, v_db  # noqa: F401  (fixtures and helpers)

KEMI = "+2348055550777"
ACCOUNT, BVN, DOB = "2012345678", "22334455667", "14031996"


@pytest.fixture
def kemi(v_db):
    """Kemi banks with Demo Bank already but has never connected it to Sofa."""
    mockbank.add_identity(v_db, "demobank", ACCOUNT, "Kemi Ade", "1996-03-14", BVN, balance_naira=61_000)
    v_db.commit()


def kemi_links(db):
    return list(db.scalars(select(ServiceLink).join(Customer, Customer.id == ServiceLink.customer_id).where(Customer.phone == KEMI)))


def to_dob(call):
    call.say("I want my Demo Bank balance")
    assert "Would you like me to connect you with Demo Bank?" in call.reply
    call.say("yes")
    assert "To connect your Demo Bank account" in call.reply
    call.say("yes")
    assert call.keypad and "code" in call.reply
    call.type(OTP)
    assert call.keypad and "date of birth" in call.reply


def test_an_existing_customer_connects_with_bvn_date_of_birth_and_account_number(v_client, v_db, sms, kemi):
    call = Caller(v_client, KEMI)
    to_dob(call)
    call.type(DOB)
    assert "14 March 1996" in call.reply
    call.say("yes")
    assert call.keypad and "BVN" in call.reply
    call.type(BVN)
    assert call.keypad and "account number" in call.reply
    call.type(ACCOUNT)
    assert "Demo Bank account is now connected" in call.reply
    v_db.expire_all()
    customer = v_db.scalar(select(Customer).where(Customer.phone == KEMI))
    assert links.get(v_db, customer.id, "banking", "demobank").status == "active"
    assert v_db.scalar(select(MockBankAccount).where(MockBankAccount.customer_id == customer.id)).balance_kobo == 61_000 * 100
    assert customer.name == "Kemi"  # the first name comes from the bank's record
    assert v_db.scalar(select(Handoff)) is None  # nothing needed a person


def test_the_bvn_and_account_number_are_never_kept_by_sofa(v_client, v_db, sms, kemi):
    call = Caller(v_client, KEMI)
    to_dob(call)
    call.type(DOB)
    call.say("yes")
    call.type(BVN)
    call.type(ACCOUNT)
    text = stored_text(v_db)
    assert BVN not in text and ACCOUNT not in text
    v_db.expire_all()
    assert BVN not in (v_db.scalar(select(MockBankLinkCheck)).bvn_hash or "")  # the bank holds it only hashed


def test_details_that_do_not_match_get_one_more_try_and_then_the_bank_follows_up(v_client, v_db, sms, kemi):
    call = Caller(v_client, KEMI)
    to_dob(call)
    call.type(DOB)
    call.say("yes")
    call.type("99999999999")  # a wrong BVN
    call.type(ACCOUNT)
    assert "do not match" in call.reply and call.keypad  # and the date of birth is asked for again, as the second clip
    call.type(DOB)
    call.say("yes")
    call.type("99999999999")
    call.type(ACCOUNT)
    assert "pass this to them" in call.reply
    v_db.expire_all()
    assert not kemi_links(v_db)
    assert v_db.scalar(select(Handoff)).reason == "link_failed"


def test_an_account_number_the_bank_does_not_have_offers_to_open_one(v_client, v_db, sms, kemi):
    call = Caller(v_client, KEMI)
    to_dob(call)
    call.type(DOB)
    call.say("yes")
    call.type(BVN)
    call.type("1111111111")
    assert "does not have an account with that number" in call.reply and not call.keypad
    call.say("yes")
    assert "full name" in call.reply  # the phone code was already proven: straight on to opening one


def test_someone_under_sixteen_cannot_connect_a_bank_account(v_client, v_db, sms, kemi):
    call = Caller(v_client, KEMI)
    to_dob(call)
    call.type(born_years_ago(14))
    assert "at least 16" in call.reply
    v_db.expire_all()
    assert not kemi_links(v_db)
