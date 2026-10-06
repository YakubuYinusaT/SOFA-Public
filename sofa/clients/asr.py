from pydantic import BaseModel

from .http import http_for


class ASRResult(BaseModel):
    text: str
    confidence: float  # average log-probability; higher (closer to 0) is better
    model: str = ""
    language: str = ""


class ASRClient:
    """POST /asr (audio bytes, language) -> text, confidence. One N-ATLAS Whisper model per language."""

    def __init__(self, url: str, api_key: str = ""):
        self.http, self.base = http_for(url, api_key)

    async def transcribe(self, audio: bytes, language: str) -> ASRResult:
        resp = await self.http.post(
            f"{self.base}/asr",
            files={"audio": ("turn.wav", audio, "audio/wav")},
            data={"language": language},
        )
        resp.raise_for_status()
        return ASRResult(**resp.json())
