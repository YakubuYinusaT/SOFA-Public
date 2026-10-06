"""Logins for the web consoles. Each console is a `Portal` with its own password, cookie and path, so a login to one never opens another:

  admin     /admin     the Connected Intelligence team's console (ADMIN_TOKEN)
  provider  /provider  a bank's service desk: the handoffs, applications and link problems SOFA passes to it (PROVIDER_TOKEN)
  vendor   /vendor     a shop owner's portal: stock, prices, orders and customer questions (VENDOR_TOKEN)

A shared password gives a signed, expiring, HttpOnly, SameSite=Strict cookie that carries a per-session CSRF token which every POST form
must echo.

Each console is protected by one password; per-user accounts with roles are the next step for it.
"""

import hmac
import secrets
import time
from collections import defaultdict

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

MAX_AGE = 8 * 3600
MAX_FAILURES, FAILURE_WINDOW = 10, 15 * 60
_failures: dict[str, list[float]] = defaultdict(list)


class LoginRequired(Exception):
    def __init__(self, next_url: str, login_url: str = "/admin/login"):
        self.next_url, self.login_url = next_url, login_url


class Portal:
    def __init__(self, scope: str, token_setting: str, admin_opens: bool = False):
        self.scope, self.token_setting = scope, token_setting
        self.admin_opens = admin_opens  # the team's admin login also opens this console, so the admin never types a second password
        # The cookie is sent site-wide so the master page can show which consoles you are logged into; each console has its own cookie name and its own signing key,
        # so a login to one never opens another.
        self.cookie, self.path = f"sofa_{scope}", "/"
        self.login_url = f"/{scope}/login"

    def token(self, request: Request) -> str:
        return getattr(request.app.state.svc.settings, self.token_setting)

    def _serializer(self, request: Request) -> URLSafeTimedSerializer:
        return URLSafeTimedSerializer(self.token(request) + f"|{self.scope}-web", salt=f"sofa-{self.scope}-session")

    def check_password(self, request: Request, password: str) -> bool:
        return hmac.compare_digest(password.encode(), self.token(request).encode())

    def too_many_failures(self, request: Request) -> bool:
        key = f"{self.scope}|{request.client.host if request.client else '?'}"
        now = time.time()
        _failures[key] = [t for t in _failures[key] if now - t < FAILURE_WINDOW]
        return len(_failures[key]) >= MAX_FAILURES

    def record_failure(self, request: Request) -> None:
        _failures[f"{self.scope}|{request.client.host if request.client else '?'}"].append(time.time())

    def new_session_cookie(self, request: Request) -> str:
        return self._serializer(request).dumps({"csrf": secrets.token_urlsafe(24)})

    def set_cookie(self, request: Request, response, value: str) -> None:
        secure = request.app.state.svc.settings.public_base_url.startswith("https://")
        response.set_cookie(self.cookie, value, max_age=MAX_AGE, httponly=True, samesite="strict", secure=secure, path=self.path)

    def _own(self, request: Request) -> dict | None:
        raw = request.cookies.get(self.cookie)
        try:
            return self._serializer(request).loads(raw, max_age=MAX_AGE) if raw else None
        except (BadSignature, SignatureExpired):
            return None

    def access(self, request: Request) -> tuple[str, dict] | None:
        """How the caller may enter this console: ("own", session) with its own password, ("admin", session) as the admin, or None."""
        own = self._own(request)
        if own:
            return "own", own
        if self.admin_opens:
            via = admin._own(request)
            if via:
                return "admin", via  # the admin session's csrf token is the one the pages render and the posts check
        return None

    def session(self, request: Request) -> dict:
        """The logged-in session (with its csrf token) or a redirect to the login page."""
        found = self.access(request)
        if not found:
            raise LoginRequired(request.url.path + (f"?{request.url.query}" if request.url.query else ""), self.login_url)
        return found[1]

    def logged_in(self, request: Request) -> bool:
        return self.access(request) is not None

    async def verified_post(self, request: Request) -> dict:
        """Session + matching CSRF token for a state-changing form post."""
        sess = self.session(request)
        form = await request.form()
        if not hmac.compare_digest(str(form.get("csrf", "")), sess["csrf"]):
            raise HTTPException(status_code=403, detail="CSRF check failed: reload the page and try again")
        return sess


admin = Portal("admin", "admin_token")
provider = Portal("provider", "provider_token", admin_opens=True)
vendor = Portal("vendor", "vendor_token", admin_opens=True)

# The admin console's original names (routes and tests use these).
COOKIE = admin.cookie
check_password, too_many_failures, record_failure = admin.check_password, admin.too_many_failures, admin.record_failure
new_session_cookie, set_cookie, session, verified_post = admin.new_session_cookie, admin.set_cookie, admin.session, admin.verified_post


def clear_failures() -> None:
    _failures.clear()
