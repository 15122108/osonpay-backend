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
from app.services.commission import credit_commission

router = APIRouter()
COMMISSION_RATE = 0.01
HOME_SERVICES = [
    {"id": "mobile", "name": "Mobil operatorlar", "badge": "1%"},
    {"id": "internet", "name": "Internet provayderlar", "badge": "1%"},
    {"id": "utilities", "name": "Kommunal to'lovlar", "badge": "1%"},
]

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


def calc_fee(amount: float) -> float:
    return round(float(amount) * COMMISSION_RATE, 2)


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
    fee = calc_fee(b.amount)
    debit_amount = b.amount + fee

    if float(sender["balance"]) < debit_amount:
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
            {"a": debit_amount, "id": uid}
        )
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:id",
            {"a": b.amount, "id": str(rec["id"])}
        )
        commission_user_id = await credit_commission(
            fee,
            source_user_id=uid,
            reference=ref,
            description="Pul o'tkazma komissiyasi",
        )
        tx = await database.fetch_one(
            """INSERT INTO transactions
               (sender_id, receiver_id, amount, fee, type, status, description, reference)
               VALUES (:s, :r, :a, :fee, 'send', 'completed', :d, :ref)
               RETURNING *""",
            {"s": uid, "r": str(rec["id"]), "a": b.amount,
             "fee": fee, "d": b.description or "Pul o'tkazma", "ref": ref}
        )

    await audit.log(
        action="money_sent",
        user_id=uid,
        entity_type="transaction",
        entity_id=str(tx["id"]),
        details={
            "amount": b.amount,
            "fee": fee,
            "receiver": b.receiverPhone,
            "commission_user_id": commission_user_id,
            "ref": ref,
            "fraud_risk": fraud["risk"]
        },
        ip_address=ip
    )

    await notify_transaction(
        database,
        receiver_id=str(rec["id"]),
        sender_name=sender["full_name"] or "Foydalanuvchi",
        amount=b.amount,
        ref=ref
    )

    return {"success": True, "transaction": dict(tx), "fee": fee, "total": debit_amount, "fraud_risk": fraud["risk"]}


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
        action="wallet_topup",
        user_id=uid,
        entity_type="transaction",
        entity_id=str(tx["id"]),
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

    offset = (page - 1) * limit
    filters = ""
    params = {"uid": uid, "limit": limit, "offset": offset}

    if type:
        filters = "AND t.type = :type"
        params["type"] = type

    rows = await database.fetch_all(
        f"""SELECT t.*,
               s.full_name as sender_name, s.phone as sender_phone,
               r.full_name as receiver_name, r.phone as receiver_phone
            FROM transactions t
            LEFT JOIN users s ON s.id = t.sender_id
            LEFT JOIN users r ON r.id = t.receiver_id
            WHERE (t.sender_id=:uid OR t.receiver_id=:uid)
            {filters}
            ORDER BY t.created_at DESC
            LIMIT :limit OFFSET :offset""",
        params
    )

    total = await database.fetch_one(
        f"""SELECT COUNT(*) as cnt FROM transactions t
            WHERE (t.sender_id=:uid OR t.receiver_id=:uid) {filters}""",
        {k: v for k, v in params.items() if k != "limit" and k != "offset"}
    )

    return {
        "transactions": [dict(r) for r in rows],
        "total": total["cnt"],
        "page": page,
        "limit": limit
    }


@router.get("/stats")
async def stats(uid: str = Depends(get_user)):
    row = await database.fetch_one(
        """SELECT
              COALESCE(SUM(CASE WHEN receiver_id=:uid THEN amount ELSE 0 END), 0) AS total_in,
              COALESCE(SUM(CASE WHEN sender_id=:uid THEN amount + fee ELSE 0 END), 0) AS total_out,
              COALESCE(SUM(fee), 0) AS total_fee,
              COUNT(*) AS total_count
           FROM transactions
           WHERE sender_id=:uid OR receiver_id=:uid""",
        {"uid": uid}
    )
    return {
        "stats": {
            "total_in": float(row["total_in"]),
            "total_out": float(row["total_out"]),
            "total_fee": float(row["total_fee"]),
            "total_count": row["total_count"],
            "commission_rate": COMMISSION_RATE,
        }
    }


@router.get("/home-summary")
async def home_summary(uid: str = Depends(get_user)):
    tx_rows = await database.fetch_all(
        """SELECT t.*,
               s.full_name as sender_name, s.phone as sender_phone,
               r.full_name as receiver_name, r.phone as receiver_phone
            FROM transactions t
            LEFT JOIN users s ON s.id = t.sender_id
            LEFT JOIN users r ON r.id = t.receiver_id
            WHERE (t.sender_id=:uid OR t.receiver_id=:uid)
            ORDER BY t.created_at DESC
            LIMIT 4""",
        {"uid": uid},
    )
    stat_row = await database.fetch_one(
        """SELECT
              COALESCE(SUM(CASE WHEN receiver_id=:uid THEN amount ELSE 0 END), 0) AS total_in,
              COALESCE(SUM(CASE WHEN sender_id=:uid THEN amount + fee ELSE 0 END), 0) AS total_out,
              COALESCE(SUM(fee), 0) AS total_fee,
              COUNT(*) AS total_count
           FROM transactions
           WHERE sender_id=:uid OR receiver_id=:uid""",
        {"uid": uid},
    )
    return {
        "transactions": [dict(r) for r in tx_rows],
        "stats": {
            "total_in": float(stat_row["total_in"]),
            "total_out": float(stat_row["total_out"]),
            "total_fee": float(stat_row["total_fee"]),
            "total_count": stat_row["total_count"],
            "commission_rate": COMMISSION_RATE,
        },
        "services": HOME_SERVICES,
    }


@router.get("/{tx_id}")
async def get_transaction(tx_id: str, uid: str = Depends(get_user)):
    tx = await database.fetch_one(
        """SELECT t.*, s.full_name as sender_name, s.phone as sender_phone,
                  r.full_name as receiver_name, r.phone as receiver_phone
           FROM transactions t
           LEFT JOIN users s ON s.id=t.sender_id
           LEFT JOIN users r ON r.id=t.receiver_id
           WHERE t.id=:id AND (t.sender_id=:uid OR t.receiver_id=:uid)""",
        {"id": tx_id, "uid": uid}
    )
    if not tx:
        raise HTTPException(404, "Tranzaksiya topilmadi")
    return {"transaction": dict(tx)}
