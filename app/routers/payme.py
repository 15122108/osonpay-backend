import base64
import time
import uuid
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.database import database
from app.utils import audit
import os

router = APIRouter()

PAYME_KEY = os.getenv("PAYME_KEY")

ERR_INVALID_AMOUNT   = -31001
ERR_INVALID_ACCOUNT  = -31050
ERR_TX_NOT_FOUND     = -31003
ERR_CANT_PERFORM     = -31008
ERR_ALREADY_DONE     = -31060
ERR_METHOD_NOT_FOUND = -32601


def check_auth(request: Request) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        login, password = decoded.split(":", 1)
        return password == PAYME_KEY
    except Exception:
        return False


def json_response(data: dict) -> JSONResponse:
    return JSONResponse(data)


def ok(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def err(request_id, code: int, message: str) -> dict:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": {"uz": message, "ru": message, "en": message}
        }
    }


async def get_user_by_order(order_id) -> dict | None:
    order_str = str(order_id)

    # Telefon raqami orqali qidirish
    user = await database.fetch_one(
        "SELECT id FROM users WHERE phone=:oid",
        {"oid": order_str}
    )
    if user:
        return user

    # UUID orqali qidirish
    try:
        uuid.UUID(order_str)
        user = await database.fetch_one(
            "SELECT id FROM users WHERE id=:oid::uuid",
            {"oid": order_str}
        )
        return user
    except ValueError:
        return None


@router.post("/payme")
async def payme_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return json_response(err(None, -32700, "JSON xato"))

    req_id = body.get("id")

    if not check_auth(request):
        await audit.log("payme_auth_failed", details={"id": req_id})
        return json_response(err(req_id, -32504, "Autentifikatsiya xatosi"))

    method = body.get("method")
    params = body.get("params", {})

    handlers = {
        "CheckPerformTransaction": check_perform,
        "CreateTransaction":       create_transaction,
        "PerformTransaction":      perform_transaction,
        "CheckTransaction":        check_transaction,
        "CancelTransaction":       cancel_transaction,
        "GetStatement":            get_statement,
    }

    handler = handlers.get(method)
    if not handler:
        return json_response(err(req_id, ERR_METHOD_NOT_FOUND, "Method topilmadi"))

    try:
        result = await handler(req_id, params)
    except Exception as e:
        print(f"[Payme] {method} xatosi: {e}")
        await audit.log("payme_error", details={"method": method, "error": str(e)})
        return json_response(err(req_id, -32400, "Ichki xato"))

    return json_response(result)


async def check_perform(req_id, params):
    amount   = params.get("amount", 0)
    account  = params.get("account", {})
    order_id = account.get("order_id")

    if not isinstance(amount, (int, float)) or not (100_000 <= amount <= 5_000_000_000):
        return err(req_id, ERR_INVALID_AMOUNT, "Summa xato (1,000 — 50,000,000 UZS)")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "order_id yo'q")

    user = await get_user_by_order(order_id)
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

    # Wallet muzlatilganligini tekshirish
    wallet = await database.fetch_one(
        "SELECT is_frozen FROM wallets WHERE user_id=:uid",
        {"uid": str(user["id"])}
    )
    if wallet and wallet["is_frozen"]:
        return err(req_id, ERR_CANT_PERFORM, "Foydalanuvchi hisobi muzlatilgan")

    return ok(req_id, {"allow": True})


async def create_transaction(req_id, params):
    payme_tx_id = params.get("id")
    amount      = params.get("amount", 0)
    account     = params.get("account", {})
    order_id    = account.get("order_id")
    create_time = params.get("time", int(time.time() * 1000))

    if not isinstance(amount, (int, float)) or not (100_000 <= amount <= 5_000_000_000):
        return err(req_id, ERR_INVALID_AMOUNT, "Summa xato")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "order_id yo'q")

    user = await get_user_by_order(order_id)
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

    # Mavjud tranzaksiyani tekshirish
    existing = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id=:pid",
        {"pid": payme_tx_id}
    )
    if existing:
        if existing["state"] != 1:
            return err(req_id, ERR_CANT_PERFORM, "Tranzaksiya holati xato")
        return ok(req_id, {
            "create_time": existing["create_time"],
            "transaction": str(existing["id"]),
            "state": 1
        })

    tx = await database.fetch_one(
        """INSERT INTO payme_transactions
           (payme_id, user_id, amount, state, create_time)
           VALUES (:pid, :uid::uuid, :amt, 1, :ct)
           RETURNING *""",
        {"pid": payme_tx_id, "uid": str(user["id"]), "amt": amount, "ct": create_time}
    )

    await audit.log(
        "payme_tx_created",
        user_id=str(user["id"]),
        details={"payme_id": payme_tx_id, "amount": amount}
    )

    return ok(req_id, {
        "create_time": create_time,
        "transaction": str(tx["id"]),
        "state": 1
    })


async def perform_transaction(req_id, params):
    payme_tx_id = params.get("id")

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id=:pid",
        {"pid": payme_tx_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    if tx["state"] == 2:
        return ok(req_id, {
            "transaction": str(tx["id"]),
            "perform_time": tx["perform_time"],
            "state": 2
        })

    if tx["state"] != 1:
        return err(req_id, ERR_CANT_PERFORM, "Tranzaksiya holati xato")

    perform_time = int(time.time() * 1000)
    amount_uzs   = tx["amount"] / 100
    user_id_str  = str(tx["user_id"])

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid::uuid",
            {"a": amount_uzs, "uid": user_id_str}
        )
        ledger_tx = await database.fetch_one(
            """INSERT INTO transactions
               (receiver_id, amount, type, status, description, reference)
               VALUES (:uid::uuid, :a, 'topup', 'completed', 'Payme orqali toldirish', :ref)
               RETURNING id""",
            {"uid": user_id_str, "a": amount_uzs, "ref": payme_tx_id}
        )
        await database.execute(
            "UPDATE payme_transactions SET state=2, perform_time=:pt WHERE payme_id=:pid",
            {"pt": perform_time, "pid": payme_tx_id}
        )

    await audit.log(
        "payme_tx_performed",
        user_id=user_id_str,
        entity_type="transaction",
        entity_id=str(ledger_tx["id"]),
        details={"payme_id": payme_tx_id, "amount_uzs": amount_uzs}
    )

    # Push notification
    try:
        from app.services.fcm import get_user_tokens, send_push
        tokens = await get_user_tokens(database, user_id_str)
        if tokens:
            amt_fmt = f"{amount_uzs:,.0f}".replace(",", " ")
            await send_push(
                tokens,
                title="Hisob to'ldirildi ✅",
                body=f"Payme orqali {amt_fmt} UZS tushdi",
                data={"type": "topup", "reference": payme_tx_id}
            )
    except Exception as e:
        print(f"[Push] Xato: {e}")

    return ok(req_id, {
        "transaction":  str(tx["id"]),
        "perform_time": perform_time,
        "state": 2
    })


async def check_transaction(req_id, params):
    payme_tx_id = params.get("id")

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id=:pid",
        {"pid": payme_tx_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    return ok(req_id, {
        "create_time":  tx["create_time"],
        "perform_time": tx["perform_time"] or 0,
        "cancel_time":  tx["cancel_time"] or 0,
        "transaction":  str(tx["id"]),
        "state":        tx["state"],
        "reason":       tx["reason"],
    })


async def cancel_transaction(req_id, params):
    payme_tx_id = params.get("id")
    reason      = params.get("reason", 1)

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id=:pid",
        {"pid": payme_tx_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    if tx["state"] == -1:
        return ok(req_id, {
            "transaction": str(tx["id"]),
            "cancel_time": tx["cancel_time"],
            "state": -1
        })

    if tx["state"] == 2:
        return err(req_id, ERR_ALREADY_DONE, "To'lov allaqachon amalga oshirilgan")

    cancel_time = int(time.time() * 1000)
    await database.execute(
        "UPDATE payme_transactions SET state=-1, cancel_time=:ct, reason=:r WHERE payme_id=:pid",
        {"ct": cancel_time, "r": reason, "pid": payme_tx_id}
    )

    await audit.log(
        "payme_tx_cancelled",
        user_id=str(tx["user_id"]),
        details={"payme_id": payme_tx_id, "reason": reason}
    )

    return ok(req_id, {
        "transaction": str(tx["id"]),
        "cancel_time": cancel_time,
        "state": -1
    })


async def get_statement(req_id, params):
    from_time = params.get("from", 0)
    to_time   = params.get("to", int(time.time() * 1000))

    rows = await database.fetch_all(
        """SELECT * FROM payme_transactions
           WHERE create_time>=:f AND create_time<=:t
           ORDER BY create_time ASC""",
        {"f": from_time, "t": to_time}
    )

    return ok(req_id, {
        "transactions": [
            {
                "id":           tx["payme_id"],
                "time":         tx["create_time"],
                "amount":       tx["amount"],
                "account":      {"order_id": str(tx["user_id"])},
                "create_time":  tx["create_time"],
                "perform_time": tx["perform_time"] or 0,
                "cancel_time":  tx["cancel_time"] or 0,
                "transaction":  str(tx["id"]),
                "state":        tx["state"],
                "reason":       tx["reason"],
            }
            for tx in rows
        ]
    })