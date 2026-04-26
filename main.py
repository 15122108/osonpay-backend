from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from contextlib import asynccontextmanager
from app.database import database
from app.migrations import run_migrations
from app.routers import auth, transactions, cards, kyc, admin, payments, payme
import os, time

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

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    allow_credentials=False,
)

@app.middleware("http")
async def security_headers(request: Request, call_next):
    start = time.time()
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["X-Request-Time"] = str(round(time.time() - start, 4))
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
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
app.include_router(payme.router,        prefix="/api/payments",     tags=["payme"])

@app.get("/")
async def root():
    return {"status": "ok", "app": "Oson Pay", "version": "2.0.0"}

@app.get("/api/health")
async def health():
    try:
        await database.fetch_one("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False
    return {"status": "OK" if db_ok else "DB_ERROR", "database": db_ok, "version": "2.0.0"}