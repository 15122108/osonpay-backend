import os
from app.database import database
from app.utils.auth import tokenize_card


def _commission_card_token() -> str | None:
    card = os.getenv("COMMISSION_CARD_NUMBER", "")
    clean = "".join(ch for ch in card if ch.isdigit())
    if not clean:
        return None
    if len(clean) != 16:
        raise RuntimeError("COMMISSION_CARD_NUMBER 16 ta raqam bo'lishi kerak")
    return tokenize_card(clean)


async def credit_commission(amount: float, source_user_id: str, reference: str, description: str) -> str:
    if amount <= 0:
        return ""

    token = _commission_card_token()
    if not token:
        raise RuntimeError("COMMISSION_CARD_NUMBER sozlanmagan")

    card = await database.fetch_one(
        """SELECT c.id, c.card_number_masked, c.user_id
           FROM cards c
           WHERE c.card_number_token=:token AND c.is_active=TRUE""",
        {"token": token},
    )
    if not card:
        raise RuntimeError("COMMISSION_CARD_NUMBER kartasi bazada topilmadi")

    commission_user_id = str(card["user_id"])
    await database.execute(
        "UPDATE wallets SET balance=balance+:amount, updated_at=NOW() WHERE user_id=:uid",
        {"amount": amount, "uid": commission_user_id},
    )
    await database.execute(
        """INSERT INTO transactions
           (sender_id, receiver_id, amount, fee, type, status, description, reference)
           VALUES (:s, :r, :a, 0, 'commission', 'completed', :d, :ref)""",
        {
            "s": source_user_id,
            "r": commission_user_id,
            "a": amount,
            "d": description,
            "ref": f"{reference}-FEE",
        },
    )
    return commission_user_id
