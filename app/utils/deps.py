from fastapi import Depends, HTTPException
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from app.utils.auth import decode_token
from app.database import database

security = HTTPBearer()

async def get_user(creds: HTTPAuthorizationCredentials = Depends(security)) -> str:
    try:
        data = decode_token(creds.credentials)
        uid = data["userId"]
        s = await database.fetch_one(
            "SELECT id FROM sessions WHERE token=:t AND expires_at>NOW()",
            {"t": creds.credentials}
        )
        if not s:
            raise Exception("Session topilmadi")
        return uid
    except Exception:
        raise HTTPException(401, "Token yaroqsiz")
