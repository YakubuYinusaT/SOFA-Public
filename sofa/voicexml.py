"""Africa's Talking Voice XML builders (Say, Play, Record, GetDigits, Redirect)."""

from xml.sax.saxutils import escape, quoteattr


def _wrap(body: str) -> str:
    return f'<?xml version="1.0" encoding="UTF-8"?><Response>{body}</Response>'


def _plays(urls: list[str]) -> str:
    return "".join(f"<Play url={quoteattr(u)}/>" for u in urls)


def record(prompts: list[str], callback_url: str, timeout: int = 2, max_length: int = 15) -> str:
    """One turn: prompt audio nested in Record. No beep, trim silence, # ends early."""
    return _wrap(
        f'<Record finishOnKey="#" maxLength="{max_length}" timeout="{timeout}" trimSilence="true" '
        f'playBeep="false" callbackUrl={quoteattr(callback_url)}>{_plays(prompts)}</Record>'
    )


def get_digits(prompts: list[str], callback_url: str, num_digits: int, timeout: int = 20, fallback_url: str | None = None) -> str:
    """Collect keypad digits (a PIN or a one-time code). Nothing is recorded: the caller's voice is not captured here, and the
    digits come back as a form field. If the timeout passes with nothing typed the call carries on to the fallback."""
    body = (f'<GetDigits timeout="{timeout}" finishOnKey="#" numDigits="{num_digits}" callbackUrl={quoteattr(callback_url)}>'
            f"{_plays(prompts)}</GetDigits>")
    return _wrap(body + (f"<Redirect>{escape(fallback_url)}</Redirect>" if fallback_url else ""))


def play_and_hangup(prompts: list[str]) -> str:
    """Play then end the call (no Record follows)."""
    return _wrap(_plays(prompts))


def say_and_hangup(text: str) -> str:
    return _wrap(f"<Say>{escape(text)}</Say>")


def redirect(url: str, prompts: list[str] | None = None) -> str:
    return _wrap(f"{_plays(prompts or [])}<Redirect>{escape(url)}</Redirect>")
