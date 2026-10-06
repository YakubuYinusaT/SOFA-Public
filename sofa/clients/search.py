"""Where SOFA looks things up. One small interface, `search(query)`, so the source can change without touching the conversation.

`GoogleSearch` calls Google's Programmable Search JSON API (needs GOOGLE_API_KEY and GOOGLE_CSE_ID). With no keys, `OfflineSearch`
answers from a small set of built-in facts so the whole flow can be demonstrated and tested; it says "nothing found" for anything
else and never invents an answer.
"""

import logging
from dataclasses import dataclass

import httpx

from ..textutil import clean

log = logging.getLogger("sofa.search")


@dataclass
class Result:
    title: str
    snippet: str
    url: str = ""


class OfflineSearch:
    """A handful of general facts: used when no internet search key is set."""

    FACTS = [
        ({"capital", "ghana"}, Result("Ghana", "The capital of Ghana is Accra.")),
        ({"capital", "nigeria"}, Result("Nigeria", "The capital of Nigeria is Abuja.")),
        ({"capital", "kenya"}, Result("Kenya", "The capital of Kenya is Nairobi.")),
        ({"currency", "nigeria"}, Result("Nigeria", "The currency of Nigeria is the naira.")),
        ({"president", "nigeria"}, Result("Nigeria", "Nigeria is led by a president, who is both head of state and head of government.")),
        ({"independence", "nigeria"}, Result("Nigeria", "Nigeria became independent from Britain on the first of October, nineteen sixty.")),
        ({"largest", "city", "nigeria"}, Result("Nigeria", "Lagos is the largest city in Nigeria.")),
        ({"languages", "nigeria"}, Result("Nigeria", "The three largest languages in Nigeria are Hausa, Yoruba and Igbo, and English is the official language.")),
    ]

    async def search(self, query: str, limit: int = 3) -> list[Result]:
        words = set(clean(query).split())
        hits = [r for key, r in self.FACTS if key <= words]
        return hits[:limit]


class GoogleSearch:
    URL = "https://www.googleapis.com/customsearch/v1"

    def __init__(self, api_key: str, engine_id: str, timeout: float = 6.0):
        self.key, self.cx, self.timeout = api_key, engine_id, timeout

    async def search(self, query: str, limit: int = 3) -> list[Result]:
        params = {"key": self.key, "cx": self.cx, "q": query[:200], "num": limit, "gl": "ng", "safe": "active"}
        async with httpx.AsyncClient(timeout=self.timeout) as http:
            resp = await http.get(self.URL, params=params)
        resp.raise_for_status()
        return [Result(i.get("title", ""), (i.get("snippet") or "").replace("\n", " "), i.get("link", "")) for i in resp.json().get("items", [])[:limit]]


def build_search(settings):
    if settings.google_api_key and settings.google_cse_id:
        return GoogleSearch(settings.google_api_key, settings.google_cse_id, settings.search_timeout_seconds)
    return OfflineSearch()
