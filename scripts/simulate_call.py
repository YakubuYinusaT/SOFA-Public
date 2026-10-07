"""Simulate a phone call end to end with no telephony and no GPU (mock ASR / LLM / TTS).

    python -m scripts.simulate_call                       # interactive: type what the caller says
    python -m scripts.simulate_call --script calls/order.txt
    python -m scripts.simulate_call --caller +2348033333333   # call as the owner (stock updates)
    python -m scripts.simulate_call --lang yo             # caller speaks 'yo' (mock language detection)

A line can name its own language with a prefix: "[ha] Ina son madara" (the mock speech models then treat
it as Hausa). Handy for testing what Sofa does with a language she is not set up for.

Uses the same /voice/* endpoints Africa's Talking calls, so it exercises the real callback code.
Replies are read from the X-Sofa-Reply-Text debug header (DEBUG_HEADERS=true).
"""

import argparse
import re
import sys
import uuid

from fastapi.testclient import TestClient

from scripts.seed import DEMO_NUMBER, seed
from sofa.audio import mock_recording_url
from sofa.config import get_settings
from sofa.main import create_app


def run_call(client: TestClient, lines: list[str] | None, caller: str, dest: str, lang: str, secret: str, echo=print) -> list[str]:
    session_id = f"sim-{uuid.uuid4().hex[:8]}"
    replies: list[str] = []
    keypad = False  # the last reply asked for keypad digits (a PIN or code) instead of speech

    def show(resp):
        nonlocal keypad
        text = resp.headers.get("X-Sofa-Reply-Text", "")
        replies.append(text)
        echo(f"  Sofa: {text}")
        keypad = "<GetDigits" in resp.text
        return "<Record" in resp.text or keypad

    r = client.post(f"/voice/inbound/{secret}", data={"sessionId": session_id, "callerNumber": caller,
                                                       "destinationNumber": dest, "isActive": "1", "direction": "Inbound"})
    active = show(r)
    lines_iter = iter(lines) if lines is not None else None
    while active:
        if lines_iter is not None:
            line = next(lines_iter, None)
        else:
            try:
                line = input("  You:  ")
            except EOFError:
                line = None
        if line is None:
            break
        if keypad:  # type digits on the keypad; an empty line lets the timeout pass. Shown masked, like the real thing.
            echo("  You:  " + ("*" * len(line.strip()) if line.strip() else "(nothing typed)"))
            data = {"sessionId": session_id, "callerNumber": caller, "destinationNumber": dest, "isActive": "1"}
            if line.strip():
                data["dtmfDigits"] = line.strip()
            r = client.post(f"/voice/digits/{secret}", data=data)
            active = show(r)
            continue
        echo(f"  You:  {line}")
        tagged = re.match(r"^\[(en|yo|ha|ig)\]\s*(.*)$", line)
        spoken_lang, spoken = (tagged.group(1), tagged.group(2)) if tagged else (lang, line)
        data = {"sessionId": session_id, "callerNumber": caller, "destinationNumber": dest, "isActive": "1"}
        if spoken.strip():  # an empty line simulates silence (no recording)
            data["recordingUrl"] = mock_recording_url(spoken, spoken_lang)
        r = client.post(f"/voice/turn/{secret}", data=data)
        hops = 0
        while "<Redirect>" in r.text and "<GetDigits" not in r.text and hops < 12:  # slow turn: holding messages, then the real reply (after a keypad prompt a Redirect is only the timeout fallback)
            held = r.headers.get("X-Sofa-Reply-Text", "")
            if held:
                echo(f"  Sofa (holding): {held}")
            path = r.text.split("<Redirect>")[1].split("</Redirect>")[0].replace(get_settings().public_base_url, "")
            r = client.post(path, data={"sessionId": session_id})
            hops += 1
        active = show(r)
    client.post(f"/voice/events/{secret}", data={"sessionId": session_id, "isActive": "0", "durationInSeconds": "60"})
    return replies


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--script")
    ap.add_argument("--caller", default="+2348000000001")
    ap.add_argument("--dest", default=DEMO_NUMBER)
    ap.add_argument("--lang", default="en")
    args = ap.parse_args()
    settings = get_settings()
    app = create_app(settings)
    seed(app.state.svc.session_factory)
    lines = None
    if args.script:
        lines = [l.rstrip("\n") for l in open(args.script, encoding="utf-8") if not l.startswith("#")]
    elif not sys.stdin.isatty():
        lines = [l.rstrip("\n") for l in sys.stdin]
    with TestClient(app) as client:
        run_call(client, lines, args.caller, args.dest, args.lang, settings.at_callback_secret)


if __name__ == "__main__":
    main()
