"""The bank's service desk: what lands there when Sofa cannot finish, and the bank's back-office actions on an account application."""

import re

import pytest
from sqlalchemy import select

from sofa.gateway import links, mockbank
from sofa.models import Call, Customer, Handoff, MockBankAccount, MockBankApplication, MockBankComplaint
from sofa.web import auth

PASSWORD = "dev-provider-token"
PHONE = "+2348055550888"


@pytest.fixture(autouse=True)
def fresh_state():
    auth.clear_failures()


def login(client):
    r = client.post("/provider/login", data={"password": PASSWORD}, follow_redirects=False)
    assert r.status_code == 303
    return client


def post(client, path, data=None, page="/provider"):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get(page).text).group(1)
    return client.post(path, data={"csrf": csrf, **(data or {})}, follow_redirects=False)


@pytest.fixture
def caller(db):
    c = Customer(phone=PHONE, name="Kemi")
    db.add(c)
    db.flush()
    call = Call(customer_id=c.id, provider_session_id="s1", from_number=PHONE, to_number="+2347000000000")
    db.add(call)
    db.flush()
    db.commit()
    return c, call


def test_the_desk_needs_a_password_unless_you_are_the_admin(client):
    r = client.get("/provider/handoffs", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/provider/login?next=")
    assert client.post("/provider/login", data={"password": "dev-admin-token"}, follow_redirects=False).status_code == 401  # typing the admin password here still fails
    login(client)  # the bank's own password opens the desk
    assert client.get("/provider/handoffs").status_code == 200
    assert client.get("/admin", follow_redirects=False).status_code == 303  # and says nothing about the admin console


def test_the_admin_login_opens_the_desk_with_no_second_password(client):
    assert client.post("/admin/login", data={"password": "dev-admin-token"}, follow_redirects=False).status_code == 303
    assert client.get("/provider/handoffs").status_code == 200 and client.get("/provider").status_code == 200
    r = client.get("/provider/login?next=/provider/applications", follow_redirects=False)  # the login page just passes an admin through
    assert r.status_code == 303 and r.headers["location"] == "/provider/applications"


def test_a_handoff_shows_the_caller_to_phone_and_can_be_worked(client, db, caller):
    c, call = caller
    db.add(Handoff(call_id=call.id, reason="link_failed", service_domain="banking", provider="demobank",
                   summary="Caller could not connect an existing account: the details did not match."))
    db.add(Handoff(call_id=call.id, reason="link_failed", service_domain="banking", provider="gtbank", summary="belongs to another bank"))
    db.commit()
    login(client)
    html = client.get("/provider/handoffs").text
    assert PHONE in html and "Could not connect an existing account" in html and "belongs to another bank" not in html
    h = db.scalar(select(Handoff).where(Handoff.provider == "demobank"))
    post(client, f"/provider/handoffs/{h.id}", {"action": "contacted", "note": "rang, no answer", "by": "Bisi"}, page="/provider/handoffs")
    db.expire_all()
    assert h.status == "contacted" and "rang, no answer" in h.summary
    assert "Waiting" in client.get("/provider/handoffs?status=open").text and PHONE in client.get("/provider/handoffs?status=open").text
    post(client, f"/provider/handoffs/{h.id}", {"action": "resolved", "by": "Bisi"}, page="/provider/handoffs")
    db.expire_all()
    assert (h.status, h.resolved_by) == ("resolved", "Bisi")
    assert PHONE in client.get("/provider/handoffs?status=resolved").text


def test_another_banks_handoff_cannot_be_touched(client, db, caller):
    _, call = caller
    other = Handoff(call_id=call.id, reason="link_failed", provider="gtbank", summary="x")
    db.add(other)
    db.commit()
    login(client)
    assert post(client, f"/provider/handoffs/{other.id}", {"action": "resolved"}, page="/provider/handoffs").status_code == 404


def test_the_bank_approves_an_application_and_the_caller_is_connected_and_texted(client, app, db, caller):
    c, _ = caller
    row = MockBankApplication(customer_id=c.id, provider="demobank", reference="APPTEST1", status="awaiting_identity_verification",
                              full_name="Kemi Ade", dob="1996-03-14", address="12 Allen Avenue", bvn_given=True)
    db.add(row)
    db.commit()
    sent = []

    async def capture(to, message):
        sent.append((to, message))
        return "mock_sent", None

    app.state.svc.sms.send = capture
    login(client)
    html = client.get("/provider/applications").text
    assert "Kemi Ade" in html and "22334455667" not in html  # the BVN is not here: nobody on Sofa's side has it
    post(client, "/provider/applications/APPTEST1", {"action": "identity_ok"}, page="/provider/applications")
    db.expire_all()
    assert row.status == "under_review"
    post(client, "/provider/applications/APPTEST1", {"action": "approve"}, page="/provider/applications")
    db.expire_all()
    assert row.status == "approved"
    assert links.get(db, c.id, "banking", "demobank").status == "active"
    acct = db.scalar(select(MockBankAccount).where(MockBankAccount.customer_id == c.id))
    assert acct and sent and acct.account_number in sent[0][1] and sent[0][0] == PHONE


def test_the_bank_can_reject_and_a_finished_application_cannot_be_approved_twice(client, db, caller):
    c, _ = caller
    row = MockBankApplication(customer_id=c.id, provider="demobank", reference="APPTEST2", status="under_review", full_name="Kemi Ade")
    db.add(row)
    db.commit()
    login(client)
    post(client, "/provider/applications/APPTEST2", {"action": "reject", "reason": "ID photo unclear"}, page="/provider/applications")
    db.expire_all()
    assert (row.status, row.reason) == ("rejected", "ID photo unclear")
    r = post(client, "/provider/applications/APPTEST2", {"action": "approve"}, page="/provider/applications")
    assert "does+not+fit" in r.headers["location"] or "does%20not%20fit" in r.headers["location"]
    db.expire_all()
    assert row.status == "rejected"


def test_complaints_are_listed(client, db, caller):
    c, _ = caller
    db.add(MockBankComplaint(customer_id=c.id, provider="demobank", details="Card swallowed by an ATM", reference="CMPTEST1"))
    db.commit()
    login(client)
    html = client.get("/provider/complaints").text
    assert "CMPTEST1" in html and "Card swallowed by an ATM" in html


def test_approving_for_someone_who_already_banks_there_reuses_their_account(client, app, db, caller):
    c, _ = caller
    mockbank.open_account(db, c, "demobank", 5_000)
    db.add(MockBankApplication(customer_id=c.id, provider="demobank", reference="APPTEST3", status="under_review", full_name="Kemi Ade"))
    db.commit()
    app.state.svc.sms.send = lambda to, message: _ok()
    login(client)
    r = post(client, "/provider/applications/APPTEST3", {"action": "approve"}, page="/provider/applications")
    assert r.status_code == 303 and "msg=" in r.headers["location"]
    db.expire_all()
    accounts = list(db.scalars(select(MockBankAccount).where(MockBankAccount.customer_id == c.id)))
    assert len(accounts) == 1 and accounts[0].balance_kobo == 500_000  # still the one account, with its money


async def _ok():
    return "mock_sent", None
