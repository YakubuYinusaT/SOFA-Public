"""Verifying the caller inside a banking call: PIN and one-time code on the keypad.

Secrets never touch the audio path: the caller is asked for them with a keypad prompt, not a recording; if they speak
during that step anyway the audio is dropped unheard (see `keypad_only`); and the digits are checked and forgotten, never
stored, logged or put in the turn record. The bank is the one that says whether they are right.
"""

from ..dialogue.manager import Outcome
from ..models import Notification
from ..services.handoff import open_handoff
from . import banks, links, registry


def digits_step(st: dict) -> str | None:
    """What the call is waiting for on the keypad right now (pin, otp, dob, bvn), if anything. While it waits, speech is dropped unheard."""
    return (st.get("auth") or {}).get("step") or (st.get("onboarding") or {}).get("collecting")


class VerificationMixin:
    """Methods of GatewayManager that run a banking action behind the verification policy."""

    # ---- running a banking tool ------------------------------------------------------------

    async def pick_bank_tool(self, transcript: str, alternatives: list[str] | None):
        """The tool the caller asked for, from this turn's understanding (the model can only name registered tools)."""
        u = await self.understand(transcript, alternatives)
        tool = self.svc.gateway.tools.get(u.tool) if u.tool else None
        return tool if tool and tool.domain == "banking" else None

    async def bank_act(self, transcript: str, alternatives: list[str] | None, tool=None) -> Outcome:
        code = self.st["bank"]
        bank = banks.BANKS[code]
        tool = tool or await self.pick_bank_tool(transcript, alternatives)
        self.raw_json = {"domain": "banking", "tool": tool.tool_id if tool else None}
        if not tool:
            return self.reply("bank_ask_what", action="bank_ask_what", bank=bank.name)
        now = self.svc.gateway.clock()
        link = links.get(self.db, self.customer.id, "banking", code)
        if link and links.locked(link, now):
            return self.reply("verify_locked", action="verify_locked", bank=bank.name)
        await self.start_op(tool, transcript, alternatives)
        return await self.advance_op()  # verifies first if the policy says so, then asks, reads back and runs

    # ---- the challenge ---------------------------------------------------------------------

    def collect(self, out: Outcome, step: str) -> Outcome:
        s = self.svc.settings
        out.collect = {"kind": step, "digits": {"pin": s.verify_pin_length, "otp": s.verify_otp_length, "dob": 8, "bvn": 11, "acct": 10}[step]}
        return out

    def begin_challenge(self, tool, challenge, bank) -> Outcome:
        auth = self.st.get("auth") or {}
        if auth.get("bank") != self.st["bank"]:  # a different bank: nothing carries over
            auth = {"bank": self.st["bank"]}
        auth.update(step="pin", pending_tool=tool.tool_id, reason=challenge.reason, need_otp=challenge.kind == "pin_otp")
        self.st["auth"] = auth
        return self.collect(self.reply(f"verify_pin_{challenge.reason}", action=f"verify_ask_pin_{challenge.reason}", bank=bank.name), "pin")

    async def _on_digits(self, digits: str) -> Outcome:
        """The caller typed something on the keypad. The digits are used here and nowhere else."""
        ob = self.st.get("onboarding")
        if ob and ob.get("collecting"):  # a code, a date of birth or a BVN for an account application
            return await self.onboarding_digits(digits)
        st = self.st
        auth = st.get("auth") or {}
        step = auth.get("step")
        if not step:
            return self.reply("gateway_anything_else", action="digits_unexpected")
        digits = (digits or "").strip()
        if not digits:  # nothing typed before the timeout
            return self.abandon()
        code, bank, phone = st["bank"], banks.BANKS[st["bank"]], self.customer.phone
        now = self.svc.gateway.clock()
        s = self.svc.settings
        if step == "pin":
            ok = digits.isdigit() and len(digits) == s.verify_pin_length and await self.svc.bankauth.verify_pin(code, phone, digits)
            self.raw_json = {"verify": "pin", "ok": ok}
            if not ok:
                return await self.verification_failed("pin")
            auth["last_pin_at"] = now
            if auth.get("need_otp"):
                await self.svc.bankauth.issue_otp(code, phone, bank.name, now)
                self.db.add(Notification(channel="sms", template="otp", to_number=phone, customer_id=self.customer.id,
                                         body="[verification code: hidden]", status="sent"))  # the code itself is never stored
                auth["step"] = "otp"
                return self.collect(self.reply("verify_ask_otp", action="verify_ask_otp"), "otp")
            return await self.finish_verification(now)
        ok = digits.isdigit() and await self.svc.bankauth.verify_otp(code, phone, digits, now)
        self.raw_json = {"verify": "otp", "ok": ok}
        if not ok:
            return await self.verification_failed("otp")
        return await self.finish_verification(now)

    async def verification_failed(self, step: str) -> Outcome:
        st, auth, s = self.st, self.st["auth"], self.svc.settings
        bank = banks.BANKS[st["bank"]]
        now = self.svc.gateway.clock()
        link = links.get(self.db, self.customer.id, "banking", st["bank"])
        if link:
            links.record_failure(link, now, s.verify_max_attempts, s.verify_lockout_minutes)
        if link and links.locked(link, now):  # too many wrong: stop, and tell the bank (it is their customer's account)
            auth.update(step=None, pending_tool=None, verified_at=None)
            st["bank_op"] = None
            await open_handoff(self.db, self.svc, self.call, "verification_failed",
                               f"Caller {self.call.from_number} failed {step.upper()} verification repeatedly for {bank.name}. Account access paused.",
                               provider=st["bank"])
            return self.reply("verify_failed_handoff", action="verify_failed_handoff", bank=bank.name)
        return self.collect(self.reply(f"verify_{step}_retry", action=f"verify_{step}_retry"), step)

    async def finish_verification(self, now: float) -> Outcome:
        st, auth = self.st, self.st["auth"]
        tool = self.svc.gateway.tools.get(auth["pending_tool"])
        link = links.get(self.db, self.customer.id, "banking", st["bank"])
        if link:
            links.clear_failures(link, now)
        reason = auth.get("reason")
        auth.update(verified=True, step=None, reason=None, pending_tool=None, last_pin_at=now,
                    resources=sorted(set(auth.get("resources", [])) | {tool.resource}))
        auth["verified_at"] = auth.get("verified_at") or now
        op = st.get("bank_op")
        if reason == "authorise" and op:
            op["authorised"] = True  # this PIN authorises this one action, and only this one
        done = await self.reword(await self.advance_op())  # whatever the operation needs next: a question, a read-back, or the bank's answer
        if reason == "authorise":
            return done  # no "you are verified": just what the bank did
        ok = self.reply("verify_ok", action="verified")
        ok.also = [*(done.parts or [(done.text, done.lang)]), *done.also]
        ok.collect = done.collect
        ok.action = f"verified|{done.action}"
        return ok

    def abandon(self) -> Outcome:
        auth = self.st.get("auth") or {}
        auth.update(step=None, pending_tool=None)
        self.st["bank_op"] = None  # nothing was verified, so the request is dropped
        return self.reply("verify_abandoned", action="verify_abandoned")

    def keypad_only(self) -> Outcome:
        """Speech arrived while a PIN or code was being collected. It was dropped unheard; ask for the keypad instead."""
        step = digits_step(self.st) or "pin"
        return self.collect(self.reply("verify_use_keypad", action="verify_use_keypad"), step)
