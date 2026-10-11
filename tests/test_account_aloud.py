"""The account number to pay into is read aloud on the call as well as texted, so a text that is late, or never arrives, does not stop the customer paying."""

from sqlalchemy import select

from sofa.dialogue import templates
from sofa.models import VirtualAccount
from tests.test_call_flow import SPEC_CONVERSATION


def account_of(db):
    return db.scalar(select(VirtualAccount))


def test_digits_are_read_one_at_a_time_in_short_groups():
    assert templates.read_digits("0123456789") == "0 1 2, 3 4 5, 6 7 8 9"
    assert templates.read_digits("012345678") == "0 1 2, 3 4 5, 6 7 8"
    assert templates.read_digits("12345678901") == "1 2 3, 4 5 6, 7 8 9, 0 1"
    assert templates.read_digits("0123 4567-89") == "0 1 2, 3 4 5, 6 7 8 9"          # spaces and dashes are not read


def test_the_account_number_is_read_twice_and_the_text_is_mentioned(call, db):
    replies = call(SPEC_CONVERSATION)
    reply, acct = replies[8], account_of(db)
    spoken = templates.read_digits(acct.account_number)
    assert reply.count(spoken) == 2 and acct.bank_name in reply
    assert "sent the account number" in reply and "Once you pay" in reply and "Musa" in reply


def test_when_the_text_cannot_be_sent_the_caller_is_asked_to_write_the_number_down(app, call, db):
    class Refusing:
        async def send(self, to, message):
            return "failed", None

    app.state.svc.sms = Refusing()
    reply = call(SPEC_CONVERSATION)[8]
    spoken = templates.read_digits(account_of(db).account_number)
    assert reply.count(spoken) == 2 and "write it down" in reply and "sent the account number" not in reply


def test_the_two_new_phrases_have_translator_notes():
    for key in ("order_placed_account", "order_placed_account_aloud"):
        assert key in templates.CONTEXT and key in templates.translatable_keys()
