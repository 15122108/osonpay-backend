import base64
import time
import uuid
import traceback
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from app.database import database
from app.utils import audit
import os

router = APIRouter()

PAYME_KEY = os.getenv("PAYME_KEY", "")

ERR_INVALID_AMOUNT   = -31001
ERR_INVALID_ACCOUNT  = -31050
ERR_TX_NOT_FOUND     = -31003
ERR_CANT_PERFORM     = -31008
ERR_ALREADY_DONE     = -31060
ERR_METHOD_NOT_FOUND = -32601
ERR_AUTH             = -32504
ERR_INTERNAL         = -32400

MIN_AMOUNT = 100_000        # 1 000 UZS tiyinda
MAX_AMOUNT = 200_000_000    # 2 000 000 UZS tiyinda


# ─── Yordamchi funksiyalar ───────────────────────────────────────────────────

def ok(req_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def err(req_id, code: int, message: str) -> dict:
    msg = {"uz": message, "ru": message, "en": message}
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": msg}}


def check_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        _, password = decoded.split(":", 1)
        return password == PAYME_KEY
    except Exception:
        return False


async def find_user(order_id: str):
    """order_id bo'yicha foydalanuvchini topish."""
    s = str(order_id).strip()

    # 1) Telefon orqali (test: "123", real: "+998901234567")
    user = await database.fetch_one(
        "SELECT id, phone FROM users WHERE phone = :v AND is_active = TRUE",
        {"v": s}
    )
    if user:
        return user

    # 2) + prefiksi bilan
    normalized = s.lstrip("+")
    if normalized != s:
        user = await database.fetch_one(
            "SELECT id, phone FROM users WHERE phone = :v AND is_active = TRUE",
            {"v": normalized}
        )
        if user:
            return user

    # 3) UUID orqali
    try:
        uid = uuid.UUID(s)
        return await database.fetch_one(
            "SELECT id, phone FROM users WHERE id = :v AND is_active = TRUE",
            {"v": uid}
        )
    except ValueError:
        return None


# ─── Endpoints ──────────────────────────────────────────────────────────────

@router.options("/payme")
async def payme_options():
    r = Response(status_code=204)
    r.headers["Access-Control-Allow-Origin"]  = "*"
    r.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    r.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    return r


@router.get("/payme-version")
async def payme_version():
    return {"version": "3.0", "payme_key_set": bool(PAYME_KEY)}


@router.post("/payme")
async def payme_handler(request: Request):
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(err(None, -32700, "JSON parse xato"))

    req_id = body.get("id")

    if not check_auth(request):
        return JSONResponse(err(req_id, ERR_AUTH, "Autentifikatsiya xatosi"))

    method  = body.get("method", "")
    params  = body.get("params", {})

    METHODS = {
        "CheckPerformTransaction": handle_check_perform,
        "CreateTransaction":       handle_create,
        "PerformTransaction":      handle_perform,
        "CheckTransaction":        handle_check_tx,
        "CancelTransaction":       handle_cancel,
        "GetStatement":            handle_statement,
    }

    handler = METHODS.get(method)
    if not handler:
        return JSONResponse(err(req_id, ERR_METHOD_NOT_FOUND, "Noto'g'ri method"))

    try:
        result = await handler(req_id, params)
        return JSONResponse(result)
    except Exception as e:
        print(f"[Payme ERROR] {method}: {e}")
        print(traceback.format_exc())
        return JSONResponse(err(req_id, ERR_INTERNAL, f"Ichki xato: {e}"))


# ─── CheckPerformTransaction ─────────────────────────────────────────────────

async def handle_check_perform(req_id, params):
    amount   = params.get("amount", 0)
    order_id = params.get("account", {}).get("order_id")

    if not isinstance(amount, (int, float)) or not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        return err(req_id, ERR_INVALID_AMOUNT,
                   f"Summa {MIN_AMOUNT//100}–{MAX_AMOUNT//100} UZS oralig'ida bo'lishi kerak")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "account.order_id majburiy")

    user = await find_user(order_id)
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

    wallet = await database.fetch_one(
        "SELECT is_frozen FROM wallets WHERE user_id = :uid",
        {"uid": user["id"]}
    )
    if wallet and wallet["is_frozen"]:
        return err(req_id, ERR_CANT_PERFORM, "Hisob muzlatilgan")

    return ok(req_id, {"allow": True})


# ─── CreateTransaction ───────────────────────────────────────────────────────

async def handle_create(req_id, params):
    payme_id    = params.get("id")
    amount      = params.get("amount", 0)
    create_time = params.get("time", int(time.time() * 1000))
    order_id    = params.get("account", {}).get("order_id")

    if not isinstance(amount, (int, float)) or not (MIN_AMOUNT <= amount <= MAX_AMOUNT):
        return err(req_id, ERR_INVALID_AMOUNT, "Summa xato")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "account.order_id majburiy")

    user = await find_user(order_id)
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

    # Idempotentlik — mavjud tranzaksiyani tekshirish
    existing = await database.fetch_one(
        "SELECT state, create_time FROM payme_transactions WHERE payme_id = :pid",
        {"pid": payme_id}
    )
    if existing:
        if existing["state"] != 1:
            return err(req_id, ERR_CANT_PERFORM, "Tranzaksiya holati yaroqsiz")
        return ok(req_id, {
            "create_time": existing["create_time"],
            "transaction": payme_id,
            "state": 1,
        })

    await database.execute(
        """INSERT INTO payme_transactions (payme_id, user_id, amount, state, create_time)
           VALUES (:pid, :uid, :amt, 1, :ct)""",
        {"pid": payme_id, "uid": user["id"],
         "amt": int(amount), "ct": int(create_time)}
    )

    await audit.log("payme_tx_created", user_id=str(user["id"]),
                    details={"payme_id": payme_id, "amount": amount})

    return ok(req_id, {
        "create_time": int(create_time),
        "transaction": payme_id,
        "state": 1,
    })


# ─── PerformTransaction ──────────────────────────────────────────────────────

async def handle_perform(req_id, params):
    payme_id = params.get("id")

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id = :pid",
        {"pid": payme_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    if tx["state"] == 2:
        return ok(req_id, {
            "transaction":  payme_id,
            "perform_time": tx["perform_time"],
            "state": 2,
        })

    if tx["state"] != 1:
        return err(req_id, ERR_CANT_PERFORM, "Tranzaksiyani bajarib bo'lmaydi")

    perform_time = int(time.time() * 1000)
    amount_uzs   = tx["amount"] / 100
    user_id      = tx["user_id"]

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance = balance + :a, updated_at = NOW() WHERE user_id = :uid",
            {"a": amount_uzs, "uid": user_id}
        )
        await database.execute(
            """INSERT INTO transactions
               (receiver_id, amount, type, status, description, reference)
               VALUES (:uid, :a, 'topup', 'completed', 'Payme orqali toldirish', :ref)""",
            {"uid": user_id, "a": amount_uzs, "ref": payme_id}
        )
        await database.execute(
            "UPDATE payme_transactions SET state = 2, perform_time = :pt WHERE payme_id = :pid",
            {"pt": perform_time, "pid": payme_id}
        )

    await audit.log("payme_tx_performed", user_id=str(user_id),
                    details={"payme_id": payme_id, "amount_uzs": amount_uzs})

    try:
        from app.services.fcm import get_user_tokens, send_push
        tokens = await get_user_tokens(database, str(user_id))
        if tokens:
            amt_fmt = f"{amount_uzs:,.0f}".replace(",", " ")
            await send_push(tokens,
                title="Hisob to'ldirildi ✅",
                body=f"Payme orqali {amt_fmt} UZS tushdi",
                data={"type": "topup", "reference": payme_id})
    except Exception as e:
        print(f"[Push xato] {e}")

    return ok(req_id, {
        "transaction":  payme_id,
        "perform_time": perform_time,
        "state": 2,
    })


# ─── CheckTransaction ────────────────────────────────────────────────────────

async def handle_check_tx(req_id, params):
    payme_id = params.get("id")

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id = :pid",
        {"pid": payme_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    return ok(req_id, {
        "create_time":  tx["create_time"],
        "perform_time": tx["perform_time"] or 0,
        "cancel_time":  tx["cancel_time"] or 0,
        "transaction":  payme_id,
        "state":        tx["state"],
        "reason":       tx["reason"],
    })


# ─── CancelTransaction ───────────────────────────────────────────────────────

async def handle_cancel(req_id, params):
    payme_id = params.get("id")
    reason   = params.get("reason", 1)

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id = :pid",
        {"pid": payme_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    if tx["state"] == -1:
        return ok(req_id, {
            "transaction": payme_id,
            "cancel_time": tx["cancel_time"],
            "state": -1,
        })

    if tx["state"] == 2:
        return err(req_id, ERR_ALREADY_DONE, "To'lov allaqachon amalga oshirilgan")

    cancel_time = int(time.time() * 1000)
    await database.execute(
        "UPDATE payme_transactions SET state = -1, cancel_time = :ct, reason = :r WHERE payme_id = :pid",
        {"ct": cancel_time, "r": reason, "pid": payme_id}
    )

    await audit.log("payme_tx_cancelled", user_id=str(tx["user_id"]),
                    details={"payme_id": payme_id, "reason": reason})

    return ok(req_id, {
        "transaction": payme_id,
        "cancel_time": cancel_time,
        "state": -1,
    })


# ─── GetStatement ────────────────────────────────────────────────────────────

async def handle_statement(req_id, params):
    from_time = int(params.get("from", 0))
    to_time   = int(params.get("to", time.time() * 1000))

    rows = await database.fetch_all(
        """SELECT pt.payme_id, pt.amount, pt.state, pt.reason,
                  pt.create_time, pt.perform_time, pt.cancel_time,
                  u.phone
           FROM payme_transactions pt
           LEFT JOIN users u ON u.id = pt.user_id
           WHERE pt.create_time >= :f AND pt.create_time <= :t
           ORDER BY pt.create_time ASC""",
        {"f": from_time, "t": to_time}
    )

    transactions = []
    for row in rows:
        transactions.append({
            "id":           row["payme_id"],
            "time":         row["create_time"],
            "amount":       row["amount"],
            "account":      {"id": row["phone"] or ""},
            "create_time":  row["create_time"],
            "perform_time": row["perform_time"] or 0,
            "cancel_time":  row["cancel_time"] or 0,
            "transaction":  row["payme_id"],
            "state":        row["state"],
            "reason":       row["reason"],
        })

    return ok(req_id, {"transactions": transactions})
