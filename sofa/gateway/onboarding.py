"""Opening a bank account by phone, and picking up an application that was not finished.

A new customer has no PIN, so this journey is secured differently from banking: a one-time code sent by SMS proves the caller
holds the phone number, and the bank verifies who they are through a secure link texted at the end (selfie and ID photo).
SOFA only carries the conversation. The bank runs the application and is the only one that decides.

What is asked, in order, and how:
  phone code      keypad    proves the number is theirs
  full name       voice     read back and confirmed (it must match their ID)
  date of birth   keypad    eight digits; read back in words and confirmed; at least 18
  BVN             keypad    eleven digits, passed straight to the bank, never kept or logged by SOFA
  home address    voice     as heard; the bank checks it
  secure link     SMS       selfie and ID photo, done on the bank's page

Whatever the caller types is checked and forgotten: the BVN is never put in the session, the turn record or a log.
"""

import re
from datetime import date

from ..dialogue import templates
from ..dialogue.manager import Outcome
from ..models import Notification
from ..services.handoff import open_handoff
from . import banks, links
from .adapters.banking import ONBOARDING_TOOLS

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"]
INTENT = {"bank.open_account": "open", "bank.resume_registration": "resume", "bank.application_status": "status"}
DIGITS = {"dob": 8, "bvn": 11, "acct": 10}
MAX_TRIES, MAX_OTP_SENDS = 3, 3


def parse_dob(digits: str) -> date | None:
    """DDMMYYYY typed on the keypad -> a real date, or None."""
    if not (digits.isdigit() and len(digits) == 8):
        return None
    try:
        d = date(int(digits[4:]), int(digits[2:4]), int(digits[:2]))
    except ValueError:
        return None
    return d if date(1900, 1, 1) <= d <= date.today() else None


def spoken_date(d: date) -> str:
    return f"{d.day} {MONTHS[d.month - 1]} {d.year}"


def age(d: date, today: date | None = None) -> int:
    today = today or date.today()
    return today.year - d.year - ((today.month, today.day) < (d.month, d.day))


class OnboardingMixin:
    """Methods of GatewayManager that run the account-opening journey."""

    # ---- getting in ----------------------------------------------------------------------------

    def onboarding_banks(self) -> list[str]:
        return [c.strip() for c in self.svc.settings.onboarding_banks.split(",") if c.strip() in banks.BANKS]

    async def onboarding_entry(self, tool, transcript: str, alternatives: list[str] | None) -> Outcome:
        """The caller asked to open an account, carry on an application, or check one. Which bank first."""
        codes = self.onboarding_banks()
        named = banks.mentioned(transcript, *(alternatives or []))
        if named and named[0].code not in codes:
            return self.reply("onboard_cannot_bank", action="onboard_cannot_bank", bank=named[0].name)
        code = named[0].code if named else self.st.get("bank") if self.st.get("bank") in codes else codes[0] if len(codes) == 1 else None
        if code:
            return await self.onboarding_start(tool.tool_id, code)
        if not codes:
            return self.reply("onboard_cannot_bank", action="onboard_cannot_bank", bank="that bank")
        return self.ask("onboard_which_bank", {"kind": "onboard_bank", "options": codes, "tool": tool.tool_id}, banks=self.bank_names(codes))

    async def onboarding_start(self, tool_id: str, code: str) -> Outcome:
        bank = banks.BANKS[code]
        if tool_id == "bank.account_requirements":  # information, no code needed
            r = await self.svc.bank.account_requirements(self.db, code)
            return self.reply("bank_product_info", action="bank_account_requirements", bank=bank.name, text=r["text"])
        intent = INTENT[tool_id]
        latest = await self.svc.bank.latest_application(self.db, self.customer, code)
        if intent == "open":
            if latest and latest["status"] == "approved" or any(l.provider == code for l in links.active(self.db, self.customer.id, "banking")):
                return self.reply("onboard_already", action="onboard_already", bank=bank.name)
            if latest and latest["status"] in ("draft", "awaiting_identity_verification", "under_review"):
                intent = "resume" if latest["status"] == "draft" else "status"  # they already started: pick that up, not a second one
        self.st["onboarding"] = {"bank": code, "intent": intent, "data": {}, "collecting": None, "otp_sent": 0, "tries": 0,
                                 "phone_verified": False, "ref": None}
        if intent == "open":
            return self.ask("onboard_intro", {"kind": "onboard_confirm", "step": "consent"}, action="onboard_intro", bank=bank.name)
        return await self.onboarding_otp()

    async def link_start(self, code: str) -> Outcome:
        """Connect an account the caller already has: the phone code, then date of birth, BVN and account number, all checked by the bank."""
        bank = banks.BANKS[code]
        self.st["onboarding"] = {"bank": code, "intent": "link", "data": {}, "collecting": None, "otp_sent": 0, "tries": 0,
                                 "phone_verified": False, "ref": None, "link_tries": 0}
        return self.ask("link_intro", {"kind": "onboard_confirm", "step": "consent"}, action="link_intro", bank=bank.name)

    async def link_finish(self) -> Outcome:
        """All three typed and held by the bank: it says whether they match one of its customers."""
        ob, be = self.st["onboarding"], self.svc.bank
        bank = banks.BANKS[ob["bank"]]
        r = await be.link_verify(self.db, ob["ref"])
        self.raw_json = {"link": r["status"]}
        if r["status"] == "verified":
            links.link(self.db, self.customer.id, "banking", ob["bank"], status="active")
            if not self.customer.name and r.get("full_name"):
                self.customer.name = r["full_name"].split()[0].title()
            self.end_onboarding()
            return self.reply("link_done", action=f"link_done_{ob['bank']}", bank=bank.name)
        if r["status"] == "not_found":
            ob["intent"], ob["data"] = "open", {}  # the number is theirs (code verified): offer to open an account instead
            return self.ask("link_not_found", {"kind": "onboard_confirm", "step": "start_open"}, action="link_not_found", bank=bank.name)
        ob["link_tries"] += 1
        if ob["link_tries"] >= 2:
            await open_handoff(self.db, self.svc, self.call, "link_failed",
                               f"Caller {self.call.from_number} could not connect an existing account: the details did not match your records.",
                               provider=ob["bank"])
            self.end_onboarding()
            return self.reply("link_gave_up", action="link_gave_up", bank=bank.name)
        ob["data"], ob["ref"] = {}, (await be.start_link_check(self.db, self.customer, ob["bank"]))["reference"]
        opening = self.reply("link_mismatch", action="link_mismatch", bank=bank.name)
        nxt = await self.onboarding_next()
        opening.also = [*(nxt.parts or [(nxt.text, nxt.lang)]), *nxt.also]
        opening.collect = nxt.collect
        return opening

    def end_onboarding(self) -> None:
        self.st["onboarding"] = None
        self.st["pending"] = None

    # ---- the phone code ------------------------------------------------------------------------

    async def onboarding_otp(self) -> Outcome:
        ob = self.st["onboarding"]
        if ob["phone_verified"]:
            return await self.onboarding_after_otp()
        if ob["otp_sent"] >= MAX_OTP_SENDS:
            self.end_onboarding()
            return self.reply("onboard_otp_failed", action="onboard_otp_failed")
        bank = banks.BANKS[ob["bank"]]
        await self.svc.bankauth.issue_otp(ob["bank"], self.customer.phone, bank.name, self.svc.gateway.clock())
        self.db.add(Notification(channel="sms", template="otp", to_number=self.customer.phone, customer_id=self.customer.id,
                                 body="[verification code: hidden]", status="sent"))
        ob["otp_sent"] += 1
        ob["collecting"] = "otp"
        return self.collect(self.reply("onboard_ask_otp", action="onboard_ask_otp"), "otp")

    async def onboarding_after_otp(self) -> Outcome:
        """The number is proven. Start, resume or report, depending on what they asked."""
        ob, be = self.st["onboarding"], self.svc.bank
        bank = banks.BANKS[ob["bank"]]
        if ob["intent"] == "link":
            ob["ref"] = (await be.start_link_check(self.db, self.customer, ob["bank"]))["reference"]
            return await self.onboarding_next()
        latest = await be.latest_application(self.db, self.customer, ob["bank"])
        if ob["intent"] == "open":
            if not (latest and latest["status"] == "draft"):  # a draft is carried on, not duplicated
                latest = await be.start_application(self.db, self.customer, ob["bank"])
            ob["ref"] = latest["reference"]
            self.load(ob, latest)
            return await self.onboarding_next()
        if not latest:
            return self.ask("onboard_status_none", {"kind": "onboard_confirm", "step": "start_open"}, action="onboard_status_none")
        ob["ref"] = latest["reference"]
        if ob["intent"] == "resume" and latest["status"] == "draft":
            self.load(ob, latest)
            opening = self.reply("onboard_resume", action="onboard_resume", ref=latest["reference"])
            nxt = await self.onboarding_next()
            opening.also = [*(nxt.parts or [(nxt.text, nxt.lang)]), *nxt.also]
            opening.collect = nxt.collect
            return opening
        return await self.onboarding_report(latest, bank)

    @staticmethod
    def load(ob: dict, app: dict) -> None:
        """What the bank already holds for this application, so nothing is asked twice."""
        ob["data"] = {k: v for k, v in (("full_name", app.get("full_name")), ("dob", app.get("dob")), ("address", app.get("address"))) if v}
        ob["data"]["bvn_given"] = bool(app.get("bvn_given"))

    async def onboarding_report(self, app: dict, bank) -> Outcome:
        status, ref = app["status"], app["reference"]
        if status == "awaiting_identity_verification":
            return self.ask("onboard_status_awaiting", {"kind": "onboard_confirm", "step": "resend"}, action="onboard_status_awaiting", ref=ref)
        self.end_onboarding()
        if status == "under_review":
            return self.reply("onboard_status_review", action="onboard_status_review", ref=ref, bank=bank.name)
        if status == "approved":
            return self.reply("onboard_status_approved", action="onboard_status_approved", bank=bank.name)
        if status == "rejected":
            await open_handoff(self.db, self.svc, self.call, "application_rejected",
                               f"Caller {self.call.from_number}. Application {ref} rejected: {app.get('reason') or 'no reason given'}.", provider=bank.code)
            return self.reply("onboard_status_rejected", action="onboard_status_rejected", ref=ref, bank=bank.name)
        return self.reply("onboard_status_none", action="onboard_status_none")

    # ---- the questions, in order -----------------------------------------------------------------

    async def onboarding_next(self) -> Outcome:
        ob = self.st["onboarding"]
        data, bank = ob["data"], banks.BANKS[ob["bank"]]
        ob["tries"] = 0
        if ob["intent"] == "link":
            if not data.get("dob"):
                ob["collecting"] = "dob"
                return self.collect(self.reply("onboard_ask_dob", action="onboard_ask_dob"), "dob")
            if not data.get("bvn_given"):
                ob["collecting"] = "bvn"
                return self.collect(self.reply("onboard_ask_bvn", action="onboard_ask_bvn", bank=bank.name), "bvn")
            if not data.get("acct_given"):
                ob["collecting"] = "acct"
                return self.collect(self.reply("link_ask_acct", action="link_ask_acct", bank=bank.name), "acct")
            return await self.link_finish()
        if not data.get("full_name"):
            return self.ask("onboard_ask_name", {"kind": "onboard_text", "field": "full_name"}, action="onboard_ask_name")
        if not data.get("dob"):
            ob["collecting"] = "dob"
            return self.collect(self.reply("onboard_ask_dob", action="onboard_ask_dob"), "dob")
        if not data.get("bvn_given"):
            ob["collecting"] = "bvn"
            return self.collect(self.reply("onboard_ask_bvn", action="onboard_ask_bvn", bank=bank.name), "bvn")
        if not data.get("address"):
            return self.ask("onboard_ask_address", {"kind": "onboard_text", "field": "address"}, action="onboard_ask_address")
        return await self.onboarding_submit()

    async def onboarding_submit(self) -> Outcome:
        ob, be = self.st["onboarding"], self.svc.bank
        bank = banks.BANKS[ob["bank"]]
        r = await be.submit_application(self.db, ob["ref"])
        if r["status"] != "success":
            await open_handoff(self.db, self.svc, self.call, "application_incomplete",
                               f"Caller {self.call.from_number}. Application {ob['ref']} is missing: {', '.join(r.get('missing', []))}.", provider=bank.code)
            self.end_onboarding()
            return self.reply("onboard_incomplete", action="onboard_incomplete", bank=bank.name)
        await self.send_link(bank, r["link"], ob["ref"])
        links.request_link(self.db, self.customer.id, "banking", bank.code)  # pending until the bank approves
        self.end_onboarding()
        return self.reply("onboard_submitted", action="onboard_submitted", bank=bank.name, ref=r["reference"])

    async def send_link(self, bank, link: str, ref: str) -> None:
        await self.svc.sms.send(self.customer.phone, f"{bank.name}: finish opening your account here: {link} (reference {ref}). Do not share this link.")
        self.db.add(Notification(channel="sms", template="secure_link", to_number=self.customer.phone, customer_id=self.customer.id,
                                 body="[secure link: hidden]", status="sent"))

    # ---- keypad entries --------------------------------------------------------------------------

    async def onboarding_digits(self, digits: str) -> Outcome:
        """A code, a date of birth or a BVN was typed. Each is checked and forgotten here."""
        ob, be = self.st["onboarding"], self.svc.bank
        step = ob["collecting"]
        digits = (digits or "").strip()
        if not digits:  # nothing typed before the timeout: the application stays saved
            ob["collecting"] = None
            return self.reply("onboard_abandoned", action="onboard_abandoned")
        now = self.svc.gateway.clock()
        if step == "otp":
            ok = digits.isdigit() and await self.svc.bankauth.verify_otp(ob["bank"], self.customer.phone, digits, now)
            self.raw_json = {"onboarding": "otp", "ok": ok}
            if ok:
                ob["collecting"], ob["phone_verified"], ob["tries"] = None, True, 0
                return await self.onboarding_after_otp()
            ob["tries"] += 1
            if ob["tries"] >= MAX_TRIES:
                self.end_onboarding()
                return self.reply("onboard_otp_failed", action="onboard_otp_failed")
            return self.collect(self.reply("onboard_otp_retry", action="onboard_otp_retry"), "otp")
        if step == "dob":
            d = parse_dob(digits)
            self.raw_json = {"onboarding": "dob", "ok": d is not None}
            if d and age(d) < self.svc.settings.min_age_user:  # Sofa is for people aged 12 and above
                self.end_onboarding()
                self.call.outcome = self.call.outcome or "underage"
                return self.reply("age_too_young_service", end=True, action="age_too_young_service")
            if d and age(d) < self.svc.settings.banking_min_age:  # banking needs 16
                self.end_onboarding()
                return self.reply("onboard_too_young", action="onboard_too_young")
            if not d:
                return self.retry_or_stop("dob", "onboard_dob_retry")
            ob["collecting"], ob["pending_dob"] = None, d.isoformat()
            return self.ask("onboard_confirm_dob", {"kind": "onboard_confirm", "step": "dob_confirm"}, action="onboard_confirm_dob", date=spoken_date(d))
        if step == "acct":  # an existing account number: straight to the bank
            r = await be.link_field(self.db, ob["ref"], "account_number", digits)
            self.raw_json = {"onboarding": "acct", "ok": r["status"] == "success"}
            if r["status"] != "success":
                return self.retry_or_stop("acct", "link_acct_retry")
            ob["collecting"] = None
            ob["data"]["acct_given"] = True
            return await self.onboarding_next()
        # BVN: straight to the bank, never kept
        if ob["intent"] == "link":
            r = await be.link_field(self.db, ob["ref"], "bvn", digits)
        else:
            r = await be.submit_identity(self.db, ob["ref"], digits) if digits.isdigit() and len(digits) == DIGITS["bvn"] else {"status": "invalid_bvn"}
        self.raw_json = {"onboarding": "bvn", "ok": r["status"] == "success"}
        if r["status"] != "success":
            return self.retry_or_stop("bvn", "onboard_bvn_retry")
        ob["collecting"] = None
        ob["data"]["bvn_given"] = True
        return await self.onboarding_next()

    def retry_or_stop(self, step: str, key: str) -> Outcome:
        ob = self.st["onboarding"]
        ob["tries"] += 1
        if ob["tries"] >= MAX_TRIES:
            self.end_onboarding()
            return self.reply("onboard_gave_up", action="onboard_gave_up")
        return self.collect(self.reply(key, action=key), step)

    # ---- answers to what was asked -----------------------------------------------------------------

    async def resolve_onboarding(self, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        st, kind = self.st, pending["kind"]
        ob = st.get("onboarding")
        if kind == "onboard_bank":
            found = banks.mentioned(transcript, *(alternatives or []), among=pending["options"])
            self.raw_json = {**(self.raw_json or {}), "pending": kind, "bank": [b.code for b in found]}
            if len(found) == 1:
                st["pending"] = None
                return await self.onboarding_start(pending["tool"], found[0].code)
            st["pending"] = None
            return None
        if not ob:
            st["pending"] = None
            return None
        if kind == "onboard_confirm":
            return await self.onboarding_confirm(pending, transcript, alternatives)
        return await self.onboarding_text(pending, transcript, alternatives)

    async def onboarding_confirm(self, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        st, ob, be, step = self.st, self.st["onboarding"], self.svc.bank, pending["step"]
        answer = await self.answer_to(transcript, alternatives)
        self.raw_json = {**(self.raw_json or {}), "pending": "onboard_confirm", "step": step, "answer": answer}
        st["pending"] = None
        if answer == "other":  # they moved on; a saved application can be picked up later
            return None
        yes = answer == "yes"
        if step == "consent":
            if not yes:
                self.end_onboarding()
                return self.reply("onboard_declined", action="onboard_declined")
            return await self.onboarding_otp()
        if step == "start_open":
            if not yes:
                self.end_onboarding()
                return self.reply("onboard_declined", action="onboard_declined")
            ob["intent"] = "open"
            return await self.onboarding_after_otp()
        if step == "resend":
            if yes:
                r = await be.resend_link(self.db, ob["ref"])
                if r["status"] == "success":
                    await self.send_link(banks.BANKS[ob["bank"]], r["link"], ob["ref"])
            self.end_onboarding()
            return self.reply("onboard_resent" if yes else "gateway_anything_else", action="onboard_resent" if yes else "onboard_resend_declined")
        if step == "name_confirm":
            if yes:
                name = ob["pending_name"]
                ob["data"]["full_name"] = name
                self.customer.name = self.customer.name or name.split()[0]
                await be.save_fields(self.db, ob["ref"], full_name=name)
                return await self.onboarding_next()
            ob["tries"] += 1
            if ob["tries"] >= MAX_TRIES - 1:
                self.end_onboarding()
                return self.reply("onboard_gave_up", action="onboard_gave_up")
            return self.ask("onboard_ask_name_again", {"kind": "onboard_text", "field": "full_name"}, action="onboard_ask_name_again")
        # dob_confirm
        if yes:
            ob["data"]["dob"] = ob["pending_dob"]
            if ob["intent"] == "link":
                await be.link_field(self.db, ob["ref"], "dob", ob["pending_dob"])
            else:
                await be.save_fields(self.db, ob["ref"], dob=ob["pending_dob"])
            return await self.onboarding_next()
        ob["tries"] += 1
        if ob["tries"] >= MAX_TRIES - 1:
            self.end_onboarding()
            return self.reply("onboard_gave_up", action="onboard_gave_up")
        ob["collecting"] = "dob"
        return self.collect(self.reply("onboard_dob_retry", action="onboard_dob_retry"), "dob")

    async def onboarding_text(self, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        st, ob, be, field = self.st, self.st["onboarding"], self.svc.bank, pending["field"]
        u = await self.understand(transcript, alternatives)
        value = (u.full_name if field == "full_name" else u.address) or ""
        self.raw_json = {**(self.raw_json or {}), "pending": "onboard_text", "field": field, "heard": bool(value)}
        if not value and u.domain not in ("unclear", "banking") and not u.answer:  # said something else entirely: the application stays saved
            st["pending"] = None
            return None
        if u.answer == "no" and not value:
            self.end_onboarding()
            return self.reply("onboard_declined", action="onboard_declined")
        value = (value or transcript).strip()[:200]
        st["pending"] = None
        if field == "full_name":
            if len(value) < 3 or not re.search(r"[A-Za-z]", value):
                return self.ask("onboard_ask_name_again", {"kind": "onboard_text", "field": "full_name"}, action="onboard_ask_name_again")
            ob["pending_name"] = value
            return self.ask("onboard_confirm_name", {"kind": "onboard_confirm", "step": "name_confirm"}, action="onboard_confirm_name", full_name=value)
        ob["data"]["address"] = value  # as heard: the bank checks it against the ID
        await be.save_fields(self.db, ob["ref"], address=value)
        return await self.onboarding_next()
