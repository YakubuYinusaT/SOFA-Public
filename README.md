# SOFA: System Of Functional Assistance

A voice-based technology developed by Connected Intelligence to deliver digital and AI services to the
underserved through an ordinary phone call: no smartphone, app or internet needed. A caller speaks
English or Yoruba, SOFA understands what they want, and a service does it for them. Services plug into
one gateway through a common set of tools and rules, so a new service can be added without changing
the conversation.

This release applies SOFA to farming. Farmers phone one number to buy fertiliser and other farm
inputs, pay for them, and get farming advice.

Africa's Talking carries the call, this FastAPI backend runs the conversation and the business logic,
and a GPU host runs N-ATLaS Whisper speech recognition, the N-ATLaS language model and speech output.
Built for the National AI Innovation Challenge 2026, Problem Statement 2: Voice-First Access.

> N-ATLaS is an initiative of the Federal Ministry of Communications, Innovation and Digital
> Economy, and powered by Awarri Technologies.

## Run it

```bash
pip install -e ".[dev]"
python -m pytest                       # the full test suite, including a complete order conversation
python -m scripts.seed_agro            # the CII Store: farm inputs, stock and farming advice
python -m scripts.simulate_call        # phone yourself: type what the caller says
python -m scripts.simulate_call --caller +2348033333333   # call as the store owner (stock updates)
```

The simulator calls the same `/voice/*` endpoints Africa's Talking calls. Speech recognition, the
language model and speech output run in-process (`ASR_URL=inprocess://mock`), so nothing else is
needed to try the whole conversation.

Serve it: `uvicorn sofa.main:create_app --factory --port 8000`. The team console is at `/admin`, the
store owner's portal at `/vendor` and a read-only live page at `/demo` (switched on with
`DEMO_PAGE_ENABLED=true`). Set `ADMIN_TOKEN`, `VENDOR_TOKEN` and `PROVIDER_TOKEN` to long random
passwords before exposing them. The bearer-token API is under `/api`.

## What SOFA does

| Area | What it does |
|---|---|
| Voice loop | Inbound calls, record and play turns, holding messages while an answer is prepared, call-back with the answer if the caller hangs up, silence handling, a call time limit and a polite goodbye |
| Language | English and Yoruba. Both speech models hear each turn and the most confident one sets the language; callers who mix Yoruba, English and Pidgin in one sentence are understood |
| Understanding | One language-model call per turn returns what the caller wants as structured JSON. The model never states a fact: products, prices, stock and totals come from the database |
| Shopping | Product matching by exact alias, trigram similarity and model reranking; orders are read back and confirmed with a PIN typed on the keypad (never stored or logged); stock is reserved and an SMS invoice is sent |
| Payments | Paystack dedicated bank accounts per customer, a signed `charge.success` webhook, automatic matching to the open invoice, and a handoff for anything that does not match |
| Advice | Farming advice written by the store and its agronomists, matched to the caller's words, together with the product to offer; general questions are answered by Google Programmable Search |
| Store owner | Update stock and prices by voice, a daily report by SMS, outbound status calls for payment, dispatch and delivery, and a web portal for orders, stock, advice and customer questions |
| Gateway | Services register as adapters with their tools and rules; callers are recognised by number and the right service is chosen from what they say |
| Records | Every call is logged with audio, transcript, confidence, model output, action, reply and per-stage latency, with a labelling screen and anonymised exports |
| Team console | Merchants and onboarding, catalogue with CSV import, orders, payments, handoffs, call view, metrics and a translation sheet for new languages |

## Languages

`ENABLED_LANGUAGES=en,yo` sets the languages. There is no keypad menu: every enabled speech model
listens to the caller's first words and the most confident one sets the language. If none is sure,
SOFA asks out loud which language the caller prefers and understands the spoken answer. Yoruba
wording comes from `docs/translation_sheet.xlsx` through `scripts/import_translations.py`; a phrase
that has no translation is spoken in English. More languages are added by listing them in
`ENABLED_LANGUAGES` (backend) and `ASR_LANGUAGES` (GPU host).

## Connecting the real services

1. **GPU host** (one RTX 4090 runs all three models): follow `gpu/README.md` (`gpu/runpod/bootstrap.sh`
   once, `gpu/runpod/start.sh` after each restart, then `python -m scripts.gpu_smoke`). Set `ASR_URL`,
   `LLM_URL`, `TTS_URL` and `GPU_API_KEY`. The language model runs in fp8 so that it fits beside speech
   recognition and speech output in 24 GB.
2. **Database and session**: `DATABASE_URL=postgresql+psycopg://...` and `REDIS_URL=redis://...`.
3. **Africa's Talking**: set `AT_USERNAME`, `AT_API_KEY`, `AT_SANDBOX=false` and a long random
   `AT_CALLBACK_SECRET`, then point the number's callback URL at `https://HOST/voice/inbound/<secret>`
   and events at `/voice/events/<secret>`. `PUBLIC_BASE_URL` is the public HTTPS address (it is
   embedded in the audio URLs). See `docs/at_number_setup.md`.
4. **Paystack**: `PAYSTACK_SECRET_KEY`, and a webhook to `https://HOST/webhooks/paystack`.
5. **Google search**: `GOOGLE_API_KEY` and `GOOGLE_CSE_ID`.
6. `python -m scripts.warm_cache` pre-generates the audio for fixed phrases.

The language model can also be served by a hosted OpenAI-style API: set `LLM_PROVIDER=openai`,
`LLM_URL`, `LLM_MODEL` and `LLM_API_KEY`.

## Measuring speech recognition

Record real calls in a language, write the true words next to each file, then:

```bash
python -m scripts.asr_gate --asr-url http://GPU_HOST:9000 --lang yo --dir recordings/yo
```

It prints the word error rate per file and writes `asr_gate_yo.json`.

## Layout

```
sofa/            backend (routes/, gateway/, dialogue/, services/, clients/, models.py)
sofa/web/        console and portal templates, styles and fonts
mocks/           in-process stand-ins for the GPU host: /asr, /tts, /v1/chat/completions
gpu/             GPU-host services (vLLM launcher, ASR, YarnGPT2 speech) and gpu/runpod/ setup scripts
scripts/         seed, seed_agro, simulate_call, warm_cache, asr_gate, gpu_smoke
docs/            translation guide and sheet, number setup, code-mixing notes
tests/           unit, call-flow, webhook, gateway and console tests
```

## Design notes

- **N-ATLaS never states a fact.** It returns intent JSON; products, prices, stock and totals come from
  the database. Model-written wording is used only if the validator confirms every number and name
  comes from a placeholder, otherwise the template is spoken.
- **Callbacks never return an empty body**: any error becomes a cached "one moment" and a redirect, or a goodbye.
- **PINs and one-time codes are typed on the keypad**, never spoken, stored or logged.
- **Trigram matching runs in Python** (the same measure as `pg_trgm`); small catalogues need nothing more.
- **Speech recognition and language understanding use N-ATLaS**; speech output uses YarnGPT2.
- Shared free use of N-ATLaS is capped at 1,000 active users per rolling 30 days; commercial use
  needs a separate licence from Awarri.
