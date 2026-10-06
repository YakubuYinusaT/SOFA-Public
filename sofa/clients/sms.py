import logging

import httpx

log = logging.getLogger("sofa.sms")


class SMSClient:
    """Africa's Talking SMS. With no AT_API_KEY it logs instead of sending (mock mode)."""

    def __init__(self, settings):
        self.s = settings

    async def send(self, to: str, message: str) -> tuple[str, str | None]:
        """Returns (status, provider_message_id)."""
        if self.s.sms_is_mock:
            log.info("[mock sms] to=%s: %s", to, message)
            return "mock_sent", None
        host = "api.sandbox.africastalking.com" if self.s.at_sandbox else "api.africastalking.com"
        data = {"username": self.s.at_username, "to": to, "message": message}
        if self.s.sms_sender_id:
            data["from"] = self.s.sms_sender_id
        async with httpx.AsyncClient(timeout=15) as http:
            resp = await http.post(
                f"https://{host}/version1/messaging",
                data=data,
                headers={"apiKey": self.s.at_api_key, "Accept": "application/json"},
            )
        if resp.status_code >= 300:
            log.error("sms failed %s: %s", resp.status_code, resp.text)
            return "failed", None
        recipients = resp.json().get("SMSMessageData", {}).get("Recipients", [])
        return "sent", recipients[0].get("messageId") if recipients else None
