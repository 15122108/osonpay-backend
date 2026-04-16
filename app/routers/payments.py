from fastapi import APIRouter, HTTPException, Depends, Request, BackgroundTasks
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user
from app.utils.auth import gen_ref
from app.utils.rate_limit import get_client_ip
from app.utils import audit
from app.services.paytech import (
    create_topup_payment,
    get_payment_status,
    verify_webhook_signature,
    parse_webhook,
)
from app.services.fcm import get_user_tokens, send_push
import json

router = APIRouter()

FRONTEND_DEEP_LINK = "osonpay://payment"   # Mobil ilova deep link


class TopupInitReq(BaseModel):
    amount: float


@router.post("/topup/init")
async def topup_init(b: TopupInitReq, request: Request, uid: str = Depends(get_user)):
    """
    1-qadam: To'lov yaratish → redirectUrl qaytarish
    Frontend bu URL ni WebView da ochadi
    """
    ip = get_client_ip(request)

    if b.amount < 1000:
        raise HTTPException(400, "Minimum 1,000 UZS")
    if b.amount > 50_000_000:
        raise HTTPException(400, "Maksimum 50,000,000 UZS")

    user = await database.fetch_one(
        "SELECT phone, full_name FROM users WHERE id=:uid", {"uid": uid}
    )
    if not user:
        raise HTTPException(404, "Foydalanuvchi topilmadi")

    wallet = await database.fetch_one(
        "SELECT is_frozen FROM wallets WHERE user_id=:uid", {"uid": uid}
    )
    if wallet and wallet["is_frozen"]:
        raise HTTPException(403, "Hisobingiz muzlatilgan")

    ref = gen_ref()

    # PayTech da to'lov yaratish
    try:
        result = await create_topup_payment(
            user_id=uid,
            amount=b.amount,
            phone=user["phone"],
            full_name=user["full_name"] or "Foydalanuvchi",
            reference_id=ref,
        )
    except Exception as e:
        raise HTTPException(502, f"To'lov tizimi xatosi: {str(e)}")

    # Kutayotgan to'lovni DBga yozish
    await database.execute(
        """INSERT INTO pending_payments
           (user_id, amount, reference, paytech_payment_id, status)
           VALUES (:uid, :amt, :ref, :pid, 'pending')""",
        {
            "uid": uid,
            "amt": b.amount,
            "ref": ref,
            "pid": result["payment_id"],
        }
    )

    await audit.log(
        action="topup_initiated",
        user_id=uid,
        details={"amount": b.amount, "ref": ref, "payment_id": result["payment_id"]},
        ip_address=ip
    )

    return {
        "success":      True,
        "redirectUrl":  result["redirect_url"],
        "paymentId":    result["payment_id"],
        "reference":    ref,
        "amount":       b.amount,
    }


@router.get("/topup/status/{payment_id}")
async def topup_status(payment_id: str, uid: str = Depends(get_user)):
    """To'lov holatini tekshirish (polling uchun)"""
    pending = await database.fetch_one(
        "SELECT * FROM pending_payments WHERE paytech_payment_id=:pid AND user_id=:uid",
        {"pid": payment_id, "uid": uid}
    )
    if not pending:
        raise HTTPException(404, "To'lov topilmadi")

    # Agar allaqachon yakunlangan bo'lsa — DBdan qaytarish
    if pending["status"] in ("completed", "failed"):
        return {
            "success": True,
            "status":  pending["status"],
            "amount":  float(pending["amount"]),
        }

    # PayTech dan hozirgi holat
    try:
        status = await get_payment_status(payment_id)
    except Exception:
        return {"success": True, "status": pending["status"]}

    state = status.get("state", "")

    if state == "COMPLETED":
        # Pul hisob balansiga qo'shiladi — webhook ham buni qiladi
        # Bu faqat polling uchun — ikki marta qo'shilmasin deb tekshiramiz
        already = await database.fetch_one(
            "SELECT id FROM pending_payments WHERE paytech_payment_id=:pid AND status='completed'",
            {"pid": payment_id}
        )
        if not already:
            await _credit_user(uid, float(pending["amount"]), pending["reference"], payment_id)

    return {
        "success": True,
        "status":  state.lower() if state else "pending",
        "amount":  float(pending["amount"]),
    }


@router.post("/webhook")
async def paytech_webhook(request: Request, background_tasks: BackgroundTasks):
    """
    PayTech webhook — to'lov yakunlanganda chaqiriladi.
    COMPLETED → foydalanuvchi balansiga pul qo'shiladi.
    """
    body_bytes = await request.body()
    signature  = request.headers.get("Signature", "")

    # Imzoni tekshirish
    if not verify_webhook_signature(body_bytes, signature):
        raise HTTPException(400, "Webhook imzo xato")

    try:
        body = json.loads(body_bytes)
    except Exception:
        raise HTTPException(400, "JSON xato")

    event = parse_webhook(body)
    payment_id = event["payment_id"]
    state      = event["state"]

    pending = await database.fetch_one(
        "SELECT * FROM pending_payments WHERE paytech_payment_id=:pid",
        {"pid": payment_id}
    )
    if not pending:
        return {"received": True}   # Bizning to'lov emas — OK qaytarish

    if pending["status"] == "completed":
        return {"received": True}   # Allaqachon qayta ishlangan

    if state == "COMPLETED":
        background_tasks.add_task(
            _credit_user,
            str(pending["user_id"]),
            float(pending["amount"]),
            pending["reference"],
            payment_id,
        )
    elif state in ("DECLINED", "CANCELLED"):
        await database.execute(
            "UPDATE pending_payments SET status='failed', updated_at=NOW() WHERE paytech_payment_id=:pid",
            {"pid": payment_id}
        )
        await audit.log(
            action="topup_failed",
            user_id=str(pending["user_id"]),
            details={"amount": float(pending["amount"]), "reason": event.get("error"), "state": state},
        )

    return {"received": True}


@router.get("/return")
async def payment_return(ref: str):
    """
    Foydalanuvchi to'lov sahifasidan qaytgach shu URL ochiladi.
    Deep link orqali ilovaga yo'naltirish.
    """
    return RedirectResponse(url=f"{FRONTEND_DEEP_LINK}?ref={ref}")


async def _credit_user(user_id: str, amount: float, reference: str, payment_id: str):
    """To'lov muvaffaqiyatli — balansga qo'shish"""
    try:
        async with database.transaction():
            # Balansga qo'shish
            await database.execute(
                "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid",
                {"a": amount, "uid": user_id}
            )
            # Tranzaksiya yozish
            tx = await database.fetch_one(
                """INSERT INTO transactions
                   (receiver_id, amount, type, status, description, reference)
                   VALUES (:uid, :a, 'topup', 'completed', :desc, :ref)
                   RETURNING id""",
                {
                    "uid":  user_id,
                    "a":    amount,
                    "desc": "Karta orqali to'ldirish (PayTech)",
                    "ref":  reference,
                }
            )
            # Pending to'lovni yakunlangan deb belgilash
            await database.execute(
                "UPDATE pending_payments SET status='completed', updated_at=NOW() WHERE paytech_payment_id=:pid",
                {"pid": payment_id}
            )

        await audit.log(
            action="topup_completed",
            user_id=user_id,
            entity_type="transaction",
            entity_id=str(tx["id"]),
            details={"amount": amount, "ref": reference, "payment_id": payment_id},
        )

        # Push notification
        tokens = await get_user_tokens(database, user_id)
        if tokens:
            amt_fmt = f"{amount:,.0f}".replace(",", " ")
            await send_push(
                tokens,
                title="Hisob to'ldirildi",
                body=f"{amt_fmt} UZS kartangizdan hisob raqamingizga o'tkazildi",
                data={"type": "topup", "reference": reference}
            )

    except Exception as e:
        print(f"[PayTech] Credit error: {e}")
