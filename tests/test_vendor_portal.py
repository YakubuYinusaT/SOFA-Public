"""The shop owner's portal: a vendor runs stock, orders, advice and customer questions without the Connected Intelligence team."""

import re

import pytest
from sqlalchemy import select

from sofa.models import Call, Handoff, Merchant, Order, Product, ShopAdvice
from sofa.services import advice
from sofa.web import auth
from tests.test_call_flow import MUSA, SPEC_CONVERSATION

PASSWORD = "dev-vendor-token"


@pytest.fixture(autouse=True)
def fresh_state():
    auth.clear_failures()


def login(client):
    assert client.post("/vendor/login", data={"password": PASSWORD}, follow_redirects=False).status_code == 303
    return client


def post(client, path, data=None, page="/vendor"):
    csrf = re.search(r'name="csrf" value="([^"]+)"', client.get(page).text).group(1)
    return client.post(path, data={"csrf": csrf, **(data or {})}, follow_redirects=False)


def test_the_portal_has_its_own_password(client):
    r = client.get("/vendor/stock", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/vendor/login?next=")
    assert client.post("/vendor/login", data={"password": "dev-admin-token"}, follow_redirects=False).status_code == 401
    assert client.post("/vendor/login", data={"password": "dev-provider-token"}, follow_redirects=False).status_code == 401
    login(client)
    assert "Stock and prices" in client.get("/vendor/stock").text


def test_a_price_and_stock_change_is_used_by_the_next_call(client, call, db):
    login(client)
    p = db.scalar(select(Product).where(Product.name == "Peak Milk Tin"))
    post(client, f"/vendor/stock/{p.id}", {"price_naira": "900", "stock_qty": "150", "active": "on"}, page="/vendor/stock")
    db.expire_all()
    assert p.price_kobo == 90_000 and p.stock_qty == 150
    assert "900 naira per tin" in call(["How much is Peak milk tin?"])[1]


def test_a_product_taken_off_sale_cannot_be_ordered(client, call, db):
    login(client)
    p = db.scalar(select(Product).where(Product.name == "Peak Milk Tin"))
    post(client, f"/vendor/stock/{p.id}", {"price_naira": "800", "stock_qty": "150"}, page="/vendor/stock")  # the "for sale" box left unticked
    db.expire_all()
    assert p.active is False
    assert "Peak Milk Tin" not in call(["How much is Peak milk tin?"])[1]  # it is no longer offered at any price


def test_a_new_product_can_be_added_and_a_bad_one_is_refused(client, db):
    login(client)
    r = post(client, "/vendor/stock", {"name": "Neem Oil", "unit": "bottle", "price_naira": "2500", "stock_qty": "30", "category": "agrochemical", "aliases": "neem"},
             page="/vendor/stock")
    assert "msg=" in r.headers["location"] and db.scalar(select(Product).where(Product.name == "Neem Oil"))
    r = post(client, "/vendor/stock", {"name": "", "unit": "bottle", "price_naira": "x", "category": "tools"}, page="/vendor/stock")
    assert r.status_code == 422 and 'class="flash err"' in r.text and 'value="bottle"' in r.text and 'value="tools"' in r.text  # refused, and what was typed stays


def test_vendors_write_advice_and_sofa_gives_it(client, call, db):
    login(client)
    r = post(client, "/vendor/advice", {"topic": "Fertilizer for cassava", "keywords": "cassava, fertilizer, npk, apply",
                                        "answer": "Apply NPK around each cassava plant four weeks after planting.", "product_name": "Peak Milk Tin"}, page="/vendor/advice")
    assert "msg=" in r.headers["location"]
    replies = call(["When should I apply fertilizer on my cassava?"])
    assert "Apply NPK around each cassava plant four weeks after planting." in replies[1]


@pytest.mark.parametrize("fields, problem", [
    ({"topic": "x", "keywords": "one, two", "answer": "a"}, "at least three words"),
    ({"topic": "", "keywords": "a, b, c", "answer": "a"}, "what the advice is about"),
    ({"topic": "x", "keywords": "a, b, c", "answer": "z" * 901}, "under 900"),
    ({"topic": "x", "keywords": "a, b, c", "answer": "ok", "product_name": "Moon Rocks"}, "not one of your products"),
])
def test_advice_that_would_not_work_is_refused_with_the_reason(client, db, fields, problem):
    login(client)
    r = post(client, "/vendor/advice", fields, page="/vendor/advice")
    assert r.status_code == 422 and problem in r.text and db.scalar(select(ShopAdvice)) is None
    if fields["topic"]:
        assert f'value="{fields["topic"]}"' in r.text  # what the vendor typed is put back, not wiped
    assert "ok" not in fields["answer"] or ">ok</textarea>" in r.text


def test_a_refused_question_form_keeps_the_answer_the_vendor_wrote(client, call, db):
    call(["How should I use a sprayer on my cassava?"])
    login(client)
    h = db.scalar(select(Handoff))
    r = post(client, f"/vendor/questions/{h.id}", {"action": "advise", "topic": "Sprayer", "keywords": "sprayer, cassava", "answer": "Fill it to the mark and spray in calm weather."},
             page="/vendor/questions")
    assert r.status_code == 422 and "at least three words" in r.text
    assert "Fill it to the mark and spray in calm weather." in r.text and 'value="Sprayer"' in r.text
    db.expire_all()
    assert h.status == "open" and db.scalar(select(ShopAdvice)) is None


def test_advice_can_be_edited_and_deleted(client, db):
    login(client)
    m = db.scalar(select(Merchant))
    entry = ShopAdvice(merchant_id=m.id, topic="Old", keywords="a, b, c", answer="old answer")
    db.add(entry)
    db.commit()
    post(client, f"/vendor/advice/{entry.id}", {"topic": "New", "keywords": "x, y, z", "answer": "new answer", "active": "on"}, page="/vendor/advice")
    db.expire_all()
    assert (entry.topic, entry.answer) == ("New", "new answer")
    post(client, f"/vendor/advice/{entry.id}", {"delete": "1"}, page="/vendor/advice")
    db.expire_all()
    assert db.scalar(select(ShopAdvice)) is None


def test_an_unanswered_question_becomes_advice_in_one_step(client, call, db):
    assert "do not have a recommendation" in call(["How should I use a sprayer on my cassava?"])[1]
    login(client)
    html = client.get("/vendor/questions").text
    assert "advice question" in html and "cassava" in html and "sprayer" in html
    h = db.scalar(select(Handoff))
    post(client, f"/vendor/questions/{h.id}", {"action": "advise", "topic": "Sprayer on cassava", "keywords": "sprayer, cassava, spray, use",
                                               "answer": "Fill the sprayer to the mark and spray in calm weather."}, page="/vendor/questions")
    db.expire_all()
    assert h.status == "resolved" and db.scalar(select(ShopAdvice)).topic == "Sprayer on cassava"
    assert "Fill the sprayer to the mark" in call(["How should I use a sprayer on my cassava?"], caller="+2348055550999")[1]


def test_a_vendor_moves_an_order_on_and_cannot_touch_anothers(client, call, db):
    call(SPEC_CONVERSATION, caller=MUSA)
    order = db.scalar(select(Order))
    login(client)
    assert str(order.id)[:8] not in client.get("/vendor/orders?status=paid").text
    assert "Orders" in client.get("/vendor/orders").text
    post(client, f"/vendor/orders/{order.id}/status", {"status": "dispatched"}, page="/vendor/orders")  # not a step an awaiting-payment order can take
    db.expire_all()
    assert order.status == "awaiting_payment"
    post(client, f"/vendor/orders/{order.id}/status", {"status": "cancelled"}, page="/vendor/orders")
    db.expire_all()
    assert order.status == "cancelled"


def test_the_dashboard_shows_what_needs_attention(client, call, db):
    call(SPEC_CONVERSATION, caller=MUSA)
    p = db.scalar(select(Product).where(Product.name == "Golden Penny Spaghetti"))  # stock 0 in the sample shop
    login(client)
    html = client.get("/vendor").text
    assert "What needs you today" in html and p.name in html and "AWAITING_PAYMENT" in html


def test_the_orders_pages_show_what_was_ordered_not_a_python_method(client, call, db):
    call(SPEC_CONVERSATION, caller=MUSA)
    login(client)
    assert "built-in method" not in client.get("/vendor/orders").text
    assert "Indomie" in client.get("/vendor/orders").text
    admin = client.post("/admin/login", data={"password": "dev-admin-token"}, follow_redirects=False)
    assert admin.status_code == 303
    html = client.get("/admin/orders").text
    assert "built-in method" not in html and "Indomie" in html


def test_the_admin_login_opens_the_shop_portal_and_everything_in_it_works(client, db):
    admin = client.post("/admin/login", data={"password": "dev-admin-token"}, follow_redirects=False)
    assert admin.status_code == 303
    assert client.get("/vendor/stock").status_code == 200 and client.get("/vendor/orders").status_code == 200  # no vendor password typed
    p = db.scalar(select(Product).where(Product.name == "Peak Milk Tin"))
    r = post(client, f"/vendor/stock/{p.id}", {"price_naira": "950", "stock_qty": "40", "active": "on"}, page="/vendor/stock")  # a change made as the admin is accepted
    assert r.status_code == 303
    db.expire_all()
    assert p.price_kobo == 95_000


def test_the_shop_password_does_not_open_the_admin_console(client):
    login(client)
    assert client.get("/admin", follow_redirects=False).status_code == 303 and client.get("/admin/home", follow_redirects=False).status_code == 303
