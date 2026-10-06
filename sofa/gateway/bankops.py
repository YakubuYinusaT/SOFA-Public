"""Running a banking operation with a caller: ask for what is missing, read it back, check the PIN at the moment of
action, run it at the bank, and speak the bank's answer.

Every figure and name that is spoken comes from the bank's structured answer, never from the language model. The model's
job is only to pick the operation and pull out what the caller said (an amount, who to, which bill). A money move needs a
spoken yes after the read-back, and a PIN that is still good for exactly that one action.
"""

import logging
import re

from ..dialogue import templates
from ..dialogue.manager import Outcome
from ..dialogue.templates import spell_digits
from ..services.handoff import open_handoff
from ..textutil import clean, naira
from . import banks, registry
from .policy import Challenge

log = logging.getLogger("sofa.bankops")

ASK_AMOUNT = {"bank.transfer": "bank_ask_amount_transfer", "bank.buy_airtime": "bank_ask_amount_airtime", "bank.pay_bill": "bank_ask_amount_bill"}
ASK = {"beneficiary": "bank_ask_beneficiary", "biller": "bank_ask_biller", "details": "bank_ask_details"}
CONFIRM = {"bank.transfer": "bank_confirm_transfer", "bank.buy_airtime": "bank_confirm_airtime", "bank.pay_bill": "bank_confirm_bill",
           "bank.block_card": "bank_confirm_card"}
ORDINALS = {"first": 0, "1st": 0, "second": 1, "2nd": 1, "third": 2, "3rd": 2, "fourth": 3, "4th": 3}


def has(params: dict, name: str) -> bool:
    return bool(params.get("amount_kobo") if name == "amount" else params.get(name))


class BankOpsMixin:
    """Methods of GatewayManager that carry a banking operation from request to the bank's answer."""

    # ---- starting ----------------------------------------------------------------------------

    async def extract_params(self, tool, transcript: str, alternatives: list[str] | None, focus: str | None = None) -> dict:
        """What the caller said for this operation, from this turn's understanding (no separate model call)."""
        if not (tool.required or tool.tool_id == "bank.transfer"):
            return {}
        u = await self.understand(transcript, alternatives)
        out: dict = {}
        if u.amount_naira and u.amount_naira > 0:
            out["amount_kobo"] = int(round(u.amount_naira * 100))
        if u.beneficiary:
            out["beneficiary"] = u.beneficiary
        if u.biller:
            out["biller"] = u.biller
        if u.usual:
            out["usual"] = True
        return out

    async def start_op(self, tool, transcript: str, alternatives: list[str] | None) -> None:
        self.st["pending"] = None
        self.st["bank_op"] = {"tool": tool.tool_id, "params": await self.extract_params(tool, transcript, alternatives), "confirmed": False}

    # ---- moving it along ---------------------------------------------------------------------

    async def advance_op(self) -> Outcome:
        """Do the next thing the operation needs: verify, ask for something, read it back, or run it."""
        st = self.st
        op = st.get("bank_op")
        if not op:
            return self.reply("gateway_anything_else", action="no_bank_op")
        tool = self.svc.gateway.tools.get(op["tool"])
        bank = banks.BANKS[st["bank"]]
        challenge = self.svc.gateway.challenge(tool.tool_id, st)
        if challenge:  # not cleared right now (first time, stale, new resource, or a money move): verify, then carry on
            return self.begin_challenge(tool, challenge, bank)
        params = op["params"]
        if tool.tool_id == "bank.transfer" and params.get("beneficiary") and not params.get("beneficiary_id"):
            early = await self.resolve_beneficiary(op)
            if early:
                return early
        if params.get("usual") and not has(params, "amount") and params.get("usual_kobo"):
            params["amount_kobo"], params["used_usual"] = params["usual_kobo"], True
        for name in tool.required:
            if not has(params, name):
                return self.ask_param(tool, name, bank)
        if tool.confirm and not op.get("confirmed"):
            return self.ask_confirm(tool, params)
        if tool.risk == "sensitive" and not op.get("authorised"):
            # The caller has heard the details and said yes. Before the bank is told to act, a PIN typed now authorises this one
            # action. The PIN from earlier in the call does not: someone else may be holding the phone by now.
            return self.begin_challenge(tool, Challenge("pin", "authorise"), bank)
        return await self.execute_op(tool, params, bank)

    def ask_param(self, tool, name: str, bank) -> Outcome:
        key = ASK_AMOUNT[tool.tool_id] if name == "amount" else ASK[name]
        return self.ask(key, {"kind": "bank_op_param", "expect": name}, action=f"bank_ask_{name}", bank=bank.name)

    def ask_confirm(self, tool, params: dict) -> Outcome:
        key = CONFIRM[tool.tool_id]
        if tool.tool_id == "bank.transfer" and params.get("used_usual"):
            key += "_usual"
        facts = {"amount": naira(params["amount_kobo"]) if params.get("amount_kobo") else "", "who": params.get("beneficiary_label", ""),
                 "biller": params.get("biller", "")}
        return self.ask(key, {"kind": "bank_op_confirm"}, action="bank_confirm", **facts)

    def cancel_op(self, key: str = "bank_cancelled") -> Outcome:
        self.st["bank_op"] = None
        self.st["pending"] = None
        return self.reply(key, action="bank_op_cancelled")

    # ---- the recipient --------------------------------------------------------------------------

    @staticmethod
    def label(c: dict) -> str:
        """How a recipient is said aloud: the name and the bank, since one name can be at two banks."""
        return f"{c['name']} at {c['bank_name']}" if c.get("bank_name") else c["name"]

    def apply_beneficiary(self, params: dict, c: dict) -> None:
        params.update(beneficiary=self.label(c), beneficiary_id=c["id"], beneficiary_name=c["name"], beneficiary_label=self.label(c),
                      usual_kobo=c["last_amount_kobo"])

    async def resolve_beneficiary(self, op: dict) -> Outcome | None:
        """Match what the caller said ("mama") to one of their saved recipients at the bank. None means resolved. Two matches
        (the same name at two banks, say) are never guessed between: SOFA asks, reading out the bank and the last digits."""
        params, code = op["params"], self.st["bank"]
        people = await self.svc.bank.beneficiaries(self.db, self.customer, code)
        spoken = clean(params["beneficiary"])
        said = {w for w in spoken.split() if len(w) >= 2} - {"my", "the", "to", "a"}
        scored: list[tuple[float, dict]] = []
        for p in people:
            names = [clean(n) for n in [p["name"], *p["nicknames"]]]
            pool = {w for n in names for w in n.split()}
            if spoken in names:
                scored.append((1.0, p))
            elif said and said <= pool:
                scored.append((0.8, p))
            elif said & {w for n in names for w in n.split() if len(w) >= 3}:
                scored.append((0.5, p))
        best = max((s for s, _ in scored), default=0)
        top = [p for s, p in scored if s == best]
        if len(top) == 1:
            self.apply_beneficiary(params, top[0])
            return None
        if len(top) > 1:
            params["beneficiary"] = None
            op["candidates"] = top[:4]
            return self.ask_which(op)
        who = params["beneficiary"]
        self.st["bank_op"] = None
        return self.reply("bank_beneficiary_unknown", action="bank_beneficiary_unknown", who=who)

    def ask_which(self, op: dict) -> Outcome:
        options = templates.join_list([f"{self.label(c)}, account ending {spell_digits(c['last4'])}" for c in op["candidates"]], self.lang, "word_or")
        return self.ask("bank_beneficiary_which", {"kind": "bank_op_param", "expect": "beneficiary_choice"}, action="bank_beneficiary_which", people=options)

    async def pick_candidate(self, op: dict, transcript: str, alternatives: list[str] | None) -> dict | None:
        """Which of the offered accounts did they mean? By bank ("the GT one"), by the last digits, or by place ("the first")."""
        u = await self.understand(transcript, alternatives)
        texts = [transcript, *(alternatives or []), u.choice or ""]
        hits = []
        for i, c in enumerate(op.get("candidates", [])):
            bank_words = set(clean(c["bank_name"]).split()) - {"bank"}
            for t in texts:
                words = clean(t).split()
                digits = "".join(re.findall(r"\d", t))
                first = next((w for w in words if w in ORDINALS), None)
                if (bank_words and bank_words <= set(words)) or (c["last4"] in digits) or (first is not None and ORDINALS[first] == i):
                    hits.append(c)
                    break
        return hits[0] if len(hits) == 1 else None

    # ---- answering what was asked ------------------------------------------------------------

    async def resolve_bank_op(self, pending: dict, transcript: str, alternatives: list[str] | None) -> Outcome | None:
        """Read this turn as the answer to what SOFA just asked about the operation. None: not an answer, route it normally."""
        st = self.st
        op = st.get("bank_op")
        if not op:
            st["pending"] = None
            return None
        tool = self.svc.gateway.tools.get(op["tool"])
        seen = self.raw_json or {}
        if pending["kind"] == "bank_op_confirm":
            answer = await self.answer_to(transcript, alternatives)
            self.raw_json = {**seen, "pending": "bank_op_confirm", "answer": answer}
            st["pending"] = None
            if answer == "other":  # they moved on: the unconfirmed operation is dropped, nothing is done
                st["bank_op"] = None
                return None
            if answer == "no":
                return self.cancel_op()
            op["confirmed"] = True
            return await self.advance_op()

        expect = pending["expect"]
        if expect == "details":  # a complaint in their own words
            op["params"]["details"] = transcript.strip()[:500]
            st["pending"] = None
            self.raw_json = {**seen, "pending": "bank_op_param", "expect": expect}
            return await self.advance_op()
        if expect == "beneficiary_choice":
            pick = await self.pick_candidate(op, transcript, alternatives)
            self.raw_json = {**seen, "pending": "bank_op_param", "expect": expect, "picked": bool(pick)}
            if pick:
                self.apply_beneficiary(op["params"], pick)
                st["pending"] = None
                return await self.advance_op()
        else:
            found = await self.extract_params(tool, transcript, alternatives, focus=expect)
            self.raw_json = {**seen, "pending": "bank_op_param", "expect": expect, "found": sorted(found)}
            if has(found, expect):
                st["pending"] = None
                op["params"].update(found)
                return await self.advance_op()
        # not an answer to the question
        answer = await self.answer_to(transcript, alternatives)
        domain = await self.route(transcript, alternatives)
        pending["tries"] = pending.get("tries", 0) + 1
        if answer == "no" or pending["tries"] >= 2:
            return self.cancel_op()
        if domain not in (registry.UNCLEAR, "banking"):  # they changed the subject
            st["pending"] = None
            st["bank_op"] = None
            return None
        out = self.ask_which(op) if expect == "beneficiary_choice" else self.ask_param(tool, expect, banks.BANKS[st["bank"]])
        st["pending"]["tries"] = pending["tries"]  # `ask` made a fresh pending: keep count of the failed answers
        return out

    # ---- running it at the bank ------------------------------------------------------------------

    async def execute_op(self, tool, params: dict, bank) -> Outcome:
        st, code = self.st, self.st["bank"]
        op_id = tool.tool_id
        try:
            out = await self.run_at_bank(op_id, params, bank, code)
            if out.action.startswith("bank_no_account"):  # the bank has no account for them: that is the bank's case to follow up
                await open_handoff(self.db, self.svc, self.call, "no_account", f"Caller {self.call.from_number}. No account found at {bank.name} for {op_id}.",
                                   provider=code)
        except Exception:
            log.exception("bank operation failed")
            out = await self.hand_to_bank("backend_error", f"{op_id} failed at {bank.name}", "bank_failed_handoff", bank)
        finally:
            st["bank_op"] = None
        return out

    async def hand_to_bank(self, reason: str, summary: str, key: str, bank) -> Outcome:
        await open_handoff(self.db, self.svc, self.call, reason, f"Caller {self.call.from_number}. {summary}", provider=self.st["bank"])
        return self.reply(key, action=f"handoff_{reason}", bank=bank.name)

    async def run_at_bank(self, op_id: str, p: dict, bank, code: str) -> Outcome:
        be, db, me = self.svc.bank, self.db, self.customer
        if op_id == "bank.balance":
            r = await be.balance(db, me, code)
            return self.answer(r, bank, "bank_balance", balance=naira(r.get("balance_kobo", 0)))
        if op_id == "bank.transfer":
            r = await be.transfer(db, me, code, p["beneficiary_id"], p["amount_kobo"])
            return self.answer(r, bank, "bank_transfer_done", amount=naira(p["amount_kobo"]), who=p["beneficiary_label"], balance=naira(r.get("balance_kobo", 0)))
        if op_id == "bank.buy_airtime":
            r = await be.buy_airtime(db, me, code, p["amount_kobo"])
            return self.answer(r, bank, "bank_airtime_done", amount=naira(p["amount_kobo"]), balance=naira(r.get("balance_kobo", 0)))
        if op_id == "bank.pay_bill":
            r = await be.pay_bill(db, me, code, p["biller"], p["amount_kobo"])
            return self.answer(r, bank, "bank_bill_done", amount=naira(p["amount_kobo"]), biller=p["biller"], balance=naira(r.get("balance_kobo", 0)))
        if op_id == "bank.statement":
            r = await be.recent_transactions(db, me, code, 5)
            if r["status"] == "success" and not r["transactions"]:
                return self.reply("bank_no_transactions", action="bank_no_transactions")
            if r["status"] == "success":
                await self.sms_statement(r["transactions"], bank)
            return self.answer(r, bank, "bank_statement_sent", number=len(r.get("transactions", [])))
        if op_id == "bank.transaction_status":
            r = await be.recent_transactions(db, me, code, 1)
            if r["status"] == "success" and not r["transactions"]:
                return self.reply("bank_no_transactions", action="bank_no_transactions")
            t = (r.get("transactions") or [{}])[0]
            return self.answer(r, bank, "bank_last_transaction", summary=self.describe_tx(t) if t else "", state=t.get("state", ""))
        if op_id == "bank.block_card":
            r = await be.block_card(db, me, code)
            if r["status"] == "no_card":
                return self.reply("bank_card_none", action="bank_card_none")
            return self.answer(r, bank, "bank_card_blocked", last4=spell_digits(r.get("last4", "")))
        if op_id == "bank.complaint":
            r = await be.log_complaint(db, me, code, p["details"])
            if r["status"] == "success":  # the bank's desk has it; the case also goes to them as a handoff with the details
                await open_handoff(db, self.svc, self.call, "complaint", f"Caller {self.call.from_number}. Complaint {r['reference']}: {p['details']}"[:500], provider=code)
            return self.answer(r, bank, "bank_complaint_logged", ref=r.get("reference", ""))
        if op_id == "bank.funding_instructions":
            r = await be.funding_details(db, me, code)
            if r["status"] == "success":
                await self.svc.sms.send(me.phone, f"{bank.name}: to fund your account, pay into account number {r['account_number']}.")
                from ..models import Notification  # local: only needed here
                db.add(Notification(channel="sms", template="funding", to_number=me.phone, customer_id=me.id,
                                    body="[account details: hidden]", status="sent"))
            return self.answer(r, bank, "bank_funding_sent")
        if op_id == "bank.product_info":
            r = await be.product_info(db, me, code)
            return self.answer(r, bank, "bank_product_info", text=r.get("text", ""))
        raise ValueError(f"no handler for {op_id}")

    def answer(self, r: dict, bank, ok_key: str, **facts) -> Outcome:
        """Turn the bank's structured answer into words. A `status` the bank gave decides which wording; nothing is invented."""
        status = r.get("status")
        if status == "success":
            return self.reply(ok_key, action=ok_key, bank=bank.name, **facts)
        if status == "insufficient_funds":
            return self.reply("bank_insufficient", action="bank_insufficient", balance=naira(r["balance_kobo"]), amount=facts.get("amount", ""))
        if status == "over_limit":
            return self.reply("bank_over_limit", action="bank_over_limit", limit=naira(r["limit_kobo"]), amount=facts.get("amount", ""))
        if status == "no_account":
            return self.reply("bank_no_account", action="bank_no_account", bank=bank.name)
        raise RuntimeError(f"bank answered {status!r}")

    def describe_tx(self, t: dict) -> str:
        what = f"{naira(t['amount_kobo'])} airtime" if t["kind"] == "airtime" else f"{naira(t['amount_kobo'])} to {t['who']}"
        return what

    async def sms_statement(self, txs: list[dict], bank) -> None:
        from ..models import Notification  # local: models are heavy and only needed here
        lines = "; ".join(f"{self.describe_tx(t)} ({t['state']})" for t in txs)
        await self.svc.sms.send(self.customer.phone, f"{bank.name} last {len(txs)} transactions: {lines}")
        self.db.add(Notification(channel="sms", template="statement", to_number=self.customer.phone, customer_id=self.customer.id,
                                 body=f"[statement: {len(txs)} transactions]", status="sent"))  # the figures are not kept here
