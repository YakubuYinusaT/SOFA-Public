"""The tool registry: what SOFA may do, and what must hold before it does it."""

import pytest

from sofa.gateway.adapters.commerce import INTENT_TOOL
from sofa.gateway.hub import build_gateway
from sofa.gateway.tools import Tool, ToolRegistry


@pytest.fixture
def tools():
    return build_gateway().tools


def test_an_unregistered_operation_never_runs(tools):
    d = tools.authorize("bank.empty_the_account")
    assert not d.allowed and d.reason == "unregistered"


def test_a_protected_tool_waits_for_verification(tools):
    assert tools.authorize("bank.balance").reason == "needs_verification"
    assert tools.authorize("bank.balance", {"verified": False}).reason == "needs_verification"
    assert tools.authorize("bank.balance", {"verified": True}).allowed


def test_an_open_tool_needs_no_verification(tools):
    assert tools.authorize("commerce.ask_price").allowed
    assert tools.authorize("bank.product_info").allowed


def test_missing_parameters_are_named(tools):
    d = tools.authorize("bank.transfer", {"verified": True}, {"amount": 5000})
    assert not d.allowed and d.reason == "missing:beneficiary"
    assert tools.authorize("bank.transfer", {"verified": True}, {"amount": 5000, "beneficiary": "Mama"}).allowed


def test_every_commerce_intent_has_a_registered_tool(tools):
    for tool_id in INTENT_TOOL.values():
        assert tools.get(tool_id), tool_id


def test_money_moving_banking_tools_are_protected_and_confirmed(tools):
    for tool_id in ("bank.transfer", "bank.buy_airtime", "bank.pay_bill", "bank.block_card"):
        tool = tools.get(tool_id)
        assert tool.protected and tool.confirm and tool.risk == "sensitive", tool_id


def test_account_information_is_protected_but_product_information_is_not(tools):
    for tool_id in ("bank.balance", "bank.statement", "bank.transaction_status", "bank.funding_instructions"):
        assert tools.get(tool_id).protected, tool_id
    assert not tools.get("bank.product_info").protected


def test_opening_an_account_needs_no_pin_because_the_caller_has_none_yet(tools):
    for tool_id in ("bank.open_account", "bank.resume_registration", "bank.application_status", "bank.account_requirements"):
        assert tools.get(tool_id) and not tools.get(tool_id).protected, tool_id
    assert tools.authorize("bank.open_account").allowed


def test_a_tool_cannot_be_registered_twice_or_malformed():
    registry = ToolRegistry()
    registry.register(Tool("x.one", "x", "one"))
    with pytest.raises(ValueError):
        registry.register(Tool("x.one", "x", "again"))
    with pytest.raises(ValueError):
        Tool("nodot", "x", "no operation")
    with pytest.raises(ValueError):
        Tool("x.three", "x", "bad risk", risk="spicy")
