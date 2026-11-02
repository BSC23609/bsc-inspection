"""Bharat Steel Group — one sign-in, every app."""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from .database import Base, SessionLocal, engine
from .routes import admin, auth, home
from .seed import bootstrap
from .web import RedirectTo

BASE = Path(__file__).parent


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        bootstrap(db)
    finally:
        db.close()


STARTUP_ERROR: str | None = None


def _mask(text: str) -> str:
    """Hide any password that appears in a connection string inside an error message."""
    import re
    return re.sub(r"(://[^:/@\s]+:)[^@\s]+@", r"\1****@", text)


def try_init() -> None:
    """Create tables and seed. Never crashes the app: the reason is kept and shown on /health."""
    global STARTUP_ERROR
    try:
        init_db()
        STARTUP_ERROR = None
    except Exception as exc:  # noqa: BLE001
        import traceback
        STARTUP_ERROR = _mask(f"{type(exc).__name__}: {exc}\n\n{traceback.format_exc(limit=3)}")
        print("[startup] database init failed:", STARTUP_ERROR)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try_init()
    yield


app = FastAPI(title="Bharat Steel Group", lifespan=lifespan, docs_url=None, redoc_url=None)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")


@app.exception_handler(RedirectTo)
async def _redirect_to(_request: Request, exc: RedirectTo):
    return RedirectResponse(exc.url, status_code=303)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    if STARTUP_ERROR and not request.url.path.startswith("/static"):
        try_init()  # retry, e.g. after the database was woken or the setting fixed
        if STARTUP_ERROR:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("The app could not reach its database.\n\n" + STARTUP_ERROR, status_code=503)
    response = await call_next(request)
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")  # hand-off tokens never leak via Referer
    if not request.url.path.startswith("/static"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response


app.include_router(auth.router)
app.include_router(home.router)
app.include_router(admin.router)
