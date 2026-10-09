"""The overview pages: they open for anyone, say the right things, and show only what the deployment has set."""

from fastapi.testclient import TestClient

from sofa.config import Settings
from sofa.main import create_app


def make(**kw):
    return TestClient(create_app(Settings(_env_file=None, services_enabled="commerce,lookup", database_url="sqlite://", **kw)))


def test_the_pages_open_without_a_login_and_hold_no_passwords():
    with make(demo_page_enabled=True, admin_token="a-secret-admin", vendor_token="a-secret-vendor") as c:
        for path in ("/overview", "/overview/technology", "/overview/use-cases", "/overview/findings"):
            r = c.get(path)
            assert r.status_code == 200, path
            assert "a-secret-admin" not in r.text and "a-secret-vendor" not in r.text
        assert "Talk" in c.get("/overview").text
        assert "Demo Bank" not in c.get("/overview").text


def test_the_front_door_of_the_public_build_goes_to_the_overview():
    with make() as c:
        r = c.get("/", follow_redirects=False)
        assert r.status_code == 307 and r.headers["location"] == "/overview"


def test_what_the_deployment_sets_is_shown_and_what_it_leaves_out_is_not():
    with make() as c:
        page = c.get("/overview").text
        assert "mailto:" not in page and "Watch the story" not in page and "Pitch deck" not in page
    with make(hub_contact_email="hello@example.com", hub_test_number="+2348000000000", hub_story_video_url="https://youtu.be/abc",
              hub_deck_url="https://example.com/deck.pdf", hub_cost_note="A live test costs us about 0.49 an hour.") as c:
        page = c.get("/overview").text
        assert "hello@example.com" in page and "+2348000000000" in page and "Watch the story" in page
        assert "https://example.com/deck.pdf" in page and "costs us about" in page


def test_a_video_file_plays_in_the_page_and_a_link_does_not():
    with make(hub_story_video_url="https://example.com/story.mp4") as c:
        assert "<video" in c.get("/overview").text
    with make(hub_story_video_url="https://youtu.be/abc") as c:
        assert "<video" not in c.get("/overview").text


def test_the_model_log_is_shown_as_tables():
    with make() as c:
        page = c.get("/overview/findings").text
        assert "<table" in page and "Language model" in page


def test_the_pages_can_be_switched_off():
    with make(overview_enabled=False, demo_page_enabled=False) as c:
        assert c.get("/overview").status_code == 404
