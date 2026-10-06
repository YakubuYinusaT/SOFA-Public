"""A bank's own system, modelled for development and demos.

In production none of this data lives with SOFA: the bank holds accounts, balances and recipients and is the only source of
truth. SOFA asks the backend a question and speaks the structured answer. This module is that backend with the same shape a
real one will have (every method returns a plain dict with a `status`), so replacing it changes one file.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

import hashlib

from ..models import (Customer, MockBankAccount, MockBankApplication, MockBankBeneficiary, MockBankComplaint, MockBankIdentity,
                      MockBankLinkCheck, MockBankTransaction)


def _ref() -> str:
    return "ZB" + uuid.uuid4().hex[:10].upper()


class MockBank:
    def __init__(self, settings):
        self.s = settings

    # ---- helpers ---------------------------------------------------------------------------

    def account(self, db: Session, customer_id, provider: str) -> MockBankAccount | None:
        return db.scalar(select(MockBankAccount).where(MockBankAccount.customer_id == customer_id, MockBankAccount.provider == provider))

    def _spend(self, db: Session, acct: MockBankAccount, kind: str, amount: int, who: str, limit_naira: int) -> dict:
        if amount <= 0:
            return {"status": "invalid_amount"}
        if amount > limit_naira * 100:
            return {"status": "over_limit", "limit_kobo": limit_naira * 100}
        if amount > acct.balance_kobo:
            return {"status": "insufficient_funds", "balance_kobo": acct.balance_kobo}
        acct.balance_kobo -= amount
        tx = MockBankTransaction(customer_id=acct.customer_id, provider=acct.provider, kind=kind, amount_kobo=amount, counterparty=who,
                                 reference=_ref(), status="successful", balance_after_kobo=acct.balance_kobo)
        db.add(tx)
        db.flush()
        return {"status": "success", "reference": tx.reference, "amount_kobo": amount, "balance_kobo": acct.balance_kobo}

    # ---- the bank's operations -------------------------------------------------------------

    async def balance(self, db: Session, customer, provider: str) -> dict:
        acct = self.account(db, customer.id, provider)
        return {"status": "success", "balance_kobo": acct.balance_kobo} if acct else {"status": "no_account"}

    async def beneficiaries(self, db: Session, customer, provider: str) -> list[dict]:
        rows = db.scalars(select(MockBankBeneficiary).where(MockBankBeneficiary.customer_id == customer.id,
                                                            MockBankBeneficiary.provider == provider).order_by(MockBankBeneficiary.name))
        return [{"id": str(b.id), "name": b.name, "nicknames": [n.strip() for n in b.nicknames.split(",") if n.strip()],
                 "bank_name": b.bank_name, "last4": b.account_number[-4:], "last_amount_kobo": b.last_amount_kobo}
                for b in sorted(rows, key=lambda b: (b.name, b.bank_name))]

    async def transfer(self, db: Session, customer, provider: str, beneficiary_id: str, amount_kobo: int) -> dict:
        acct = self.account(db, customer.id, provider)
        who = db.get(MockBankBeneficiary, uuid.UUID(beneficiary_id))
        if not acct or not who or who.customer_id != customer.id:
            return {"status": "no_account" if not acct else "unknown_beneficiary"}
        label = f"{who.name} at {who.bank_name}" if who.bank_name else who.name
        result = self._spend(db, acct, "transfer", amount_kobo, label, self.s.mock_bank_transfer_limit_naira)
        if result["status"] == "success":
            who.last_amount_kobo = amount_kobo
        return result

    async def buy_airtime(self, db: Session, customer, provider: str, amount_kobo: int) -> dict:
        acct = self.account(db, customer.id, provider)
        if not acct:
            return {"status": "no_account"}
        return self._spend(db, acct, "airtime", amount_kobo, "airtime", self.s.mock_bank_airtime_limit_naira)

    async def pay_bill(self, db: Session, customer, provider: str, biller: str, amount_kobo: int) -> dict:
        acct = self.account(db, customer.id, provider)
        if not acct:
            return {"status": "no_account"}
        return self._spend(db, acct, "bill", amount_kobo, biller, self.s.mock_bank_transfer_limit_naira)

    async def recent_transactions(self, db: Session, customer, provider: str, n: int = 5) -> dict:
        if not self.account(db, customer.id, provider):
            return {"status": "no_account"}
        rows = list(db.scalars(select(MockBankTransaction).where(MockBankTransaction.customer_id == customer.id,
                                                                 MockBankTransaction.provider == provider)
                               .order_by(MockBankTransaction.created_at.desc(), MockBankTransaction.id).limit(n)))
        return {"status": "success", "transactions": [{"kind": t.kind, "amount_kobo": t.amount_kobo, "who": t.counterparty,
                                                       "state": t.status, "reference": t.reference} for t in rows]}

    async def block_card(self, db: Session, customer, provider: str) -> dict:
        acct = self.account(db, customer.id, provider)
        if not acct:
            return {"status": "no_account"}
        if not acct.card_last4:
            return {"status": "no_card"}
        acct.card_blocked = True
        return {"status": "success", "last4": acct.card_last4}

    async def log_complaint(self, db: Session, customer, provider: str, details: str) -> dict:
        row = MockBankComplaint(customer_id=customer.id, provider=provider, details=details, reference="CMP" + uuid.uuid4().hex[:8].upper())
        db.add(row)
        db.flush()
        return {"status": "success", "reference": row.reference}

    # ---- connecting an account that already exists: BVN, date of birth and account number --------

    @staticmethod
    def _hash(bvn: str) -> str:
        return hashlib.sha256(("bvn|" + bvn).encode()).hexdigest()

    def _check(self, db: Session, reference: str) -> MockBankLinkCheck:
        return db.scalar(select(MockBankLinkCheck).where(MockBankLinkCheck.reference == reference))

    async def start_link_check(self, db: Session, customer, provider: str) -> dict:
        row = MockBankLinkCheck(customer_id=customer.id, provider=provider, reference="LNK" + uuid.uuid4().hex[:8].upper())
        db.add(row)
        db.flush()
        return {"status": "success", "reference": row.reference}

    async def link_field(self, db: Session, reference: str, name: str, value: str) -> dict:
        """One thing typed on the keypad goes to the bank and nowhere else: a BVN is stored hashed, never as typed."""
        row = self._check(db, reference)
        if name == "bvn":
            if not (value.isdigit() and len(value) == 11):
                return {"status": "invalid_bvn"}
            row.bvn_hash = self._hash(value)
        elif name == "account_number":
            if not (value.isdigit() and len(value) == 10):
                return {"status": "invalid_account_number"}
            row.account_number = value
        elif name == "dob":
            row.dob = value
        return {"status": "success"}

    async def link_verify(self, db: Session, reference: str) -> dict:
        """Does a customer of this bank have this account number, BVN and date of birth? On a match the account is made reachable for the caller."""
        row = self._check(db, reference)
        if not (row.dob and row.bvn_hash and row.account_number):
            return {"status": "incomplete"}
        known = db.scalar(select(MockBankIdentity).where(MockBankIdentity.provider == row.provider,
                                                         MockBankIdentity.account_number == row.account_number))
        if not known:
            row.status = "not_found"
            return {"status": "not_found"}
        if known.dob != row.dob or known.bvn_hash != row.bvn_hash:
            row.status = "mismatch"
            return {"status": "mismatch"}
        row.status = "verified"
        if not self.account(db, row.customer_id, row.provider):
            db.add(MockBankAccount(customer_id=row.customer_id, provider=row.provider, account_number=known.account_number,
                                   balance_kobo=known.balance_kobo, card_last4=known.card_last4))
        db.flush()
        return {"status": "verified", "full_name": known.full_name}

    # ---- opening an account: the bank's onboarding workflow ---------------------------------

    @staticmethod
    def _app(a: MockBankApplication | None) -> dict | None:
        return None if a is None else {"reference": a.reference, "status": a.status, "full_name": a.full_name, "dob": a.dob,
                                       "address": a.address, "bvn_given": a.bvn_given, "reason": a.reason}

    def _row(self, db: Session, reference: str) -> MockBankApplication:
        return db.scalar(select(MockBankApplication).where(MockBankApplication.reference == reference))

    async def latest_application(self, db: Session, customer, provider: str) -> dict | None:
        return self._app(db.scalar(select(MockBankApplication).where(MockBankApplication.customer_id == customer.id,
                                                                     MockBankApplication.provider == provider)
                                   .order_by(MockBankApplication.created_at.desc(), MockBankApplication.id)))

    async def start_application(self, db: Session, customer, provider: str) -> dict:
        row = MockBankApplication(customer_id=customer.id, provider=provider, reference="APP" + uuid.uuid4().hex[:8].upper())
        db.add(row)
        db.flush()
        return self._app(row)

    async def save_fields(self, db: Session, reference: str, **fields) -> dict:
        row = self._row(db, reference)
        for name in ("full_name", "dob", "address"):
            if name in fields:
                setattr(row, name, fields[name])
        return self._app(row)

    async def submit_identity(self, db: Session, reference: str, bvn: str) -> dict:
        """The BVN goes to the bank and nowhere else: it is checked here and dropped; only the fact that it was given is kept."""
        if not (bvn.isdigit() and len(bvn) == 11):
            return {"status": "invalid_bvn"}
        self._row(db, reference).bvn_given = True
        return {"status": "success"}

    async def submit_application(self, db: Session, reference: str) -> dict:
        row = self._row(db, reference)
        missing = [n for n, v in (("full_name", row.full_name), ("dob", row.dob), ("address", row.address), ("bvn", row.bvn_given)) if not v]
        if missing:
            return {"status": "incomplete", "missing": missing}
        row.status = "awaiting_identity_verification"
        return {"status": "success", "reference": reference, "next_action": "send_secure_verification_link",
                "link": f"https://bank.example/verify/{reference}"}

    async def resend_link(self, db: Session, reference: str) -> dict:
        row = self._row(db, reference)
        if row.status != "awaiting_identity_verification":
            return {"status": "not_waiting"}
        return {"status": "success", "link": f"https://bank.example/verify/{reference}"}

    async def account_requirements(self, db: Session, provider: str) -> dict:
        return {"status": "success", "text": "To open an account you need your BVN, a valid government ID such as your NIN slip, driver's licence, "
                "international passport or voter's card, a selfie, and your home address."}

    async def funding_details(self, db: Session, customer, provider: str) -> dict:
        acct = self.account(db, customer.id, provider)
        return {"status": "success", "account_number": acct.account_number} if acct else {"status": "no_account"}

    async def product_info(self, db: Session, customer, provider: str) -> dict:
        return {"status": "success", "text": "Fees and requirements depend on the product and are set by the bank."}


# ---- the bank's side of an application moving on (what the bank does after the secure-link check; used by tests and demos) --

def complete_identity_check(db: Session, reference: str) -> None:
    db.scalar(select(MockBankApplication).where(MockBankApplication.reference == reference)).status = "under_review"


def approve(db: Session, reference: str) -> MockBankApplication:
    """The bank approves: an account exists. (The bank then tells SOFA to activate the link: links.link(...).)"""
    row = db.scalar(select(MockBankApplication).where(MockBankApplication.reference == reference))
    row.status = "approved"
    customer = db.get(Customer, row.customer_id)
    if not MockBank(None).account(db, customer.id, row.provider):  # a customer who already banks here keeps the account they have
        open_account(db, customer, row.provider, 0)
    return row


def reject(db: Session, reference: str, reason: str) -> None:
    row = db.scalar(select(MockBankApplication).where(MockBankApplication.reference == reference))
    row.status, row.reason = "rejected", reason


def add_identity(db: Session, provider: str, account_number: str, full_name: str, dob: str, bvn: str, balance_naira: int = 0,
                 card_last4: str | None = "4321") -> MockBankIdentity:
    """A person who already banks here (the bank's side, for demos and tests)."""
    row = MockBankIdentity(provider=provider, account_number=account_number, full_name=full_name, dob=dob, bvn_hash=MockBank._hash(bvn),
                           balance_kobo=balance_naira * 100, card_last4=card_last4)
    db.add(row)
    db.flush()
    return row


# ---- demo data (the bank's side; used by scripts/seed.py and tests) -----------------------------------------------

def open_account(db: Session, customer, provider: str, balance_naira: int, card_last4: str | None = "4321") -> MockBankAccount:
    acct = MockBankAccount(customer_id=customer.id, provider=provider, account_number="0" + str(uuid.uuid4().int)[:9],
                           balance_kobo=balance_naira * 100, card_last4=card_last4)
    db.add(acct)
    db.flush()
    return acct


def add_beneficiary(db: Session, customer, provider: str, name: str, nicknames: str = "", last_amount_naira: int | None = None,
                    bank_name: str = "", account_number: str = "") -> MockBankBeneficiary:
    row = MockBankBeneficiary(customer_id=customer.id, provider=provider, name=name, nicknames=nicknames, bank_name=bank_name,
                              account_number=account_number, last_amount_kobo=last_amount_naira * 100 if last_amount_naira else None)
    db.add(row)
    db.flush()
    return row
