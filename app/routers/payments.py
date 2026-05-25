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
from app.services.commission import credit_commission
from app.services.fcm import notify_transaction
from app.services.paysys import mobile_configured, mobile_info, mobile_pay, mobile_status
import os

router = APIRouter()

FRONTEND_URL = os.getenv("FRONTEND_URL", "https://your-frontend.com")
COMMISSION_RATE = 0.01


class TopUpInitReq(BaseModel):
    amount: float


class MobileInfoReq(BaseModel):
    phone_number: str


class MobilePayReq(BaseModel):
    phone_number: str
    amount: float


def calc_fee(amount: float) -> float:
    return round(float(amount) * COMMISSION_RATE, 2)


SERVICE_ITEMS = [
    {"id": "popular", "name": "Ko'p qo'llaniladigan", "icon": "list", "category": "popular", "badge": "1%"},
    {"id": "mobile", "name": "Mobil operatorlar", "icon": "phone", "category": "mobile", "badge": "1%", "real": mobile_configured()},
    {"id": "internet", "name": "Internet provayderlar", "icon": "globe", "category": "internet", "badge": "1%"},
    {"id": "utilities", "name": "Kommunal to'lovlar", "icon": "home", "category": "utilities", "badge": "1%"},
    {"id": "bank", "name": "Bank xizmatlari", "icon": "bank", "category": "bank", "badge": "1%"},
    {"id": "charity", "name": "Xayriya", "icon": "heart", "category": "charity", "badge": "1%"},
]


@router.get("/saved")
async def saved_payments(uid: str = Depends(get_user)):
    return {"items": []}


@router.get("/home")
async def my_home(uid: str = Depends(get_user)):
    return {"items": []}


@router.get("/services")
async def services(uid: str = Depends(get_user)):
    return {"items": SERVICE_ITEMS}


def _clean_phone_number(phone: str) -> str:
    clean = "".join(ch for ch in str(phone or "") if ch.isdigit())
    if clean.startswith("998") and len(clean) == 12:
        return clean
    if len(clean) == 9:
        return f"998{clean}"
    raise HTTPException(400, "Telefon raqami noto'g'ri")


@router.post("/mobile/info")
async def mobile_operator_info(b: MobileInfoReq, uid: str = Depends(get_user)):
    phone = _clean_phone_number(b.phone_number)
    try:
        result = await mobile_info(phone)
    except Exception as e:
        raise HTTPException(502, f"Mobil to'lov provayderi xatosi: {str(e)}")
    return {"success": True, "info": result}


@router.post("/mobile/pay")
async def mobile_operator_pay(b: MobilePayReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)
    phone = _clean_phone_number(b.phone_number)
    if b.amount < 1000:
        raise HTTPException(400, "Minimal summa 1,000 UZS")
    if b.amount > 5_000_000:
        raise HTTPException(400, "Maksimal summa 5,000,000 UZS")

    wallet = await database.fetch_one(
        "SELECT balance, is_frozen FROM wallets WHERE user_id=:uid", {"uid": uid}
    )
    if not wallet:
        raise HTTPException(404, "Wallet topilmadi")
    if wallet["is_frozen"]:
        raise HTTPException(403, "Hisobingiz muzlatilgan")

    fee = calc_fee(b.amount)
    total = b.amount + fee
    if float(wallet["balance"]) < total:
        raise HTTPException(400, "Balans yetarli emas")

    ref = gen_ref()
    provider_result = None
    async with database.transaction():
        await database.execute(
            """INSERT INTO service_payments
               (user_id, provider, category, account, amount, fee, total, reference, status)
               VALUES (:uid, 'paysys', 'mobile', :account, :amount, :fee, :total, :ref, 'pending')""",
            {"uid": uid, "account": phone, "amount": b.amount, "fee": fee, "total": total, "ref": ref},
        )
        await database.execute(
            "UPDATE wallets SET balance=balance-:total, updated_at=NOW() WHERE user_id=:uid",
            {"total": total, "uid": uid},
        )

    try:
        provider_result = await mobile_pay(phone, b.amount, ref)
        provider_tx_id = str(provider_result.get("transaction_id") or "")
        status_code = int(provider_result.get("status", -1))
        status = "completed" if status_code == 2 else ("pending" if status_code in (0, 1, 7) else "failed")
    except Exception as e:
        async with database.transaction():
            await database.execute(
                "UPDATE wallets SET balance=balance+:total, updated_at=NOW() WHERE user_id=:uid",
                {"total": total, "uid": uid},
            )
            await database.execute(
                """UPDATE service_payments
                   SET status='failed', raw_response=:raw, updated_at=NOW()
                   WHERE reference=:ref""",
                {"raw": str(e), "ref": ref},
            )
        raise HTTPException(502, f"Mobil to'lov bajarilmadi: {str(e)}")

    async with database.transaction():
        await database.execute(
            """UPDATE service_payments
               SET provider_tx_id=:ptx, status=:status, raw_response=:raw, updated_at=NOW()
               WHERE reference=:ref""",
            {"ptx": provider_tx_id, "status": status, "raw": json.dumps(provider_result), "ref": ref},
        )
        await database.execute(
            """INSERT INTO transactions
               (sender_id, amount, fee, type, status, description, reference)
               VALUES (:uid, :amount, :fee, 'service', :status, :description, :ref)""",
            {
                "uid": uid,
                "amount": b.amount,
                "fee": fee,
                "status": status,
                "description": f"Mobil aloqa to'lovi: {phone}",
                "ref": ref,
            },
        )
    await credit_commission(
        fee,
        source_user_id=uid,
        reference=ref,
        description="Mobil aloqa to'lovi komissiyasi",
    )
    await audit.log(
        "mobile_payment",
        user_id=uid,
        entity_type="service_payment",
        entity_id=ref,
        details={"phone": phone, "amount": b.amount, "fee": fee, "provider_tx_id": provider_tx_id, "status": status},
        ip_address=ip,
    )
    return {
        "success": True,
        "reference": ref,
        "provider_transaction_id": provider_tx_id,
        "status": status,
        "amount": b.amount,
        "fee": fee,
        "total": total,
    }


@router.get("/mobile/status/{reference}")
async def mobile_operator_status(reference: str, uid: str = Depends(get_user)):
    payment = await database.fetch_one(
        "SELECT * FROM service_payments WHERE reference=:ref AND user_id=:uid",
        {"ref": reference, "uid": uid},
    )
    if not payment:
        raise HTTPException(404, "To'lov topilmadi")
    if not payment["provider_tx_id"]:
        return {"success": True, "status": payment["status"]}
    try:
        result = await mobile_status(payment["provider_tx_id"])
    except Exception:
        return {"success": True, "status": payment["status"]}
    status_code = int(result.get("status", -1))
    status = "completed" if status_code == 2 else ("pending" if status_code in (0, 1, 7) else "failed")
    await database.execute(
        "UPDATE service_payments SET status=:status, raw_response=:raw, updated_at=NOW() WHERE reference=:ref",
        {"status": status, "raw": json.dumps(result), "ref": reference},
    )
    return {"success": True, "status": status, "provider": result}


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
                    title="Hisob to'ldirildi",
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
