"""Commerce: the shop dialogue (catalogue, product matching, orders, invoices, payments) behind the gateway.

The gateway picks the shop (the caller names it, or there is only one) and then hands the conversation to the existing
shop dialogue manager, with two differences from a call to a shop's own number: every action goes through the tool
registry first, and the call is never ended by the shop flow (the caller decides when to hang up).
"""

import uuid

from sqlalchemy import select

from ...dialogue import templates
from ...dialogue.manager import DialogueManager, Outcome, new_state
from ...models import CustomerProfile, Merchant
from ...textutil import clean
from .. import registry
from ..tools import Tool
from . import ServiceAdapter

# The intents the shop dialogue acts on, and the registered tool each one needs.
INTENT_TOOL = {
    "check_availability": "commerce.check_availability",
    "ask_price": "commerce.ask_price",
    "place_order": "commerce.place_order",
    "modify_order": "commerce.modify_order",
    "repeat_last_order": "commerce.repeat_last_order",
    "track_order": "commerce.track_order",
    "payment_status": "commerce.payment_status",
    "cancel_order": "commerce.cancel_order",
}
ORDERISH = {"place_order", "modify_order", "check_availability", "ask_price"}
GENERIC_NAME_WORDS = {"store", "stores", "shop", "shops", "supermarket", "mart", "ventures", "enterprises", "limited", "ltd", "the", "and"}
VAGUE_WORDS = {"something", "anything", "stuff", "things", "thing", "some", "on", "from", "at", "in", "for", "me", "to", "buy", "order", "get",
               "actually", "just", "also", "please", "now", "like", "would", "want", "need", "i", "can", "could", "you"}


class GatedDialogueManager(DialogueManager):
    """The shop dialogue, with every action the caller asks for checked against the tool registry first."""

    def __init__(self, *args, gate, **kwargs):
        super().__init__(*args, **kwargs)
        self.gate = gate

    async def dispatch(self, intent: str, tj, transcript: str) -> Outcome:
        tool = INTENT_TOOL.get(intent)
        if tool and not self.gate(tool).allowed:
            return self.reply("tool_blocked", action=f"blocked:{tool}")
        return await super().dispatch(intent, tj, transcript)

    async def on_confirm(self) -> Outcome:
        if self.st["stage"] == "await_confirm" and not self.gate("commerce.confirm_order").allowed:
            return self.reply("tool_blocked", action="blocked:commerce.confirm_order")
        return await super().on_confirm()


class CommerceAdapter(ServiceAdapter):
    domain = "commerce"

    def tools(self) -> list[Tool]:
        return [
            Tool("commerce.check_availability", "commerce", "Is a product in stock", "informational"),
            Tool("commerce.ask_price", "commerce", "The price of a product", "informational"),
            Tool("commerce.place_order", "commerce", "Add items to an order", "service"),
            Tool("commerce.modify_order", "commerce", "Change or remove items in an order", "service"),
            Tool("commerce.confirm_order", "commerce", "Place the order after the spoken read-back", "service", confirm=True),
            Tool("commerce.repeat_last_order", "commerce", "Order the same as last time", "service"),
            Tool("commerce.track_order", "commerce", "Where an order is", "account"),
            Tool("commerce.payment_status", "commerce", "Whether a payment has been received", "account"),
            Tool("commerce.cancel_order", "commerce", "Cancel an order", "service", confirm=True),
        ]

    # ---- sessions --------------------------------------------------------------------------

    def in_dialogue(self, st: dict) -> bool:
        shop = st.get("commerce")
        return bool(shop and (shop.get("stage") or shop.get("pending")))

    def holds_session(self, st: dict) -> bool:
        return bool(st.get("merchant_id"))

    # ---- which shop ------------------------------------------------------------------------

    def shops(self, db) -> list[Merchant]:
        return list(db.scalars(select(Merchant).where(Merchant.status == "active").order_by(Merchant.name)))

    def named(self, db, *texts: str, among: list[str] | None = None) -> list[Merchant]:
        """Shops the caller named: the full name, or every distinctive word in it (so 'CI' alone does not match 'CI Store'
        unless the name has nothing else distinctive)."""
        found = []
        for shop in self.shops(db):
            if among is not None and str(shop.id) not in among:
                continue
            name = clean(shop.name)
            distinct = [w for w in name.split() if w not in GENERIC_NAME_WORDS]
            for text in texts:
                padded = f" {clean(text)} "
                if f" {name} " in padded or (distinct and len(" ".join(distinct)) >= 3 and all(f" {w} " in padded for w in distinct)):
                    found.append(shop)
                    break
        return found

    def strip_shop(self, text: str, shop: Merchant) -> str:
        """The request without the shop's name: 'two cartons of indomie from CI Store' -> 'two cartons of indomie'."""
        padded = f" {clean(text)} "
        name = clean(shop.name)
        # the whole name first, then its distinctive words (a fixed order: removing "ci" first would leave "store" behind)
        for phrase in [name, *sorted({w for w in name.split() if w not in GENERIC_NAME_WORDS}, key=lambda w: (-len(w), w))]:
            padded = padded.replace(f" {phrase} ", " ")
        words = padded.split()
        while words and words[-1] in {"on", "from", "at", "in", "with"}:
            words.pop()
        return " ".join(words)

    def shop_names(self, gw, shops: list[Merchant]) -> str:
        return templates.join_list([s.name for s in shops], gw.lang, "word_or")

    # ---- the conversation ------------------------------------------------------------------

    async def turn(self, gw, transcript: str, confidence: float, alternatives: list[str] | None) -> Outcome:
        st = gw.st
        named = self.named(gw.db, transcript, *(alternatives or []))
        if named and str(named[0].id) != st.get("merchant_id"):
            return await self.select(gw, named[0], transcript, alternatives)
        if st.get("merchant_id"):
            return await self.delegate(gw, transcript, confidence, alternatives)
        shops = self.shops(gw.db)
        if not shops:
            return gw.reply("shop_none_available", action="shop_none_available")
        if len(shops) == 1:
            return gw.ask("shop_confirm_one", {"kind": "shop_confirm", "shop": str(shops[0].id)}, shop=shops[0].name)
        return gw.ask("shop_which", {"kind": "shop_choose", "options": [str(s.id) for s in shops[:5]]}, shops=self.shop_names(gw, shops[:5]))

    async def select(self, gw, shop: Merchant, request: str, alternatives: list[str] | None) -> Outcome:
        """Start shopping at this shop. If the caller already said what they want in the same breath, carry on with it."""
        st = gw.st
        st["merchant_id"], st["merchant_name"] = str(shop.id), shop.name
        gw.call.merchant_id = shop.id  # from here the call, its orders and any handoff belong to this shop
        if not gw.db.scalar(select(CustomerProfile).where(CustomerProfile.customer_id == gw.customer.id, CustomerProfile.merchant_id == shop.id)):
            gw.db.add(CustomerProfile(customer_id=gw.customer.id, merchant_id=shop.id))
            gw.db.flush()
        st["commerce"] = new_state(gw.lang, True, False)
        st["commerce"].update(call_id=st["call_id"], merchant_id=str(shop.id), customer_id=st["customer_id"], secret=st.get("secret"))
        stripped = self.strip_shop(request, shop)
        if stripped and await self.has_order(gw, shop, stripped, alternatives):
            return await self.delegate(gw, stripped, 0.0, alternatives)
        return gw.reply("shop_selected", action="shop_selected", shop=shop.name)

    async def has_order(self, gw, shop: Merchant, text: str, alternatives: list[str] | None) -> bool:
        """Did they already say what they want? ('something on CI Store' says nothing; 'two cartons of indomie' does.)"""
        inner = self.inner(gw, shop)
        tj, _ = await inner.llm.parse_turn(inner.build_prompt(), text, alternatives)
        if tj.intent not in ORDERISH:
            return False
        return any(set(clean(i.spoken_name).split()) - VAGUE_WORDS for i in tj.items)

    def inner(self, gw, shop: Merchant | None = None) -> GatedDialogueManager:
        st = gw.st
        shop = shop or gw.db.get(Merchant, uuid.UUID(st["merchant_id"]))
        shop_state = st["commerce"]
        shop_state["lang"] = st["lang"]  # the caller's language can change mid-call
        profile = gw.db.scalar(select(CustomerProfile).where(CustomerProfile.customer_id == gw.customer.id, CustomerProfile.merchant_id == shop.id))
        gate = lambda tool: gw.svc.gateway.authorize(tool, st)  # noqa: E731
        return GatedDialogueManager(gw.db, gw.svc, shop_state, gw.call, shop, gw.customer, profile, gw.turn_id, gate=gate)

    async def delegate(self, gw, transcript: str, confidence: float, alternatives: list[str] | None) -> Outcome:
        inner = self.inner(gw)
        out = await inner.handle(transcript, confidence, alternatives)
        gw.raw_json, gw.match_log = inner.raw_json, inner.match_log
        return self.adapt(gw, inner, out)

    def adapt(self, gw, inner: DialogueManager, out: Outcome) -> Outcome:
        """The shop flow was written for a call to a shop's own number: it ends the call after an order and promises that
        the owner will phone back. On the gateway the caller decides when to hang up, and SOFA only says the provider
        will follow up."""
        st = gw.st
        if out.action.startswith("handoff"):  # the shop dialogue has recorded the handoff and told the shop
            st["domain"] = None
            st["commerce"] = new_state(gw.lang, True, False)
            st["commerce"].update(call_id=st["call_id"], merchant_id=st["merchant_id"], customer_id=st["customer_id"], secret=st.get("secret"))
            return gw.reply("gateway_handoff_provider", action=out.action, provider=st.get("merchant_name") or "the shop")
        if out.end_call:
            out.end_call = False
            lang = templates.speak_lang("gateway_anything_else", gw.lang)
            out.also = [*out.also, (templates.render("gateway_anything_else", lang, {}), lang)]
        return out

    async def resolve_pending(self, gw, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        st, kind = gw.st, pending["kind"]
        request = st.get("request") or ""
        if kind == "shop_confirm":
            answer = await gw.answer_to(transcript, alternatives)
            gw.raw_json = {"pending": kind, "answer": answer}
            st["pending"] = None
            if answer == "other":
                return None
            if answer == "no":
                return gw.reply("declined", action="shop_declined")
            return await self.select(gw, gw.db.get(Merchant, uuid.UUID(pending["shop"])), request, alternatives)
        found = self.named(gw.db, transcript, *(alternatives or []), among=pending["options"])
        gw.raw_json = {"pending": kind, "shop": [s.name for s in found]}
        if len(found) == 1:
            st["pending"] = None
            return await self.select(gw, found[0], request, alternatives)
        domain = await gw.route(transcript, alternatives)
        pending["tries"] = pending.get("tries", 0) + 1
        if domain not in (registry.UNCLEAR, self.domain):  # they moved on to something else
            st["pending"] = None
            return None
        if pending["tries"] >= 2:
            st["pending"] = None
            return gw.reply("declined", action="shop_choice_dropped")
        st["pending"] = pending
        shops = [s for s in self.shops(gw.db) if str(s.id) in pending["options"]]
        return gw.reply("shop_which", action="shop_which_again", shops=self.shop_names(gw, shops))
