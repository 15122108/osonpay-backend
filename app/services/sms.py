import os
import httpx
from dotenv import load_dotenv

load_dotenv()

ENV      = os.getenv("NODE_ENV", "development")
SMS_URL  = os.getenv("SMS_API_URL", "https://notify.eskiz.uz/api")
SMS_FROM = os.getenv("SMS_FROM", "4546")
EMAIL    = os.getenv("SMS_EMAIL", "")
PASSWORD = os.getenv("SMS_PASSWORD", "")

_token = None

async def _get_token():
    global _token
    if _token:
        return _token
    async with httpx.AsyncClient(timeout=10) as c:
        r = await c.post(f"{SMS_URL}/auth/login",
            data={"email": EMAIL, "password": PASSWORD})
        data = r.json()
        if "data" not in data or "token" not in data["data"]:
            raise Exception(f"Eskiz login xatosi: {data}")
        _token = data["data"]["token"]
    return _token

async def _refresh_token():
    global _token
    _token = None
    return await _get_token()

async def send_sms(phone: str, msg: str) -> bool:
    if ENV == "development":
        print(f"[DEV SMS] {phone}: {msg}")
        return True
    if not EMAIL or not PASSWORD:
        print(f"[SMS] Kredensial yo'q")
        return False

    # Eskiz test rejimida faqat ruxsat etilgan matn ishlatiladi.
    # Hisob aktivlashtirilgandan keyin bu qatorni olib tashlang.
    eskiz_activated = os.getenv("ESKIZ_ACTIVATED", "false").lower() == "true"
    if not eskiz_activated:
        # OTP kodni matn ichidan ajratib olib test formatga solish
        import re
        codes = re.findall(r'\b\d{6}\b', msg)
        if codes:
            msg = f"Bu Eskiz dan test {codes[0]}"
        else:
            msg = "Bu Eskiz dan test"

    clean = phone.replace("+", "").replace(" ", "")
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(
                f"{SMS_URL}/message/sms/send",
                json={"mobile_phone": clean, "message": msg, "from": SMS_FROM, "callback_url": ""},
                headers={"Authorization": f"Bearer {token}"}
            )
            data = r.json()
            if r.status_code == 401 or data.get("status") == "token_invalid":
                token = await _refresh_token()
                r = await c.post(
                    f"{SMS_URL}/message/sms/send",
                    json={"mobile_phone": clean, "message": msg, "from": SMS_FROM},
                    headers={"Authorization": f"Bearer {token}"}
                )
                data = r.json()
            ok = data.get("status") == "waiting" or bool(data.get("id"))
            print(f"[SMS] {'OK' if ok else 'XATO'}: {phone} — {data}")
            return ok
    except Exception as e:
        print(f"[SMS] Error: {e}")
        return False