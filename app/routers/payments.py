import hmac
import hashlib
import json
from fastapi import APIRouter, HTTPException, Depends, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user
from app.utils.auth import gen_ref
from app.utils.rate_limit import check_rate_limit, get_client_ip
from app.utils import audit
from app.services.paytech import (
    create_topup_payment,
    get_payment_status,
    verify_webhook_signature,
    parse_webhook,
)
from app.services.fcm import notify_transaction
import os

router = APIRouter()

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://your-frontend.com")


class TopUpInitReq(BaseModel):
    amount: float


# ─────────────────────────────────────────────
# 1. To'ldirish boshlash → PayTech redirect URL
# ─────────────────────────────────────────────
@router.post("/topup/init")
async def topup_init(b: TopUpInitReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)

    if b.amount < 1000:
        raise HTTPException(400, "Minimal summa 1,000 UZS")
    if b.amount > 100_000_000:
        raise HTTPException(400, "Maksimal summa 100,000,000 UZS")

    wallet = await database.fetch_one(
        "SELECT is_frozen FROM wallets WHERE user_id=:uid", {"uid": uid}
    )
    if wallet and wallet["is_frozen"]:
        raise HTTPException(403, "Hisobingiz muzlatilgan")

    user = await database.fetch_one(
        "SELECT phone, full_name FROM users WHERE id=:uid", {"uid": uid}
    )
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi")

    ref = gen_ref()

    # Kutayotgan to'lovni bazaga yozish
    await database.execute(
        """INSERT INTO pending_payments (user_id, amount, reference, status)
           VALUES (:uid, :amt, :ref, 'pending')""",
        {"uid": uid, "amt": b.amount, "ref": ref}
    )

    try:
        result = await create_topup_payment(
            user_id=uid,
            amount=b.amount,
            phone=user["phone"],
            full_name=user["full_name"] or "Foydalanuvchi",
            reference_id=ref,
        )
    except Exception as e:
        # Xato bo'lsa pending_payment ni o'chirish
        await database.execute(
            "DELETE FROM pending_payments WHERE reference=:ref", {"ref": ref}
        )
        raise HTTPException(502, f"To'lov tizimi xatosi: {str(e)}")

    # PayTech payment_id ni saqlash
    await database.execute(
        """UPDATE pending_payments
           SET paytech_payment_id=:pid, status='initiated'
           WHERE reference=:ref""",
        {"pid": result["payment_id"], "ref": ref}
    )

    await audit.log(
        "topup_initiated", user_id=uid,
        details={"amount": b.amount, "ref": ref, "payment_id": result["payment_id"]},
        ip_address=ip
    )

    return {
        "success": True,
        "redirect_url": result["redirect_url"],
        "reference": ref,
        "payment_id": result["payment_id"],
    }


# ─────────────────────────────────────────────
# 2. PayTech Webhook — to'lov natijasi
# ─────────────────────────────────────────────
@router.post("/webhook")
async def paytech_webhook(request: Request):
    raw_body = await request.body()
    signature = request.headers.get("X-Signature", "")

    # Imzoni tekshirish
    if not verify_webhook_signature(raw_body, signature):
        await audit.log("webhook_invalid_signature", details={"sig": signature})
        raise HTTPException(401, "Imzo noto'g'ri")

    try:
        body = json.loads(raw_body)
    except Exception:
        raise HTTPException(400, "JSON xato")

    data = parse_webhook(body)
    payment_id = data.get("payment_id")
    state = data.get("state")
    amount_tiyin = data.get("amount", 0)

    if not payment_id:
        raise HTTPException(400, "payment_id yo'q")

    # Idempotency — allaqachon qayta ishlangan bo'lsa qaytish
    pending = await database.fetch_one(
        "SELECT * FROM pending_payments WHERE paytech_payment_id=:pid",
        {"pid": payment_id}
    )
    if not pending:
        # Noma'lum to'lov — log yozib 200 qaytarish (PayTech qayta urinmasin)
        await audit.log("webhook_unknown_payment", details={"payment_id": payment_id})
        return {"success": True}

    if pending["status"] == "completed":
        # Allaqachon bajarilgan — idempotent javob
        return {"success": True}

    if state == "COMPLETED":
        amount_uzs = amount_tiyin / 100
        user_id = str(pending["user_id"])
        ref = pending["reference"]

        async with database.transaction():
            # Wallet balansini oshirish
            await database.execute(
                "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid",
                {"a": amount_uzs, "uid": user_id}
            )
            # Tranzaksiya yozish
            tx = await database.fetch_one(
                """INSERT INTO transactions
                   (receiver_id, amount, type, status, description, reference)
                   VALUES (:uid, :a, 'topup', 'completed', 'PayTech orqali toldirish', :ref)
                   RETURNING *""",
                {"uid": user_id, "a": amount_uzs, "ref": ref}
            )
            # Pending to'lovni yangilash
            await database.execute(
                "UPDATE pending_payments SET status='completed', updated_at=NOW() WHERE paytech_payment_id=:pid",
                {"pid": payment_id}
            )

        await audit.log(
            "topup_completed", user_id=user_id,
            entity_type="transaction", entity_id=str(tx["id"]),
            details={"amount": amount_uzs, "ref": ref, "payment_id": payment_id}
        )

        # Push notification
        try:
            user = await database.fetch_one(
                "SELECT full_name FROM users WHERE id=:uid", {"uid": user_id}
            )
            from app.services.fcm import get_user_tokens, send_push
            tokens = await get_user_tokens(database, user_id)
            if tokens:
                amt_fmt = f"{amount_uzs:,.0f}".replace(",", " ")
                await send_push(
                    tokens,
                    title="Hisob to'ldirildi ✅",
                    body=f"{amt_fmt} UZS hisobingizga tushdi",
                    data={"type": "topup", "reference": ref}
                )
        except Exception as e:
            print(f"[Push] Xato: {e}")

    elif state in ("DECLINED", "CANCELLED"):
        await database.execute(
            "UPDATE pending_payments SET status=:s, updated_at=NOW() WHERE paytech_payment_id=:pid",
            {"s": state.lower(), "pid": payment_id}
        )
        await audit.log(
            "topup_failed",
            user_id=str(pending["user_id"]),
            details={
                "state": state,
                "error": data.get("error"),
                "payment_id": payment_id
            }
        )

    return {"success": True}


# ─────────────────────────────────────────────
# 3. Return URL — to'lovdan so'ng qaytish
# ─────────────────────────────────────────────
@router.get("/return")
async def payment_return(ref: str = ""):
    if not ref:
        return RedirectResponse(f"{FRONTEND_URL}/wallet?status=error")

    pending = await database.fetch_one(
        "SELECT status FROM pending_payments WHERE reference=:ref", {"ref": ref}
    )
    if not pending:
        return RedirectResponse(f"{FRONTEND_URL}/wallet?status=error")

    status = pending["status"]
    if status == "completed":
        return RedirectResponse(f"{FRONTEND_URL}/wallet?status=success&ref={ref}")
    elif status in ("declined", "cancelled"):
        return RedirectResponse(f"{FRONTEND_URL}/wallet?status=failed&ref={ref}")
    else:
        return RedirectResponse(f"{FRONTEND_URL}/wallet?status=pending&ref={ref}")


# ─────────────────────────────────────────────
# 4. To'lov holatini tekshirish
# ─────────────────────────────────────────────
@router.get("/status/{payment_id}")
async def payment_status(payment_id: str, uid: str = Depends(get_user)):
    pending = await database.fetch_one(
        """SELECT pp.*, t.id as tx_id
           FROM pending_payments pp
           LEFT JOIN transactions t ON t.reference = pp.reference
           WHERE pp.paytech_payment_id=:pid AND pp.user_id=:uid""",
        {"pid": payment_id, "uid": uid}
    )
    if not pending:
        raise HTTPException(404, "To'lov topilmadi")

    # Agar hali pending bo'lsa, PayTech dan so'rash
    if pending["status"] == "initiated":
        try:
            live = await get_payment_status(payment_id)
            return {
                "success": True,
                "status": live["state"],
                "amount": pending["amount"],
                "reference": pending["reference"],
            }
        except Exception:
            pass

    return {
        "success": True,
        "status": pending["status"].upper(),
        "amount": float(pending["amount"]),
        "reference": pending["reference"],
        "transaction_id": str(pending["tx_id"]) if pending["tx_id"] else None,
    }


# ─────────────────────────────────────────────
# 5. To'lov tarixini ko'rish
# ─────────────────────────────────────────────
@router.get("/history")
async def payment_history(
    page: int = 1,
    limit: int = 20,
    uid: str = Depends(get_user)
):
    if limit > 100:
        limit = 100
    offset = (page - 1) * limit

    rows = await database.fetch_all(
        """SELECT id, amount, status, reference, paytech_payment_id, created_at, updated_at
           FROM pending_payments
           WHERE user_id=:uid
           ORDER BY created_at DESC
           LIMIT :limit OFFSET :offset""",
        {"uid": uid, "limit": limit, "offset": offset}
    )
    total = await database.fetch_one(
        "SELECT COUNT(*) as cnt FROM pending_payments WHERE user_id=:uid", {"uid": uid}
    )
    return {
        "success": True,
        "payments": [dict(r) for r in rows],
        "total": total["cnt"],
        "page": page,
        "limit": limit,
    }