from app.database import database
from typing import Optional
import json

async def log(
    action: str,
    user_id: Optional[str] = None,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None
):
    try:
        await database.execute(
            """INSERT INTO audit_logs
               (user_id, action, entity_type, entity_id, details, ip_address, user_agent)
               VALUES (:uid, :action, :etype, :eid, :details, :ip, :ua)""",
            {
                "uid": user_id,
                "action": action,
                "etype": entity_type,
                "eid": entity_id,
                "details": json.dumps(details) if details else None,
                "ip": ip_address,
                "ua": user_agent
            }
        )
    except Exception as e:
        print(f"Audit log error: {e}")
