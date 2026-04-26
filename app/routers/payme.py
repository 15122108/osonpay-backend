import base64
import time
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from app.database import database
import os

router = APIRouter()

PAYME_KEY = os.getenv("PAYME_KEY")

ERR_INVALID_AMOUNT   = -31001
ERR_INVALID_ACCOUNT  = -31050
ERR_TX_NOT_FOUND     = -31003
ERR_CANT_PERFORM     = -31008
ERR_ALREADY_DONE     = -31060
ERR_METHOD_NOT_FOUND = -32601


def check_auth(request: Request):
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
    response = JSONResponse(data)
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
    return response


def ok(request_id, result):
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def err(request_id, code, message):
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {
            "code": code,
            "message": {"uz": message, "ru": message, "en": message}
        }
    }


@router.options("/payme")
async def payme_options():
    return Response(
        status_code=200,
        headers={
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, OPTIONS",
            "Access-Control-Allow-Headers": "*",
        }
    )


@router.post("/payme")
async def payme_webhook(request: Request):
    try:
        body = await request.json()
    except Exception:
        return json_response(err(None, -32700, "JSON xato"))

    req_id = body.get("id")

    if not check_auth(request):
        return json_response(err(req_id, -32504, "Autentifikatsiya xatosi"))

    method = body.get("method")
    params = body.get("params", {})

    if method == "CheckPerformTransaction":
        result = await check_perform(req_id, params)
    elif method == "CreateTransaction":
        result = await create_transaction(req_id, params)
    elif method == "PerformTransaction":
        result = await perform_transaction(req_id, params)
    elif method == "CheckTransaction":
        result = await check_transaction(req_id, params)
    elif method == "CancelTransaction":
        result = await cancel_transaction(req_id, params)
    elif method == "GetStatement":
        result = await get_statement(req_id, params)
    else:
        result = err(req_id, ERR_METHOD_NOT_FOUND, "Method topilmadi")

    return json_response(result)


async def check_perform(req_id, params):
    amount = params.get("amount", 0)
    account = params.get("account", {})
    order_id = account.get("order_id")

    if not isinstance(amount, (int, float)) or amount <= 0:
        return err(req_id, ERR_INVALID_AMOUNT, "Summa xato")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "order_id yoq")

    user = await database.fetch_one(
        "SELECT id FROM users WHERE id::text=:oid OR phone=:oid",
        {"oid": str(order_id)}
    )
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

    return ok(req_id, {"allow": True})


async def create_transaction(req_id, params):
    payme_tx_id = params.get("id")
    amount = params.get("amount", 0)
    account = params.get("account", {})
    order_id = account.get("order_id")
    create_time = params.get("time", int(time.time() * 1000))

    if not isinstance(amount, (int, float)) or amount <= 0:
        return err(req_id, ERR_INVALID_AMOUNT, "Summa xato")

    if not order_id:
        return err(req_id, ERR_INVALID_ACCOUNT, "order_id yoq")

    user = await database.fetch_one(
        "SELECT id FROM users WHERE id::text=:oid OR phone=:oid",
        {"oid": str(order_id)}
    )
    if not user:
        return err(req_id, ERR_INVALID_ACCOUNT, "Foydalanuvchi topilmadi")

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
        "INSERT INTO payme_transactions (payme_id, user_id, amount, state, create_time) VALUES (:pid, :uid, :amt, 1, :ct) RETURNING *",
        {"pid": payme_tx_id, "uid": str(user["id"]), "amt": amount, "ct": create_time}
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
    amount_uzs = tx["amount"] / 100

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid",
            {"a": amount_uzs, "uid": str(tx["user_id"])}
        )
        await database.execute(
            "INSERT INTO transactions (receiver_id, amount, type, status, description, reference) VALUES (:uid, :a, 'topup', 'completed', 'Payme orqali toldirish', :ref)",
            {"uid": str(tx["user_id"]), "a": amount_uzs, "ref": payme_tx_id}
        )
        await database.execute(
            "UPDATE payme_transactions SET state=2, perform_time=:pt WHERE payme_id=:pid",
            {"pt": perform_time, "pid": payme_tx_id}
        )

    return ok(req_id, {
        "transaction": str(tx["id"]),
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
        "reason":       tx["reason"]
    })


async def cancel_transaction(req_id, params):
    payme_tx_id = params.get("id")
    reason = params.get("reason", 1)

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
        return err(req_id, ERR_ALREADY_DONE, "Tolov allaqachon amalga oshirilgan")

    cancel_time = int(time.time() * 1000)
    await database.execute(
        "UPDATE payme_transactions SET state=-1, cancel_time=:ct, reason=:r WHERE payme_id=:pid",
        {"ct": cancel_time, "r": reason, "pid": payme_tx_id}
    )

    return ok(req_id, {
        "transaction": str(tx["id"]),
        "cancel_time": cancel_time,
        "state": -1
    })


async def get_statement(req_id, params):
    from_time = params.get("from", 0)
    to_time = params.get("to", int(time.
[26.04.2026 13:11] Farhod: time() * 1000))

    rows = await database.fetch_all(
        "SELECT * FROM payme_transactions WHERE create_time>=:f AND create_time<=:t ORDER BY create_time ASC",
        {"f": from_time, "t": to_time}
    )

    transactions = []
    for tx in rows:
        transactions.append({
            "id": tx["payme_id"],
            "time": tx["create_time"],
            "amount": tx["amount"],
            "account": {"order_id": tx["user_id"]},
            "create_time": tx["create_time"],
            "perform_time": tx["perform_time"] or 0,
            "cancel_time": tx["cancel_time"] or 0,
            "transaction": str(tx["id"]),
            "state": tx["state"],
            "reason": tx["reason"]
        })

    return ok(req_id, {"transactions": transactions})