"""Information search: the caller asks a question, SOFA looks it up and says the answer in a sentence or two.

The search source (Google when its keys are set, a small offline set of facts otherwise) returns snippets. The model writes a short
spoken answer from those snippets only. The answer is spoken only if every number in it comes from a snippet; if the model fails or
adds a figure of its own, SOFA reads the best snippet as it is. If nothing is found SOFA says so: it never answers from memory.
"""

import logging
import re

from pydantic import BaseModel

from ...dialogue.manager import Outcome
from ...textutil import clean
from .. import registry
from ..tools import Tool
from . import ServiceAdapter

log = logging.getLogger("sofa.lookup")

# What a caller says before the question itself: "can you look up", "please search for", "google".
LEAD_IN = re.compile(
    r"^(?:(?:hi|hello|hey|please|sofa|abeg|can|could|would|you|i|want|to|wanna|need|help|me|us|just|quickly|go|ahead)\s+)*"
    r"(?:look\s+(?:something\s+|it\s+)?up|search(?:\s+for|\s+the\s+web\s+for)?|google|find\s+out|find|tell\s+me|ask)\b[\s,:-]*"
    r"(?:(?:something|for|me|us|about|on|that|please|for\s+me)\b[\s,:-]*)*", re.I)
NO_QUESTION = {"something", "anything", "it", "that", "this", "stuff", "some", "for", "me", "please", "up"}
NUMBER = re.compile(r"\d[\d,.]*")


class SearchAnswer(BaseModel):
    say: str = ""


SCHEMA = {"type": "object", "properties": {"say": {"type": "string", "minLength": 10}}, "required": ["say"], "additionalProperties": False}


def question_of(transcript: str) -> str:
    """The question itself, without the 'can you look up' in front. Empty if the caller asked for a search but has not said what for."""
    text = " ".join(transcript.split())
    stripped = LEAD_IN.sub("", text).strip(" ,.?!")
    words = [w for w in clean(stripped).split() if w not in NO_QUESTION]
    return stripped if words else ""


def prompt(question: str, snippets: list[str]) -> str:
    return (
        "ANSWER. You are Sofa, a warm voice assistant on a phone call in Nigeria. Answer the QUESTION in one or two short, plain "
        "sentences that sound natural when spoken aloud. Use ONLY the SNIPPETS below. If they do not answer the question, say you "
        "could not find it. Do not add any fact, figure or name that is not in the SNIPPETS. No lists, no web addresses.\n"
        f"QUESTION: {question}\n" + "\n".join(f"SNIPPET {i}: {s}" for i, s in enumerate(snippets, 1))
    )


def grounded(say: str, snippets: list[str]) -> bool:
    """Every figure the answer states was in a snippet."""
    source = " ".join(snippets)
    return bool(say.strip()) and all(n.strip(",.") in source for n in NUMBER.findall(say))


def sentence_of(snippet: str, limit: int = 220) -> str:
    snippet = " ".join(snippet.split())
    return snippet if len(snippet) <= limit else snippet[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "."


class LookupAdapter(ServiceAdapter):
    domain = "lookup"

    def tools(self) -> list[Tool]:
        return [Tool("lookup.search", "lookup", "Look up general information", "informational")]

    def holds_session(self, st: dict) -> bool:
        return st.get("domain") == "lookup"  # a follow-up the router cannot place ("and the capital of Nigeria") is another question

    async def turn(self, gw, transcript: str, confidence: float, alternatives: list[str] | None) -> Outcome:
        question = question_of(transcript)
        if not question:  # "can you look something up for me": ask what for
            return gw.ask("lookup_ask", {"kind": "lookup_query"}, action="lookup_ask")
        return await self.answer(gw, question)

    async def resolve_pending(self, gw, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        domain = await gw.route(transcript, alternatives)
        if domain not in (registry.UNCLEAR, "lookup"):  # they moved on to something else
            gw.st["pending"] = None
            return None
        gw.st["pending"] = None
        return await self.answer(gw, question_of(transcript) or transcript.strip())

    async def answer(self, gw, question: str) -> Outcome:
        verdict = gw.svc.gateway.authorize("lookup.search", gw.st)
        if not verdict.allowed:
            return gw.reply("tool_blocked", action="blocked:lookup.search")
        try:
            results = await gw.svc.search.search(question)
        except Exception:
            log.warning("search failed", exc_info=True)
            return gw.reply("lookup_unavailable", action="lookup_unavailable")
        gw.raw_json = {"lookup": question, "results": len(results)}
        snippets = [r.snippet for r in results if r.snippet]
        if not snippets:
            return gw.reply("lookup_none", action="lookup_none")
        say = ""
        try:
            obj, _ = await gw.svc.llm.extract(SearchAnswer, prompt(question, snippets), "Write the answer now.", json_schema=SCHEMA)
            say = " ".join(obj.say.split())
        except Exception:
            say = ""
        source = "model"
        if not grounded(say, snippets):  # nothing the model added is spoken: the best snippet is
            say, source = sentence_of(snippets[0]), "snippet"
        return gw.reply("lookup_answer", action=f"lookup_{source}", answer=say)
