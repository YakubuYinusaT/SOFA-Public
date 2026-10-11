import logging

import httpx

log = logging.getLogger("sofa.sms")

# Africa's Talking answers 201 even when it refuses the message itself; what happened to each recipient is in statusCode:
# 100 processed, 101 sent, 102 queued are accepted. The rest are refusals (401 risk hold, 402 invalid sender id, 403 invalid phone number,
# 404 unsupported number type, 405 insufficient balance, 406 user in blacklist, 407 could not route, 500 and up their own faults).
ACCEPTED = {100, 101, 102}


class SMSClient:
    """Africa's Talking SMS. With no AT_API_KEY it logs instead of sending (mock mode)."""

    def __init__(self, settings):
        self.s = settings

    async def send(self, to: str, message: str) -> tuple[str, str | None]:
        """Returns (status, provider_message_id). The status is 'sent' only when Africa's Talking accepted the message for this recipient."""
        status, message_id, _ = await self.deliver(to, message)
        return status, message_id

    async def deliver(self, to: str, message: str) -> tuple[str, str | None, str]:
        """Returns (status, provider_message_id, what happened in words). The words say why a message was refused, so a refusal is never silent."""
        if self.s.sms_is_mock:
            log.info("[mock sms] to=%s: %s", to, message)
            return "mock_sent", None, "mock mode: no AT_API_KEY, nothing was sent"
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
            reason = f"HTTP {resp.status_code}: {resp.text[:200]}"
            log.error("sms to %s failed, %s", to, reason)
            return "failed", None, reason
        try:
            body = resp.json().get("SMSMessageData", {})
        except ValueError:
            body = {}
        recipients = body.get("Recipients") or []
        first = recipients[0] if recipients else {}
        try:
            code = int(first.get("statusCode"))
        except (TypeError, ValueError):
            code = None
        if code not in ACCEPTED:
            reason = f"refused: {first.get('status') or body.get('Message') or 'no recipient in the answer'} (code {code})"
            log.error("sms to %s not sent, %s", to, reason)
            return "failed", None, reason
        reason = f"{first.get('status', 'accepted')} (code {code}), cost {first.get('cost', '?')}"
        log.info("sms to %s %s", to, reason)
        return "sent", first.get("messageId"), reason
