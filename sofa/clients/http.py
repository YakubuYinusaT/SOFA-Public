import asyncio
import logging
import os
import ssl

import httpx

log = logging.getLogger("sofa.http")

# Connection-level failures: a dropped or garbled secure connection through RunPod's proxy, a refused connection.
# Not timeouts (a retry would make the caller wait twice as long) and not HTTP error answers (the host did answer).
RETRYABLE = (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError, httpx.ReadError, httpx.WriteError,
             httpx.CloseError, ssl.SSLError, ConnectionError)


class RetryingClient(httpx.AsyncClient):
    """An HTTP client that tries a request again when the connection itself broke. The GPU calls (speech to text, the language
    model, text to speech) can safely be repeated, and one bad connection must not cost a caller a turn."""

    def __init__(self, *args, retries: int = 2, **kwargs):
        super().__init__(*args, **kwargs)
        self.retries = retries

    async def post(self, *args, **kwargs):
        for attempt in range(self.retries + 1):
            try:
                return await super().post(*args, **kwargs)
            except RETRYABLE as exc:
                if attempt == self.retries:
                    raise
                log.warning("connection to the GPU host broke (%s); trying again (%d of %d)", type(exc).__name__, attempt + 1, self.retries)
                await asyncio.sleep(0.2 * (attempt + 1))


def http_for(url: str, api_key: str = "") -> tuple[httpx.AsyncClient, str]:
    """Return (client, base_url). inprocess://mock routes straight into the mock GPU app.
    api_key is sent as a Bearer token to the GPU host (vLLM --api-key, gpu/common.py)."""
    if url.startswith("inprocess://"):
        from mocks.gpu_mock import app as mock_app

        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app), timeout=30)
        return client, "http://mock"
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    return RetryingClient(timeout=30, headers=headers, verify=_tls_context()), url.rstrip("/")


def _tls_context() -> ssl.SSLContext:
    """Connections to the GPU host use TLS 1.2 at most. On the first real GPU run, requests over TLS 1.3 from this Python (3.13,
    OpenSSL 3.0.15) through RunPod's proxy failed with 'bad record mac' in 6 to 23 of 25 tries, and with TLS 1.2 in none of 25.
    TLS 1.2 is fully supported by the proxy and just as secure. Set GPU_TLS12=0 to allow TLS 1.3 again."""
    ctx = ssl.create_default_context()
    if os.environ.get("GPU_TLS12", "1") != "0":
        ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    return ctx
