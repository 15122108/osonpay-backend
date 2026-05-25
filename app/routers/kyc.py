from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user
from app.services.ekyc import verify_passport

router = APIRouter()

class KYCReq(BaseModel):
    passportSeries: str
    passportNumber: str
    birthDate: str
    fullName: str

@router.post("")
async def submit_kyc(b: KYCReq, uid: str = Depends(get_user)):
    verification = await verify_passport(
        passport_series=b.passportSeries,
        passport_number=b.passportNumber,
        birth_date=b.birthDate,
        full_name=b.fullName,
    )
    status = "approved" if verification.get("verified") else "pending"
    ex = await database.fetch_one("SELECT id FROM kyc_data WHERE user_id=:uid", {"uid": uid})
    if ex:
        await database.execute(
            "UPDATE kyc_data SET passport_series=:s,passport_number=:n,birth_date=:b,full_name=:f,status=:status, reviewed_at=CASE WHEN :status='approved' THEN NOW() ELSE reviewed_at END WHERE user_id=:uid",
            {"s": b.passportSeries, "n": b.passportNumber, "b": b.birthDate, "f": b.fullName, "status": status, "uid": uid})
    else:
        await database.execute(
            "INSERT INTO kyc_data(user_id,passport_series,passport_number,birth_date,full_name,status,reviewed_at) VALUES(:uid,:s,:n,:b,:f,:status,CASE WHEN :status='approved' THEN NOW() ELSE NULL END)",
            {"uid": uid, "s": b.passportSeries, "n": b.passportNumber, "b": b.birthDate, "f": b.fullName, "status": status})
    await database.execute(
        "UPDATE users SET full_name=:f, is_verified=:verified WHERE id=:uid",
        {"f": b.fullName, "verified": status == "approved", "uid": uid},
    )
    return {
        "success": True,
        "status": status,
        "message": "Ma'lumotlar tasdiqlandi" if status == "approved" else "Ma'lumotlar qabul qilindi",
    }

@router.get("")
async def get_kyc(uid: str = Depends(get_user)):
    kyc = await database.fetch_one("SELECT * FROM kyc_data WHERE user_id=:uid", {"uid": uid})
    return {"success": True, "kyc": dict(kyc) if kyc else None}
