import os
import httpx
import json
from datetime import datetime, timedelta
from app.database import database

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

RISK_LOW      = "low"
RISK_MEDIUM   = "medium"
RISK_HIGH     = "high"
RISK_CRITICAL = "critical"


async def get_sender_stats(sender_id: str) -> dict:
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    day_ago  = datetime.utcnow() - timedelta(hours=24)

    last_hour = await database.fetch_one(
        """SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as vol
           FROM transactions
           WHERE sender_id=:uid AND created_at > :t AND status='completed'""",
        {"uid": sender_id, "t": hour_ago}
    )
    last_day = await database.fetch_one(
        """SELECT COUNT(*) as cnt, COALESCE(SUM(amount),0) as vol
           FROM transactions
           WHERE sender_id=:uid AND created_at > :t AND status='completed'""",
        {"uid": sender_id, "t": day_ago}
    )
    user = await database.fetch_one(
        "SELECT created_at FROM users WHERE id=:uid", {"uid": sender_id}
    )
    account_days = 0
    if user and user["created_at"]:
        account_days = (datetime.utcnow() - user["created_at"]).days

    same_receiver = await database.fetch_one(
        """SELECT receiver_id, COUNT(*) as cnt
           FROM transactions
           WHERE sender_id=:uid AND created_at > :t
           GROUP BY receiver_id ORDER BY cnt DESC LIMIT 1""",
        {"uid": sender_id, "t": day_ago}
    )
    recent_amounts = await database.fetch_all(
        """SELECT amount FROM transactions
           WHERE sender_id=:uid AND status='completed'
           ORDER BY created_at DESC LIMIT 5""",
        {"uid": sender_id}
    )

    return {
        "hour_count":      last_hour["cnt"]  if last_hour else 0,
        "hour_volume":     float(last_hour["vol"]) if last_hour else 0,
        "day_count":       last_day["cnt"]   if last_day else 0,
        "day_volume":      float(last_day["vol"])  if last_day else 0,
        "account_days":    account_days,
        "same_recv_count": same_receiver["cnt"] if same_receiver else 0,
        "current_hour":    (datetime.utcnow().hour + 5) % 24,  # Toshkent vaqti
        "recent_amounts":  [float(r["amount"]) for r in recent_amounts],
    }


def _rule_check(amount: float, stats: dict) -> tuple[str, list[str]]:
    flags = []
    score = 0

    if stats["account_days"] < 3 and amount > 1_000_000:
        flags.append(f"Yangi hisob ({stats['account_days']} kun), katta summa")
        score += 35

    if stats["hour_count"] >= 10:
        flags.append(f"1 soatda {stats['hour_count']} ta o'tkazma")
        score += 40

    if stats["hour_volume"] + amount > 10_000_000:
        flags.append(f"1 soatda 10 mln+ UZS")
        score += 35

    if stats["current_hour"] in range(0, 5) and amount > 5_000_000:
        flags.append(f"Tunda ({stats['current_hour']}:00) katta summa")
        score += 25

    if stats["same_recv_count"] >= 8:
        flags.append(f"Bir kunda bir odamga {stats['same_recv_count']} ta")
        score += 20

    amts = stats["recent_amounts"]
    if len(amts) >= 3 and len(set(amts[:3])) == 1:
        flags.append(f"Bir xil summa 3 marta ketma-ket: {amts[0]:,.0f}")
        score += 20

    if amount >= 50_000_000:
        flags.append("Juda katta summa: 50 mln+")
        score += 30
    elif amount >= 10_000_000:
        score += 10

    score = min(score, 100)

    if score >= 80:   level = RISK_CRITICAL
    elif score >= 60: level = RISK_HIGH
    elif score >= 30: level = RISK_MEDIUM
    else:             level = RISK_LOW

    return level, flags, score


async def _ai_check(amount: float, stats: dict, flags: list[str]) -> tuple[str, str, int]:
    if not OPENAI_API_KEY:
        return None, None, None

    prompt = f"""Sen O'zbekiston moliyaviy fraud detection tizimisan.
P2P o'tkazma ma'lumotlari:
- Summa: {amount:,.0f} UZS
- Vaqt: {stats['current_hour']}:00 (Toshkent)
- Hisob yoshi: {stats['account_days']} kun
- 1 soatda: {stats['hour_count']} ta, {stats['hour_volume']:,.0f} UZS
- 24 soatda: {stats['day_count']} ta, {stats['day_volume']:,.0f} UZS
- Bir odamga bugun: {stats['same_recv_count']} ta
- Qoidalar tomonidan: {', '.join(flags) if flags else 'hech narsa'}

Faqat JSON qaytар:
{{"score": 0-100, "level": "low|medium|high|critical", "action": "allow|warn|block", "reason": "o'zbek tilida 1 jumla"}}"""

    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.post(
                "https://api.openai.com/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENAI_API_KEY}"},
                json={
                    "model": "gpt-4o-mini",
                    "messages": [{"role": "user", "content": prompt}],
                    "max_tokens": 120,
                    "temperature": 0
                }
            )
            text = r.json()["choices"][0]["message"]["content"].strip()
            text = text.replace("```json", "").replace("```", "").strip()
            res = json.loads(text)
            return res.get("level"), res.get("reason"), res.get("score")
    except Exception as e:
        print(f"[AI Fraud] Error: {e}")
        return None, None, None


async def check_transaction(
    sender_id: str,
    receiver_id: str,
    amount: float,
    description: str = "",
) -> dict:
    stats = await get_sender_stats(sender_id)
    level, flags, score = _rule_check(amount, stats)
    reason = ", ".join(flags) if flags else "Tranzaksiya normal"

    # Faqat shubhali holatlarda AI ga yuborish
    if level in (RISK_MEDIUM, RISK_HIGH) and OPENAI_API_KEY:
        ai_level, ai_reason, ai_score = await _ai_check(amount, stats, flags)
        if ai_level:
            order = {RISK_LOW: 0, RISK_MEDIUM: 1, RISK_HIGH: 2, RISK_CRITICAL: 3}
            if order.get(ai_level, 0) > order.get(level, 0):
                level  = ai_level
                reason = ai_reason
                score  = ai_score or score

    blocked = level == RISK_CRITICAL
    action  = "block" if blocked else ("warn" if level in (RISK_MEDIUM, RISK_HIGH) else "allow")

    # Fraud log yozish
    try:
        await database.execute(
            """INSERT INTO fraud_logs
               (sender_id, receiver_id, amount, risk_score, risk_level, action, reasons, blocked)
               VALUES (:s, :r, :a, :sc, :l, :act, :rs, :bl)""",
            {
                "s": sender_id, "r": receiver_id, "a": amount,
                "sc": score, "l": level, "act": action,
                "rs": json.dumps(flags), "bl": blocked
            }
        )
    except Exception as e:
        print(f"[Fraud log] Error: {e}")

    return {
        "risk":    level,
        "score":   score,
        "blocked": blocked,
        "action":  action,
        "reason":  reason,
        "flags":   flags,
    }
