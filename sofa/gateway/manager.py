"""The front desk: SOFA answers the phone, hears what the caller needs, and names the service.

It has the same face as the shop DialogueManager (reply, handle, on_silence, understands, not_heard) so the voice route
can run either one through the same turn loop: speech recognition, language choice, logging and audio are shared.

Banking starts by finding which bank the caller means, from the banks connected to their number:
none connected -> offer to connect one, one -> confirm it, several -> ask which. Acting on a request (balance, transfer)
and verifying the caller come next; the adapters plug into `serve()`.
"""

import logging

from sqlalchemy.orm import Session

from ..dialogue import templates
from ..dialogue.manager import Outcome
from ..dialogue.validator import fill
from ..services.handoff import open_handoff
from . import banks, links, registry, understanding, wording
from .answers import closing, quick_answer
from .adapters.banking import ONBOARDING_TOOLS
from .bankops import BankOpsMixin
from .onboarding import OnboardingMixin
from .verification import VerificationMixin

log = logging.getLogger("sofa.gateway")

BANKING = "banking"


class GatewayManager(VerificationMixin, BankOpsMixin, OnboardingMixin):
    def __init__(self, db: Session, svc, st: dict, call, customer, turn_id=None):
        self.db, self.svc, self.st, self.call, self.customer, self.turn_id = db, svc, st, call, customer, turn_id
        self.raw_json: dict | None = None
        self.match_log: list[dict] = []
        self._u: understanding.Understanding | None = None  # this turn's understanding (one model call, reused)
        self._u_key = None

    # ---- reply construction (same shape as the shop manager) --------------------------------

    @property
    def lang(self) -> str:
        return self.st["lang"]

    def reply(self, key: str, *, end: bool = False, action: str | None = None, **facts) -> Outcome:
        spoken = templates.speak_lang(key, self.lang)
        raw = templates.pick(key, spoken, self.st["turn"])
        facts = {"name": self.customer.name or "", **facts}
        if "{ack}" in raw:
            options = templates.acks(spoken)
            facts["ack"] = options[self.st["ack_i"] % len(options)]
            self.st["ack_i"] += 1
        # `extra` keeps what the wording step needs: the real facts and the English reference sentence
        return Outcome(fill(raw, facts), key, end, f"{action or key}|template", lang=spoken, parts=templates.value_parts(raw, facts, spoken),
                       extra={"facts": facts, "reference": templates.pick(key, "en", self.st["turn"])})

    def ask(self, key: str, pending: dict, *, action: str | None = None, **facts) -> Outcome:
        """A reply that waits for an answer: remember what was asked so the next turn is read as the answer."""
        out = self.reply(key, action=action, **facts)
        self.st["pending"] = {**pending, "question": out.text}
        return out

    def bank_names(self, codes: list[str]) -> str:
        return templates.join_list([banks.BANKS[c].name for c in codes], self.lang, "word_or")

    # ---- entry points ------------------------------------------------------------------------

    async def not_heard(self, transcript: str) -> Outcome:
        self.st["low_conf"] += 1
        if self.st["low_conf"] >= 2:
            return await self.give_up("asr_low_confidence", transcript)
        return self.reply("repeat_prompt", action="asr_low_confidence")

    # ---- understanding: one model call per turn --------------------------------------------------

    async def understand(self, transcript: str, alternatives: list[str] | None = None) -> understanding.Understanding:
        """Language mix, service, tool, details and yes/no, all in one call. Cached for the turn. The model may only name
        a registered tool; anything else is dropped here, before anything can act on it."""
        key = (transcript, tuple(alternatives or []))
        if self._u is not None and self._u_key == key:
            return self._u
        tool_ids = [t.tool_id for t in self.svc.gateway.tools.for_domain(BANKING)]
        u, _ = await self.svc.llm.extract(understanding.Understanding, understanding.prompt(self), transcript, alternatives,
                                           json_schema=understanding.schema(self.svc.settings.languages, tool_ids, understanding.extra_fields(self.st)))
        if u.domain not in registry.DOMAIN_CHOICES:
            u.domain = registry.UNCLEAR
        u.settle(set(tool_ids))  # the model may only name a registered tool; anything else is dropped here, before anything acts on it
        self.ground(u, transcript, alternatives)
        if self.st.get("pending") and u.answer is None and (plain := quick_answer(transcript)):
            u.answer = plain  # SOFA asked and the whole reply is a plain yes or no: do not leave that to the model
        if u.answer not in ("yes", "no", "other"):
            u.answer = None
        self._u, self._u_key = u, key
        return u

    @staticmethod
    def ground(u: understanding.Understanding, transcript: str, alternatives: list[str] | None) -> None:
        """A name, a bill, an address or a choice only counts if the caller's own words contain it. The first GPU run showed the
        model copying a recipient out of SOFA's own question; whatever was not said is dropped, and SOFA asks instead."""
        from ..textutil import clean

        heard = f" {clean(' '.join([transcript, *(alternatives or [])]))} "
        for field in ("beneficiary", "biller", "full_name", "address", "choice"):
            value = getattr(u, field)
            if value and f" {clean(value)} " not in heard and clean(value) not in heard:
                setattr(u, field, None)

    async def route(self, transcript: str, alternatives: list[str] | None = None) -> str:
        return (await self.understand(transcript, alternatives)).domain

    async def answer_to(self, transcript: str, alternatives: list[str] | None = None) -> str:
        """yes / no / other, for the question SOFA just asked (the same single understanding call)."""
        return (await self.understand(transcript, alternatives)).answer or "other"

    async def understands(self, transcript: str, alternatives: list[str] | None = None) -> bool:
        return await self.route(transcript, alternatives) != registry.UNCLEAR

    def note_language(self, u: understanding.Understanding) -> None:
        """The reply language follows the caller: two turns running in another base language switches it, so one English
        word inside a Yoruba sentence never does (the model reports the base language of the sentence, not its loanwords)."""
        st = self.st
        if not self.svc.settings.llm_language_switching:
            return
        lang = u.language if u.language in self.svc.settings.languages else None
        if not lang or lang == st["lang"]:
            st["llm_votes"] = []
            return
        votes = (st.get("llm_votes", []) + [lang])[-2:]
        if len(votes) == 2 and votes[0] == votes[1]:
            st["lang"] = self.customer.language = self.call.language = lang
            st["llm_votes"] = []
        else:
            st["llm_votes"] = votes

    # ---- wording: the model writes the sentence, the backend keeps the facts -----------------------

    # PIN, code, BVN and date-of-birth prompts are fixed words: they must never drift
    NEVER_REWORDED = ("verify_", "onboard_ask_otp", "onboard_otp", "onboard_ask_bvn", "onboard_bvn", "onboard_ask_dob", "onboard_dob",
                      "link_ask", "link_acct")

    async def reword(self, out: Outcome) -> Outcome:
        s = self.svc.settings
        facts, reference = out.extra.get("facts"), out.extra.get("reference")
        if (facts is None or not s.gateway_wording or out.key.startswith(self.NEVER_REWORDED) or s.reply_mode(self.lang) != "generated"
                or "|template" not in out.action or "|template_fallback" in out.action
                or wording.carries_facts(reference or "")):
            return out  # (or it was already worded once, or it carries a figure or a name from the bank: those stay exact)
        try:
            written = await wording.write(self.svc.llm, out.key, reference, self.lang, bool(self._u and self._u.mixed),
                                          (self.st.get("said") or [""])[-1], facts)
        except Exception as exc:  # the model being slow or wrong must never cost the caller a reply
            log.warning("wording failed for %s: %s", out.key, exc)
            return out
        if not written:
            out.action = out.action.replace("|template", "|template_fallback")
            return out
        say, filled = written
        out.extra["template"] = (out.text, out.lang, out.parts)  # spoken instead if the voice model is too slow for the new sentence
        out.text, out.lang = filled, self.lang
        out.parts = templates.value_parts(say, facts, self.lang)
        out.action = out.action.replace("|template", "|generated")
        return out

    # ---- a turn ------------------------------------------------------------------------------------

    async def handle(self, transcript: str, confidence: float, alternatives: list[str] | None = None) -> Outcome:
        return self.note_asked_more(await self.reword(await self._handle(transcript, confidence, alternatives)))

    async def on_digits(self, digits: str) -> Outcome:
        return self.note_asked_more(await self.reword(await self._on_digits(digits)))

    def note_asked_more(self, out: Outcome) -> Outcome:
        """Remember whether this reply ended with "is there anything else?": the next "no" then means goodbye."""
        spoken = " ".join([out.extra.get("reference") or out.text or "", *[t for t, _ in out.also]]).lower()
        # the gateway's own question only: the shop's "Anything else?" in the middle of an order is part of the order, not a goodbye
        self.st["asked_more"] = "anything else i can help" in spoken and not out.end_call
        return out

    async def _handle(self, transcript: str, confidence: float, alternatives: list[str] | None = None) -> Outcome:
        st = self.st
        st["turn"] += 1
        st["silence"] = 0
        if not transcript.strip():
            return await self.not_heard(transcript)
        if closing(transcript, bool(st.get("asked_more")) and not st.get("pending")):  # "no, that's all": say goodbye, do not hold the line
            self.call.outcome = self.call.outcome or "completed"
            return self.reply("gateway_goodbye", end=True, action="goodbye")
        st["said"] = (st.get("said", []) + [transcript.strip()[:200]])[-3:]  # what the provider is told if this goes unresolved
        u = await self.understand(transcript, alternatives)
        self.note_language(u)
        self.raw_json = {"language": u.language, "mixed": u.mixed, "domain": u.domain, "tool": u.tool}
        if st.get("pending"):  # SOFA asked something: this turn is probably the answer
            out = await self.resolve_pending(st["pending"], transcript, alternatives)
            if out:
                st["low_conf"] = st["unknown_streak"] = 0
                return out
        active = self.svc.gateway.adapter(st.get("domain"))
        if active and active.in_dialogue(st):  # mid-conversation with a service (an order being built): it owns this turn
            st["low_conf"] = st["unknown_streak"] = 0
            return await active.turn(self, transcript, confidence, alternatives)
        domain = u.domain
        if domain == registry.UNCLEAR and active and active.holds_session(st):
            # Already shopping: a bare "Peak milk" cannot be routed, but the shop knows its products
            st["low_conf"] = st["unknown_streak"] = 0
            return await active.turn(self, transcript, confidence, alternatives)
        # Mixed-language speech scores low even when heard well, so low confidence only rejects a turn nothing was found in.
        if confidence < self.svc.settings.asr_min_confidence and domain == registry.UNCLEAR:
            return await self.not_heard(transcript)
        st["low_conf"] = 0

        if domain == registry.HUMAN:  # SOFA has no staff: the provider follows up. With no service known yet, find out which.
            if st.get("domain"):
                return await self.give_up("asked_for_person", transcript)
            return self.reply("gateway_no_human", action="asked_for_person")
        if domain == registry.CHAT:  # "Hello Sofa", "thanks": answer like a person would, and ask what they need
            st["unknown_streak"] = 0
            return self.reply("gateway_chat", action="chat")
        if domain == registry.UNCLEAR:
            st["unknown_streak"] += 1
            if st["unknown_streak"] >= 3:
                return await self.give_up("three_unknown_turns", transcript)
            return self.reply("gateway_ask_again", action="unclear")
        st["unknown_streak"] = 0
        if st.get("domain") != domain:
            st["request"] = transcript.strip()[:200]  # what they first asked this service for: the provider needs it most
            st["request_u"] = u.model_dump()  # ...and what the model made of it, so it is not worked out a second time
        if domain in registry.SERVICES and not self.svc.gateway.adapter(domain):  # switched off on this line
            st["domain"] = None
            offered = [registry.SERVICES[d].label for d in self.svc.gateway.domains()]
            return self.reply("service_off", action=f"service_off_{domain}", service=registry.SERVICES[domain].label,
                              offered=templates.join_list(offered, self.lang, "word_and"))
        st["domain"] = self.call.service_domain = domain
        if domain == BANKING:
            return await self.banking(transcript, alternatives)
        adapter = self.svc.gateway.adapter(domain)
        if adapter:
            return await adapter.turn(self, transcript, confidence, alternatives)
        return await self.serve(domain, transcript)

    async def serve(self, domain: str, transcript: str, service: str | None = None) -> Outcome:
        """Hand the request to the service. Adapters are not connected yet, so say so honestly instead of inventing an answer."""
        return self.reply("gateway_not_ready", action=f"route_{domain}", service=service or registry.SERVICES[domain].label)

    # ---- banking: which bank? ----------------------------------------------------------------

    async def banking(self, transcript: str, alternatives: list[str] | None) -> Outcome:
        asked = await self.pick_bank_tool(transcript, alternatives)
        if asked and asked.tool_id in ONBOARDING_TOOLS:  # opening an account needs no linked bank: it is how one gets linked
            return await self.onboarding_entry(asked, transcript, alternatives)
        heard = [transcript, *(alternatives or [])]
        linked = [l.provider for l in links.active(self.db, self.customer.id, BANKING)]
        named = banks.mentioned(*heard)
        if named:
            bank = named[0]
            if bank.code in linked:
                return await self.select_bank(bank.code, transcript, alternatives)
            return self.offer_link(bank.code)  # they named a bank that is not connected to this number
        if self.st.get("bank") in linked:  # already chosen earlier on this call
            return await self.bank_act(transcript, alternatives)
        if not linked:
            return self.offer_link(None)
        if len(linked) == 1:
            return self.ask("bank_confirm_one", {"kind": "confirm_bank", "bank": linked[0]}, bank=banks.BANKS[linked[0]].name)
        return self.ask("bank_choose", {"kind": "choose_bank", "options": linked}, action="bank_choose", banks=self.bank_names(linked))

    async def select_bank(self, code: str, request: str | None, alternatives: list[str] | None = None, known: dict | None = None) -> Outcome:
        """The bank is settled. If the caller already said what they want ("my Demo Bank balance"), carry on with it. `known` is
        what the model already made of that request on an earlier turn, so it is not asked a second time."""
        if known and request:
            self._u, self._u_key = understanding.Understanding(**known), (request, tuple(alternatives or []))
        self.st["bank"] = code
        self.st["domain"] = self.call.service_domain = BANKING
        tool = await self.pick_bank_tool(request, alternatives) if request else None
        if tool:
            return await self.bank_act(request, alternatives, tool)
        return self.reply("bank_selected", action=f"bank_selected_{code}", bank=banks.BANKS[code].name)

    def offer_link(self, code: str | None) -> Outcome:
        """No connected bank for what the caller wants: offer to connect one (unless they have already asked)."""
        waiting = links.with_status(self.db, self.customer.id, BANKING, "pending")
        mine = [l for l in waiting if code is None or l.provider == code]
        if mine:
            return self.reply("bank_link_pending", action="bank_link_pending", bank=banks.BANKS[mine[0].provider].name)
        if code:
            return self.ask("bank_none_linked_named", {"kind": "offer_link", "bank": code}, bank=banks.BANKS[code].name)
        return self.ask("bank_none_linked", {"kind": "offer_link", "bank": None})

    async def connect(self, code: str | None) -> Outcome:
        """The caller said yes to being connected. Name the bank (asking if there is more than one to offer), then pass
        the request to that bank to set them up."""
        if not code:
            offered = list(banks.BANKS)
            if len(offered) > 1:
                return self.ask("bank_link_which", {"kind": "choose_link_bank", "options": offered}, banks=self.bank_names(offered))
            code = offered[0]
        return await self.link_start(code)  # an existing account is connected by checking BVN, date of birth and account number with the bank

    async def resolve_pending(self, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        """Read this turn as the answer to the question SOFA asked. None means it was not an answer: route it normally."""
        st, kind = self.st, pending["kind"]
        if kind.startswith("bank_op"):
            return await self.resolve_bank_op(pending, transcript, alternatives)
        if kind.startswith("onboard"):
            return await self.resolve_onboarding(pending, transcript, alternatives)
        if kind == "lookup_query":
            return await self.svc.gateway.adapter("lookup").resolve_pending(self, pending, transcript, alternatives)
        if kind.startswith("shop_"):
            return await self.svc.gateway.adapter("commerce").resolve_pending(self, pending, transcript, alternatives)
        if kind in ("offer_link", "confirm_bank"):
            answer = await self.answer_to(transcript, alternatives)
            self.raw_json = {"pending": kind, "answer": answer}
            if answer == "other":
                st["pending"] = None
                return None
            st["pending"] = None
            if answer == "no":
                return self.reply("declined", action=f"{kind}_declined")
            return (await self.select_bank(pending["bank"], st.get("request"), known=st.get("request_u"))
                    if kind == "confirm_bank" else await self.connect(pending.get("bank")))

        found = banks.mentioned(transcript, *(alternatives or []), among=pending["options"])
        self.raw_json = {"pending": kind, "bank": [b.code for b in found]}
        if len(found) == 1:
            st["pending"] = None
            return (await self.select_bank(found[0].code, st.get("request"), known=st.get("request_u"))
                    if kind == "choose_bank" else await self.connect(found[0].code))
        domain = await self.route(transcript, alternatives)
        pending["tries"] = pending.get("tries", 0) + 1
        if domain not in (registry.UNCLEAR, BANKING):  # they moved on to something else
            st["pending"] = None
            return None
        if pending["tries"] >= 2:
            st["pending"] = None
            return self.reply("declined", action=f"{kind}_dropped")
        st["pending"] = pending
        key = "bank_choose" if kind == "choose_bank" else "bank_link_which"
        return self.reply(key, action=f"{kind}_again", banks=self.bank_names(pending["options"]))

    # ---- silence and failure -----------------------------------------------------------------

    async def on_silence(self) -> Outcome:
        self.st["silence"] += 1
        if self.st["silence"] >= self.svc.settings.gateway_max_silences:  # an abandoned line costs minutes: the only reason SOFA ends a call
            self.call.outcome = self.call.outcome or "silence"
            return self.reply("gateway_goodbye_silence", end=True, action="silence_goodbye")
        return self.reply("gateway_still_there", action="silence_prompt")

    async def give_up(self, reason: str, transcript: str = "") -> Outcome:
        """A request SOFA could not finish. SOFA is a medium, not the customer-care desk: when the service is known, the
        details go to that service's provider to follow up (we promise no time and own no outcome). When it is not
        known there is nobody to pass it to. Either way the call record keeps why, and the call carries on: SOFA never
        hangs up on a caller, it asks if there is anything else."""
        st = self.st
        domain = st.get("domain")
        provider = registry.SERVICES[domain].provider_label if domain in registry.SERVICES else None
        code = None
        if domain == BANKING and st.get("bank"):  # a bank was chosen: it is the one who follows up
            code = st["bank"]
            provider = banks.BANKS[code].name
        if domain == "commerce" and st.get("merchant_name"):  # a shop was chosen: it is the one who follows up
            provider = st["merchant_name"]
        st["unknown_streak"] = st["low_conf"] = 0
        st["pending"] = None
        if not provider:
            self.call.outcome = "unresolved"
            return self.reply("gateway_give_up", action=f"give_up_{reason}")
        said = " | ".join(st.get("said", []) + ([transcript] if transcript and transcript not in st.get("said", []) else []))
        summary = (f"Unresolved on SOFA ({reason}). Service: {domain}. Caller {self.call.from_number}. "
                   f"First asked: {st.get('request') or 'unknown'}. Last words: {said}")[:500]
        await open_handoff(self.db, self.svc, self.call, reason, summary, provider=code)
        st["domain"] = st["request"] = None  # this case is handed over; whatever they ask next starts fresh
        return self.reply("gateway_handoff_provider", action=f"handoff_{reason}", provider=provider)
