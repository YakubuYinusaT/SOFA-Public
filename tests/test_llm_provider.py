"""The language model can be N-ATLaS on our GPU (vLLM) or a hosted OpenAI-style model; the request shape differs, the answers do not."""

import asyncio
import json

from pydantic import BaseModel

from sofa.clients.llm import EXTRACT_MAX_TOKENS, LLMClient, strict_schema


class Params(BaseModel):
    action: str = "unclear"
    amount_naira: float | None = None


class FakeResp:
    def __init__(self, content):
        self.content = content

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self.content}}]}


class FakeHttp:
    def __init__(self, content):
        self.content, self.payloads = content, []

    async def post(self, url, json):  # noqa: A002
        self.payloads.append((url, json))
        return FakeResp(self.content)


def client(provider, content='{"action": "bank.balance", "amount_naira": null}'):
    c = LLMClient("https://llm.example", "some-model", "k", "modern", provider)
    c.http = FakeHttp(content)
    return c


def test_vllm_keeps_its_own_request_shape():
    c = client("vllm")
    obj, _ = asyncio.run(c.extract(Params, "SYSTEM", "what is my balance"))
    url, payload = c.http.payloads[0]
    assert obj.action == "bank.balance" and url.endswith("/v1/chat/completions")
    assert payload["max_tokens"] == EXTRACT_MAX_TOKENS and payload["temperature"] == 0.1 and "max_completion_tokens" not in payload
    assert "strict" not in payload["response_format"]["json_schema"]


def test_a_hosted_model_gets_the_strict_format_and_no_vllm_only_options():
    c = client("openai")
    obj, _ = asyncio.run(c.extract(Params, "SYSTEM", "what is my balance"))
    _, payload = c.http.payloads[0]
    fmt = payload["response_format"]["json_schema"]
    assert obj.action == "bank.balance"
    assert fmt["strict"] is True and fmt["schema"]["additionalProperties"] is False
    assert fmt["schema"]["required"] == ["action", "amount_naira"]  # strict mode wants every field required
    assert payload["max_completion_tokens"] == EXTRACT_MAX_TOKENS and "max_tokens" not in payload and "temperature" not in payload


def test_the_rerank_choice_works_on_a_hosted_model_too():
    c = client("openai", '{"choice": "2"}')
    assert asyncio.run(c.rerank("peak", ["Peak Milk Tin", "Peak Milk Sachet"])) == 2
    _, payload = c.http.payloads[0]
    assert "structured_outputs" not in payload and payload["response_format"]["json_schema"]["strict"] is True


def test_strict_schema_leaves_field_names_alone_and_drops_unsupported_keywords():
    out = strict_schema({"type": "object", "title": "X", "properties": {"title": {"type": "string", "minLength": 10, "default": ""},
                                                                      "n": {"type": "integer", "minimum": 1}}})
    assert list(out["properties"]) == ["title", "n"]  # a field called "title" is still there
    assert out["properties"]["title"] == {"type": "string"} and out["properties"]["n"] == {"type": "integer"}
    assert "title" not in out and out["required"] == ["title", "n"]


def test_a_shop_turn_is_given_room_to_finish_its_answer():
    """With the default of 200 tokens the real model's JSON was cut off mid-sentence and every shop turn became "unknown"."""
    c = client("vllm", content=json.dumps({"intent": "ask_price", "items": [], "customer_name": None, "delivery_note": None, "needs_clarification": False,
                                           "clarification_topic": None, "say": None, "language_detected": "en"}))
    asyncio.run(c.parse_turn("SYSTEM", "how much is rice"))
    assert c.http.payloads[0][1]["max_tokens"] >= 500


def test_the_gateway_extraction_calls_have_room_too():
    c = client("vllm")
    asyncio.run(c.extract(Params, "SYSTEM", "what is my balance"))
    assert c.http.payloads[0][1]["max_tokens"] >= 300
