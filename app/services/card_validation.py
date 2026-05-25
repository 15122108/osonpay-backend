import os
from datetime import datetime


TEST_CARD_PREFIXES = (
    "400000",
    "411111",
    "424242",
    "510510",
    "555555",
)


def normalize_card(number: str) -> str:
    return "".join(ch for ch in (number or "") if ch.isdigit())


def detect_card_type(number: str) -> str:
    if number.startswith("8600"):
        return "uzcard"
    if number.startswith("9860"):
        return "humo"
    if number.startswith("4"):
        return "visa"
    prefix2 = int(number[:2]) if len(number) >= 2 else 0
    prefix4 = int(number[:4]) if len(number) >= 4 else 0
    if 51 <= prefix2 <= 55 or 2221 <= prefix4 <= 2720:
        return "mastercard"
    return "unknown"


def luhn_ok(number: str) -> bool:
    total = 0
    reverse_digits = list(map(int, reversed(number)))
    for idx, digit in enumerate(reverse_digits):
        if idx % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def expiry_ok(month: str, year: str) -> bool:
    try:
        m = int(str(month).strip())
        y = int(str(year).strip())
    except ValueError:
        return False
    if y < 100:
        y += 2000
    if m < 1 or m > 12:
        return False
    now = datetime.utcnow()
    return y > now.year or (y == now.year and m >= now.month)


def is_obvious_fake(number: str) -> bool:
    if len(set(number)) <= 2:
        return True
    if number in ("1234567890123456", "0000000000000000"):
        return True
    return any(number.startswith(prefix) for prefix in TEST_CARD_PREFIXES)


def validate_card_number(number: str) -> tuple[str, str]:
    clean = normalize_card(number)
    if len(clean) != 16:
        raise ValueError("Karta raqami 16 ta raqam bo'lishi kerak")
    if is_obvious_fake(clean):
        raise ValueError("Test yoki soxta karta raqami qabul qilinmaydi")
    card_type = detect_card_type(clean)
    if card_type == "unknown":
        raise ValueError("Faqat Uzcard, Humo, Visa va Mastercard qabul qilinadi")
    strict_luhn = os.getenv("STRICT_CARD_LUHN", "false").lower() == "true"
    if strict_luhn and not luhn_ok(clean):
        raise ValueError("Karta raqami noto'g'ri")
    return clean, card_type
