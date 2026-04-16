from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from app.database import database
from app.utils.auth import make_admin_token, decode_admin_token, hash_pin
from app.utils import audit
from app.utils.rate_limit import get_client_ip
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from passlib.context import CryptContext
import os

router = APIRouter()
security = HTTPBearer()
pwd_ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")

async def get_admin(creds: HTTPAuthorizationCredentials = Depends(security)):
    try:
        data = decode_admin_token(creds.credentials)
        return data
    except Exception:
        raise HTTPException(401, "Admin token yaroqsiz")

class AdminLoginReq(BaseModel):
    username: str
    password: str

class BlockUserReq(BaseModel):
    reason: str = ""

class KYCReviewReq(BaseModel):
    status: str
    reason: str = ""

@router.post("/login")
async def admin_login(b: AdminLoginReq, request: Request):
    ip = get_client_ip(request)
    if not ADMIN_PASSWORD:
        raise HTTPException(503, "Admin sozlanmagan")
    if b.username != ADMIN_USERNAME or b.password != ADMIN_PASSWORD:
        await audit.log("admin_login_failed", ip_address=ip,
                        details={"username": b.username})
        raise HTTPException(401, "Login yoki parol noto'g'ri")
    token = make_admin_token(b.username, "admin")
    await audit.log("admin_login", ip_address=ip, details={"username": b.username})
    return {"success": True, "token": token}

@router.get("/stats")
async def admin_stats(admin=Depends(get_admin)):
    users = await database.fetch_one("SELECT COUNT(*) as c FROM users")
    active = await database.fetch_one("SELECT COUNT(*) as c FROM users WHERE is_active=TRUE")
    tx_today = await database.fetch_one(
        "SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as vol FROM transactions "
        "WHERE DATE(created_at)=CURRENT_DATE AND status='completed'"
    )
    tx_month = await database.fetch_one(
        "SELECT COUNT(*) as c, COALESCE(SUM(amount),0) as vol FROM transactions "
        "WHERE DATE_TRUNC('month',created_at)=DATE_TRUNC('month',CURRENT_DATE) AND status='completed'"
    )
    total_balance = await database.fetch_one(
        "SELECT COALESCE(SUM(balance),0) as total FROM wallets WHERE is_frozen=FALSE"
    )
    kyc_pending = await database.fetch_one(
        "SELECT COUNT(*) as c FROM kyc_data WHERE status='pending'"
    )
    return {
        "users": {"total": users["c"], "active": active["c"]},
        "transactions": {
            "today_count": tx_today["c"],
            "today_volume": float(tx_today["vol"]),
            "month_count": tx_month["c"],
            "month_volume": float(tx_month["vol"])
        },
        "wallets": {"total_balance": float(total_balance["total"])},
        "kyc": {"pending": kyc_pending["c"]}
    }

@router.get("/users")
async def admin_users(
    page: int = 1, limit: int = 50,
    search: str = "",
    admin=Depends(get_admin)
):
    offset = (page - 1) * limit
    if search:
        rows = await database.fetch_all(
            """SELECT u.id, u.phone, u.full_name, u.is_verified, u.is_active,
                      u.is_blocked, u.created_at, w.balance
               FROM users u LEFT JOIN wallets w ON w.user_id=u.id
               WHERE u.phone ILIKE :s OR u.full_name ILIKE :s
               ORDER BY u.created_at DESC LIMIT :l OFFSET :o""",
            {"s": f"%{search}%", "l": limit, "o": offset}
        )
    else:
        rows = await database.fetch_all(
            """SELECT u.id, u.phone, u.full_name, u.is_verified, u.is_active,
                      u.is_blocked, u.created_at, w.balance
               FROM users u LEFT JOIN wallets w ON w.user_id=u.id
               ORDER BY u.created_at DESC LIMIT :l OFFSET :o""",
            {"l": limit, "o": offset}
        )
    count = await database.fetch_one("SELECT COUNT(*) as c FROM users")
    return {
        "users": [dict(r) for r in rows],
        "total": count["c"],
        "page": page
    }

@router.get("/users/{uid}")
async def admin_user_detail(uid: str, admin=Depends(get_admin)):
    user = await database.fetch_one(
        """SELECT u.*, w.balance, w.is_frozen FROM users u
           LEFT JOIN wallets w ON w.user_id=u.id WHERE u.id=:id""",
        {"id": uid}
    )
    if not user:
        raise HTTPException(404, "Topilmadi")
    txs = await database.fetch_all(
        """SELECT t.*, s.phone as sender_phone, r.phone as receiver_phone
           FROM transactions t
           LEFT JOIN users s ON s.id=t.sender_id
           LEFT JOIN users r ON r.id=t.receiver_id
           WHERE t.sender_id=:uid OR t.receiver_id=:uid
           ORDER BY t.created_at DESC LIMIT 20""",
        {"uid": uid}
    )
    kyc = await database.fetch_one(
        "SELECT * FROM kyc_data WHERE user_id=:uid", {"uid": uid}
    )
    logs = await database.fetch_all(
        "SELECT * FROM audit_logs WHERE user_id=:uid ORDER BY created_at DESC LIMIT 20",
        {"uid": uid}
    )
    d = dict(user)
    d["id"] = str(d["id"])
    return {
        "user": d,
        "transactions": [dict(t) for t in txs],
        "kyc": dict(kyc) if kyc else None,
        "logs": [dict(l) for l in logs]
    }

@router.post("/users/{uid}/block")
async def admin_block_user(uid: str, b: BlockUserReq, request: Request, admin=Depends(get_admin)):
    await database.execute(
        "UPDATE users SET is_blocked=TRUE, is_active=FALSE WHERE id=:id", {"id": uid}
    )
    await database.execute(
        "DELETE FROM sessions WHERE user_id=:uid", {"uid": uid}
    )
    await audit.log(
        "admin_block_user", entity_type="user", entity_id=uid,
        details={"reason": b.reason, "admin": admin.get("adminId")},
        ip_address=get_client_ip(request)
    )
    return {"success": True}

@router.post("/users/{uid}/unblock")
async def admin_unblock_user(uid: str, request: Request, admin=Depends(get_admin)):
    await database.execute(
        "UPDATE users SET is_blocked=FALSE, is_active=TRUE WHERE id=:id", {"id": uid}
    )
    await audit.log(
        "admin_unblock_user", entity_type="user", entity_id=uid,
        details={"admin": admin.get("adminId")},
        ip_address=get_client_ip(request)
    )
    return {"success": True}

@router.post("/users/{uid}/freeze-wallet")
async def admin_freeze(uid: str, request: Request, admin=Depends(get_admin)):
    await database.execute(
        "UPDATE wallets SET is_frozen=TRUE WHERE user_id=:uid", {"uid": uid}
    )
    await audit.log(
        "admin_freeze_wallet", entity_type="wallet", entity_id=uid,
        details={"admin": admin.get("adminId")},
        ip_address=get_client_ip(request)
    )
    return {"success": True}

@router.post("/users/{uid}/unfreeze-wallet")
async def admin_unfreeze(uid: str, request: Request, admin=Depends(get_admin)):
    await database.execute(
        "UPDATE wallets SET is_frozen=FALSE WHERE user_id=:uid", {"uid": uid}
    )
    await audit.log(
        "admin_unfreeze_wallet", entity_type="wallet", entity_id=uid,
        details={"admin": admin.get("adminId")},
        ip_address=get_client_ip(request)
    )
    return {"success": True}

@router.get("/transactions")
async def admin_transactions(
    page: int = 1, limit: int = 50,
    type: str = "", status: str = "",
    admin=Depends(get_admin)
):
    offset = (page - 1) * limit
    where = "WHERE 1=1"
    params = {"l": limit, "o": offset}
    if type:
        where += " AND t.type=:type"
        params["type"] = type
    if status:
        where += " AND t.status=:status"
        params["status"] = status

    rows = await database.fetch_all(
        f"""SELECT t.*, s.phone as sender_phone, s.full_name as sender_name,
                   r.phone as receiver_phone, r.full_name as receiver_name
           FROM transactions t
           LEFT JOIN users s ON s.id=t.sender_id
           LEFT JOIN users r ON r.id=t.receiver_id
           {where} ORDER BY t.created_at DESC LIMIT :l OFFSET :o""",
        params
    )
    count = await database.fetch_one(
        f"SELECT COUNT(*) as c FROM transactions t {where}",
        {k: v for k, v in params.items() if k not in ("l", "o")}
    )
    return {"transactions": [dict(r) for r in rows], "total": count["c"]}

@router.get("/kyc")
async def admin_kyc(status: str = "pending", admin=Depends(get_admin)):
    rows = await database.fetch_all(
        """SELECT k.*, u.phone, u.full_name as user_full_name
           FROM kyc_data k JOIN users u ON u.id=k.user_id
           WHERE k.status=:s ORDER BY k.created_at ASC""",
        {"s": status}
    )
    return {"kyc": [dict(r) for r in rows]}

@router.post("/kyc/{kyc_id}/review")
async def admin_kyc_review(
    kyc_id: str, b: KYCReviewReq,
    request: Request, admin=Depends(get_admin)
):
    if b.status not in ("approved", "rejected"):
        raise HTTPException(400, "Status: approved yoki rejected")
    from datetime import datetime
    await database.execute(
        """UPDATE kyc_data
           SET status=:s, reviewed_by=:rb, reviewed_at=NOW(), reject_reason=:rr
           WHERE id=:id""",
        {
            "s": b.status, "rb": admin.get("adminId"),
            "rr": b.reason if b.status == "rejected" else None,
            "id": kyc_id
        }
    )
    await audit.log(
        "admin_kyc_review", entity_type="kyc", entity_id=kyc_id,
        details={"status": b.status, "reason": b.reason},
        ip_address=get_client_ip(request)
    )
    return {"success": True}

@router.get("/audit-logs")
async def admin_audit(page: int = 1, limit: int = 100, admin=Depends(get_admin)):
    offset = (page - 1) * limit
    rows = await database.fetch_all(
        """SELECT a.*, u.phone FROM audit_logs a
           LEFT JOIN users u ON u.id=a.user_id
           ORDER BY a.created_at DESC LIMIT :l OFFSET :o""",
        {"l": limit, "o": offset}
    )
    return {"logs": [dict(r) for r in rows]}
