"""Any bank backend must offer every method SOFA asks for, with the same arguments. Today that is the mock; the bank's must pass this too."""

import inspect

import pytest

from sofa.config import Settings
from sofa.gateway.bankapi import BANK_METHODS, BankBackend, BankNotConfigured, DemoBank, build_bank
from sofa.gateway.mockbank import MockBank


def parameters(fn):
    return [p for p in inspect.signature(fn).parameters if p != "self"]


@pytest.mark.parametrize("name", BANK_METHODS)
def test_the_mock_bank_meets_the_contract(name):
    assert inspect.iscoroutinefunction(getattr(MockBank, name))
    assert parameters(getattr(MockBank, name)) == parameters(getattr(BankBackend, name))


def test_the_contract_lists_every_method_it_defines():
    defined = {n for n, v in vars(BankBackend).items() if inspect.iscoroutinefunction(v)}
    assert defined == set(BANK_METHODS)


def test_the_setting_picks_the_backend():
    assert isinstance(build_bank(Settings(_env_file=None)), MockBank)
    assert isinstance(build_bank(Settings(_env_file=None, bank_backend="demobank")), DemoBank)


def test_demobank_says_clearly_when_it_is_not_connected():
    import asyncio

    bank = DemoBank(Settings(_env_file=None))
    with pytest.raises(BankNotConfigured, match="not connected"):
        asyncio.run(bank.balance(None, None, "demobank"))
