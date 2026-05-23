import os
import hmac
import hashlib
import json
import httpx

PAYTECH_API_KEY = (
    os.getenv("PAYTECH_API_KEY")
    or os.getenv("PAYME_API_KEY")
    or os.getenv("PAYME_KEY")
    or ""
)
PAYTECH_SIGN_KEY = (
    os.getenv("PAYTECH_SIGNING_KEY")
    or os.getenv("PAYTECH_SIGN_KEY")
    or os.getenv("PAYME_SIGNING_KEY")
    or os.getenv("PAYME_SIGN_KEY")
    or ""
)
PAYTECH_IS_TEST = os.getenv("PAYTECH_TEST_MODE", "false").lower() == "true"
PAYTECH_BASE_URL = os.getenv(
    "PAYTECH_BASE_URL",
    "https://engine-sandbox.pay.tech" if PAYTECH_IS_TEST else "https://engine.pay.tech",
)
PAYTECH_CURRENCY = "UZS"
BACKEND_URL = os.getenv("BACKEND_URL", "https://YOUR-APP.onrender.com")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {PAYTECH_API_KEY}",
        "Content-Type": "application/json",
    }


def provider_configured() -> bool:
    return bool(PAYTECH_API_KEY)


async def create_topup_payment(
    user_id: str,
    amount: float,
    phone: str,
    full_name: str,
    reference_id: str,
) -> dict:
    if not PAYTECH_API_KEY:
        raise Exception("PAYTECH_API_KEY/PAYME_API_KEY sozlanmagan")

    amount_in_tiyin = int(amount * 100)
    name_parts = (full_name or "Foydalanuvchi").split(" ", 1)
    first_name = name_parts[0]
    last_name = name_parts[1] if len(name_parts) > 1 else "."

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
            "firstName": first_name,
            "lastName": last_name,
            "phone": phone.replace("+", ""),
            "locale": "uz",
        },
    }

    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            f"{PAYTECH_BASE_URL}/api/v1/payments",
            headers=_headers(),
            json=payload,
        )
        data = r.json()

    if not r.is_success:
        raise Exception(f"PayTech xatosi: {data}")

    return {
        "payment_id": data.get("id"),
        "redirect_url": data.get("redirectUrl"),
        "state": data.get("state"),
        "reference_id": reference_id,
    }


async def get_payment_status(payment_id: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{PAYTECH_BASE_URL}/api/v1/payments/{payment_id}",
            headers=_headers(),
        )
        data = r.json()

    result = data.get("result", data)
    return {
        "payment_id": payment_id,
        "state": result.get("state"),
        "amount": result.get("amount"),
        "currency": result.get("currency"),
        "error": result.get("errorMessage"),
        "method": result.get("paymentMethod"),
    }


async def get_customer_card_tokens(customer_reference_id: str) -> list[dict]:
    if not PAYTECH_API_KEY:
        raise Exception("PAYTECH_API_KEY/PAYME_API_KEY sozlanmagan")

    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(
            f"{PAYTECH_BASE_URL}/api/v1/customers/{customer_reference_id}/card-tokens",
            headers=_headers(),
        )
        data = r.json()

    if not r.is_success:
        raise Exception(f"PayTech karta tokenlari xatosi: {data}")

    if isinstance(data, list):
        return data
    result = data.get("result", data)
    if isinstance(result, list):
        return result
    return result.get("cardTokens") or result.get("items") or result.get("tokens") or []


def verify_webhook_signature(body: bytes, signature: str) -> bool:
    if not PAYTECH_SIGN_KEY:
        return True
    expected = hmac.new(
        PAYTECH_SIGN_KEY.encode(),
        body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature or "")


def parse_webhook(body: dict) -> dict:
    return {
        "payment_id": body.get("id"),
        "state": body.get("state"),
        "payment_type": body.get("paymentType"),
        "method": body.get("paymentMethod"),
        "amount": body.get("amount"),
        "currency": body.get("currency"),
        "error_code": body.get("errorCode"),
        "error": body.get("errorMessage"),
    }
