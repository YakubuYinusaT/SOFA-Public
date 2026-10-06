import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("sofa.voice_client")


@dataclass
class Placed:
    ok: bool
    session_id: str | None = None
    error: str | None = None
    simulated: bool = False


class VoiceClient:
    """Places outbound calls through the Africa's Talking Voice API. With no AT_API_KEY it logs instead (mock mode).

    POST https://voice.africastalking.com/call  with  username, from (our number), to (the customer) and an optional
    clientRequestId. The answered call is then sent to the callback URL configured on our number in the AT dashboard,
    which is why the clientRequestId carries our own job id: it is how the answer is matched back to the job."""

    URL = "https://voice.africastalking.com/call"

    def __init__(self, settings):
        self.s = settings

    async def call(self, from_number: str, to_number: str, client_request_id: str) -> Placed:
        if self.s.sms_is_mock:
            log.info("[mock call] from=%s to=%s id=%s", from_number, to_number, client_request_id)
            return Placed(ok=True, simulated=True)
        data = {"username": self.s.at_username, "from": from_number, "to": to_number, "clientRequestId": client_request_id}
        try:
            async with httpx.AsyncClient(timeout=15) as http:
                resp = await http.post(self.URL, data=data, headers={"apiKey": self.s.at_api_key, "Accept": "application/json"})
        except httpx.HTTPError as exc:
            return Placed(ok=False, error=f"network: {type(exc).__name__}")
        if resp.status_code >= 300:
            log.error("call failed %s: %s", resp.status_code, resp.text[:300])
            return Placed(ok=False, error=f"http {resp.status_code}")
        try:
            body = resp.json()
            entry = (body.get("entries") or [{}])[0]
        except ValueError:
            return Placed(ok=False, error="bad response")
        status = str(entry.get("status", ""))
        if status.lower() == "queued":
            return Placed(ok=True, session_id=entry.get("sessionId"))
        return Placed(ok=False, error=(status or str(body.get("errorMessage") or "rejected"))[:200])
