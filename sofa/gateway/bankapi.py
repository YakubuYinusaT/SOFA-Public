"""What SOFA needs from a bank: the contract any bank backend has to meet.

SOFA never keeps accounts, balances or recipients, and it never decides anything about money. It asks the bank a question and speaks
the structured answer. Every method here returns a plain dict with a `status`, so the conversation code is the same whichever bank is
behind it. The built-in backend is `MockBank` (gateway/mockbank.py). To connect another bank, write a class that meets this contract and set BANK_BACKEND. No conversation code changes.

The full list of methods is `BANK_METHODS` below. `tests/test_bank_contract.py` checks that a backend
offers every method with the same arguments.
"""

from typing import Protocol

# Every method a bank backend must offer, in the order the docs list them.
BANK_METHODS = (
    "balance", "beneficiaries", "transfer", "buy_airtime", "pay_bill", "recent_transactions", "block_card", "log_complaint",
    "start_link_check", "link_field", "link_verify",
    "latest_application", "start_application", "save_fields", "submit_identity", "submit_application", "resend_link",
    "account_requirements", "funding_details", "product_info",
)

# The statuses a method may answer with (the conversation code handles exactly these).
STATUSES = ("success", "verified", "no_account", "no_card", "unknown_beneficiary", "invalid_amount", "over_limit", "insufficient_funds",
            "invalid_bvn", "invalid_account_number", "mismatch", "not_found", "incomplete", "not_waiting")


class BankBackend(Protocol):
    """The questions SOFA asks a bank. `db` is SOFA's own session (a real bank ignores it); `customer` is the SOFA subscriber."""

    async def balance(self, db, customer, provider: str) -> dict: ...
    async def beneficiaries(self, db, customer, provider: str) -> list[dict]: ...
    async def transfer(self, db, customer, provider: str, beneficiary_id: str, amount_kobo: int) -> dict: ...
    async def buy_airtime(self, db, customer, provider: str, amount_kobo: int) -> dict: ...
    async def pay_bill(self, db, customer, provider: str, biller: str, amount_kobo: int) -> dict: ...
    async def recent_transactions(self, db, customer, provider: str, n: int = 5) -> dict: ...
    async def block_card(self, db, customer, provider: str) -> dict: ...
    async def log_complaint(self, db, customer, provider: str, details: str) -> dict: ...
    async def start_link_check(self, db, customer, provider: str) -> dict: ...
    async def link_field(self, db, reference: str, name: str, value: str) -> dict: ...
    async def link_verify(self, db, reference: str) -> dict: ...
    async def latest_application(self, db, customer, provider: str) -> dict | None: ...
    async def start_application(self, db, customer, provider: str) -> dict: ...
    async def save_fields(self, db, reference: str, **fields) -> dict: ...
    async def submit_identity(self, db, reference: str, bvn: str) -> dict: ...
    async def submit_application(self, db, reference: str) -> dict: ...
    async def resend_link(self, db, reference: str) -> dict: ...
    async def account_requirements(self, db, provider: str) -> dict: ...
    async def funding_details(self, db, customer, provider: str) -> dict: ...
    async def product_info(self, db, customer, provider: str) -> dict: ...


class BankNotConfigured(RuntimeError):
    """BANK_BACKEND names a bank that has not been connected yet."""


class DemoBank:
    """A bank's own API behind the contract: implement each method in BANK_METHODS by calling the bank's endpoint and mapping its answer to the dict shape of the matching MockBank method."""

    def __init__(self, settings):
        self.s = settings

    def __getattr__(self, name):
        if name in BANK_METHODS:
            async def not_connected(*args, **kwargs):
                raise BankNotConfigured(f"BANK_BACKEND=demobank but DemoBank.{name} is not connected: see sofa/gateway/bankapi.py")
            return not_connected
        raise AttributeError(name)


def build_bank(settings):
    """The bank backend the settings ask for."""
    from .mockbank import MockBank

    if settings.bank_backend == "demobank":
        return DemoBank(settings)
    return MockBank(settings)
