from .http import http_for


class TTSClient:
    """POST /tts (text, language) -> WAV bytes. YarnGPT2 on the GPU host."""

    def __init__(self, url: str, api_key: str = ""):
        self.http, self.base = http_for(url, api_key)

    async def synthesize(self, text: str, language: str) -> bytes:
        resp = await self.http.post(f"{self.base}/tts", json={"text": text, "language": language})
        resp.raise_for_status()
        return resp.content
