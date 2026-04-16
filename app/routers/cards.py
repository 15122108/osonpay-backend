from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user
from app.utils.auth import mask_card, tokenize_card
from app.utils import audit
from app.utils.rate_limit import get_client_ip

router = APIRouter()

COLORS = {
    "uzcard":     ("#7B2FBE", "#FF6B00"),
    "humo":       ("#00C896", "#0099AA"),
    "visa":       ("#1A1A2E", "#4040CC"),
    "mastercard": ("#FF3B5C", "#FF8C00"),
}

class CardReq(BaseModel):
    cardNumber: str
    cardHolder: str
    expiryMonth: str
    expiryYear: str
    cardType: str = "uzcard"

@router.get("")
async def get_cards(uid: str = Depends(get_user)):
    cards = await database.fetch_all(
        """SELECT id, card_number_masked, card_holder, expiry_month, expiry_year,
                  card_type, is_default, color_from, color_to, created_at
           FROM cards WHERE user_id=:uid AND is_active=TRUE
           ORDER BY is_default DESC""",
        {"uid": uid}
    )
    return {"success": True, "cards": [dict(c) for c in cards]}

@router.post("")
async def add_card(b: CardReq, request: Request, uid: str = Depends(get_user)):
    clean = b.cardNumber.replace(" ", "")
    if len(clean) != 16 or not clean.isdigit():
        raise HTTPException(400, "16 ta raqam kerak")

    # Token orqali duplicate tekshirish (raqam saqlanmaydi)
    token = tokenize_card(clean)
    ex = await database.fetch_one(
        "SELECT id FROM cards WHERE card_number_token=:t AND user_id=:uid",
        {"t": token, "uid": uid}
    )
    if ex:
        raise HTTPException(400, "Bu karta qo'shilgan")

    cnt = await database.fetch_one(
        "SELECT COUNT(*) as c FROM cards WHERE user_id=:uid AND is_active=TRUE",
        {"uid": uid}
    )
    is_def = cnt["c"] == 0
    cf, ct = COLORS.get(b.cardType, COLORS["uzcard"])
    masked = mask_card(clean)

    c = await database.fetch_one(
        """INSERT INTO cards
           (user_id, card_number_masked, card_number_token, card_holder,
            expiry_month, expiry_year, card_type, is_default, color_from, color_to)
           VALUES (:u,:masked,:token,:h,:m,:y,:t,:d,:cf,:ct)
           RETURNING id, card_number_masked, card_holder, expiry_month, expiry_year,
                     card_type, is_default, color_from, color_to""",
        {
            "u": uid, "masked": masked, "token": token,
            "h": b.cardHolder.upper(), "m": b.expiryMonth,
            "y": b.expiryYear, "t": b.cardType, "d": is_def,
            "cf": cf, "ct": ct
        }
    )
    await audit.log(
        "card_added", user_id=uid,
        entity_type="card", entity_id=str(c["id"]),
        details={"masked": masked, "type": b.cardType},
        ip_address=get_client_ip(request)
    )
    return {"success": True, "card": dict(c)}

@router.put("/{cid}/default")
async def set_default(cid: str, uid: str = Depends(get_user)):
    async with database.transaction():
        await database.execute(
            "UPDATE cards SET is_default=FALSE WHERE user_id=:uid", {"uid": uid}
        )
        await database.execute(
            "UPDATE cards SET is_default=TRUE WHERE id=:id AND user_id=:uid",
            {"id": cid, "uid": uid}
        )
    return {"success": True}

@router.delete("/{cid}")
async def del_card(cid: str, request: Request, uid: str = Depends(get_user)):
    await database.execute(
        "UPDATE cards SET is_active=FALSE WHERE id=:id AND user_id=:uid",
        {"id": cid, "uid": uid}
    )
    await audit.log(
        "card_deleted", user_id=uid,
        entity_type="card", entity_id=cid,
        ip_address=get_client_ip(request)
    )
    return {"success": True}
