from dataclasses import dataclass, field

from .audio import AudioService
from .clients.asr import ASRClient
from .clients.llm import LLMClient
from .clients.paystack import PaystackClient
from .clients.search import build_search
from .clients.sms import SMSClient
from .clients.tts import TTSClient
from .clients.voice import VoiceClient
from .config import Settings
from .gateway.bankauth import MockBankAuth
from .gateway.hub import Gateway, build_gateway
from .gateway.bankapi import build_bank
from .gateway.policy import VerificationPolicy
from .session import make_store


@dataclass
class Services:
    settings: Settings
    session_factory: object
    sessions: object
    asr: ASRClient
    llm: LLMClient
    tts: TTSClient
    sms: SMSClient
    paystack: PaystackClient
    voice: VoiceClient
    audio: AudioService
    turn_tasks: dict = field(default_factory=dict)  # turn_id -> asyncio.Task (single-process; use Redis for multi-worker)
    gateway: Gateway = field(default_factory=build_gateway)  # registered service adapters, tool registry, verification policy
    bankauth: object = None  # checks PINs and one-time codes (the bank in production, a mock until then)
    bank: object = None      # the bank's own operations: balance, transfer, ... (the built-in bank unless another is configured)
    search: object = None    # where information questions are looked up (Google when its keys are set)


def build_services(settings: Settings, session_factory) -> Services:
    tts = TTSClient(settings.tts_url, settings.gpu_api_key)
    sms = SMSClient(settings)
    gateway = build_gateway(settings)
    gateway.policy = VerificationPolicy(settings)
    return Services(
        settings=settings,
        session_factory=session_factory,
        sessions=make_store(settings.redis_url),
        asr=ASRClient(settings.asr_url, settings.gpu_api_key),
        llm=LLMClient(settings.llm_url, settings.llm_model, settings.llm_api_key or settings.gpu_api_key, settings.llm_structured_mode, settings.llm_provider),
        tts=tts,
        sms=sms,
        paystack=PaystackClient(settings),
        voice=VoiceClient(settings),
        audio=AudioService(settings, tts),
        gateway=gateway,
        bankauth=MockBankAuth(settings, sms),
        bank=build_bank(settings),
        search=build_search(settings),
    )
