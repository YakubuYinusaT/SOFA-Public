"""Telephony callbacks (Africa's Talking Voice XML).

Every callback must answer inside the provider timeout and never return an empty body: any
exception falls back to a cached "one moment" filler + Redirect, or a polite goodbye.
"""

import asyncio
import json
import logging
import random
import time
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy import func, select

from .. import voicexml
from ..audio import fetch_recording, to_16k_mono
from ..dialogue.manager import DialogueManager, Outcome, daypart, greeting_key, new_state
from ..dialogue import language
from ..gateway import links
from ..gateway.manager import GatewayManager
from ..gateway.verification import digits_step
from ..dialogue.templates import render, speak_lang
from ..models import Call, CallTurn, Customer, CustomerProfile, LateReply, Merchant, MerchantUser, Order, OutboundCall, PhoneNumber
from ..services import orders as order_svc
from ..services import latereply
from ..services import outbound as outbound_svc
from ..textutil import clean, e164, naira

log = logging.getLogger("sofa.voice")
router = APIRouter()



def _xml(body: str, reply_text: str | None = None, request: Request | None = None) -> Response:
    headers = {}
    if reply_text is not None and request and request.app.state.svc.settings.debug_headers:
        headers["X-Sofa-Reply-Text"] = reply_text.encode("ascii", "replace").decode()
    return Response(body, media_type="application/xml", headers=headers)


def _log_fields(kind: str, form) -> None:
    """Field NAMES only (never values): shows what the provider really sends, which the docs do not fully say."""
    log.info("voice %s fields: %s", kind, ",".join(sorted(form.keys())))


def _check_secret(request: Request, secret: str) -> None:
    if secret != request.app.state.svc.settings.at_callback_secret:
        raise HTTPException(status_code=404)


def _url(request: Request, path: str, secret: str) -> str:
    return f"{request.app.state.svc.settings.public_base_url.rstrip('/')}/voice/{path}/{secret}"


WAIT_STAGES = 5  # wait_1 .. wait_5, then wait_giveup


async def _giveup(request: Request, secret: str, lang: str, key: str) -> Response:
    """The answer is not coming in time: say so, ask whether there is anything else, and listen. If the caller has nothing more, the
    silence ends the call (gateway_max_silences): a line left open costs money."""
    svc = request.app.state.svc
    try:
        urls = []
        for k in (key, "gateway_anything_else"):
            vlang = speak_lang(k, lang)
            urls.append((await svc.audio.speak(render(k, vlang, {}), vlang))[0])
        return _xml(voicexml.record(urls, _url(request, "turn", secret), svc.settings.record_timeout_seconds, svc.settings.record_max_seconds))
    except Exception:
        return await _fallback(request, secret, lang, redirect_to=_url(request, "turn", secret), key=key)


async def _fallback(request: Request, secret: str, lang: str = "en", redirect_to: str | None = None, key: str = "filler") -> Response:
    """Never an empty body: a cached holding message + Redirect, or a goodbye."""
    svc = request.app.state.svc
    try:
        vlang = speak_lang(key, lang)
        url, _ = await svc.audio.speak(render(key, vlang, {}), vlang)
        if redirect_to:
            return _xml(voicexml.redirect(redirect_to, [url]))
        return _xml(voicexml.play_and_hangup([url]))
    except Exception:
        return _xml(voicexml.say_and_hangup("Sorry, please call again."))


def _find_outbound_job(db, form):
    """Match an answered outbound call to its queued row. The clientRequestId we sent is the reliable key; if the
    provider does not echo it, fall back to the customer's number on a call we are waiting for."""
    raw = str(form.get("clientRequestId") or "")
    try:
        job = db.get(OutboundCall, uuid.UUID(raw)) if raw else None
    except ValueError:
        job = None
    if job:
        return job
    numbers = {e164(str(form.get(k) or "")) for k in ("callerNumber", "destinationNumber", "callerCarrierName") if form.get(k)}
    for job in db.scalars(select(OutboundCall).where(OutboundCall.status == "calling").order_by(OutboundCall.updated_at.desc())):
        order = db.get(Order, job.order_id)
        customer = db.get(Customer, order.customer_id) if order else None
        if customer and customer.phone in numbers:
            return job
    return None


async def _answered_outbound(request: Request, secret: str, form, job_id: str | None = None) -> Response:
    """The customer picked up a status call: say the news, and for some messages listen for an answer."""
    svc = request.app.state.svc
    try:
        with svc.session_factory() as db:
            job = db.get(OutboundCall, uuid.UUID(job_id)) if job_id else _find_outbound_job(db, form)
            if not job:
                return _xml(voicexml.say_and_hangup("Thank you."))
            order = db.get(Order, job.order_id)
            merchant, customer = db.get(Merchant, order.merchant_id), db.get(Customer, order.customer_id)
            number = db.scalar(select(PhoneNumber).where(PhoneNumber.merchant_id == merchant.id, PhoneNumber.active.is_(True)))
            profile = db.scalar(select(CustomerProfile).where(CustomerProfile.customer_id == customer.id,
                                                              CustomerProfile.merchant_id == merchant.id))
            if not profile:
                db.add(CustomerProfile(customer_id=customer.id, merchant_id=merchant.id))
            session_id = str(form.get("sessionId") or job.provider_session_id or uuid.uuid4())
            enabled = svc.settings.languages
            lang = customer.language if customer.language in enabled else (merchant.default_language if merchant.default_language in enabled else "en")
            call = Call(
                merchant_id=merchant.id, customer_id=customer.id, direction="outbound", provider_session_id=session_id,
                from_number=number.e164 if number else "", to_number=customer.phone, language=lang,
                is_test=customer.phone in svc.settings.test_numbers,
            )
            db.add(call)
            db.flush()
            key = f"outbound_{job.trigger}"
            vlang = speak_lang(key, lang)  # English voice if the message is not translated yet
            text = render(key, vlang, {"merchant": merchant.name, "total": naira(order.total_kobo)})
            audio_url, _ = await svc.audio.speak(text, vlang)
            job.status, job.call_id, job.provider_session_id = "answered", call.id, session_id
            if job.trigger in outbound_svc.ASK_AFTER:
                st = new_state(lang, lang_locked=True, owner=False)
                st.update(call_id=str(call.id), merchant_id=str(merchant.id), customer_id=str(customer.id), secret=secret)
                svc.sessions.set(session_id, st)
                body = voicexml.record([audio_url], _url(request, "turn", secret),
                                       svc.settings.record_timeout_seconds, svc.settings.record_max_seconds)
            else:
                body = voicexml.play_and_hangup([audio_url])
            db.commit()
            return _xml(body, text, request)
    except Exception:
        log.exception("outbound answer failed")
        return _xml(voicexml.say_and_hangup("Thank you."))


def _late_reply_body(request: Request, secret: str, svc, row: LateReply, prompts: list[str], new_session: str) -> str:
    """The apology and the answer, then the line stays open with the first call's state (the same account and verification)."""
    st = svc.sessions.get(row.session_id)
    if st and not row.ends:
        svc.sessions.set(new_session, {**st, "t0": time.time()})
        return voicexml.record(prompts, _url(request, "turn", secret), svc.settings.record_timeout_seconds, svc.settings.record_max_seconds)
    return voicexml.play_and_hangup(prompts)


async def _answered_late_reply(request: Request, secret: str, form) -> Response:
    """The caller picked up the call back. If they have a bank PIN, it is asked first, so that nobody else who picks up that phone
    hears the answer; only then the apology and the answer. Callers with no bank link get the answer straight away."""
    svc = request.app.state.svc
    try:
        with svc.session_factory() as db:
            row = db.get(LateReply, uuid.UUID(str(form.get("clientRequestId"))[len(latereply.PREFIX):]))
            if not row:
                return _xml(voicexml.say_and_hangup("Thank you."))
            new_session = str(form.get("sessionId") or uuid.uuid4())
            row.status, row.provider_session_id = "answered", new_session
            vlang = speak_lang("late_reply_intro", row.lang)
            intro, _ = await svc.audio.speak(render("late_reply_intro", vlang, {}), vlang)
            if links.active(db, row.customer_id, "banking"):
                plang = speak_lang("late_reply_pin", row.lang)
                ask, _ = await svc.audio.speak(render("late_reply_pin", plang, {}), plang)
                db.commit()
                url = f"{svc.settings.public_base_url.rstrip('/')}/voice/late-pin/{secret}/{row.id}"
                return _xml(voicexml.get_digits([intro, ask], url, svc.settings.verify_pin_length, svc.settings.digits_timeout_seconds))
            prompts = [intro, *json.loads(row.audio)]
            db.commit()
            return _xml(_late_reply_body(request, secret, svc, row, prompts, new_session), row.text, request)
    except Exception:
        log.exception("late reply answer failed")
        return _xml(voicexml.say_and_hangup("Thank you."))


@router.post("/voice/late-pin/{secret}/{row_id}")
async def late_reply_pin(secret: str, row_id: str, request: Request):
    """The PIN typed on the call back. The digits are checked and forgotten: not stored, not logged."""
    _check_secret(request, secret)
    svc = request.app.state.svc
    form = await request.form()
    _log_fields("late-pin", form)
    pin = str(form.get("dtmfDigits") or "").strip()
    try:
        with svc.session_factory() as db:
            row = db.get(LateReply, uuid.UUID(row_id))
            if not row:
                return _xml(voicexml.say_and_hangup("Thank you."))
            lang = row.lang
            bank = (links.active(db, row.customer_id, "banking") or [None])[0]
            customer = db.get(Customer, row.customer_id)
            ok = bool(bank and pin.isdigit() and len(pin) == svc.settings.verify_pin_length
                      and await svc.bankauth.verify_pin(bank.provider, customer.phone, pin))
            if ok:
                vlang = speak_lang("late_reply_intro", lang)
                intro, _ = await svc.audio.speak(render("late_reply_intro", vlang, {}), vlang)
                new_session = row.provider_session_id or str(form.get("sessionId") or uuid.uuid4())
                db.commit()
                return _xml(_late_reply_body(request, secret, svc, row, [*json.loads(row.audio)], new_session), row.text, request)
            row.pin_attempts += 1
            stop = row.pin_attempts >= svc.settings.late_reply_pin_attempts or bool(bank and links.locked(bank, svc.gateway.clock()))
            key = "late_reply_pin_stop" if stop else "late_reply_pin_wrong"
            vlang = speak_lang(key, lang)
            say, _ = await svc.audio.speak(render(key, vlang, {}), vlang)
            db.commit()
            if stop:
                return _xml(voicexml.play_and_hangup([say]))
            url = f"{svc.settings.public_base_url.rstrip('/')}/voice/late-pin/{secret}/{row_id}"
            return _xml(voicexml.get_digits([say], url, svc.settings.verify_pin_length, svc.settings.digits_timeout_seconds))
    except Exception:
        log.exception("late reply pin failed")  # nothing in this path puts the PIN into a message
        return _xml(voicexml.say_and_hangup("Thank you."))


async def _gateway_inbound(request: Request, secret: str, form, caller: str, dest: str) -> Response:
    """Someone phoned SOFA itself. No shop yet: greet like a customer-care agent and ask what they need."""
    svc = request.app.state.svc
    try:
        with svc.session_factory() as db:
            session_id = str(form.get("sessionId") or uuid.uuid4())
            recent = db.scalar(select(func.count(Call.id)).where(Call.from_number == caller,
                                                                 Call.created_at > datetime.now(timezone.utc) - timedelta(hours=1)))
            if (recent or 0) >= svc.settings.max_calls_per_hour:
                return _xml(voicexml.say_and_hangup("Sorry, please try again later."))
            customer = db.scalar(select(Customer).where(Customer.phone == caller))
            if not customer:
                customer = Customer(phone=caller)
                db.add(customer)
                db.flush()
            enabled = svc.settings.languages
            saved = customer.language if customer.language in enabled else None
            lang = saved or "en"
            call = Call(merchant_id=None, customer_id=customer.id, provider_session_id=session_id, from_number=caller,
                        to_number=dest, language=saved, is_test=caller in svc.settings.test_numbers)
            db.add(call)
            db.flush()
            st = new_state(lang, lang_locked=bool(saved) or len(enabled) == 1, owner=False)
            st.update(call_id=str(call.id), customer_id=str(customer.id), secret=secret, gateway=True, domain=None)
            has_bank = bool(links.active(db, customer.id, "banking"))
            key = ("greet_gateway_returning_bank" if has_bank else "greet_gateway_returning") if customer.name else "greet_gateway"
            vlang = speak_lang(key, lang)  # English voice until this greeting has wording in the caller's language
            greeting = render(f"daypart_{daypart()}", speak_lang(f"daypart_{daypart()}", vlang), {})
            # a random phrasing each call, so the front desk never sounds scripted
            text = render(key, vlang, {"name": customer.name or "", "greeting": greeting}, random.randrange(1000))
            audio_url, _ = await svc.audio.speak(text, vlang)
            svc.sessions.set(session_id, st)
            body = voicexml.record([audio_url], _url(request, "turn", secret),
                                   svc.settings.record_timeout_seconds, svc.settings.record_max_seconds)
            db.commit()
            return _xml(body, text, request)
    except Exception:
        log.exception("gateway inbound failed")
        return await _fallback(request, secret)


@router.api_route("/voice/inbound/{secret}", methods=["GET", "HEAD"], include_in_schema=False)
@router.api_route("/voice/events/{secret}", methods=["GET", "HEAD"], include_in_schema=False)
async def reachable(secret: str, request: Request):
    """A plain visit to a callback address answers 200, so a dashboard that checks the address before saving it (Africa's Talking does) accepts it.
    It shows nothing and does nothing; the secret is still checked."""
    _check_secret(request, secret)
    return Response(content="ok", media_type="text/plain")


@router.post("/voice/inbound/{secret}")
async def inbound(secret: str, request: Request):
    _check_secret(request, secret)
    svc = request.app.state.svc
    form = await request.form()
    _log_fields("inbound", form)
    # The number's one callback URL receives answered outbound calls as well as inbound ones.
    if str(form.get("clientRequestId") or "").startswith(latereply.PREFIX):
        return await _answered_late_reply(request, secret, form)
    if str(form.get("direction", "")).lower() == "outbound" or form.get("clientRequestId"):
        return await _answered_outbound(request, secret, form)
    gateway = svc.settings.gateway_number.strip()
    if gateway and e164(str(form.get("destinationNumber", ""))) == e164(gateway):
        return await _gateway_inbound(request, secret, form, e164(str(form.get("callerNumber", ""))), e164(gateway))
    try:
        with svc.session_factory() as db:
            session_id = str(form.get("sessionId") or uuid.uuid4())
            caller, dest = e164(str(form.get("callerNumber", ""))), e164(str(form.get("destinationNumber", "")))
            number = db.scalar(select(PhoneNumber).where(PhoneNumber.e164 == dest, PhoneNumber.active.is_(True)))
            if not number:
                return _xml(voicexml.say_and_hangup(render("no_service", "en", {})))
            merchant = db.get(Merchant, number.merchant_id)

            # abuse cap: 20 calls per caller per hour
            recent = db.scalar(
                select(func.count(Call.id)).where(
                    Call.from_number == caller, Call.created_at > datetime.now(timezone.utc) - timedelta(hours=1)
                )
            )
            if (recent or 0) >= svc.settings.max_calls_per_hour:
                return _xml(voicexml.say_and_hangup("Sorry, please try again later."))

            customer = db.scalar(select(Customer).where(Customer.phone == caller))
            if not customer:
                customer = Customer(phone=caller)
                db.add(customer)
                db.flush()
            profile = db.scalar(
                select(CustomerProfile).where(CustomerProfile.customer_id == customer.id, CustomerProfile.merchant_id == merchant.id)
            )
            if not profile:
                profile = CustomerProfile(customer_id=customer.id, merchant_id=merchant.id)
                db.add(profile)
            owner = db.scalar(
                select(MerchantUser).where(MerchantUser.merchant_id == merchant.id, MerchantUser.phone == caller,
                                           MerchantUser.can_update_stock.is_(True))
            )
            call = Call(
                merchant_id=merchant.id, customer_id=customer.id, provider_session_id=session_id,
                from_number=caller, to_number=dest, language=customer.language or merchant.default_language,
                is_test=caller in svc.settings.test_numbers,
            )
            db.add(call)
            db.flush()

            enabled = svc.settings.languages
            saved = customer.language if customer.language in enabled else None  # a language since switched off is forgotten
            lang = saved or (merchant.default_language if merchant.default_language in enabled else "en")
            st = new_state(lang, lang_locked=bool(saved) or len(enabled) == 1, owner=bool(owner))
            st.update(call_id=str(call.id), merchant_id=str(merchant.id), customer_id=str(customer.id), secret=secret)
            if owner:
                customer.name = customer.name or owner.name

            last = order_svc.latest_order(db, merchant.id, customer.id, ("delivered", "paid", "dispatched", "awaiting_payment", "confirmed"))
            last_desc = order_svc.describe_items(last, speak_lang("greet_returning_repeat", lang)) if last and last.items else None
            key, facts = greeting_key(customer, last_desc, bool(owner))
            if key == "greet_returning_repeat":
                st["stage"] = "offer_repeat"
            vlang = speak_lang(key, lang)  # English voice if we have no wording in the caller's language yet
            greeting = render(f"daypart_{daypart()}", speak_lang(f"daypart_{daypart()}", vlang), {})
            text = render(key, vlang, {"merchant": merchant.name, "name": customer.name or "", "greeting": greeting, **facts})
            audio_url, _ = await svc.audio.speak(text, vlang)
            svc.sessions.set(session_id, st)
            body = voicexml.record([audio_url], _url(request, "turn", secret),
                                   svc.settings.record_timeout_seconds, svc.settings.record_max_seconds)
            db.commit()
            return _xml(body, text, request)
    except Exception:
        log.exception("inbound failed")
        return await _fallback(request, secret)


def _next_prompt(svc, outcome: Outcome, prompts: list[str], secret: str) -> str:
    """Voice XML for what happens after SOFA speaks: hang up, listen for speech, or collect keypad digits."""
    base = svc.settings.public_base_url.rstrip("/")
    if outcome.end_call:
        return voicexml.play_and_hangup(prompts)
    if outcome.collect:
        digits_url = f"{base}/voice/digits/{secret}"
        return voicexml.get_digits(prompts, digits_url, outcome.collect["digits"], svc.settings.digits_timeout_seconds, digits_url)
    return voicexml.record(prompts, f"{base}/voice/turn/{secret}", svc.settings.record_timeout_seconds, svc.settings.record_max_seconds)


async def _speak_outcome(svc, outcome) -> list[str]:
    """Audio for every clip of a reply. A model-worded sentence is new text, so the voice model has to make it from scratch, which can
    be slow: past the time budget the stored template wording is spoken instead (always cached ahead of time), never a long silence."""
    async def speak(clips):
        return [(await svc.audio.speak(text_, lang_))[0] for text_, lang_ in clips]

    clips = [*(outcome.parts or [(outcome.text, outcome.lang)]), *outcome.also]
    template = outcome.extra.get("template") if outcome.action.endswith("|generated") else None
    if not template:
        return await speak(clips)
    try:
        return await asyncio.wait_for(speak(clips), timeout=svc.settings.tts_budget_seconds)
    except (asyncio.TimeoutError, Exception) as exc:
        log.warning("voice too slow for the worded reply (%s), speaking the template", type(exc).__name__)
        text_, lang_, parts = template
        outcome.text, outcome.lang, outcome.parts = text_, lang_, parts
        outcome.action = outcome.action.replace("|generated", "|template_fallback")
        return await speak([*(parts or [(text_, lang_)]), *outcome.also])


async def _process_digits(svc, session_id: str, digits: str, secret: str, turn_id: uuid.UUID) -> tuple[str, str]:
    """The caller typed a PIN or code. The digits go to the bank check and nowhere else: not stored, not logged."""
    st = svc.sessions.get(session_id)
    if not st or not st.get("gateway"):
        raise LookupError("no gateway session")
    with svc.session_factory() as db:
        call, customer = db.get(Call, uuid.UUID(st["call_id"])), None
        customer = db.get(Customer, call.customer_id)
        seq = (db.scalar(select(func.max(CallTurn.seq)).where(CallTurn.call_id == call.id)) or 0) + 1
        turn = CallTurn(id=turn_id, call_id=call.id, seq=seq, asr_model="keypad")  # no audio, no transcript
        db.add(turn)
        mgr = GatewayManager(db, svc, st, call, customer, turn_id)
        outcome = await mgr.on_digits(digits)
        prompts = await _speak_outcome(svc, outcome)
        turn.llm_json, turn.action_taken = mgr.raw_json, outcome.action
        turn.reply_text = " / ".join([outcome.text, *[t for t, _ in outcome.also]])
        svc.sessions.set(session_id, st)
        db.commit()
    return _next_prompt(svc, outcome, prompts, secret), outcome.text


@router.post("/voice/digits/{secret}")
async def digits(secret: str, request: Request):
    """Keypad digits (a PIN or a one-time code), or no digits if the caller let the timeout pass."""
    _check_secret(request, secret)
    svc = request.app.state.svc
    form = await request.form()
    _log_fields("digits", form)  # field names only, never values
    session_id = str(form.get("sessionId", ""))
    value = str(form.get("dtmfDigits") or "")
    st = svc.sessions.get(session_id)
    lang = st["lang"] if st else "en"
    task = asyncio.create_task(_process_digits(svc, session_id, value, secret, uuid.uuid4()))
    return await _reply_or_hold(request, secret, task, lang)  # the bank call is here, so this is where a slow answer happens


async def _process_turn(svc, session_id: str, recording_url: str | None, secret: str, turn_id: uuid.UUID) -> tuple[str, str, bool]:
    """Run one caller turn. Returns (voice xml, reply text, ended)."""
    t0 = time.perf_counter()
    lat: dict[str, int] = {}
    st = svc.sessions.get(session_id)
    if not st:
        raise LookupError("unknown session")
    with svc.session_factory() as db:
        call = db.get(Call, uuid.UUID(st["call_id"]))
        merchant, customer = (db.get(Merchant, call.merchant_id) if call.merchant_id else None), db.get(Customer, call.customer_id)
        profile = db.scalar(select(CustomerProfile).where(CustomerProfile.customer_id == customer.id,
                                                          CustomerProfile.merchant_id == merchant.id)) if merchant else None
        seq = (db.scalar(select(func.max(CallTurn.seq)).where(CallTurn.call_id == call.id)) or 0) + 1
        turn = CallTurn(id=turn_id, call_id=call.id, seq=seq, recording_url=recording_url)
        db.add(turn)
        if st.get("gateway"):  # the front desk, before any shop or bank is chosen
            mgr = GatewayManager(db, svc, st, call, customer, turn_id)
        else:
            mgr = DialogueManager(db, svc, st, call, merchant, customer, profile, turn_id)
        lang = st["lang"]
        try:
            if time.time() - st.get("t0", time.time()) > svc.settings.max_call_seconds:  # the longest a call may run
                vlang = speak_lang("call_time_limit", lang)
                outcome = Outcome(render("call_time_limit", vlang, {}), "call_time_limit", True, "time_limit", lang=vlang)
                call.outcome = call.outcome or "time_limit"
                transcript, conf = "", None
            elif not recording_url:  # caller said nothing
                outcome = await mgr.on_silence()
                transcript, conf = "", None
            elif st.get("gateway") and digits_step(st):
                # A PIN or code is being collected, and the caller spoke instead of typing. Drop the audio unheard: it is
                # never transcribed or saved, because it may contain the secret itself.
                turn.recording_url = None
                outcome = mgr.keypad_only()
                transcript, conf = "", None
            else:
                t = time.perf_counter()
                raw = await fetch_recording(recording_url)
                turn.audio_path = svc.audio.save_call_audio(call.id, seq, raw)
                audio = to_16k_mono(raw)
                lat["audio_fetch"] = int((time.perf_counter() - t) * 1000)

                t = time.perf_counter()
                asked_now, alts, how = False, [], "known"
                enabled = svc.settings.languages
                dual = svc.settings.dual_asr and len(enabled) > 1
                # Code-mixing: people switch languages inside one sentence, so with dual_asr every enabled model hears every
                # turn. One reading becomes the transcript; the other is kept as a second opinion for the LLM and the logs.
                if dual or not st["lang_locked"]:
                    results = await asyncio.gather(*[svc.asr.transcribe(audio, l) for l in enabled])
                else:
                    results = [await svc.asr.transcribe(audio, lang)]
                top = max(results, key=lambda r: r.confidence)

                if not st["lang_locked"]:
                    # No menu. The most confident model sets the language. If none is confident Sofa asks out loud which
                    # language the caller wants, and understands the spoken answer.
                    answer = language.spoken_choice([r.text for r in results], enabled) if st.get("asked_language") else None
                    if answer:  # "Yoruba", "English please": the caller named it
                        chosen, how = answer, "answered"
                    elif top.confidence >= svc.settings.asr_min_confidence:  # they just carried on in their language
                        chosen, how = top.language, "detected"
                    elif st.get("language_asks", 0) < language.MAX_ASKS:
                        # Low confidence is normal for mixed speech. Only interrupt with a question if we cannot find a
                        # request in what was said.
                        others = [r.text for r in results if r is not top]
                        if await mgr.understands(top.text, others):
                            chosen, how = top.language, "detected"
                        else:
                            chosen, how = None, "ask"
                    else:  # asked twice, still unclear: carry on in English rather than loop
                        chosen, how = "en", "default"
                    if chosen:
                        st["lang"], st["lang_locked"] = chosen, True
                        lang = chosen
                        if how != "default":  # a guessed default is not worth remembering for next time
                            customer.language = call.language = chosen
                        best = next(r for r in results if r.language == chosen)
                        alts = [r for r in results if r is not best]
                    else:
                        best, asked_now = top, True
                else:
                    mine = next((r for r in results if r.language == lang), results[0])
                    # The session-language model is the default reading; another one takes over only if clearly better.
                    best = top if top is not mine and top.confidence - mine.confidence >= svc.settings.primary_margin else mine
                    alts = [r for r in results if r is not best]
                    if dual:
                        # The reply language follows the caller, without flip-flopping on one English word: a clearly better
                        # OTHER model two turns running, or the caller asking for a language by name.
                        leader = top.language if top.confidence - mine.confidence >= svc.settings.language_switch_margin else lang
                        votes = (st.get("votes", []) + [leader])[-2:]
                        named = language.spoken_choice([r.text for r in results], enabled) if len(best.text.split()) <= 5 else None
                        new_lang = named if named and named != lang else (votes[0] if len(votes) == 2 and votes[0] == votes[1] != lang else None)
                        st["votes"] = [] if new_lang else votes
                        if new_lang:
                            st["lang"], lang = new_lang, new_lang
                            customer.language = call.language = new_lang
                            how = "switched"
                distinct, seen = [], {clean(best.text)}
                for r in alts:  # a second reading is only useful when it says something different (ignoring case and punctuation)
                    key = clean(r.text)
                    if key and key not in seen:
                        seen.add(key)
                        distinct.append(r)
                alts = distinct
                lat["asr"] = int((time.perf_counter() - t) * 1000)
                turn.asr_model, turn.transcript, turn.asr_confidence = best.model, best.text, best.confidence
                turn.asr_alternatives = [{"language": r.language, "model": r.model, "text": r.text, "confidence": r.confidence} for r in alts] or None
                transcript, conf = best.text, best.confidence

                t = time.perf_counter()
                if asked_now:
                    st["asked_language"], st["language_asks"] = True, st.get("language_asks", 0) + 1
                    prompts_ask = language.ask_prompts(svc.settings.languages, st["language_asks"])
                    outcome = Outcome(prompts_ask[0][0], "language_ask", action="language_ask", lang=prompts_ask[0][1], also=prompts_ask[1:])
                elif how in ("answered", "default"):  # the answer was about the language, not an order: confirm and ask for it
                    outcome = mgr.reply("ask_again_language", action=f"language_{how}")
                else:
                    outcome = await mgr.handle(best.text, best.confidence, [r.text for r in alts] or None)
                lat["understand_and_act"] = int((time.perf_counter() - t) * 1000)
            turn.llm_json, turn.match_candidates = mgr.raw_json, mgr.match_log or None
        except Exception as exc:
            log.exception("turn failed")
            db.rollback()
            db.add(turn)
            turn.error = repr(exc)
            vlang = speak_lang("repeat_prompt", lang)
            outcome = Outcome(render("repeat_prompt", vlang, {}), "repeat_prompt", action="error", lang=vlang)

        prompts: list[str] = []
        t = time.perf_counter()
        if not customer.consent_recorded_at and recording_url and not st["owner"] and outcome.key != "language_ask":
            clang = speak_lang("consent", st["lang"])
            consent_url, _ = await svc.audio.speak(render("consent", clang, {}), clang)
            prompts.append(consent_url)  # recording notice, once per customer, after the greeting reply
            customer.consent_recorded_at = datetime.now(timezone.utc)
        prompts.extend(await _speak_outcome(svc, outcome))  # money is a clip of its own, in the English voice; also: the same question again in Yoruba
        reply_path = str(svc.audio.out_dir / f"{svc.audio.key(*(outcome.parts or [(outcome.text, outcome.lang)])[0])}.wav")
        lat["tts"] = int((time.perf_counter() - t) * 1000)
        lat["total"] = int((time.perf_counter() - t0) * 1000)

        turn.action_taken, turn.reply_text, turn.reply_audio_path, turn.latency_ms = outcome.action, " / ".join([outcome.text, *[t for t, _ in outcome.also]]), reply_path, lat
        svc.sessions.set(session_id, st)
        db.commit()
        body = _next_prompt(svc, outcome, prompts, secret)
        return body, outcome.text, outcome.end_call


@router.post("/voice/turn/{secret}")
async def turn(secret: str, request: Request):
    _check_secret(request, secret)
    svc = request.app.state.svc
    form = await request.form()
    _log_fields("turn", form)
    session_id = str(form.get("sessionId", ""))
    recording_url = str(form.get("recordingUrl") or "") or None
    st = svc.sessions.get(session_id)
    lang = st["lang"] if st else "en"
    task = asyncio.create_task(_process_turn(svc, session_id, recording_url, secret, uuid.uuid4()))
    return await _reply_or_hold(request, secret, task, lang)


async def _reply_or_hold(request: Request, secret: str, task: asyncio.Task, lang: str) -> Response:
    """The reply if it is ready in a moment; otherwise a holding message now and the reply when it is ready."""
    svc = request.app.state.svc
    done, _ = await asyncio.wait({task}, timeout=svc.settings.filler_after_seconds)
    if task in done:
        try:
            result = task.result()
            return _xml(result[0], result[1], request)
        except Exception:
            log.exception("voice task failed")  # nothing in this path puts digits or a PIN into a message
            return await _fallback(request, secret, lang, redirect_to=_url(request, "turn", secret))  # carry on: never hang up on a caller
    turn_id = str(uuid.uuid4())
    svc.turn_tasks[turn_id] = task
    return await _fallback(request, secret, lang, redirect_to=f"{_url(request, 'continue', secret)}/{turn_id}?n=1")


@router.post("/voice/continue/{secret}/{turn_id}")
async def continue_turn(secret: str, turn_id: str, request: Request):
    _check_secret(request, secret)
    svc = request.app.state.svc
    task = svc.turn_tasks.get(turn_id)
    again = _url(request, "turn", secret)
    st = svc.sessions.get(str(dict(await request.form()).get("sessionId", "")))
    lang = st["lang"] if st else "en"
    if not task:
        return await _fallback(request, secret, lang, redirect_to=again)
    n = int(request.query_params.get("n", "1") or 1)
    done, _ = await asyncio.wait({task}, timeout=svc.settings.filler_every_seconds)
    if task in done:
        svc.turn_tasks.pop(turn_id, None)
        try:
            result = task.result()
            return _xml(result[0], result[1], request)
        except Exception:
            log.exception("continue failed")
            return await _fallback(request, secret, lang, redirect_to=again)
    if n > WAIT_STAGES:  # long enough: apologise, keep the line open and listen again
        svc.turn_tasks.pop(turn_id, None)
        if latereply.sensitive(st):  # about a bank account: no call back, the caller phones in later
            return await _giveup(request, secret, lang, "wait_giveup_sensitive")
        latereply.park(svc, task, str(dict(await request.form()).get("sessionId", "")))  # if the answer comes later, phone back with it
        return await _giveup(request, secret, lang, "wait_giveup")
    # still working: the next holding message, then wait again
    return await _fallback(request, secret, lang, redirect_to=f"{_url(request, 'continue', secret)}/{turn_id}?n={n + 1}", key=f"wait_{n}")


@router.post("/voice/outbound/{secret}/{outbound_call_id}")
async def outbound(secret: str, outbound_call_id: str, request: Request):
    """Direct route for an answered status call (the simulator and tests use it). In production the same work
    is done by /voice/inbound, because Africa's Talking sends every answered call to the number's one callback URL."""
    _check_secret(request, secret)
    return await _answered_outbound(request, secret, await request.form(), job_id=outbound_call_id)


@router.post("/voice/events/{secret}")
async def events(secret: str, request: Request):
    """Call status changes and call end: close the calls row with duration and cost."""
    _check_secret(request, secret)
    svc = request.app.state.svc
    form = await request.form()
    _log_fields("events", form)
    session_id = str(form.get("sessionId", ""))
    if str(form.get("isActive", "1")) == "0" and session_id:
        with svc.session_factory() as db:
            job = db.scalar(select(OutboundCall).where(OutboundCall.provider_session_id == session_id))
            if job and job.status == "calling":  # the call ended and was never answered: busy, rejected, no answer
                outbound_svc.retry_or_fail(job, svc.settings, str(form.get("hangupCause") or "no answer"))
                db.commit()
            call = db.scalar(select(Call).where(Call.provider_session_id == session_id))
            if call:
                call.ended_at = datetime.now(timezone.utc)
                dur = form.get("durationInSeconds")
                call.duration_s = int(dur) if dur and str(dur).isdigit() else call.duration_s
                call.cost = str(form.get("amount") or "") or call.cost
                call.outcome = call.outcome or "ended"
                db.commit()
        svc.sessions.delete(session_id)
    return Response("ok")
