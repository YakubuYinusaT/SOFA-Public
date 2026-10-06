"""Dialogue manager: one caller utterance in, one spoken reply out.

The LLM only extracts intent JSON. Every fact in a reply (products, prices, totals, status) comes
from the database. No order leaves draft without an explicit spoken confirmation of the read-back.
"""

import time
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..clients.llm import Item, TurnJSON
from ..models import Call, Customer, CustomerProfile, Invoice, Merchant, MerchantUser, MissedDemand, Product, StockMovement
from ..services import advice as advice_svc
from ..services import orders as order_svc
from ..services import reports
from ..services.handoff import open_handoff
from ..services.notify import notify_owners, send_sms
from ..services.payments import get_or_create_account
from ..textutil import canonical_unit, clean, naira, parse_number, plural_unit
from . import shop_prompt, templates
from .matching import learn_alias, match_product, pick_among, substitute_for
from .validator import check_say, fill

log = logging.getLogger("sofa.dialogue")

# Callers in Lagos mix Yoruba, English and Pidgin inside one sentence. Product names, numbers and units are often
# English while the rest is Yoruba. The examples are illustrative; review them with a Yoruba speaker (docs/code_mixing.md).
CODE_MIXING_NOTE = (
    "Callers often mix Yoruba, English and Nigerian Pidgin in one sentence. Read the whole mixed sentence as one request. "
    "Keep spoken_name exactly as the caller said it, whatever the language. Two readings of the same audio may be given: "
    "trust whichever makes sense.\n"
    "Examples:\n"
    "Caller: Mo fẹ́ carton méjì ti Indomie Super Pack -> place_order, spoken_name 'indomie super pack', quantity 2, unit carton\n"
    "Caller: How much be Peak milk tin? -> ask_price, spoken_name 'peak milk', unit tin\n"
    "Caller: Bẹ́ẹ̀ ni, ó tọ̀nà -> confirm\n"
    "Caller: Rárá, kò tọ̀nà -> deny\n"
)

# Clarification replies the model may word itself (in `generated` languages), keyed to its topic.
GENERATABLE = {"clarify_variant": "variant", "clarify_unit": "unit", "clarify_quantity": "quantity"}
NON_INTERRUPTING = {"confirm", "deny", "unknown", "give_name", "place_order", "modify_order", "give_address"}


@dataclass
class Outcome:
    text: str
    key: str
    end_call: bool = False
    action: str = ""
    lang: str = "en"  # voice to speak it in: English when we have no wording in the caller's language
    also: list = field(default_factory=list)  # more (text, voice language) spoken right after `text`
    extra: dict = field(default_factory=dict)
    collect: dict | None = None  # ask for keypad digits next instead of a recording: {"kind": "pin" | "otp", "digits": n}
    parts: list | None = None  # the reply as (text, voice language) clips, when some are spoken in another voice (money in English)


def new_state(lang: str, lang_locked: bool, owner: bool) -> dict:
    return {
        "lang": lang, "lang_locked": lang_locked, "stage": None, "pending": None, "turn": 0,
        "unknown_streak": 0, "low_conf": 0, "silence": 0, "ack_i": 0, "owner": owner,
        "history": [], "last_item_id": None, "t0": time.time(),  # t0: when the call began (no call runs past max_call_seconds)
    }


class DialogueManager:
    def __init__(self, db: Session, svc, st: dict, call: Call, merchant: Merchant,
                 customer: Customer, profile: CustomerProfile, turn_id=None):
        self.db, self.svc, self.st = db, svc, st
        self.call, self.merchant, self.customer, self.profile = call, merchant, customer, profile
        self.turn_id = turn_id
        self.llm = svc.llm
        self.tj: TurnJSON | None = None
        self.raw_json: dict | None = None
        self.match_log: list[dict] = []
        self._added: list = []

    # ---- reply construction ------------------------------------------------------------

    @property
    def lang(self) -> str:
        return self.st["lang"]

    def L(self, key: str) -> str:
        """Language this key will be spoken in (the caller's, or English if untranslated)."""
        return templates.speak_lang(key, self.lang)

    def _ack(self, lang: str) -> str:
        options = templates.acks(lang)
        ack = options[self.st["ack_i"] % len(options)]
        self.st["ack_i"] += 1
        return ack

    def reply(self, key: str, *, end: bool = False, action: str | None = None, **facts) -> Outcome:
        spoken = self.L(key)
        raw = templates.pick(key, spoken, self.st["turn"])
        facts = {"merchant": self.merchant.name, "name": self.customer.name or "", **facts}
        if "{ack}" in raw:
            facts["ack"] = self._ack(spoken)
        text, source, shape = fill(raw, facts), "template", raw
        text = re.sub(r"\s*,\s*(?=[.!?])", "", text)  # no name known: "Thank you, ." becomes "Thank you."
        topic = GENERATABLE.get(key)
        if topic and self.svc.settings.reply_mode(self.lang) == "generated" and self.tj:
            if self.tj.needs_clarification and self.tj.clarification_topic == topic:
                filled = check_say(self.tj.say, facts, expect_question=True)
                if filled:
                    text, source, spoken, shape = filled, "generated", self.lang, self.tj.say
                else:
                    source = "template_fallback"
        try:
            parts = templates.value_parts(shape, facts, spoken)
        except KeyError:
            parts = None
        return Outcome(text, key, end, f"{action or key}|{source}", lang=spoken, parts=parts)

    # ---- entry points ------------------------------------------------------------------

    async def not_heard(self, transcript: str) -> Outcome:
        """Nothing usable: ask again; the second time in a row is a handoff."""
        self.st["low_conf"] += 1
        if self.st["low_conf"] >= 2:
            return await self.handoff("asr_low_confidence", transcript)
        return self.reply("repeat_prompt", action="asr_low_confidence")

    async def understands(self, transcript: str, alternatives: list[str] | None = None) -> bool:
        """Can the LLM find a request in this? Used before Sofa asks "which language?": someone who mixed
        languages but clearly asked for something should not be interrupted with a question."""
        tj, _ = await self.llm.parse_turn(self.build_prompt(), transcript, alternatives)
        return tj.intent != "unknown"

    async def handle(self, transcript: str, confidence: float, alternatives: list[str] | None = None) -> Outcome:
        st, s = self.st, self.svc.settings
        st["turn"] += 1
        st["silence"] = 0
        if not transcript.strip():
            return await self.not_heard(transcript)

        self.tj, self.raw_json = await self.llm.parse_turn(self.build_prompt(), transcript, alternatives)
        tj = self.tj
        # Mixed-language speech scores low on any single-language model even when it was heard perfectly, so low speech
        # confidence alone does not reject a turn. It only does when the LLM cannot find a request in it either. (The
        # read-back before an order is what protects against a wrong quantity or product.)
        if confidence < s.asr_min_confidence and tj.intent == "unknown":
            return await self.not_heard(transcript)
        st["low_conf"] = 0
        if tj.customer_name and not self.customer.name:
            self.customer.name = tj.customer_name

        intent = self.sane(tj, transcript)
        out = await self.dispatch(intent, tj, transcript)
        st["history"] = (st["history"] + [[transcript, out.text]])[-2:]
        return out

    async def on_silence(self) -> Outcome:
        self.st["silence"] += 1
        if self.st["silence"] == 1:
            return self.reply("still_there", action="silence_prompt")
        await send_sms(
            self.db, self.svc, to=self.customer.phone, template="goodbye",
            body=f"{self.merchant.name}: sorry we missed you. Call us again on {self.call.to_number}.",
            merchant_id=self.merchant.id, customer_id=self.customer.id,
        )
        self.call.outcome = self.call.outcome or "silence"
        return self.reply("goodbye_silence", end=True, action="silence_goodbye")

    # ---- prompt and intent sanity --------------------------------------------------------

    def build_prompt(self) -> str:
        products = [p for p in self.db.scalars(
            select(Product).where(Product.merchant_id == self.merchant.id, Product.active.is_(True))
        )][:30]
        catalog = "\n".join(f"- {p.name} (aliases: {', '.join(a.phrase for a in p.aliases[:6]) or 'none'})" for p in products)
        draft = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        draft_desc = order_svc.describe_items(draft) if draft and draft.items else "empty"
        hist = "\n".join(f"Caller: {c}\nSofa: {r}" for c, r in self.st["history"]) or "none"
        return (
            "You are Sofa, a warm, brief shop attendant on a phone call for a Nigerian business. "
            "Extract the caller's intent as JSON only. Never invent products, prices or stock; "
            "write any reply in `say` with placeholders for facts.\n"
            + shop_prompt.block(advice_svc.topics(self.db, self.merchant.id))
            + CODE_MIXING_NOTE
            + f"LANGUAGE: {self.lang}\nMODE: {'owner' if self.st['owner'] else 'customer'}\n"
            f"STAGE: {self.st['stage'] or 'none'}\nPENDING: {(self.st['pending'] or {}).get('kind', 'none')}\n"
            f"CATEGORY: {self.merchant.category}\n"
            + ("OWNER REQUESTS: stock_update (add stock or change a price) or daily_report (asks how today went).\n"
               if self.st["owner"] else "")
            + f"PRODUCTS:\n{catalog}\nDRAFT ORDER: {draft_desc}\nLAST TURNS:\n{hist}"
        )

    def _has_draft(self) -> bool:
        d = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        return bool(d and d.items)

    def sane(self, tj: TurnJSON, transcript: str) -> str:
        """Treat intents that make no sense for the state as unknown."""
        intent, stage, pending = tj.intent, self.st["stage"], self.st["pending"]
        if not self.st["owner"] and intent in ("stock_update", "daily_report"):
            return "unknown"  # owner-only requests: for anyone else they count as not understood, so two misses hand off
        if stage == "await_address" and intent not in {"cancel_order", "speak_to_human", "deny"} and not pending:
            tj.delivery_note = tj.delivery_note or transcript.strip()
            return "give_address"
        if intent == "confirm" and not (pending or stage in {"offer_repeat", "await_confirm", "owner_confirm", "ordering"}):
            return "unknown"
        if intent == "deny" and not (pending or stage in {"offer_repeat", "await_confirm", "owner_confirm", "ordering"}):
            return "unknown"
        return intent

    # ---- dispatch ----------------------------------------------------------------------

    async def dispatch(self, intent: str, tj: TurnJSON, transcript: str) -> Outcome:
        st = self.st
        if intent == "speak_to_human":
            return await self.handoff("speak_to_human", transcript)
        if st["owner"]:
            return await self.owner_turn(intent, tj)

        pending = st["pending"]
        if pending and intent in NON_INTERRUPTING:
            out = await self.resolve_pending(pending, tj, transcript)
            if out:
                return out

        if intent != "unknown":
            st["unknown_streak"] = 0

        if intent in ("place_order", "modify_order"):
            return await self.run_items(list(tj.items), fresh=True)
        if intent in ("check_availability", "ask_price"):
            return await self.info_turn(intent, tj)
        if intent == "ask_advice":
            return await self.advice_turn(transcript)
        if intent == "give_name":
            return self.reply("ask_what", action="give_name")
        if intent == "give_address":
            self.profile.delivery_address = tj.delivery_note or transcript.strip()
            return self.checkout()
        if intent == "confirm":
            return await self.on_confirm()
        if intent == "deny":
            return self.on_deny()
        if intent == "repeat_last_order":
            return self.repeat_last()
        if intent == "track_order":
            return self.track()
        if intent == "payment_status":
            return self.payment_status()
        if intent == "cancel_order":
            return await self.cancel()

        st["unknown_streak"] += 1
        if st["unknown_streak"] >= 2:
            return await self.handoff("two_unknown_turns", transcript)
        return self.reply("repeat_prompt", action="unknown")

    # ---- items ---------------------------------------------------------------------------

    def _price_text(self, p: Product, unit: str | None = None) -> str:
        price = order_svc.unit_price(p, unit) if unit else None
        return naira(price if price is not None else p.price_kobo)

    def _options_text(self, cands: list[Product], unit: str | None) -> str:
        parts = [f"{p.name} at {self._price_text(p, unit)}" for p in cands]
        return templates.join_list(parts, self.L("clarify_variant"), "word_or")

    def _variant_question(self, cands: list[Product], unit: str | None) -> Outcome:
        facts = {"options": self._options_text(cands, unit)}
        for i, p in enumerate(cands, 1):
            facts[f"p{i}"] = {"name": p.name, "price": self._price_text(p, unit)}
        return self.reply("clarify_variant", **facts)

    async def run_items(self, items: list[Item | dict], fresh: bool = False) -> Outcome:
        if fresh:
            self._added = []
        if not items and fresh:
            return self.reply("ask_what", action="no_items")
        items = [Item(**i) if isinstance(i, dict) else i for i in items]
        while items:
            it = items.pop(0)
            act = it.action or "add"
            if act in ("remove", "set"):
                return self.edit_item(it, act)
            if not it.spoken_name.strip():
                # e.g. "two cartons" with no name: applies to the last item added
                return self.reply("repeat_prompt", action="item_without_name")
            res = await match_product(self.db, self.merchant.id, it.spoken_name, self.llm, self.svc.settings, it.unit)
            self.match_log.append({"spoken": it.spoken_name, **res.log()})
            if res.status == "none":
                self.db.add(MissedDemand(merchant_id=self.merchant.id, spoken_name=it.spoken_name, call_turn_id=self.turn_id))
                return self.reply("not_available", spoken=it.spoken_name, action="not_available")
            if res.status == "ambiguous":
                cands = [p for p, _ in res.candidates]
                self.st["pending"] = {
                    "kind": "variant", "cands": [str(p.id) for p in cands], "attempts": 0,
                    "item": it.model_dump(), "rest": [i.model_dump() for i in items],
                }
                return self._variant_question(cands, it.unit)
            out = await self.add_product(res.product, it, items)
            if out:
                return out
        return self.added_reply()

    async def add_product(self, product: Product, it: Item, rest: list[Item]) -> Outcome | None:
        """Add to the draft, or return a clarification/stock reply. None means added."""
        units = order_svc.unit_options(product)
        names = [u[0] for u in units]
        unit, qty = it.unit, it.quantity
        base = {"item": it.model_dump(), "rest": [r.model_dump() for r in rest], "product_id": str(product.id)}
        if unit not in names:
            if len(names) == 1:
                unit = names[0]
            else:
                self.st["pending"] = {"kind": "unit", "attempts": 0, "qty": qty, **base}
                return self._unit_question(names)
        if qty is None:
            self.st["pending"] = {"kind": "quantity", "attempts": 0, "unit": unit, **base}
            return self.reply("clarify_quantity")

        draft = order_svc.get_draft(self.db, self.merchant.id, self.customer.id, self.call.id)
        already = order_svc.committed_base_qty(draft).get(product.id, 0)
        need = order_svc.base_qty(product, unit, qty)
        available = product.stock_qty - already
        if available < need:
            self.st["pending"] = None
            alt = substitute_for(self._all_products(), product)
            if available <= 0:
                if alt:
                    self.st["pending"] = {"kind": "substitute", "attempts": 0, "product_id": str(alt.id), "qty": qty, "rest": base["rest"]}
                    return self.reply("out_of_stock_alt", product=product.name, alt_product=alt.name,
                                      alt_price=self._price_text(alt), action="out_of_stock")
                return self.reply("out_of_stock", product=product.name, action="out_of_stock")
            self.st["pending"] = {"kind": "quantity", "attempts": 0, "unit": unit, **base}
            return self.reply("not_enough_stock", have_qty=available // max(1, order_svc.base_qty(product, unit, 1)),
                              have_unit=plural_unit(unit, 2), product=product.name, action="not_enough_stock")

        item = order_svc.add_item(self.db, draft, product, unit, qty, it.spoken_name or None)
        self._added.append(item)
        self.st["last_item_id"] = str(item.id)
        self.st["stage"] = "ordering"
        self.st["pending"] = None
        return None

    def _unit_question(self, names: list[str]) -> Outcome:
        if len(names) == 2:
            return self.reply("clarify_unit", u1=names[0], u2=names[1])
        return self.reply("clarify_unit_many", units=templates.join_list(names, self.L("clarify_unit_many"), "word_or"))

    def _all_products(self) -> list[Product]:
        return list(self.db.scalars(select(Product).where(Product.merchant_id == self.merchant.id, Product.active.is_(True))))

    def added_reply(self) -> Outcome:
        draft = order_svc.get_draft(self.db, self.merchant.id, self.customer.id, self.call.id)
        if len(self._added) == 1:
            i = self._added[0]
            return self.reply("added", qty_unit=f"{i.qty} {plural_unit(i.unit, i.qty)}", product=i.product.name,
                              line_total=naira(i.qty * i.unit_price_kobo), action="item_added")
        items = ", ".join(order_svc.describe_item(i) for i in self._added)
        return self.reply("added_many", items=items, total=naira(draft.total_kobo), action="items_added")

    def edit_item(self, it: Item, act: str) -> Outcome:
        draft = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        if not draft or not draft.items:
            return self.reply("empty_draft", action="edit_empty")
        target = None
        if it.spoken_name.strip():
            prod = pick_among([i.product for i in draft.items], it.spoken_name)
            target = next((i for i in draft.items if prod and i.product_id == prod.id), None)
        elif len(draft.items) == 1:
            target = draft.items[0]
        else:
            target = next((i for i in draft.items if str(i.id) == self.st.get("last_item_id")), None)
        if not target:
            return self.reply("repeat_prompt", action="edit_not_found")
        if act == "remove":
            name = target.product.name
            order_svc.remove_item(self.db, draft, target)
            return self.reply("removed", product=name, action="item_removed")
        if it.quantity is None:
            return self.reply("clarify_quantity", action="edit_quantity")
        order_svc.set_qty(draft, target, it.quantity)
        return self.reply("changed", qty_unit=f"{target.qty} {plural_unit(target.unit, target.qty)}",
                          product=target.product.name, action="item_changed")

    # ---- pending clarifications ----------------------------------------------------------

    @staticmethod
    def parse_qty_unit(tj: TurnJSON, transcript: str) -> tuple[int | None, str | None]:
        qty = next((i.quantity for i in tj.items if i.quantity is not None), None)
        unit = next((i.unit for i in tj.items if i.unit), None)
        for w in clean(transcript).split():
            if qty is None and parse_number(w) is not None:
                qty = parse_number(w)
            if unit is None and canonical_unit(w):
                unit = canonical_unit(w)
        return qty, unit

    async def resolve_pending(self, pending: dict, tj: TurnJSON, transcript: str) -> Outcome | None:
        st = self.st
        kind = pending["kind"]
        item = Item(**pending["item"]) if "item" in pending else Item()
        rest = [Item(**r) for r in pending.get("rest", [])]

        def retry_or_handoff() -> Outcome | None:
            pending["attempts"] += 1
            st["pending"] = pending
            return None if pending["attempts"] >= 2 else "retry"  # type: ignore[return-value]

        if kind == "advice_offer":
            st["pending"] = None
            if tj.intent == "confirm":
                self._added = []
                return await self.run_items([Item(spoken_name=pending["product"], action="add")], fresh=True)
            return self.reply("declined", action="advice_offer_declined")

        if kind == "substitute":
            st["pending"] = None
            if tj.intent == "confirm":
                product = self.db.get(Product, uuid.UUID(pending["product_id"]))
                self._added = []
                out = await self.add_product(product, Item(spoken_name=product.name, quantity=pending.get("qty")), rest)
                return out or await self.run_items(rest) if rest else out or self.added_reply()
            return self.reply("ask_more", action="substitute_declined")

        if kind == "variant":
            cands = [self.db.get(Product, uuid.UUID(pid)) for pid in pending["cands"]]
            chosen = pick_among(cands, transcript)
            if not chosen:
                r = retry_or_handoff()
                if r is None:
                    st["pending"] = None
                    return await self.handoff("product_match_failed", transcript)
                return self._variant_question(cands, item.unit)
            self.match_log.append({"spoken": transcript, "status": "match", "step": "clarification", "chosen": chosen.name})
            st["pending"] = None
            if pending.get("info"):
                return self.answer_info(pending["info"], chosen, item.unit)
            qty, unit = self.parse_qty_unit(tj, transcript)
            if unit and qty is None and unit in clean(chosen.name).split():
                unit = None  # "super pack" names the product; it is not a unit answer
            item.quantity = item.quantity if item.quantity is not None else qty
            item.unit = item.unit or unit
            self._added = []
            out = await self.add_product(chosen, item, rest)
            return out or (await self.run_items(rest) if rest else self.added_reply())

        product = self.db.get(Product, uuid.UUID(pending["product_id"]))
        qty, unit = self.parse_qty_unit(tj, transcript)
        if kind == "unit":
            names = [u[0] for u in order_svc.unit_options(product)]
            if unit not in names:
                if retry_or_handoff() is None:
                    st["pending"] = None
                    return await self.handoff("unit_unclear", transcript)
                return self._unit_question(names)
            item.unit, item.quantity = unit, pending.get("qty") if pending.get("qty") is not None else qty
        elif kind == "quantity":
            if qty is None:
                if retry_or_handoff() is None:
                    st["pending"] = None
                    return await self.handoff("quantity_unclear", transcript)
                return self.reply("clarify_quantity")
            item.quantity = qty
            item.unit = pending.get("unit") or item.unit
        st["pending"] = None
        self._added = []
        out = await self.add_product(product, item, rest)
        return out or (await self.run_items(rest) if rest else self.added_reply())

    # ---- advice: the owner's words, never the model's ------------------------------------------

    async def advice_turn(self, transcript: str) -> Outcome:
        """A how-or-when-to-use question. Sofa speaks the shop owner's written advice as it is, then offers the product it is about. With
        no matching entry nothing is made up: the question goes to the shop team and the caller is told so."""
        entry, matched = advice_svc.find(self.db, self.merchant.id, [transcript])
        self.match_log.append({"spoken": transcript, "step": "advice", "advice": entry.topic if entry else None, "matched_words": matched})
        if not entry:
            await open_handoff(self.db, self.svc, self.call, "advice_question", f"Caller asked for advice: {transcript}"[:400])
            return self.reply("advice_none", action="advice_none")
        if entry.product_name:
            self.st["pending"] = {"kind": "advice_offer", "product": entry.product_name, "attempts": 0}
            follow = self.reply("advice_offer", product=entry.product_name, action="advice_offer")
        else:
            follow = self.reply("gateway_anything_else", action="advice")
        spoken = follow.text
        follow.parts = [(entry.answer, "en"), *(follow.parts or [(spoken, follow.lang)])]  # the owner's words are their own clip, in the English voice
        follow.text = f"{entry.answer} {spoken}"
        return follow

    # ---- availability and price ------------------------------------------------------------

    async def info_turn(self, intent: str, tj: TurnJSON) -> Outcome:
        it = next((i for i in tj.items if i.spoken_name.strip()), None)
        if not it:
            return self.reply("ask_what", action="info_no_item")
        res = await match_product(self.db, self.merchant.id, it.spoken_name, self.llm, self.svc.settings, it.unit)
        self.match_log.append({"spoken": it.spoken_name, **res.log()})
        if res.status == "none":
            self.db.add(MissedDemand(merchant_id=self.merchant.id, spoken_name=it.spoken_name, call_turn_id=self.turn_id))
            return self.reply("not_available", spoken=it.spoken_name, action="not_available")
        if res.status == "ambiguous":
            cands = [p for p, _ in res.candidates]
            self.st["pending"] = {"kind": "variant", "cands": [str(p.id) for p in cands], "attempts": 0,
                                  "item": it.model_dump(), "rest": [], "info": intent}
            return self._variant_question(cands, it.unit)
        return self.answer_info(intent, res.product, it.unit)

    def answer_info(self, intent: str, p: Product, unit: str | None) -> Outcome:
        unit = unit if unit in [u[0] for u in order_svc.unit_options(p)] else p.default_unit
        price = self._price_text(p, unit)
        if intent == "check_availability" and p.stock_qty <= 0:
            alt = substitute_for(self._all_products(), p)
            if alt:
                return self.reply("out_of_stock_alt", product=p.name, alt_product=alt.name, alt_price=self._price_text(alt), action="out_of_stock")
            return self.reply("out_of_stock", product=p.name, action="out_of_stock")
        key = "avail_yes" if intent == "check_availability" else "price_is"
        return self.reply(key, product=p.name, price=price, unit=unit, action=intent)

    # ---- checkout, confirm, deny ---------------------------------------------------------------

    def checkout(self) -> Outcome:
        draft = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        if not draft or not draft.items:
            return self.reply("empty_draft", action="checkout_empty")
        if not self.profile.delivery_address:
            self.st["stage"] = "await_address"
            return self.reply("ask_address")
        self.st["stage"] = "await_confirm"
        return self.reply("readback", items=order_svc.describe_items(draft, self.L("readback")), total=naira(draft.total_kobo),
                          address=self.profile.delivery_address, action="readback")

    async def on_confirm(self) -> Outcome:
        stage = self.st["stage"]
        if stage == "offer_repeat":
            return self.repeat_last()
        if stage == "await_confirm":
            return await self.place_order()
        if stage == "ordering":
            return self.reply("ask_more", action="wants_more")
        return self.reply("repeat_prompt", action="confirm_without_context")

    def on_deny(self) -> Outcome:
        stage = self.st["stage"]
        if stage == "offer_repeat":
            self.st["stage"] = None
            return self.reply("declined", action="repeat_declined")
        if stage == "await_confirm":
            self.st["stage"] = "ordering"
            return self.reply("ask_change")
        if stage == "ordering":
            return self.checkout()
        return self.reply("repeat_prompt", action="deny_without_context")

    async def place_order(self) -> Outcome:
        draft = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        if not draft or not draft.items:
            return self.reply("empty_draft", action="place_empty")
        mode = self.profile.payment_mode_override or self.merchant.payment_mode
        mode = "transfer_first" if mode == "per_customer" else mode
        items_desc, total = order_svc.describe_items(draft), draft.total_kobo
        invoice = order_svc.confirm_order(self.db, draft, self.profile.delivery_address, mode, "customer")
        for i in draft.items:  # confirmed read-back teaches aliases
            if i.spoken_name:
                learn_alias(self.db, self.merchant.id, i.product, i.spoken_name, self.lang, "confirmed_call")
        self.call.outcome = "order_confirmed"
        self.st.update(stage=None, pending=None)
        body = (f"New order: {items_desc}. Total {naira(total)}. Customer {self.customer.phone}. "
                f"Deliver to {self.profile.delivery_address}. Pay: {'on delivery' if mode == 'pay_on_delivery' else 'transfer'}.")
        await notify_owners(self.db, self.svc, self.merchant.id, body, "merchant_new_order", draft.id)
        if invoice:
            acct = await get_or_create_account(self.db, self.svc, self.customer, self.merchant)
            sms = order_svc.invoice_sms(self.merchant.name, draft, invoice, {"bank_name": acct.bank_name, "account_number": acct.account_number})
            await send_sms(self.db, self.svc, to=self.customer.phone, body=sms, template="invoice",
                           merchant_id=self.merchant.id, order_id=draft.id, customer_id=self.customer.id)
            invoice.sent_via, invoice.sent_at = ["sms"], datetime.now(timezone.utc)
            return self.reply("order_placed_transfer", end=True, action="order_confirmed")
        return self.reply("order_placed_pod", end=True, action="order_confirmed")

    # ---- other intents ------------------------------------------------------------------

    def repeat_last(self) -> Outcome:
        last = order_svc.latest_order(self.db, self.merchant.id, self.customer.id,
                                      ("delivered", "paid", "dispatched", "awaiting_payment", "confirmed"))
        if not last:
            return self.reply("declined", action="no_last_order")
        draft = order_svc.get_draft(self.db, self.merchant.id, self.customer.id, self.call.id)
        order_svc.copy_into_draft(self.db, last, draft)
        self.st["pending"] = None
        return self.checkout()

    def track(self) -> Outcome:
        order = order_svc.latest_order(self.db, self.merchant.id, self.customer.id)
        if not order:
            return self.reply("track_none", action="track_none")
        lang = self.L("track_status")
        phrase_lang = lang if templates.has(f"status_{order.status}", lang) else "en"
        return self.reply("track_status", status_phrase=templates.render(f"status_{order.status}", phrase_lang, {}), action="track_order")

    def payment_status(self) -> Outcome:
        inv = self.db.scalar(select(Invoice).where(Invoice.merchant_id == self.merchant.id, Invoice.customer_id == self.customer.id)
                             .order_by(Invoice.created_at.desc()))
        if not inv:
            return self.reply("payment_none", action="payment_none")
        if inv.status == "paid":
            return self.reply("payment_paid", amount=naira(inv.amount_kobo), action="payment_status")
        return self.reply("payment_pending", amount=naira(inv.amount_kobo), action="payment_status")

    async def cancel(self) -> Outcome:
        order = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft", "confirmed", "awaiting_payment"))
        if not order:
            return self.reply("nothing_to_cancel", action="nothing_to_cancel")
        order_svc.cancel_order(self.db, order, "customer")
        for inv in self.db.scalars(select(Invoice).where(Invoice.order_id == order.id, Invoice.status == "open")):
            inv.status = "void"
        self.st.update(stage=None, pending=None)
        return self.reply("cancelled", action="order_cancelled")

    async def handoff(self, reason: str, transcript: str = "") -> Outcome:
        said = " | ".join([c for c, _ in self.st["history"]] + ([transcript] if transcript else []))
        draft = order_svc.latest_order(self.db, self.merchant.id, self.customer.id, ("draft",))
        await open_handoff(self.db, self.svc, self.call, reason, f"Caller said: {said}"[:400],
                           order_svc.describe_items(draft) if draft and draft.items else "")
        return self.reply("handoff", end=True, action=f"handoff:{reason}")

    # ---- owner stock updates by voice --------------------------------------------------------

    def owner_report(self) -> Outcome:
        """The owner asked how today went: the same numbers as the 7 PM SMS, read aloud in full."""
        r = reports.compute_report(self.db, self.merchant, datetime.now(timezone.utc), include_test=self.call.is_test)
        if not r.orders:
            pieces = [("report_no_orders", {})]
        elif r.orders == 1:
            pieces = [("report_one_order", {"value": naira(r.value_kobo)})]
        else:
            pieces = [("report_orders", {"orders": r.orders, "value": naira(r.value_kobo)})]
        if r.unpaid:
            pieces.append(("report_one_unpaid", {}) if r.unpaid == 1 else ("report_unpaid", {"unpaid": r.unpaid}))
        names = reports.spoken_missed(r)
        if names:
            pieces.append(("report_missed", {"items": None}))
        if r.callbacks:
            pieces.append(("report_one_callback", {}) if r.callbacks == 1 else ("report_callbacks", {"count": r.callbacks}))
        pieces.append(("owner_more", {}))
        # every piece must exist in the caller's language, else the whole report is spoken in English
        lang = self.lang if all(templates.has(k, self.lang) for k, _ in pieces) else "en"
        text = " ".join(templates.render(k, lang, {**f, "items": templates.join_list(names, lang)} if k == "report_missed" else f)
                        for k, f in pieces)
        self.call.outcome = self.call.outcome or "answered"
        return Outcome(text, "owner_report", action="owner_report|template", lang=lang)

    async def owner_turn(self, intent: str, tj: TurnJSON) -> Outcome:
        st = self.st
        if intent == "daily_report":
            return self.owner_report()
        if intent == "confirm" and st["stage"] == "owner_confirm":
            return await self.apply_owner_change(st["pending"])
        if intent == "deny" and st["stage"] == "owner_confirm":
            st.update(stage=None, pending=None)
            return self.reply("owner_cancelled", action="owner_cancelled")
        if intent != "stock_update" or not tj.items:
            return self.reply("owner_unclear", action="owner_unclear")
        it = tj.items[0]
        res = await match_product(self.db, self.merchant.id, it.spoken_name, self.llm, self.svc.settings, it.unit)
        self.match_log.append({"spoken": it.spoken_name, **res.log()})
        if res.status != "match" or it.quantity is None:
            return self.reply("owner_unclear", action="owner_unclear")
        p = res.product
        unit = it.unit if it.unit in [u[0] for u in order_svc.unit_options(p)] else p.default_unit
        new_price = int(round(it.price * 100)) if it.price else None
        change = {"product_id": str(p.id), "unit": unit, "qty": it.quantity, "price_kobo": new_price}
        st.update(stage="owner_confirm", pending=change)
        text = f"Add {it.quantity} {plural_unit(unit, it.quantity)} of {p.name}"
        if new_price:
            text += f" at {naira(new_price)} per {unit}"
        return self.reply("owner_readback", change=text, action="owner_readback")

    async def apply_owner_change(self, change: dict) -> Outcome:
        p = self.db.get(Product, uuid.UUID(change["product_id"]))
        unit, price = change["unit"], change["price_kobo"]
        if price:
            old = order_svc.unit_price(p, unit) or price
            pct = abs(price - old) * 100 / max(1, old)
            if pct > self.svc.settings.stock_change_web_confirm_pct:
                self.st.update(stage=None, pending=None)
                return self.reply("owner_needs_web", action="owner_needs_web")
            if unit == p.default_unit:
                p.price_kobo = price
            for u in p.units:
                if u.unit == unit:
                    u.price_kobo = price
        delta = order_svc.base_qty(p, unit, change["qty"])
        p.stock_qty += delta
        self.db.add(StockMovement(merchant_id=self.merchant.id, product_id=p.id, delta=delta, reason="restock",
                                  source="voice", call_turn_id=self.turn_id))
        self.call.outcome = "stock_updated"
        self.st.update(stage=None, pending=None)
        return self.reply("owner_done", product=p.name, action="stock_updated")


def greeting_key(customer: Customer, last_order_desc: str | None, owner: bool) -> tuple[str, dict]:
    if owner:
        return "greet_owner", {}
    if customer.name and last_order_desc:
        return "greet_returning_repeat", {"last_order": last_order_desc}
    if customer.name:
        return "greet_returning", {}
    return "greet_new", {}


def daypart(now: datetime | None = None) -> str:
    lagos = (now or datetime.now(timezone.utc)) + timedelta(hours=1)
    return "morning" if lagos.hour < 12 else "afternoon" if lagos.hour < 17 else "evening"
