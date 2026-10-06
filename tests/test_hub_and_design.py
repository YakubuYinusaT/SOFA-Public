"""The master page, and the Connected Intelligence design system as it is applied: tokens, contrast, landmarks, and every link on the master page."""

from markupsafe import escape
import re
from pathlib import Path

import pytest

from sofa.routes.hub import CONSOLES, TASKS
from sofa.web import auth

STATIC = Path(__file__).resolve().parents[1] / "sofa" / "web" / "static"
PASSWORDS = {"admin": ("/admin/login", "dev-admin-token"), "provider": ("/provider/login", "dev-provider-token"), "vendor": ("/vendor/login", "dev-vendor-token")}


@pytest.fixture(autouse=True)
def fresh_state():
    auth.clear_failures()


def luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    channels = [int(h[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def ratio(a: str, b: str) -> float:
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def tokens(theme: str) -> dict:
    """The colour variables from ci.css for a theme (the :root block, overridden by the light block)."""
    css = (STATIC / "ci.css").read_text(encoding="utf-8")
    dark = dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", css.split(':root[data-theme="light"]')[0]))
    if theme == "dark":
        return dark
    light_block = css.split(':root[data-theme="light"]')[1].split("}")[0]
    return {**dark, **dict(re.findall(r"--([a-z-]+):\s*(#[0-9a-fA-F]{6})", light_block))}


# ---- the tokens are the design system's, and they keep their measured contrast -----------------------------------------


@pytest.mark.parametrize("theme, fg, bg, floor", [
    ("dark", "text-primary", "surface-base", 13.0), ("light", "text-primary", "surface-base", 13.0),
    ("dark", "text-secondary", "surface-base", 8.8), ("light", "text-secondary", "surface-base", 7.2),
    ("dark", "text-muted", "surface-base", 4.7), ("light", "text-muted", "surface-base", 4.8),
    ("dark", "signal-raised", "surface-base", 5.1), ("light", "signal-raised", "surface-base", 6.2),
    ("dark", "text-on-fill", "signal-action", 4.8), ("light", "text-on-fill", "signal-action", 8.7),
    ("dark", "border-strong", "surface-base", 3.3), ("light", "border-strong", "surface-base", 3.9),
])
def test_the_colours_clear_the_contrast_the_design_system_measured(theme, fg, bg, floor):
    t = tokens(theme)
    assert ratio(t[fg], t[bg]) >= floor


def test_the_brand_values_are_exactly_the_design_systems():
    t = tokens("dark")
    assert (t["signal"], t["surface-base"], t["surface-void"], t["signal-action"], t["text-primary"]) == ("#f50000", "#151515", "#0b0b0b", "#e60000", "#dbdad7")
    assert tokens("light")["surface-base"] == "#dbdad7"


def test_the_red_that_fails_for_text_is_never_used_for_text():
    css = (STATIC / "ci.css").read_text(encoding="utf-8")
    assert not re.search(r"(?<![-a-z])color:\s*var\(--signal\)", css)  # signal may be a rule or a fill, never type: it measures 4.25:1


def test_the_stylesheet_fonts_and_mark_are_served(client):
    css = client.get("/static/ci.css")
    assert css.status_code == 200 and "Poppins" in css.text and "prefers-reduced-motion" in css.text
    for path in ("/static/fonts/poppins-300.woff2", "/static/fonts/poppins-400.woff2", "/static/fonts/poppins-600.woff2", "/static/ci-mark-red.png", "/static/ci.js"):
        assert client.get(path).status_code == 200, path
    total = sum(p.stat().st_size for p in (STATIC / "fonts").glob("*.woff2")) + (STATIC / "ci.css").stat().st_size
    assert total < 100_000  # well inside the 350 KB critical-path budget


# ---- every page follows the shell: skip link, landmarks, one h1, the stylesheet --------------------------------------


def login(client, who):
    path, password = PASSWORDS[who]
    assert client.post(path, data={"password": password}, follow_redirects=False).status_code == 303


@pytest.mark.parametrize("who, path", [("vendor", "/vendor"), ("vendor", "/vendor/orders"), ("vendor", "/vendor/stock"), ("vendor", "/vendor/advice"),
                                       ("vendor", "/vendor/questions"), ("provider", "/provider"), ("provider", "/provider/handoffs"),
                                       ("provider", "/provider/applications"), ("provider", "/provider/complaints"), ("admin", "/admin")])
def test_each_console_page_follows_the_shell(client, who, path):
    login(client, who)
    html = client.get(path).text
    assert 'href="/static/ci.css"' in html and 'class="skip-link"' in html and '<main id="main"' in html
    assert html.count("<h1") == 1
    assert 'aria-current="page"' in html  # the page you are on is marked in the navigation


def test_login_pages_follow_the_shell(client):
    for path in ("/admin/login", "/provider/login", "/vendor/login"):
        html = client.get(path).text
        assert 'class="skip-link"' in html and "<h1>Log in</h1>" in html and 'for="pw"' in html


# ---- the admin home (the master page) -----------------------------------------------------------------------------------


def test_the_admin_home_needs_the_admin_login(client):
    r = client.get("/admin/home", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/admin/login?next=")
    login(client, "vendor")  # a shop owner's login does not open it
    assert client.get("/admin/home", follow_redirects=False).status_code == 303


def test_the_admin_home_lists_every_console_and_task(client):
    login(client, "admin")
    r = client.get("/admin/home")
    html = r.text
    assert r.status_code == 200 and "Where do you want to go?" in html and 'id="finder-input"' in html
    for key, name, *_ in CONSOLES:
        assert name in html
    for title, path, *_ in TASKS:
        assert str(escape(title)) in html and f'href="{path}"' in html


def test_the_admin_home_shows_where_you_are_logged_in(client):
    login(client, "admin")
    html = client.get("/admin/home").text
    assert html.count("OPEN_AS_ADMIN") == 2 and "LOGIN_REQUIRED" not in html  # the admin login opens the shop and bank consoles too
    login(client, "vendor")
    html = client.get("/admin/home").text
    assert html.count("LOGGED_IN") == 2 and html.count("OPEN_AS_ADMIN") == 1 and "dev-vendor-token" in html  # logged in: the admin console and the shop  # demo passwords show while they are still the demo ones


def test_the_admin_home_hides_demo_passwords_once_real_ones_are_set(client, app):
    login(client, "admin")
    app.state.svc.settings.vendor_token = "a-long-random-vendor-password"
    html = client.get("/admin/home").text
    assert "a-long-random-vendor-password" not in html and "dev-vendor-token" not in html


def test_the_demo_console_says_whether_it_is_on(client, app):
    login(client, "admin")
    assert "DEMO_PAGE_ENABLED" in client.get("/admin/home").text and client.get("/demo").status_code == 404
    app.state.svc.settings.demo_page_enabled = True
    assert client.get("/demo").status_code == 200


def test_every_link_on_the_admin_home_works(client, app):
    """The point of the page: nothing on it is a dead end. Logged out, a console link goes to its login; logged in, it opens."""
    app.state.svc.settings.demo_page_enabled = True
    paths = sorted({link[1] for c in CONSOLES for link in c[7]} | {t[1] for t in TASKS})
    for path in paths:
        r = client.get(path, follow_redirects=False)
        assert r.status_code in (200, 303), f"{path} -> {r.status_code}"
        if r.status_code == 303:
            assert "/login?next=" in r.headers["location"], path
    for who in PASSWORDS:
        login(client, who)
    for path in paths:
        assert client.get(path).status_code == 200, f"{path} does not open when logged in"
