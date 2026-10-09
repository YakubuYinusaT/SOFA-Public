import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import HTTPException

from .config import Settings, get_settings
from .container import build_services
from .db import init_db, make_engine, make_session_factory
from .routes import admin, demo, hub, overview, pages, provider, vendor, voice, webhooks
from .web.auth import LoginRequired

ATTRIBUTION = (
    "N-ATLaS is an initiative of the Federal Ministry of Communications, Innovation and Digital "
    "Economy, and powered by Awarri Technologies."
)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=logging.INFO)
    engine = make_engine(settings.database_url)
    init_db(engine)
    svc = build_services(settings, make_session_factory(engine))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        from .services import scheduler

        task = asyncio.create_task(scheduler.loop(svc)) if settings.scheduler_enabled else None
        yield
        if task:
            task.cancel()
        engine.dispose()

    from .web.ui import profile

    # The generated API reference is opened only for the full build.
    open_docs = profile(settings) == "all"
    app = FastAPI(title="SOFA", lifespan=lifespan, docs_url="/docs" if open_docs else None, redoc_url=None, openapi_url="/openapi.json" if open_docs else None)
    app.state.svc = svc
    app.state.engine = engine
    app.include_router(voice.router)
    app.include_router(webhooks.router)
    app.include_router(admin.router)
    app.include_router(pages.router)
    app.include_router(provider.router)
    app.include_router(vendor.router)
    app.include_router(demo.router)
    app.include_router(overview.router)
    app.include_router(hub.router)
    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return RedirectResponse("/static/ci-mark-red.png", status_code=307)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "web" / "static")), name="static")

    @app.exception_handler(LoginRequired)
    async def login_required(request, exc: LoginRequired):
        from urllib.parse import quote

        return RedirectResponse(f"{exc.login_url}?next={quote(exc.next_url)}", status_code=303)

    @app.get("/health")
    def health():
        return {"ok": True, "sms": "mock" if settings.sms_is_mock else "live",
                "paystack": "mock" if settings.paystack_is_mock else "live",
                "asr": settings.asr_url, "llm": settings.llm_url, "tts": settings.tts_url}

    @app.get("/audio/{name}")
    def audio(name: str):
        path = Path(settings.storage_dir) / "audio_out" / Path(name).name
        if not path.exists():
            raise HTTPException(status_code=404)
        return FileResponse(path, media_type="audio/wav")

    return app

