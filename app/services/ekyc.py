import os

import httpx

EKYC_API_URL = os.getenv("EKYC_API_URL", "").rstrip("/")
EKYC_API_KEY = os.getenv("EKYC_API_KEY", "")


def configured() -> bool:
    return bool(EKYC_API_URL and EKYC_API_KEY)


async def verify_passport(passport_series: str, passport_number: str, birth_date: str, full_name: str) -> dict:
    if not configured():
        return {"status": "not_configured", "verified": False}

    async with httpx.AsyncClient(timeout=25) as client:
        response = await client.post(
            f"{EKYC_API_URL}/passport/verify",
            headers={"Authorization": f"Bearer {EKYC_API_KEY}", "Content-Type": "application/json"},
            json={
                "passport_series": passport_series,
                "passport_number": passport_number,
                "birth_date": birth_date,
                "full_name": full_name,
            },
        )
        data = response.json()

    if not response.is_success:
        return {"status": "error", "verified": False, "raw": data}

    verified = bool(data.get("verified") or data.get("success") or data.get("status") == "verified")
    return {"status": "verified" if verified else "rejected", "verified": verified, "raw": data}
