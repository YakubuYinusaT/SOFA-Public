"""See what happened to the text messages the server sent, and send one test message.

    cd /opt/sofa/src && sudo -u sofa /opt/sofa/venv/bin/python -m scripts.sms_check --recent
    cd /opt/sofa/src && sudo -u sofa /opt/sofa/venv/bin/python -m scripts.sms_check +2348012345678

--recent lists the last 25 texts the server sent: when, what kind, to which number (last four digits), and the status Africa's Talking gave
(sent, delivered, failed, rejected). A number sends one test message and prints why it was refused when it was.
Reads the same settings as the server (AT_USERNAME, AT_API_KEY, AT_SANDBOX, SMS_SENDER_ID from the .env) and prints no key.
"""

import asyncio
import sys

from sqlalchemy import select

from sofa.clients.sms import SMSClient
from sofa.config import get_settings
from sofa.db import make_engine, make_session_factory
from sofa.models import Notification


def settings_line(s) -> str:
    return f"user {s.at_username!r}, {'sandbox' if s.at_sandbox else 'live'}, key {'set' if s.at_api_key else 'NOT SET'}, sender id {s.sms_sender_id or '(none: the default is used)'}"


def recent() -> int:
    s = get_settings()
    print(settings_line(s))
    with make_session_factory(make_engine(s.database_url))() as db:
        rows = db.scalars(select(Notification).where(Notification.channel == "sms").order_by(Notification.created_at.desc()).limit(25)).all()
    if not rows:
        print("no texts recorded yet")
    for r in rows:
        print(f"{r.created_at:%d %b %H:%M}  {r.template:<18} to ...{r.to_number[-4:]}  {r.status:<9} {r.provider_message_id or '(no message id)'}")
    return 0


async def send(to: str) -> int:
    s = get_settings()
    print(settings_line(s))
    status, message_id, reason = await SMSClient(s).deliver(to, "Talk: this is a test message from the Talk server.")
    print(f"result: {status}; {reason}" + (f"; message id {message_id}" if message_id else ""))
    return 0 if status == "sent" else 1


if __name__ == "__main__":
    arg = sys.argv[1] if len(sys.argv) == 2 else ""
    if arg == "--recent":
        raise SystemExit(recent())
    if not arg.startswith("+"):
        raise SystemExit("usage: python -m scripts.sms_check --recent   or   python -m scripts.sms_check +234XXXXXXXXXX")
    raise SystemExit(asyncio.run(send(arg)))
