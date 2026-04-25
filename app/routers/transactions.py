from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from typing import Optional
from app.database import database
from app.utils.deps import get_user
from app.utils.auth import gen_ref
from app.utils.rate_limit import check_rate_limit, get_client_ip
from app.utils import audit
from app.services.fcm import notify_transaction
from app.services.ai_fraud import check_transaction

router = APIRouter()

class SendReq(BaseModel):
    receiverPhone: str
    amount: float
    description: Optional[str] = None

class TopUpReq(BaseModel):
    amount: float
    description: Optional[str] = None

class FCMTokenReq(BaseModel):
    token: str
    platform: str = "android"


@router.post("/fcm-token")
async def save_fcm_token(b: FCMTokenReq, uid: str = Depends(get_user)):
    await database.execute(
        """INSERT INTO fcm_tokens(user_id, token, platform)
           VALUES(:uid, :token, :platform)
           ON CONFLICT(user_id, token) DO NOTHING""",
        {"uid": uid, "token": b.token, "platform": b.platform}
    )
    return {"success": True}


@router.post("/send")
async def send(b: SendReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)
    await check_rate_limit("send_money", "send_money", uid)

    if b.amount < 1000:
        raise HTTPException(400, "Minimum 1,000 UZS")
    if b.amount > 50_000_000:
        raise HTTPException(400, "Maksimum 50,000,000 UZS")

    sender = await database.fetch_one(
        "SELECT u.full_name, w.balance, w.is_frozen FROM users u "
        "JOIN wallets w ON w.user_id=u.id WHERE u.id=:id",
        {"id": uid}
    )
    if not sender:
        raise HTTPException(404, "Foydalanuvchi topilmadi")
    if sender["is_frozen"]:
        raise HTTPException(403, "Hisobingiz muzlatilgan")
    if float(sender["balance"]) < b.amount:
        raise HTTPException(400, "Mablag' yetarli emas")

    rec = await database.fetch_one(
        "SELECT u.id, u.full_name, w.is_frozen FROM users u "
        "JOIN wallets w ON w.user_id=u.id WHERE u.phone=:p AND u.is_active=TRUE",
        {"p": b.receiverPhone}
    )
    if not rec:
        raise HTTPException(400, "Qabul qiluvchi topilmadi")
    if str(rec["id"]) == uid:
        raise HTTPException(400, "O'zingizga yuborib bo'lmaydi")
    if rec["is_frozen"]:
        raise HTTPException(400, "Qabul qiluvchi hisobi muzlatilgan")

    fraud = await check_transaction(
        sender_id=uid,
        receiver_id=str(rec["id"]),
        amount=b.amount,
        description=b.description or "",
    )
    if fraud["blocked"]:
        await audit.log(
            action="transaction_blocked",
            user_id=uid,
            details={"amount": b.amount, "reason": fraud["reason"], "risk": fraud["risk"]},
            ip_address=ip
        )
        raise HTTPException(403, f"Tranzaksiya xavfsizlik tizimi tomonidan bloklandi: {fraud['reason']}")

    ref = gen_ref()

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance-:a, updated_at=NOW() WHERE user_id=:id",
            {"a": b.amount, "id": uid}
        )
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:id",
            {"a": b.amount, "id": str(rec["id"])}
        )
        tx = await database.fetch_one(
            """INSERT INTO transactions
               (sender_id, receiver_id, amount, type, status, description, reference)
               VALUES (:s, :r, :a, 'send', 'completed', :d, :ref)
               RETURNING *""",
            {"s": uid, "r": str(rec["id"]), "a": b.amount,
             "d": b.description or "Pul o'tkazma", "ref": ref}
        )

    await audit.log(
        action="money_sent", user_id=uid,
        entity_type="transaction", entity_id=str(tx["id"]),
        details={"amount": b.amount, "receiver": b.receiverPhone,
                 "ref": ref, "fraud_risk": fraud["risk"]},
        ip_address=ip
    )

    await notify_transaction(
        database,
        receiver_id=str(rec["id"]),
[25.04.2026 23:25] Farhod: sender_name=sender["full_name"] or "Foydalanuvchi",
        amount=b.amount,
        ref=ref
    )

    return {"success": True, "transaction": dict(tx), "fraud_risk": fraud["risk"]}


@router.post("/topup")
async def topup(b: TopUpReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)
    if b.amount < 1000:
        raise HTTPException(400, "Minimal 1,000 UZS")
    if b.amount > 100_000_000:
        raise HTTPException(400, "Maksimal 100,000,000 UZS")

    wallet = await database.fetch_one(
        "SELECT is_frozen FROM wallets WHERE user_id=:uid", {"uid": uid}
    )
    if wallet and wallet["is_frozen"]:
        raise HTTPException(403, "Hisobingiz muzlatilgan")

    ref = gen_ref()
    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:id",
            {"a": b.amount, "id": uid}
        )
        tx = await database.fetch_one(
            """INSERT INTO transactions
               (receiver_id, amount, type, status, description, reference)
               VALUES (:u, :a, 'topup', 'completed', :d, :ref)
               RETURNING *""",
            {"u": uid, "a": b.amount,
             "d": b.description or "To'ldirish", "ref": ref}
        )

    await audit.log(
        action="wallet_topup", user_id=uid,
        entity_type="transaction", entity_id=str(tx["id"]),
        details={"amount": b.amount, "ref": ref},
        ip_address=ip
    )
    return {"success": True, "transaction": dict(tx)}


@router.get("")
async def history(
    page: int = 1,
    limit: int = 20,
    type: Optional[str] = None,
    uid: str = Depends(get_user)
):
   if limit > 100:
           limit = 100