"""The overview pages: they open for anyone, say the right things, and show only what the deployment has set."""

import re

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


def test_a_video_file_plays_a_youtube_link_is_embedded_and_any_other_link_is_a_button():
    with make(hub_story_video_url="https://example.com/story.mp4") as c:
        assert "<video" in c.get("/overview").text
    with make(hub_story_video_url="https://youtu.be/abcdefghijk", hub_demo_video_url="https://www.youtube.com/watch?v=lmnopqrstuv") as c:
        page = c.get("/overview").text
        assert "youtube-nocookie.com/embed/abcdefghijk" in page and "youtube-nocookie.com/embed/lmnopqrstuv" in page and "<video" not in page
    with make(hub_demo_video_url="https://drive.google.com/file/d/x/view") as c:
        page = c.get("/overview").text
        assert "<iframe" not in page and "Watch the demo" in page


def test_the_audio_explainer_has_a_player_only_when_it_is_set():
    with make() as c:
        assert "<audio" not in c.get("/overview").text
    with make(hub_audio_url="/overview/files/explainer.mp3") as c:
        assert '<audio controls' in c.get("/overview").text


def test_files_put_in_the_overview_folder_are_served_and_nothing_else_is(tmp_path):
    (tmp_path / "overview").mkdir()
    (tmp_path / "overview" / "deck.pdf").write_bytes(b"%PDF-1.4 test")
    (tmp_path / "overview" / "notes.txt").write_text("not allowed")
    (tmp_path / "secret.pdf").write_bytes(b"%PDF outside the folder")
    with make(storage_dir=str(tmp_path)) as c:
        ok = c.get("/overview/files/deck.pdf")
        assert ok.status_code == 200 and ok.headers["content-type"] == "application/pdf" and ok.content.startswith(b"%PDF")
        assert c.get("/overview/files/notes.txt").status_code == 404      # not a type the page serves
        assert c.get("/overview/files/missing.pdf").status_code == 404
        assert c.get("/overview/files/..%2Fsecret.pdf").status_code == 404  # no way out of the folder


def test_the_model_log_is_shown_as_tables():
    with make() as c:
        page = c.get("/overview/findings").text
        assert "<table" in page and "Language model" in page


def test_the_pages_can_be_switched_off():
    with make(overview_enabled=False, demo_page_enabled=False) as c:
        assert c.get("/overview").status_code == 404


def test_a_plain_visit_to_a_callback_address_answers_200_only_with_the_right_secret():
    """Africa's Talking's dashboard checks the address before saving it; a visit that got 405 made it say 'A valid callback URL is needed'."""
    with make(at_callback_secret="right-secret") as c:
        for path in ("/voice/inbound/right-secret", "/voice/events/right-secret"):
            assert c.get(path).status_code == 200 and c.get(path).text == "ok"
            assert c.head(path).status_code == 200
        assert c.get("/voice/inbound/wrong").status_code == 404
        assert c.post("/voice/inbound/right-secret").status_code == 200  # the real callback still works


def test_the_phone_test_section_shows_the_number_what_to_say_and_what_happens_next():
    with make(hub_test_number="+234 708 062 9820", hub_contact_email="hello@example.com", demo_page_enabled=True) as c:
        page = c.get("/overview").text
        assert "+234 708 062 9820" in page and 'href="tel:+2347080629820"' in page
        for line in ("What to say", "What do you have in the store?", "I want two bags of NPK", "What happens next", "account number", "live demo page"):
            assert line in page, line
    with make() as c:
        page = c.get("/overview").text
        assert "tel:" not in page and "What to say" in page and "We send you the number" in page



def put_deck(tmp_path, count=3):
    folder = tmp_path / "overview" / "deck"
    folder.mkdir(parents=True)
    for k in range(1, count + 1):
        (folder / f"{k:02d}.jpg").write_bytes(b"\xff\xd8\xff fake jpeg " + bytes([k]))
    (folder / "titles.txt").write_text("The first slide\nThe second slide\nThe third slide\n", encoding="utf-8")
    (tmp_path / "overview" / "talk.pdf").write_bytes(b"%PDF-1.4 the whole deck")  # sits in the overview folder, but the viewer never links it
    return folder


def test_the_pitch_deck_is_a_viewer_of_pictures_with_nothing_to_download(tmp_path):
    put_deck(tmp_path)
    with make(storage_dir=str(tmp_path)) as c:
        page = c.get("/overview/deck")
        assert page.status_code == 200 and "noindex" in page.text and page.headers["x-robots-tag"].startswith("noindex")
        assert "/overview/deck/01.jpg" in page.text and "/overview/deck/03.jpg" in page.text and "The second slide" in page.text
        assert ".pdf" not in page.text and ".pptx" not in page.text and "download=" not in page.text and "<img" not in page.text.split("<main")[-1]
        home = c.get("/overview").text
        assert 'href="/overview/deck"' in home and "View the deck" in home and "3 slides" in home


def test_a_slide_picture_is_sent_only_as_part_of_the_page_and_is_not_kept(tmp_path):
    put_deck(tmp_path)
    with make(storage_dir=str(tmp_path)) as c:
        ok = c.get("/overview/deck/01.jpg", headers={"sec-fetch-dest": "image"})
        assert ok.status_code == 200 and ok.headers["content-type"] == "image/jpeg"
        assert "no-store" in ok.headers["cache-control"] and ok.headers["content-disposition"] == "inline" and "noindex" in ok.headers["x-robots-tag"]
        assert c.get("/overview/deck/01.jpg", headers={"sec-fetch-dest": "document"}).status_code == 404  # opened on its own in a tab
        for bad in ("titles.txt", "..%2F..%2Fsecret.jpg", "1.png", "abcd.jpg", "9999.jpg", "07.jpg"):
            assert c.get(f"/overview/deck/{bad}", headers={"sec-fetch-dest": "image"}).status_code == 404, bad


def test_without_slide_pictures_there_is_no_deck_page_and_no_link_to_one(tmp_path):
    with make(storage_dir=str(tmp_path)) as c:
        assert c.get("/overview/deck").status_code == 404
        assert "/overview/deck" not in c.get("/overview").text
    with make(storage_dir=str(tmp_path), hub_deck_url="https://example.com/deck.pdf") as c:
        assert "https://example.com/deck.pdf" in c.get("/overview").text  # the older way, a link someone gives, still works


def menu_of(page: str) -> str:
    found = re.search(r'<nav class="nav" id="site-nav".*?</nav>', page, re.S)
    assert found, "the page has no menu across the top"
    return found.group(0)


def test_every_public_page_has_the_site_menu_and_marks_where_you_are():
    with make(demo_page_enabled=True) as c:
        for path, here in (("/overview", "Overview"), ("/overview/technology", "Technology"), ("/overview/use-cases", "Use cases"),
                           ("/overview/findings", "What we found"), ("/demo", "Live demo"), ("/vendor/login", "Shop owner portal")):
            page = c.get(path).text
            menu = menu_of(page)
            for label in ("Overview", "Technology", "Use cases", "What we found", "Live demo", "Shop owner portal"):
                assert f">{label}</a>" in menu, (path, label)
            assert re.search(r'aria-current="page">\s*' + re.escape(here) + "<", menu), (path, "the page you are on is marked")
            assert menu.count('aria-current="page"') == 1, path
            assert "data-menu-toggle" in page and 'aria-controls="site-nav"' in page      # the button that opens it on a phone
            assert "/provider" not in menu and "/admin" not in menu and "/partner" not in menu


def test_the_menu_lists_the_pitch_deck_only_when_there_is_one(tmp_path):
    with make(storage_dir=str(tmp_path)) as c:
        assert "Pitch deck" not in menu_of(c.get("/overview").text)
    put_deck(tmp_path)
    with make(storage_dir=str(tmp_path)) as c:
        assert 'href="/overview/deck"' in menu_of(c.get("/overview").text)
        assert re.search(r'aria-current="page">\s*Pitch deck<', menu_of(c.get("/overview/deck").text))
    with make(storage_dir=str(tmp_path / "none"), hub_deck_url="https://example.com/deck") as c:
        assert 'href="https://example.com/deck" rel="noopener"' in menu_of(c.get("/overview").text)


def test_the_stylesheet_address_changes_when_the_file_does_and_the_header_stays_in_view():
    with make() as c:
        page = c.get("/overview").text
        assert re.search(r"/static/ci\.css\?v=\d+", page) and re.search(r"/static/ci\.js\?v=\d+", page)
        assert 'class="site-header sticky"' in page
        assert c.get("/static/ci.css").status_code == 200


def test_the_demo_page_updates_without_jumping_back_to_the_top():
    with make(demo_page_enabled=True) as c:
        page = c.get("/demo").text
        assert '<noscript><meta http-equiv="refresh"' in page and '<meta http-equiv="refresh"' not in page.replace('<noscript><meta http-equiv="refresh"', "")
        assert "fetch(location.href" in page and "window.scrollTo(0, y)" in page


def test_a_table_cell_with_code_in_it_stays_one_block_so_it_wraps_on_a_phone():
    with make() as c:
        assert '<span class="cell">' in c.get("/overview/findings").text


def test_a_link_to_a_section_lands_below_the_sticky_header():
    with make() as c:
        css = c.get("/static/ci.css").text
        assert "scroll-padding-top" in css and ".site-header.sticky" in css
