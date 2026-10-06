"""Information search through the front desk: ask a question, hear a short answer, and never an invented one."""

import pytest
from sqlalchemy import select

from sofa.clients.search import GoogleSearch, OfflineSearch, Result, build_search
from sofa.gateway.adapters.lookup import grounded, question_of
from sofa.models import Call, CallTurn
from tests.test_gateway import gw_app, gw_call, gw_client, gw_db  # noqa: F401  (fixtures)


@pytest.mark.parametrize("said, question", [
    ("What is the capital of Ghana", "What is the capital of Ghana"),
    ("Can you look up the capital of Kenya", "the capital of Kenya"),
    ("please search for the currency of Nigeria", "the currency of Nigeria"),
    ("Google the largest city in Nigeria", "the largest city in Nigeria"),
    ("Can you look something up for me", ""),
    ("search", ""),
])
def test_the_question_is_separated_from_the_ask(said, question):
    assert question_of(said) == question


def test_a_question_is_looked_up_and_answered_in_one_turn(gw_call, gw_db):
    replies = gw_call(["What is the capital of Ghana"])
    assert "The capital of Ghana is Accra." in replies[1] and "anything else" in replies[1]
    turn = gw_db.scalar(select(CallTurn))
    assert turn.llm_json["lookup"] == "What is the capital of Ghana" and turn.llm_json["results"] == 1
    assert gw_db.scalar(select(Call)).service_domain == "lookup"


def test_a_request_with_no_question_asks_what_to_look_up_and_the_next_turn_is_the_question(gw_call):
    replies = gw_call(["Can you look something up for me", "the capital of Kenya"])
    assert "What would you like me to look up" in replies[1]
    assert "Nairobi" in replies[2]


def test_nothing_found_is_said_plainly_never_made_up(gw_call):
    replies = gw_call(["What is the capital of Narnia"])
    assert "could not find a good answer" in replies[1] and "Narnia" not in replies[1].replace("What is the capital of Narnia", "")


def test_the_call_stays_open_and_the_caller_can_ask_again_or_change_service(gw_call):
    replies = gw_call(["What is the capital of Ghana", "and the capital of Nigeria", "I want to buy something"])
    assert "Accra" in replies[1] and "Abuja" in replies[2] and "CI Store" in replies[3]


def test_a_figure_the_model_adds_is_not_spoken(gw_app, gw_call):
    llm = gw_app.state.svc.llm
    original = llm.extract

    async def inventive(model, system, transcript, alternatives=None, json_schema=None):
        if system.startswith("ANSWER"):
            return model(say="The capital of Ghana is Accra and about 9,000,000 people live there."), {}
        return await original(model, system, transcript, alternatives, json_schema=json_schema)

    llm.extract = inventive
    replies = gw_call(["What is the capital of Ghana"])
    assert "9,000,000" not in replies[1] and "The capital of Ghana is Accra." in replies[1]  # the snippet itself is read instead


def test_a_search_that_fails_never_costs_the_caller_the_call(gw_app, gw_call):
    class Broken:
        async def search(self, query, limit=3):
            raise TimeoutError("search is down")

    gw_app.state.svc.search = Broken()
    replies = gw_call(["What is the capital of Ghana", "I want to buy something"])
    assert "cannot search right now" in replies[1] and "CI Store" in replies[2]


def test_every_figure_in_a_spoken_answer_must_come_from_the_sources():
    assert grounded("Lagos has 15 million people.", ["Lagos has 15 million people."])
    assert not grounded("Lagos has 20 million people.", ["Lagos has 15 million people."])
    assert not grounded("", ["x"])


def test_google_is_used_when_its_keys_are_set_and_the_offline_facts_otherwise(settings):
    assert isinstance(build_search(settings), OfflineSearch)
    settings.google_api_key, settings.google_cse_id = "k", "cx"
    assert isinstance(build_search(settings), GoogleSearch)


def test_the_google_reply_is_turned_into_results(monkeypatch):
    import asyncio

    import httpx

    def handler(request):
        assert request.url.params["q"] == "capital of ghana" and request.url.params["num"] == "3"
        return httpx.Response(200, json={"items": [{"title": "Ghana", "snippet": "Accra is\nthe capital.", "link": "https://x.test"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    out = asyncio.run(GoogleSearch("k", "cx").search("capital of ghana"))
    assert out == [Result("Ghana", "Accra is the capital.", "https://x.test")]
