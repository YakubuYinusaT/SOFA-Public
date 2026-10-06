# Connecting the Africa's Talking number

Do this once the number is allocated. Menu names on the Africa's Talking dashboard may differ a little from the ones below. If something is not where this says, look in the Voice section.

## What you need first

- The number Africa's Talking gave you (a test number is fine to start).
- The backend running on a public https address. A free tunnel from your computer works for the first tests.
- Your Africa's Talking username and API key (Settings, then API Key).

## 1. Fill in the backend settings

In the backend's `.env` file:

```
AT_USERNAME=your-username
AT_API_KEY=your-api-key
AT_SANDBOX=false
AT_CALLBACK_SECRET=a-long-random-text-with-no-spaces
PUBLIC_BASE_URL=https://your-public-address
```

Keep the API key private. Never paste it in a chat or an email.

## 2. Tell Africa's Talking where to send calls

On the dashboard, open Voice, then Phone Numbers, then your number, and set:

| Setting | Value |
|---|---|
| Callback URL | `https://your-public-address/voice/inbound/<the secret from step 1>` |
| Events URL (if there is a field for it) | `https://your-public-address/voice/events/<the same secret>` |

Save. The same callback URL receives both calls people make to the shop and the status calls we make to customers.

## 3. Add the number to the shop

In the admin console, open the shop, and add the number in the international format (for example +2348012345678). Without this, callers hear "this number is not in service", and status calls have no number to call from.

## 4. Test in this order

1. **Call the number from your phone.** You should hear the greeting. Say an order and listen to the reply.
2. **Check the raw fields.** In the backend log, look for the lines `voice inbound fields:` and `voice turn fields:`. They list the field names Africa's Talking sent. We expect `sessionId`, `callerNumber`, `destinationNumber` and, on the turn, `recordingUrl`. If the recording field has a different name, send me those log lines and I will change one line of code.
3. **Place an order and pay it** (or use "mark paid" in the admin console). A status call should ring your phone within a minute, from the shop's number. It says the payment was received.
4. **Check the outbound log lines.** After you answer, the log should show `voice inbound fields:` with `direction` and `clientRequestId`. If `clientRequestId` is missing, the call is still matched by your phone number, but tell me so I can fix it properly.
5. **Let one status call ring without answering.** After 3 minutes it is marked as unanswered and tried again 20 minutes later. You can see each try on the order page in the admin console, under "Status calls to the customer".

## Things to remember

- A status call costs NGN 15 a minute (NGN 20 to an MTN number). A normal message is about 10 seconds.
- Status calls never go out between 9 PM and 7 AM. They wait until morning.
- To stop all status calls, set `OUTBOUND_CALLS_ENABLED=false` and restart.
- Africa's Talking deletes recordings from its dashboard after 48 hours. The backend saves its own copy of every turn, so nothing is lost.
