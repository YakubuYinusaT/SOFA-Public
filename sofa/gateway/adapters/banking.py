"""Banking: the operations SOFA will offer, with their risk and the resource each belongs to.

The tools are declared so the verification rules can be built and tested against them; the adapter that runs them (a mock
bank first, then the bank's sandbox) is the next step. Which bank the caller means is already settled by the gateway (see
manager.banking). A `resource` is a part of the account that is asked about separately: moving from one resource to another
asks for the PIN again. The bank's own API can refine these groups later.
"""

from ..tools import Tool
from . import ServiceAdapter

ONBOARDING_TOOLS = {"bank.account_requirements", "bank.open_account", "bank.resume_registration", "bank.application_status"}
ACCOUNT_INFO, TRANSFERS, PAYMENTS, CARD, REQUESTS = "account_info", "transfers", "payments", "card", "service_requests"


class BankingAdapter(ServiceAdapter):
    domain = "banking"

    def tools(self) -> list[Tool]:
        return [
            Tool("bank.product_info", "banking", "Fees, requirements, branch and agent information", "informational"),
            # Opening an account: the caller has no PIN yet, so these are secured by a code sent to the phone number, inside the journey
            Tool("bank.account_requirements", "banking", "What is needed to open an account (documents, BVN)", "informational"),
            Tool("bank.open_account", "banking", "Open a new bank account", "service"),
            Tool("bank.resume_registration", "banking", "Continue an account-opening application that was not finished", "service"),
            Tool("bank.application_status", "banking", "Where an account-opening application stands, or why identity verification failed", "account"),
            Tool("bank.funding_instructions", "banking", "How to fund the account: the account number, sent by SMS", "account", protected=True, resource=ACCOUNT_INFO),
            Tool("bank.balance", "banking", "Account balance", "account", protected=True, resource=ACCOUNT_INFO),
            Tool("bank.statement", "banking", "Send an account statement", "account", protected=True, resource=ACCOUNT_INFO),
            Tool("bank.transaction_status", "banking", "Status of a payment or transfer", "account", protected=True, resource=ACCOUNT_INFO),
            Tool("bank.complaint", "banking", "Log a complaint or service request", "service", protected=True, required=("details",), resource=REQUESTS),
            Tool("bank.buy_airtime", "banking", "Buy airtime", "sensitive", protected=True, required=("amount",), confirm=True, resource=PAYMENTS),
            Tool("bank.pay_bill", "banking", "Pay a bill", "sensitive", protected=True, required=("biller", "amount"), confirm=True, resource=PAYMENTS),
            Tool("bank.transfer", "banking", "Send money to a beneficiary", "sensitive", protected=True,
                 required=("beneficiary", "amount"), confirm=True, resource=TRANSFERS),
            Tool("bank.block_card", "banking", "Block a card", "sensitive", protected=True, confirm=True, resource=CARD),
        ]


