from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
from app.database import database
from app.utils.auth import gen_otp, make_token, hash_pin, verify_pin
from app.utils.deps import get_user
from app.utils.rate_limit import check_rate_limit, get_client_ip
from app.utils import audit
from app.services.sms import send_sms
import os

router = APIRouter()
ENV = os.getenv("NODE_ENV", "development")


class OTPReq(BaseModel):
    phone: str

class VerifyReq(BaseModel):
    phone: str
    code: str
    fullName: str = "Foydalanuvchi"

class PinReq(BaseModel):
    pin: str

class PinVerifyReq(BaseModel):
    phone: str
    pin: str

class LangReq(BaseModel):
    language: str


@router.post("/send-otp")
async def send_otp(b: OTPReq, request: Request):
    ip = get_client_ip(request)
    if not b.phone.startswith("+998") or len(b.phone) != 13:
        raise HTTPException(400, "Noto'g'ri telefon raqami (+998XXXXXXXXX)")

    await check_rate_limit("otp", "otp_send", b.phone)

    await database.execute(
        "UPDATE otps SET is_used=TRUE WHERE phone=:p AND is_used=FALSE",
        {"p": b.phone}
    )
    code    = gen_otp()
    expires = datetime.utcnow() + timedelta(minutes=3)
    await database.execute(
        "INSERT INTO otps(phone,code,expires_at) VALUES(:p,:c,:e)",
        {"p": b.phone, "c": code, "e": expires}
    )
    user = await database.fetch_one(
        "SELECT id FROM users WHERE phone=:p", {"p": b.phone}
    )
    msg = f"Oson Pay: tasdiqlash kodi {code}. 3 daqiqa ichida amal qiladi."
    await send_sms(b.phone, msg)
    await audit.log("otp_sent", entity_type="phone", entity_id=b.phone, ip_address=ip)

    res = {"success": True, "isNewUser": user is None, "message": "Kod yuborildi"}
    if ENV == "development":
        res["devCode"] = code
    return res


@router.post("/verify-otp")
async def verify_otp(b: VerifyReq, request: Request):
    ip = get_client_ip(request)
    await check_rate_limit("otp_verify", "otp_verify", b.phone)

    otp = await database.fetch_one(
        """SELECT * FROM otps
           WHERE phone=:p AND code=:c AND is_used=FALSE AND expires_at>NOW()
           ORDER BY created_at DESC LIMIT 1""",
        {"p": b.phone, "c": b.code}
    )
    if not otp:
        await audit.log("otp_failed", entity_type="phone", entity_id=b.phone, ip_address=ip)
        raise HTTPException(400, "Kod noto'g'ri yoki muddati tugagan")

    async with database.transaction():
        await database.execute(
            "UPDATE otps SET is_used=TRUE WHERE id=:id", {"id": str(otp["id"])}
        )
        user = await database.fetch_one(
            "SELECT * FROM users WHERE phone=:p", {"p": b.phone}
        )
        if user:
            uid     = str(user["id"])
            has_pin = bool(user["pin_hash"])
        else:
            row = await database.fetch_one(
                "INSERT INTO users(phone,full_name,is_verified) VALUES(:p,:n,TRUE) RETURNING id",
                {"p": b.phone, "n": b.fullName}
            )
            uid = str(row["id"])
            await database.execute(
                "INSERT INTO wallets(user_id,balance) VALUES(:u,0)", {"u": uid}
            )
            has_pin = False

        token   = make_token(uid, b.phone)
        expires = datetime.utcnow() + timedelta(days=30)
        ua      = request.headers.get("User-Agent", "")[:255]
        await database.execute(
            "INSERT INTO sessions(user_id,token,expires_at,device_info) VALUES(:u,:t,:e,:d)",
            {"u": uid, "t": token, "e": expires, "d": ua}
        )

    await audit.log("login_otp", user_id=uid, ip_address=ip)
    return {
        "success": True,
        "token":   token,
        "hasPin":  has_pin,
        "user":    {"id": uid, "phone": b.phone, "fullName": b.fullName}
    }


@router.post("/set-pin")
async def set_pin(b: PinReq, request: Request, uid: str = Depends(get_user)):
    if len(b.pin) != 4 or not b.pin.isdigit():
        raise HTTPException(400, "PIN 4 ta raqam bo'lishi kerak")
    await database.execute(
        "UPDATE users SET pin_hash=:h WHERE id=:id",
        {"h": hash_pin(b.pin), "id": uid}
    )
    await audit.log("pin_set", user_id=uid, ip_address=get_client_ip(request))
    return {"success": True}


# ← BU YERDA INDENT TO'G'RI — set_pin ichida EMAS
@router.post("/verify-pin")
async def verify_pin_route(b: PinVerifyReq, request: Request):
    ip = get_client_ip(request)
    await check_rate_limit("pin_verify", "pin_verify", b.phone)

    user = await database.fetch_one(
        "SELECT * FROM users WHERE phone=:p AND is_active=TRUE", {"p": b.phone}
    )
    if not user or not user["pin_hash"]:
        raise HTTPException(400, "PIN o'rnatilmagan")
    if user["is_blocked"]:
        raise HTTPException(403, "Hisobingiz bloklangan")
    if not verify_pin(b.pin, user["pin_hash"]):
        await audit.log("pin_failed", user_id=str(user["id"]), ip_address=ip)
        raise HTTPException(400, "PIN noto'g'ri")

    token   = make_token(str(user["id"]), b.phone)
    expires = datetime.utcnow() + timedelta(days=30)
    ua      = request.headers.get("User-Agent", "")[:255]
    await database.execute(
        "INSERT INTO sessions(user_id,token,expires_at,device_info) VALUES(:u,:t,:e,:d)",
        {"u": str(user["id"]), "t": token, "e": expires, "d": ua}
    )
    await audit.log("login_pin", user_id=str(user["id"]), ip_address=ip)
    return {"success": True, "token": token}


@router.get("/profile")
async def profile(uid: str = Depends(get_user)):
    row = await database.fetch_one(
        """SELECT u.id, u.phone, u.full_name, u.is_verified, u.language,
                  u.pin_hash,
                  w.balance, w.currency, w.is_frozen
           FROM users u
           LEFT JOIN wallets w ON w.user_id=u.id
           WHERE u.id=:id""",
        {"id": uid}
    )
    if not row:
        raise HTTPException(404, "Topilmadi")
    d = dict(row)
    d["id"]       = str(d["id"])
    d["pin_hash"] = bool(d.get("pin_hash"))
    return {"success": True, "user": d}


@router.put("/profile")
async def update_profile(b: dict, uid: str = Depends(get_user)):
    await database.execute(
        "UPDATE users SET full_name=:n, updated_at=NOW() WHERE id=:id",
        {"n": b.get("fullName"), "id": uid}
    )
    return {"success": True}


@router.put("/language")
async def set_language(b: LangReq, uid: str = Depends(get_user)):
    await database.execute(
        "UPDATE users SET language=:l WHERE id=:id", {"l": b.language, "id": uid}
    )
    return {"success": True}


@router.post("/logout")
async def logout(request: Request, uid: str = Depends(get_user)):
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    await database.execute(
        "DELETE FROM sessions WHERE user_id=:uid AND token=:t",
        {"uid": uid, "t": token}
    )
    await audit.log("logout", user_id=uid, ip_address=get_client_ip(request))
    return {"success": True}