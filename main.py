from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse
from contextlib import asynccontextmanager
from app.database import database
from app.migrations import run_migrations
from app.routers import auth, transactions, cards, kyc, admin, payments, payme
import os, time
from collections import defaultdict, deque

@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    await run_migrations()
    yield
    await database.disconnect()

app = FastAPI(
    title="Oson Pay API",
    version="2.0.0",
    lifespan=lifespan,
    docs_url="/api/docs" if os.getenv("NODE_ENV") != "production" else None,
    redoc_url=None
)

def _cors_origins() -> list[str]:
    raw = os.getenv("CORS_ORIGINS", "")
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if origins:
        return origins
    if os.getenv("NODE_ENV") == "production":
        return []
    return ["*"]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins(),
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

_ip_hits: dict[str, deque[float]] = defaultdict(deque)
GLOBAL_RATE_LIMIT = int(os.getenv("GLOBAL_RATE_LIMIT_PER_MINUTE", "180"))
MAX_BODY_BYTES = int(os.getenv("MAX_REQUEST_BODY_BYTES", "1048576"))


def _client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _admin_allowed_ips() -> set[str]:
    raw = os.getenv("ADMIN_ALLOWED_IPS", "")
    return {ip.strip() for ip in raw.split(",") if ip.strip()}


def _admin_panel_allowed(request: Request) -> bool:
    allowed_ips = _admin_allowed_ips()
    if allowed_ips and _client_ip(request) in allowed_ips:
        return True

    panel_key = os.getenv("ADMIN_PANEL_KEY", "")
    if panel_key:
        supplied_key = request.query_params.get("key") or request.cookies.get("admin_panel_key")
        return supplied_key == panel_key

    return os.getenv("NODE_ENV") != "production"


@app.middleware("http")
async def request_guard(request: Request, call_next):
    if request.url.path not in ("/", "/api/health"):
        now = time.time()
        ip = _client_ip(request)
        hits = _ip_hits[ip]
        while hits and hits[0] < now - 60:
            hits.popleft()
        if len(hits) >= GLOBAL_RATE_LIMIT:
            return JSONResponse(
                status_code=429,
                content={"success": False, "error": "Juda ko'p so'rov. Keyinroq urinib ko'ring."},
            )
        hits.append(now)

    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > MAX_BODY_BYTES:
        return JSONResponse(
            status_code=413,
            content={"success": False, "error": "So'rov hajmi juda katta"},
        )

    return await call_next(request)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    # OPTIONS preflight — CORS headerlarini buzmaslik uchun o'tkazib yuboramiz
    if request.method == "OPTIONS":
        return response
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Request-Time"] = str(round(time.time() - start, 4))
    return response

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if hasattr(exc, "status_code"):
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "error": str(exc.detail)}
        )
    print(f"Unexpected error: {exc}")
    return JSONResponse(
        status_code=500,
        content={"success": False, "error": "Ichki server xatosi"}
    )

app.include_router(auth.router,         prefix="/api/auth",        tags=["auth"])
app.include_router(transactions.router, prefix="/api/transactions", tags=["transactions"])
app.include_router(cards.router,        prefix="/api/cards",        tags=["cards"])
app.include_router(kyc.router,          prefix="/api/kyc",          tags=["kyc"])
app.include_router(admin.router,        prefix="/api/admin",        tags=["admin"])
app.include_router(payments.router,     prefix="/api/payments",     tags=["payments"])
app.include_router(payme.router,        prefix="/api",              tags=["provider"])

@app.get("/")
async def root():
    return {"status": "ok", "app": "Oson Pay", "version": "2.0.0"}

@app.get("/admin", include_in_schema=False)
async def admin_panel(request: Request):
    if not _admin_panel_allowed(request):
        return JSONResponse(
            status_code=404,
            content={"success": False, "error": "Topilmadi"},
        )
    response = FileResponse(os.path.join(os.path.dirname(__file__), "admin_panel.html"))
    panel_key = os.getenv("ADMIN_PANEL_KEY", "")
    if panel_key and request.query_params.get("key") == panel_key:
        response.set_cookie(
            "admin_panel_key",
            panel_key,
            httponly=True,
            secure=os.getenv("NODE_ENV") == "production",
            samesite="strict",
            max_age=60 * 60 * 8,
        )
    return response

@app.get("/api/health")
async def health():
    try:
        await database.fetch_one("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "OK" if db_ok else "DB_ERROR", "database": db_ok, "version": "2.0.0"}
