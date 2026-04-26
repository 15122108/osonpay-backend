import json
from fastapi import APIRouter, HTTPException, Depends, Request
from pydantic import BaseModel
from app.database import database
from app.utils.deps import get_user
from app.utils.auth import mask_card, tokenize_card, gen_ref
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

class CardTransferReq(BaseModel):
    from_card_id: str
    to_card_number: str
    amount: float

class QRPayReq(BaseModel):
    qr_data: str
    amount: float


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


@router.post("/transfer")
async def card_transfer(b: CardTransferReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)

    if b.amount < 1000:
        raise HTTPException(400, "Minimum 1,000 UZS")
    if b.amount > 50_000_000:
        raise HTTPException(400, "Maksimum 50,000,000 UZS")

    from_card = await database.fetch_one(
        "SELECT * FROM cards WHERE id=:id AND user_id=:uid AND is_active=TRUE",
        {"id": b.from_card_id, "uid": uid}
    )
    if not from_card:
        raise HTTPException(404, "Karta topilmadi")

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id=:uid",
        {"uid": uid}
    )
    if not wallet or float(wallet["balance"]) < b.amount:
        raise HTTPException(400, "Mablag' yetarli emas")

    clean = b.to_card_number.replace(" ", "")
    if len(clean) != 16 or not clean.isdigit():
        raise HTTPException(400, "Karta raqami noto'g'ri")

    token = tokenize_card(clean)
    to_card = await database.fetch_one(
        """SELECT c.*, u.id as owner_id FROM cards c
           JOIN users u ON u.id=c.user_id
           WHERE c.card_number_token=:t AND c.is_active=TRUE""",
        {"t": token}
    )
    if not to_card:
        raise HTTPException(404, "Qabul qiluvchi karta topilmadi")

    if str(to_card["owner_id"]) == uid:
        raise HTTPException(400, "O'z kartangizga o'tkazib bo'lmaydi")

    ref = gen_ref()

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance-:a, updated_at=NOW() WHERE user_id=:uid",
            {"a": b.amount, "uid": uid}
        )
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid",
            {"a": b.amount, "uid": str(to_card["owner_id"])}
        )
        tx = await database.fetch_one(
            """INSERT INTO transactions
               (sender_id, receiver_id, amount, type, status, description, reference)
               VALUES (:s, :r, :a, 'card_transfer', 'completed', 'Karta orqali otkazma', :ref)
               RETURNING *""",
            {"s": uid, "r": str(to_card["owner_id"]), "a": b.amount, "ref": ref}
        )

    await audit.log(
        "card_transfer", user_id=uid,
        entity_type="transaction", entity_id=str(tx["id"]),
        details={"amount": b.amount, "to_card": b.to_card_number[-4:], "ref": ref},
        ip_address=ip
    )

    return {"success": True, "reference": ref, "transaction": dict(tx)}


@router.get("/qr")
async def get_qr(uid: str = Depends(get_user)):
    user = await database.fetch_one(
        "SELECT phone, full_name FROM users WHERE id=:uid",
        {"uid": uid}
    )
    qr_data = json.dumps({
        "type": "osonpay",
        "user_id": uid,
        "phone": user["phone"],
        "name": user["full_name"]
    })
    return {"success": True, "qr_data": qr_data}


@router.post("/qr/pay")
async def pay_by_qr(b: QRPayReq, request: Request, uid: str = Depends(get_user)):
    ip = get_client_ip(request)

    try:
        qr_data = json.loads(b.qr_data)
    except Exception:
        raise HTTPException(400, "Noto'g'ri QR kod")

    if qr_data.get("type") != "osonpay":
        raise HTTPException(400, "Noto'g'ri QR kod")

    receiver_id = qr_data.get("user_id")

    if receiver_id == uid:
        raise HTTPException(400, "O'zingizga to'lab bo'lmaydi")

    if b.amount < 1000:
        raise HTTPException(400, "Minimum 1,000 UZS")

    wallet = await database.fetch_one(
        "SELECT balance FROM wallets WHERE user_id=:uid",
        {"uid": uid}
    )
    if not wallet or float(wallet["balance"]) < b.amount:
        raise HTTPException(400, "Mablag' yetarli emas")

    receiver = await database.fetch_one(
        "SELECT id FROM users WHERE id=:rid",
        {"rid": receiver_id}
    )
    if not receiver:
        raise HTTPException(404, "Qabul qiluvchi topilmadi")

    ref = gen_ref()

    async with database.transaction():
        await database.execute(
            "UPDATE wallets SET balance=balance-:a, updated_at=NOW() WHERE user_id=:uid",
            {"a": b.amount, "uid": uid}
        )
        await database.execute(
            "UPDATE wallets SET balance=balance+:a, updated_at=NOW() WHERE user_id=:uid",
            {"a": b.amount, "uid": receiver_id}
                  )
        tx = await database.fetch_one(
            """INSERT INTO transactions
               (sender_id, receiver_id, amount, type, status, description, reference)
               VALUES (:s, :r, :a, 'qr_payment', 'completed', 'QR orqali tolov', :ref)
               RETURNING *""",
            {"s": uid, "r": receiver_id, "a": b.amount, "ref": ref}
        )

    await audit.log(
        "qr_payment", user_id=uid,
        entity_type="transaction", entity_id=str(tx["id"]),
        details={"amount": b.amount, "receiver": receiver_id, "ref": ref},
        ip_address=ip
    )

    return {"success": True, "reference": ref, "transaction": dict(tx)}