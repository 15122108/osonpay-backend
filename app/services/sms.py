import os
import httpx
from dotenv import load_dotenv

load_dotenv()

ENV      = os.getenv("NODE_ENV", "development")
SMS_URL  = os.getenv("SMS_API_URL", "https://notify.eskiz.uz/api")
SMS_FROM = os.getenv("SMS_FROM", "Oson Pay")
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
    clean = phone.replace("+","").replace(" ","")
    try:
        token = await _get_token()
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.post(f"{SMS_URL}/message/sms/send",
                json={"mobile_phone": clean, "message": msg, "from": SMS_FROM, "callback_url": ""},
                headers={"Authorization": f"Bearer {token}"})
            data = r.json()
            if r.status_code == 401 or data.get("status") == "token_invalid":
                token = await _refresh_token()
                r = await c.post(f"{SMS_URL}/message/sms/send",
                    json={"mobile_phone": clean, "message": msg, "from": SMS_FROM},
                    headers={"Authorization": f"Bearer {token}"})
                data = r.json()
            ok = data.get("status") == "waiting" or bool(data.get("id"))
            print(f"[SMS] {'OK' if ok else 'XATO'}: {phone} — {data}")
            return ok
    except Exception as e:
        print(f"[SMS] Error: {e}")
        return False
