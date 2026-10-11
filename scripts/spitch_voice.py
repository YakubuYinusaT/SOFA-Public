"""Add your own voice to Spitch, so Talk can speak in it.

    python -m scripts.spitch_voice --audio my_voice.wav --transcript "Exactly what I say in the recording." --name "Yakubu" --consent

The recording: one speaker, clear speech, 2 to 30 seconds, up to 10 MB, and the transcript must match it word for word. Spitch returns a voice id
(it starts with "voice_"). Put it in the settings as SPITCH_VOICE_EN=<that id>, restart, and Talk speaks in that voice. Check it first with
`python scripts/voice_check.py --hosted`.

--consent says you are the speaker, or have the speaker's permission. Spitch requires it, and the voice is private to your Spitch account.
Your Spitch key is read from SPITCH_API_KEY in .env. It is never printed.
"""

import argparse
import sys
from pathlib import Path

import httpx

from sofa.clients.http import tls_context
from sofa.config import get_settings

URL = "https://api.spitch.app/v1/voices"
MAX_BYTES = 10 * 1024 * 1024


def create_voice(http: httpx.Client, audio: Path, transcript: str, name: str, language: str = "") -> str:
    if not audio.is_file():
        raise SystemExit(f"cannot find the recording: {audio}")
    if audio.stat().st_size > MAX_BYTES:
        raise SystemExit("the recording is over 10 MB: use a shorter clip (2 to 30 seconds)")
    if not transcript.strip() or len(transcript) > 2000:
        raise SystemExit("the transcript must be there and at most 2,000 characters")
    data = {"transcript": transcript.strip(), "name": name[:120], "consent": "true"}
    if language:
        data["language"] = language
    with audio.open("rb") as f:
        resp = http.post(URL, data=data, files={"audio": (audio.name, f, "audio/wav" if audio.suffix.lower() == ".wav" else "application/octet-stream")})
    if resp.status_code >= 400:
        raise SystemExit(f"Spitch refused the recording ({resp.status_code}): {resp.text[:300]}")
    voice_id = resp.json().get("voice_id")
    if not voice_id:
        raise SystemExit(f"Spitch answered without a voice id: {resp.text[:300]}")
    return voice_id


def main() -> None:
    ap = argparse.ArgumentParser(description="Add your own voice to Spitch.")
    ap.add_argument("--audio", required=True, type=Path, help="the recording: one speaker, 2 to 30 seconds, up to 10 MB")
    ap.add_argument("--transcript", required=True, help="exactly what is said in the recording")
    ap.add_argument("--name", required=True, help="a name for the voice, to recognise it in your Spitch account")
    ap.add_argument("--language", default="", help="optional language code of the recording, for example en")
    ap.add_argument("--consent", action="store_true", help="you are the speaker, or have the speaker's permission (Spitch requires this)")
    args = ap.parse_args()
    if not args.consent:
        raise SystemExit("add --consent to confirm you are the speaker or have the speaker's permission to use this voice")
    key = get_settings().spitch_api_key
    if not key:
        raise SystemExit("SPITCH_API_KEY is not set in .env")
    with httpx.Client(timeout=60, headers={"Authorization": f"Bearer {key}"}, verify=tls_context()) as http:
        voice_id = create_voice(http, args.audio, args.transcript, args.name, args.language)
    print(f"Done. The voice id is: {voice_id}")
    print(f"Put SPITCH_VOICE_EN={voice_id} in the settings (the server's .env and your own), then restart.")


if __name__ == "__main__":
    sys.exit(main())
