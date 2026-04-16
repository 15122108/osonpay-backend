import os
import json
import httpx

FCM_SERVER_KEY = os.getenv("FCM_SERVER_KEY", "")

async def send_push(tokens: list[str], title: str, body: str, data: dict = None) -> bool:
    if not FCM_SERVER_KEY or not tokens:
        print(f"[PUSH] {title}: {body}")
        return False
    try:
        payload = {
            "registration_ids": tokens,
            "notification": {
                "title": title,
                "body": body,
                "sound": "default",
                "android_channel_id": "osonpay_transactions"
            },
            "data": data or {},
            "priority": "high",
            "android": {
                "priority": "high",
                "notification": {"channel_id": "osonpay_transactions", "sound": "default"}
            },
            "apns": {
                "payload": {"aps": {"sound": "default", "badge": 1}}
            }
        }
        async with httpx.AsyncClient() as client:
            r = await client.post(
                "https://fcm.googleapis.com/fcm/send",
                headers={
                    "Authorization": f"key={FCM_SERVER_KEY}",
                    "Content-Type": "application/json"
                },
                json=payload,
                timeout=10
            )
            result = r.json()
            print(f"[PUSH] sent={result.get('success', 0)} fail={result.get('failure', 0)}")
            return result.get("success", 0) > 0
    except Exception as e:
        print(f"[PUSH] Error: {e}")
        return False

async def get_user_tokens(database, user_id: str) -> list[str]:
    rows = await database.fetch_all(
        "SELECT token FROM fcm_tokens WHERE user_id=:uid",
        {"uid": user_id}
    )
    return [r["token"] for r in rows]

async def notify_transaction(database, receiver_id: str, sender_name: str, amount: float, ref: str):
    tokens = await get_user_tokens(database, receiver_id)
    if tokens:
        from app.utils.auth import mask_card
        amt_fmt = f"{amount:,.0f}".replace(",", " ")
        await send_push(
            tokens,
            title="Pul keldi",
            body=f"{sender_name} dan {amt_fmt} UZS",
            data={"type": "transaction", "reference": ref}
        )
