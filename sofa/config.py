from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict

LANGUAGES = ("en", "yo", "ha", "ig")  # every language the code supports
LANGUAGE_NAMES = {"en": "English", "yo": "Yoruba", "ha": "Hausa", "ig": "Igbo"}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///./data/sofa.db"
    redis_url: str = ""
    public_base_url: str = "http://localhost:8000"
    storage_dir: str = "./data"

    at_username: str = "sandbox"
    at_api_key: str = ""
    at_sandbox: bool = True
    at_callback_secret: str = "dev-secret"
    sms_sender_id: str = ""

    paystack_secret_key: str = ""
    paystack_preferred_bank: str = "test-bank"

    # inprocess://mock routes to the built-in mock GPU services (mocks/gpu_mock.py)
    asr_url: str = "inprocess://mock"
    llm_url: str = "inprocess://mock"
    tts_url: str = "inprocess://mock"
    llm_model: str = "NCAIR1/N-ATLaS"
    # Two ways to run the language model. "vllm": N-ATLaS on our own GPU. "openai": a hosted model through
    # an OpenAI-style API (LLM_URL=https://api.openai.com, LLM_MODEL=<model name>, LLM_API_KEY=<its key>).
    llm_provider: str = "vllm"
    llm_api_key: str = ""  # the hosted provider's key; with "vllm" the GPU key (GPU_API_KEY) is used
    llm_structured_mode: str = "modern"  # "legacy" only for vLLM older than 0.12
    tts_provider: str = "yarngpt"  # yarngpt (the GPU host), elevenlabs, azure or spitch
    tts_provider_languages: str = ""  # languages spoken by the hosted voice (the others keep the GPU host's voice); empty = English, or every Spitch language
    tts_hosted_speed: float = 0.95
    spitch_api_key: str = ""
    spitch_voice_en: str = "jude"
    spitch_voice_yo: str = "sade"
    spitch_voice_ha: str = "amina"
    spitch_voice_ig: str = "ngozi"
    elevenlabs_api_key: str = ""
    elevenlabs_voice_id: str = ""
    elevenlabs_model: str = "eleven_multilingual_v2"
    azure_speech_key: str = ""
    azure_speech_region: str = ""
    azure_voice_en: str = "en-NG-EzinneNeural"
    gpu_api_key: str = ""  # Bearer token for the GPU host (ASR, LLM, TTS); required when it is public
    hf_token: str = ""

    # Languages callers can use. The pilot runs English and Yoruba only: fewer ASR models to load and to choose
    # between (better detection), and only Yoruba needs translating. Hausa and Igbo stay in the code, switched off
    # until they have translations and a measured ASR error rate.
    enabled_languages: str = "en,yo"

    # Code-mixing. People switch between Yoruba, English and Pidgin inside one sentence, so a single-language model
    # is often wrong on part of it. With dual_asr every enabled model hears every turn; the more confident reading is
    # the transcript and the other is passed to the LLM as a second opinion.
    dual_asr: bool = True
    primary_margin: float = 0.15    # another model must beat the session-language model by this much to be the transcript
    language_switch_margin: float = 0.3  # ...and by this much, two turns running, before Sofa's reply language follows

    # The one number callers dial to reach SOFA itself (the personal assistant) instead of a shop. Empty = gateway off.
    gateway_number: str = ""
    google_api_key: str = ""         # information search: Google Programmable Search; with no keys a small offline set of facts answers
    google_cse_id: str = ""
    search_timeout_seconds: float = 6.0
    # Let the model word SOFA's gateway replies in the caller's language mix (placeholders keep every fact the bank's, and the
    # wording is checked before it is spoken). Off = fixed template wording only. Which languages: see reply_mode_<lang>.
    gateway_wording: bool = True
    # Should the language the model reports for a sentence change the reply language? OFF: on the first GPU run the model called
    # an English sentence Yoruba. Until measured on real calls, the speech models decide the language and this is only logged.
    llm_language_switching: bool = False
    # SOFA never hangs up because it failed to help: the caller decides. The one exception is a line left silent, which
    # still costs us inbound minutes: after this many silent turns in a row (a gentle prompt each time) SOFA lets go.
    gateway_max_silences: int = 2  # one "are you still there?", then goodbye: an open line costs money (NGN 5 a minute incoming)
    max_call_seconds: int = 480    # no call runs longer than this: SOFA says goodbye at the next turn after it

    # Verifying a caller before a protected (banking) action. The PIN and the one-time code are typed on the keypad, never spoken.
    verify_reverify_seconds: int = 180        # ask for the PIN again after this long
    verify_random_rate: float = 0.15          # chance of an extra PIN check on any protected action
    verify_max_attempts: int = 3              # wrong PINs/codes in a row before the link is locked
    verify_lockout_minutes: int = 15
    verify_pin_length: int = 4
    verify_otp_length: int = 6
    verify_otp_ttl_seconds: int = 300
    digits_timeout_seconds: int = 20          # how long to wait for keypad digits
    mock_bank_pin: str = "1234"               # development only: the PIN the mock bank accepts
    mock_bank_transfer_limit_naira: int = 100_000  # development only: the mock bank's limit per transfer or bill
    mock_bank_airtime_limit_naira: int = 10_000    # development only: the mock bank's limit per airtime purchase

    # Banks a caller can open an account with by phone (their onboarding API is connected). Comma-separated bank codes.
    onboarding_banks: str = "demobank"
    # Which services a caller can reach. commerce,lookup is shopping and search; listing banking too adds it. A service that is switched off is politely declined, and its tools are not even registered.
    services_enabled: str = "commerce,lookup"
    bank_backend: str = "mock"  # "mock": the built-in bank. "demobank": a bank's own API (see gateway/bankapi.py)
    min_age_user: int = 12     # the youngest person Sofa serves at all
    banking_min_age: int = 16  # the youngest person who may use banking (checked on the date of birth typed on the keypad)

    admin_token: str = "dev-admin-token"
    demo_page_enabled: bool = False  # the public live page (/demo): shows what callers said, so keep it off except on a demo deployment
    overview_enabled: bool = True  # the overview pages (/overview): the story, the technology, the use cases and what testing the models showed
    hub_contact_email: str = ""  # what differs from one deployment to the next is set here; a block with nothing set is left out of the pages
    hub_test_number: str = ""
    hub_story_video_url: str = ""
    hub_demo_video_url: str = ""
    hub_audio_url: str = ""
    hub_deck_url: str = ""
    hub_cost_note: str = ""
    vendor_merchant: str = "CI Store"  # which shop the /vendor portal runs
    admin_email: str = ""  # where anything SOFA emails to its owner goes (nothing is emailed yet); set ADMIN_EMAIL in .env
    provider_bank: str = "demobank"  # which bank the /provider desk speaks for
    provider_token: str = "dev-provider-token"  # the bank desk portal (/provider): demo password, change it before the internet can reach this
    vendor_token: str = "dev-vendor-token"      # the shop owner portal (/vendor): same
    quiet_hours: str = "21:00-07:00"
    test_caller_numbers: str = "+2348000000001"
    scheduler_enabled: bool = True  # background jobs: the 7 PM merchant report
    scheduler_interval_seconds: int = 60
    debug_headers: bool = True  # X-Sofa-* headers used by the call simulator

    # Outbound status calls (payment received, dispatched, delivered). Calls to customers cost more than incoming ones
    # (NGN 15-20 a minute), so the number of tries is small and one customer is never called more than a few times a day.
    outbound_calls_enabled: bool = True
    outbound_max_attempts: int = 2
    outbound_retry_minutes: int = 20
    outbound_ring_minutes: int = 3       # a call still unanswered after this long counts as a miss
    outbound_expire_hours: int = 12      # a status call older than this is no longer worth making
    outbound_max_per_customer_per_day: int = 3
    late_reply_expire_minutes: int = 30   # an answer that came after the call moved on is worth a call back for this long
    late_reply_max_attempts: int = 2
    late_reply_retry_minutes: int = 3
    late_reply_pin_attempts: int = 3      # wrong PINs on the call back before SOFA stops and asks the caller to phone in

    # Dialogue tuning
    asr_min_confidence: float = -1.3  # avg log-prob; below this the transcript is treated as unheard
    filler_after_seconds: float = 2.0
    tts_budget_seconds: float = 4.0  # a model-worded sentence that is not spoken within this long is replaced by its stored template wording
    filler_every_seconds: float = 6.0  # a slow reply: another holding message every this many seconds, so the caller never sits in silence
    record_timeout_seconds: int = 2
    record_max_seconds: int = 15
    match_score_floor: float = 0.3
    match_winner_gap: float = 0.12
    max_calls_per_hour: int = 20
    stock_change_web_confirm_pct: int = 50

    # Yoruba starts on templates (N-ATLaS Yoruba generation is weakest)
    reply_mode_en: str = "generated"
    reply_mode_ha: str = "generated"
    reply_mode_ig: str = "generated"
    reply_mode_yo: str = "template"

    def reply_mode(self, lang: str) -> str:
        return getattr(self, f"reply_mode_{lang}", "template")

    @property
    def languages(self) -> tuple[str, ...]:
        """Enabled languages in menu order (press 1, 2, ...). English is always on: it is the fallback voice."""
        wanted = [l.strip() for l in self.enabled_languages.split(",") if l.strip() in LANGUAGES]
        ordered = ["en"] + [l for l in wanted if l != "en"]
        return tuple(dict.fromkeys(ordered))

    @property
    def test_numbers(self) -> set[str]:
        return {n.strip() for n in self.test_caller_numbers.split(",") if n.strip()}

    @property
    def sms_is_mock(self) -> bool:
        return not self.at_api_key

    @property
    def paystack_is_mock(self) -> bool:
        return not self.paystack_secret_key


@lru_cache
def get_settings() -> Settings:
    return Settings()
