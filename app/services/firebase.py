import httpx
import os

FIREBASE_PROJECT_ID = os.getenv("FIREBASE_PROJECT_ID", "mavlonovfarhod")
FIREBASE_API_KEY = os.getenv("FIREBASE_API_KEY", "")

async def verify_firebase_token(id_token: str) -> dict:
    try:
        async with httpx.AsyncClient() as client:
            res = await client.post(
                f"https://identitytoolkit.googleapis.com/v1/accounts:lookup?key={FIREBASE_API_KEY}",
                json={"idToken": id_token}
            )
            data = res.json()
            
            if "error" in data:
                raise Exception(f"Token yaroqsiz: {data['error'].get('message', '')}")
            
            if "users" not in data or len(data["users"]) == 0:
                raise Exception("Token yaroqsiz")
            
            user = data["users"][0]
            phone = user.get("phoneNumber", "")
            
            if not phone:
                raise Exception("Telefon raqami topilmadi")
            
            return {
                "phone": phone,
                "uid": user.get("localId", "")
            }
    except Exception as e:
        raise Exception(f"Firebase token xatosi: {str(e)}")