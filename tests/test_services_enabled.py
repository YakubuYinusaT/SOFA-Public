"""The Nigerian AI contest entry is shopping and search only: banking can be switched off, and then it is not reachable at all."""

import pytest
from fastapi.testclient import TestClient

from scripts.seed import seed
from scripts.simulate_call import run_call
from sofa.config import Settings
from sofa.gateway.hub import build_gateway, enabled_services
from sofa.main import create_app
from tests.conftest import SECRET
from tests.test_gateway import GATEWAY


@pytest.fixture
def naic_call(tmp_path):
    app = create_app(Settings(database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret=SECRET, public_base_url="http://test",
                              debug_headers=True, scheduler_enabled=False, gateway_number=GATEWAY, services_enabled="commerce,lookup",
                              verify_random_rate=0.0, _env_file=None))
    seed(app.state.svc.session_factory)
    with TestClient(app) as client:
        yield lambda lines, caller="+2348055550001": run_call(client, lines, caller, GATEWAY, "en", SECRET, echo=lambda *_: None)


def test_the_setting_decides_which_services_are_registered():
    assert enabled_services(Settings(_env_file=None)) == ["commerce", "banking", "lookup"]
    only = Settings(_env_file=None, services_enabled="commerce, lookup")
    assert enabled_services(only) == ["commerce", "lookup"]
    tools = build_gateway(only).tools
    assert tools.for_domain("banking") == [] and tools.get("bank.transfer") is None  # no bank tool exists to be named, however it is asked
    assert tools.for_domain("commerce") is not None
    assert build_gateway(only).adapter("banking") is None and build_gateway(only).adapter("commerce") is not None


def test_banking_is_politely_declined_when_it_is_switched_off(naic_call):
    replies = naic_call(["I want to check my bank balance"])
    assert "cannot help with your bank on this line" in replies[1]
    assert "shopping" in replies[1] and "search" in replies[1]


def test_shopping_still_works_when_banking_is_off(naic_call):
    replies = naic_call(["I want to buy something on CI Store"])
    assert "You are shopping at CI Store" in replies[1]
