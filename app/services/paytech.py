import os
import hmac
import hashlib
import json
import httpx
from typing import Optional

# PayTech API sozlamalari
# engine.pay.tech — production
# engine-sandbox.pay.tech — test
PAYTECH_API_KEY    = os.getenv("PAYTECH_API_KEY", "")
PAYTECH_SIGN_KEY   = os.getenv("PAYTECH_SIGNING_KEY", "")
PAYTECH_IS_TEST    = os.getenv("PAYTECH_TEST_MODE", "true").lower() == "true"
PAYTECH_BASE_URL   = (
    "https://engine-sandbox.pay.tech"
    if PAYTECH_IS_TEST else
    "https://engine.pay.tech"
)
PAYTECH_CURRENCY   = "UZS"
BACKEND_URL        = os.getenv("BACKEND_URL", "https://YOUR-APP.onrender.com")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {PAYTECH_API_KEY}",
        "Content-Type": "application/json",
    }


async def create_topup_payment(
    user_id: str,
    amount: float,
    phone: str,
    full_name: str,
    reference_id: str,
) -> dict:
    """
    Hisob to'ldirish uchun PayTech to'lov yaratish.
    Foydalanuvchi redirectUrl ga o'tib karta ma'lumotlarini kiritadi.
    """
    if not PAYTECH_API_KEY:
        raise Exception("PAYTECH_API_KEY sozlanmagan")

    # To'lov summasi tiyinlarda (UZS uchun 1 UZS = 1 tiyin)
    amount_in_tiyin = int(amount * 100)

    name_parts = (full_name or "Foydalanuvchi").split(" ", 1)
    first_name = name_parts[0]
    last_name  = name_parts[1] if len(name_parts) > 1 else "."

    payload = {
        "referenceId": reference_id,
        "paymentType": "DEPOSIT",
        "amount": amount_in_tiyin,
        "currency": PAYTECH_CURRENCY,
        "description": f"Oson Pay hisob to'ldirish: {reference_id}",
        "webhookUrl": f"{BACKEND_URL}/api/payments/webhook",
        "returnUrl": f"{BACKEND_URL}/api/payments/return?ref={reference_id}",
        "customer": {
            "referenceId": user_id,
            "firstName":   first_name,
            "lastName":    last_name,
            "phone":       phone.replace("+", ""),
            "locale":      "uz",
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{PAYTECH_BASE_URL}/api/v1/payments",
            headers=_headers(),
            json=payload
        )
        data = r.json()

    if not r.is_success:
        raise Exception(f"PayTech xatosi: {data}")

    return {
        "payment_id":   data.get("id"),
        "redirect_url": data.get("redirectUrl"),
        "state":        data.get("state"),
        "reference_id": reference_id,
    }


async def get_payment_status(payment_id: str) -> dict:
    """To'lov holatini tekshirish"""
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{PAYTECH_BASE_URL}/api/v1/payments/{payment_id}",
            headers=_headers()
        )
        data = r.json()

    result = data.get("result", data)
    return {
        "payment_id": payment_id,
        "state":      result.get("state"),            # COMPLETED, DECLINED, CANCELLED
        "amount":     result.get("amount"),
        "currency":   result.get("currency"),
        "error":      result.get("errorMessage"),
        "method":     result.get("paymentMethod"),
    }


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    """
    Webhook xabarini tasdiqlash.
    PayTech HMAC-SHA256 imzo yuboradi.
    """
    if not PAYTECH_SIGN_KEY:
        return True  # Test rejimida tekshirmaslik
    expected = hmac.new(
        PAYTECH_SIGN_KEY.encode(),
        body,
        hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def parse_webhook(body: dict) -> dict:
    """Webhook payload ni parse qilish"""
    return {
        "payment_id":   body.get("id"),
        "state":        body.get("state"),         # COMPLETED, DECLINED, CANCELLED
        "payment_type": body.get("paymentType"),   # DEPOSIT
        "method":       body.get("paymentMethod"),
        "amount":       body.get("amount"),
        "currency":     body.get("currency"),
        "error_code":   body.get("errorCode"),
        "error":        body.get("errorMessage"),
    }
