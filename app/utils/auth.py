import os, secrets, hashlib, hmac
from datetime import datetime, timedelta
from jose import jwt
from passlib.context import CryptContext

SECRET = os.getenv("JWT_SECRET", "")
if not SECRET or len(SECRET) < 32:
    raise RuntimeError("JWT_SECRET atrof-muhit o'zgaruvchisi kamida 32 ta belgidan iborat bo'lishi kerak!")

ALGO = "HS256"
CARD_KEY = os.getenv("CARD_ENCRYPT_KEY", "").encode() or secrets.token_bytes(32)
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

def gen_otp() -> str:
    return str(secrets.randbelow(900000) + 100000)

def gen_ref() -> str:
    return "OP" + secrets.token_hex(6).upper()

def hash_pin(pin: str) -> str:
    return pwd_ctx.hash(pin[:72])

def verify_pin(pin: str, hashed: str) -> bool:
    return pwd_ctx.verify(pin[:72], hashed)

def make_token(uid: str, phone: str) -> str:
    payload = {
        "userId": uid,
        "phone": phone,
        "exp": datetime.utcnow() + timedelta(days=30),
        "iat": datetime.utcnow(),
        "jti": secrets.token_hex(16)
    }
    return jwt.encode(payload, SECRET, ALGO)

def decode_token(token: str) -> dict:
    return jwt.decode(token, SECRET, algorithms=[ALGO])

def mask_card(number: str) -> str:
    n = number.replace(" ", "")
    return f"**** ** ** {n[-4:]}"

def tokenize_card(number: str) -> str:
    n = number.replace(" ", "")
    h = hmac.new(CARD_KEY, n.encode(), hashlib.sha256).hexdigest()
    return h

def make_admin_token(admin_id: str, role: str) -> str:
    payload = {
        "adminId": admin_id,
        "role": role,
        "exp": datetime.utcnow() + timedelta(hours=8),
        "iat": datetime.utcnow(),
        "type": "admin"
    }
    return jwt.encode(payload, SECRET, ALGO)

def decode_admin_token(token: str) -> dict:
    data = jwt.decode(token, SECRET, algorithms=[ALGO])
    if data.get("type") != "admin":
        raise Exception("Admin token emas")
    return data