import hashlib
import os
import time
import uuid

import httpx

PAYSYS_MOBILE_API_URL = os.getenv("PAYSYS_MOBILE_API_URL", "https://paysys.uz/mobile/api").rstrip("/")
PAYSYS_MOBILE_SERVICE_ID = os.getenv("PAYSYS_MOBILE_SERVICE_ID", "")
PAYSYS_MOBILE_SECRET_KEY = os.getenv("PAYSYS_MOBILE_SECRET_KEY", "")


def mobile_configured() -> bool:
    return bool(PAYSYS_MOBILE_SERVICE_ID and PAYSYS_MOBILE_SECRET_KEY)


def _auth_header() -> str:
    timestamp = str(int(time.time() * 1000))
    digest = hashlib.sha1(f"{PAYSYS_MOBILE_SECRET_KEY}{timestamp}".encode()).hexdigest()
    return f"{PAYSYS_MOBILE_SERVICE_ID}-{digest}-{timestamp}"


async def _mobile_request(method: str, params: dict) -> dict:
    if not mobile_configured():
        raise Exception("PAYSYS_MOBILE_SERVICE_ID yoki PAYSYS_MOBILE_SECRET_KEY sozlanmagan")

    payload = {"id": str(uuid.uuid4()), "params": params}
    async with httpx.AsyncClient(timeout=20) as client:
        response = await client.post(
            f"{PAYSYS_MOBILE_API_URL}/{method}",
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Auth": _auth_header(),
            },
            json=payload,
        )
        data = response.json()

    if not response.is_success:
        raise Exception(f"PaySys HTTP xatosi: {response.status_code} {data}")
    if data.get("error"):
        raise Exception(f"PaySys xatosi: {data['error']}")
    return data.get("result") or {}


async def mobile_info(phone_number: str) -> dict:
    return await _mobile_request("info", {"phone_number": phone_number})


async def mobile_pay(phone_number: str, amount: float, payment_number: str) -> dict:
    return await _mobile_request(
        "pay",
        {
            "phone_number": phone_number,
            "payment_number": payment_number,
            "amount": int(amount),
        },
    )


async def mobile_status(transaction_id: str | int) -> dict:
    return await _mobile_request("status", {"transaction_id": transaction_id})
