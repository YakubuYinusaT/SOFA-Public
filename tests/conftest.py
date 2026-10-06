import pytest
from fastapi.testclient import TestClient

from scripts.seed import DEMO_NUMBER, OWNER_PHONE, seed
from scripts.simulate_call import run_call
from sofa.config import Settings
from sofa.main import create_app

import os

os.environ["SERVICES_ENABLED"] = "banking,commerce,lookup"  # the tests exercise every service; the shipped default is shop and search only

SECRET = "test-secret"


@pytest.fixture
def settings(tmp_path):
    return Settings(
        database_url="sqlite://", storage_dir=str(tmp_path), at_callback_secret=SECRET,
        public_base_url="http://test", debug_headers=True, scheduler_enabled=False, _env_file=None,
    )


@pytest.fixture
def app(settings):
    app = create_app(settings)
    seed(app.state.svc.session_factory)
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def call(client):
    """call(lines, caller=..., lang=...) -> list of Sofa replies (greeting first)."""

    def _call(lines, caller="+2348055550001", lang="en", dest=DEMO_NUMBER):
        return run_call(client, lines, caller, dest, lang, SECRET, echo=lambda *_: None)

    return _call


@pytest.fixture
def db(app):
    with app.state.svc.session_factory() as session:
        yield session


OWNER = OWNER_PHONE
