import json
import logging
from typing import Literal

from pydantic import BaseModel, Field, ValidationError

from .http import http_for

log = logging.getLogger("sofa.llm")

Intent = Literal[
    "check_availability", "ask_price", "place_order", "modify_order", "confirm", "deny",
    "repeat_last_order", "track_order", "payment_status", "cancel_order", "speak_to_human",
    "stock_update", "daily_report", "give_name", "give_address", "ask_advice", "unknown",
]


class Item(BaseModel):
    spoken_name: str = ""
    quantity: int | None = None
    unit: Literal["carton", "pack", "piece", "bag", "crate", "tin", "sachet", "derica", "mudu", "kg", "litre", "bottle", "drum"] | None = None
    price: float | None = None
    action: Literal["add", "remove", "set"] | None = None


class TurnJSON(BaseModel):
    intent: Intent = "unknown"
    items: list[Item] = Field(default_factory=list)
    customer_name: str | None = None
    delivery_note: str | None = None
    needs_clarification: bool = False
    clarification_topic: Literal["variant", "size", "unit", "quantity", "address"] | None = None
    say: str = ""


DROPPED = {"title", "default", "minLength", "maxLength", "minimum", "maximum"}  # keywords the strict hosted formats do not accept


def strict_schema(schema):
    """A hosted provider's strict mode wants every property required and no extra ones, and a smaller set of keywords."""
    if isinstance(schema, list):
        return [strict_schema(x) for x in schema]
    if not isinstance(schema, dict):
        return schema
    out = {}
    for k, v in schema.items():
        if k in DROPPED:
            continue
        # the names under "properties" are the caller's own field names: never filtered, only their schemas are
        out[k] = {n: strict_schema(sub) for n, sub in v.items()} if k == "properties" else strict_schema(v)
    if out.get("type") == "object" and "properties" in out:
        out["required"] = list(out["properties"])
        out["additionalProperties"] = False
    return out


TURN_MAX_TOKENS = 600     # a whole shop turn as JSON (items, the reply sentence, flags) runs past 200 tokens on the real model
EXTRACT_MAX_TOKENS = 400  # the gateway's understanding and wording answers


class LLMClient:
    """OpenAI-compatible chat completions: vLLM serving NCAIR1/N-ATLaS or,
    with llm_provider="openai", a hosted model through the same kind of API (a hosted-model build). Guided decoding forces valid JSON."""

    def __init__(self, url: str, model: str, api_key: str = "", structured_mode: str = "modern", provider: str = "vllm"):
        self.http, self.base = http_for(url, api_key)
        self.model = model
        self.hosted = provider == "openai"  # a hosted OpenAI-style API: stricter about the answer format, no vLLM-only options
        # modern: vLLM >= 0.12 (response_format json_schema / structured_outputs.choice)
        # legacy: older vLLM (guided_json / guided_choice, removed in 0.12)
        self.modern = structured_mode != "legacy"

    def schema_format(self, name: str, schema: dict) -> dict:
        """The answer-format option for a JSON schema, in the shape this provider accepts."""
        if self.hosted:
            return {"response_format": {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": strict_schema(schema)}}}
        if self.modern:
            return {"response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}}
        return {"guided_json": schema}

    async def _chat(self, system: str, user: str, max_tokens: int = 200, **extra) -> str:
        payload = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            **extra,
        }
        if self.hosted:
            payload["max_completion_tokens"] = max_tokens  # newer hosted models use this name, and some accept no temperature
        else:
            payload.update(temperature=0.1, max_tokens=max_tokens)
        resp = await self.http.post(f"{self.base}/v1/chat/completions", json=payload)
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]

    async def parse_turn(self, system: str, transcript: str, alternatives: list[str] | None = None) -> tuple[TurnJSON, dict]:
        """Returns (validated turn, raw json). Anything unparseable becomes intent=unknown."""
        try:
            constraint = self.schema_format("turn", strict_schema(TurnJSON.model_json_schema()))  # every field required: optional ones were skipped
            user = f"TRANSCRIPT: {transcript}"
            if alternatives:  # another language model heard the same audio: the caller may be mixing languages
                user += "\nALSO HEARD (same audio, read by another language model): " + " | ".join(alternatives)
            content = await self._chat(system, user, max_tokens=TURN_MAX_TOKENS, **constraint)  # the default of 200 cut the model's answer off mid-sentence
            raw = json.loads(content)
            return TurnJSON.model_validate(raw), raw
        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            log.warning("LLM output rejected: %s", exc)
            return TurnJSON(), {"error": str(exc)}

    async def extract(self, model, system: str, transcript: str, alternatives: list[str] | None = None, json_schema: dict | None = None):
        """Guided JSON for any small pydantic model. Returns (model instance, raw json); a bad answer is an empty instance."""
        try:
            constraint = self.schema_format("params", json_schema or model.model_json_schema())
            user = f"TRANSCRIPT: {transcript}"
            if alternatives:
                user += "\nALSO HEARD (same audio, read by another language model): " + " | ".join(alternatives)
            raw = json.loads(await self._chat(system, user, max_tokens=EXTRACT_MAX_TOKENS, **constraint))
            return model.model_validate(raw), raw
        except (ValidationError, json.JSONDecodeError, KeyError) as exc:
            log.warning("extraction rejected: %s", exc)
            return model(), {"error": str(exc)}

    async def rerank(self, phrase: str, candidates: list[str]) -> int | None:
        """Pick one numbered candidate for a spoken phrase, or None. Guided choice = one token."""
        numbered = "\n".join(f"{i}. {c}" for i, c in enumerate(candidates, 1))
        choices = [str(i) for i in range(1, len(candidates) + 1)] + ["none"]
        system = (
            "RERANK. A caller of a Nigerian shop spoke a product phrase. Answer with the number of "
            "the candidate they most likely mean, or none."
        )
        user = f'PHRASE: "{phrase}"\nCANDIDATES:\n{numbered}'
        try:
            if self.hosted:  # no vLLM "choice" option there: an object with one allowed value does the same job
                schema = {"type": "object", "properties": {"choice": {"type": "string", "enum": choices}}, "required": ["choice"]}
                answer = str(json.loads(await self._chat(system, user, max_tokens=20, **self.schema_format("choice", schema))).get("choice", ""))
            else:
                constraint = {"structured_outputs": {"choice": choices}} if self.modern else {"guided_choice": choices}
                answer = await self._chat(system, user, max_tokens=3, **constraint)
            answer = answer.strip().lower()
        except Exception as exc:  # network / model errors must not break the call
            log.warning("rerank failed: %s", exc)
            return None
        return int(answer) if answer.isdigit() and 1 <= int(answer) <= len(candidates) else None
