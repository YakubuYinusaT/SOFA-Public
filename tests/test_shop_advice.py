"""A shop that advises (CII Store, an agriculture store): Sofa speaks the owner's written advice, never advice of its own."""

import pytest
from sqlalchemy import select

from scripts.seed_agro import ADVICE, PRODUCTS, seed_agro
from sofa.db import init_db, make_engine, make_session_factory
from sofa.models import Handoff, Merchant, Product, ShopAdvice
from sofa.services import advice

MAIZE = ("Fertilizer for maize", "maize, fertilizer, urea, apply, acre",
         "Apply urea four to six weeks after planting, a hand's width from the plant.", "Peak Milk Tin")


@pytest.fixture
def advised(db):
    m = db.scalar(select(Merchant))
    db.add(ShopAdvice(merchant_id=m.id, topic=MAIZE[0], keywords=MAIZE[1], answer=MAIZE[2], product_name=MAIZE[3]))
    db.commit()
    return m


def test_the_owners_words_are_spoken_as_written_then_the_product_is_offered(call, advised):
    replies = call(["When should I apply urea on my maize?"])
    assert MAIZE[2] in replies[1] and "Would you like to order Peak Milk Tin?" in replies[1]


def test_saying_yes_to_the_offer_starts_the_order(call, advised):
    replies = call(["When should I apply urea on my maize?", "yes"])
    assert "How many would you like?" in replies[2]


def test_saying_no_to_the_offer_carries_on(call, advised):
    replies = call(["When should I apply urea on my maize?", "no thanks"])
    assert "No problem" in replies[2]


def test_ordering_something_else_after_the_advice_is_a_new_order_not_a_no(call, advised):
    """Found on the real model: a caller who ignored the offer and said "I want two cartons of Indomie" was told "No problem" and lost the order."""
    replies = call(["When should I apply urea on my maize?", "I want two cartons of Indomie Super Pack"])
    assert "No problem" not in replies[2]
    assert "Indomie" in replies[2]


def test_a_question_with_no_written_advice_is_passed_to_the_team_not_answered(call, db, advised):
    replies = call(["How should I use a sprayer on my cassava?"])
    assert "I do not have a recommendation for that" in replies[1]
    handoff = db.scalar(select(Handoff))
    assert handoff.reason == "advice_question" and "cassava" in handoff.summary


def test_one_matching_word_is_not_enough_to_pick_an_answer(db, advised):
    assert advice.find(db, advised.id, ["maize"])[0] is None
    assert advice.find(db, advised.id, ["urea for my maize please"])[0] is not None  # two words: maize and urea


def test_advice_that_is_switched_off_is_not_used(call, db, advised):
    db.scalar(select(ShopAdvice)).active = False
    db.commit()
    replies = call(["When should I apply urea on my maize?"])
    assert "I do not have a recommendation" in replies[1]


def test_the_agriculture_seed_has_stock_prices_and_advice_that_all_point_at_real_products():
    factory = make_session_factory(make_engine("sqlite://"))
    init_db(factory.kw["bind"])
    m = seed_agro(factory)
    with factory() as db:
        assert m.category == "agriculture"
        names = {p.name for p in db.scalars(select(Product).where(Product.merchant_id == m.id))}
        assert len(names) == len(PRODUCTS) >= 12
        assert any(p.stock_qty == 0 for p in db.scalars(select(Product)))  # one is out of stock, to show the alternatives
        for topic, keywords, answer, product in ADVICE:
            assert len(keywords.split(",")) >= 4 and answer.strip()
            assert product is None or product in names, f"{topic} offers a product the shop does not sell"
        # each advice entry is found by a natural question and by nothing about something else
        assert advice.find(db, m.id, ["How much fertilizer should I apply on my maize farm per acre"])[0].topic == "Fertilizer for maize"
        assert advice.find(db, m.id, ["I see armyworm on my maize leaves what can I spray"])[0].topic == "Fall armyworm in maize"
        assert advice.find(db, m.id, ["what is the weather like"])[0] is None


def test_that_is_all_after_anything_else_goes_to_the_read_back_without_asking_the_model(call, app, monkeypatch):
    """Found on the real model: "that is all" was read as unknown and the caller was asked to repeat it."""
    llm = app.state.svc.llm
    real, asked = llm.parse_turn, []

    async def parse_turn(prompt, transcript, alternatives=None):
        asked.append(transcript)
        return await real(prompt, transcript, alternatives)

    monkeypatch.setattr(llm, "parse_turn", parse_turn)
    replies = call(["I want two cartons of Indomie Super Pack", "that is all"])
    assert "deliver" in replies[2].lower() or "address" in replies[2].lower()
    assert asked == ["I want two cartons of Indomie Super Pack"]


def test_a_plain_yes_to_the_read_back_places_the_order_without_asking_the_model(call, app, monkeypatch):
    llm = app.state.svc.llm
    real, asked = llm.parse_turn, []

    async def parse_turn(prompt, transcript, alternatives=None):
        asked.append(transcript)
        return await real(prompt, transcript, alternatives)

    monkeypatch.setattr(llm, "parse_turn", parse_turn)
    replies = call(["I want two cartons of Indomie Super Pack", "that is all", "12 Allen Avenue Ikeja", "yes"])
    assert "Done" in replies[4] and "yes" not in asked and "that is all" not in asked
