from fastapi import APIRouter, HTTPException, Depends
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user

router = APIRouter()

class KYCReq(BaseModel):
    passportSeries: str
    passportNumber: str
    birthDate: str
    fullName: str

@router.post("")
async def submit_kyc(b: KYCReq, uid: str = Depends(get_user)):
    ex = await database.fetch_one("SELECT id FROM kyc_data WHERE user_id=:uid", {"uid": uid})
    if ex:
        await database.execute(
            "UPDATE kyc_data SET passport_series=:s,passport_number=:n,birth_date=:b,full_name=:f,status='pending' WHERE user_id=:uid",
            {"s": b.passportSeries, "n": b.passportNumber, "b": b.birthDate, "f": b.fullName, "uid": uid})
    else:
        await database.execute(
            "INSERT INTO kyc_data(user_id,passport_series,passport_number,birth_date,full_name) VALUES(:uid,:s,:n,:b,:f)",
            {"uid": uid, "s": b.passportSeries, "n": b.passportNumber, "b": b.birthDate, "f": b.fullName})
    await database.execute("UPDATE users SET full_name=:f WHERE id=:uid", {"f": b.fullName, "uid": uid})
    return {"success": True, "message": "Ma'lumotlar qabul qilindi"}

@router.get("")
async def get_kyc(uid: str = Depends(get_user)):
    kyc = await database.fetch_one("SELECT * FROM kyc_data WHERE user_id=:uid", {"uid": uid})
    return {"success": True, "kyc": dict(kyc) if kyc else None}
