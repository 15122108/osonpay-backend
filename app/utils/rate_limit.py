from fastapi import Request, HTTPException
from app.database import database
from datetime import datetime, timedelta

LIMITS = {
    "otp_send":   {"max": 3,  "window": 300},
    "otp_verify": {"max": 5,  "window": 300},
    "pin_verify": {"max": 5,  "window": 300},
    "send_money": {"max": 20, "window": 60},
    "default":    {"max": 60, "window": 60},
}

async def check_rate_limit(key: str, limit_type: str = "default", identifier: str = "") -> None:
    cfg = LIMITS.get(limit_type, LIMITS["default"])
    full_key = f"{limit_type}:{identifier or key}"
    window_start = datetime.utcnow() - timedelta(seconds=cfg["window"])

    row = await database.fetch_one(
        "SELECT count, window_start FROM rate_limits WHERE key=:k",
        {"k": full_key}
    )

    if row is None:
        await database.execute(
            "INSERT INTO rate_limits(key, count, window_start) VALUES(:k, 1, NOW()) "
            "ON CONFLICT(key) DO UPDATE SET count=1, window_start=NOW()",
            {"k": full_key}
        )
        return

    if row["window_start"] < window_start:
        await database.execute(
            "UPDATE rate_limits SET count=1, window_start=NOW() WHERE key=:k",
            {"k": full_key}
        )
        return

    if row["count"] >= cfg["max"]:
        raise HTTPException(
            status_code=429,
            detail=f"Juda ko'p urinish. {cfg['window']} soniyadan so'ng qayta urining."
        )

    await database.execute(
        "UPDATE rate_limits SET count=count+1 WHERE key=:k",
        {"k": full_key}
    )

def get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
