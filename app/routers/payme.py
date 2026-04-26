async def perform_transaction(req_id, params):
    payme_tx_id = params.get("id")

    tx = await database.fetch_one(
        "SELECT * FROM payme_transactions WHERE payme_id=:pid",
        {"pid": payme_tx_id}
    )
    if not tx:
        return err(req_id, ERR_TX_NOT_FOUND, "Tranzaksiya topilmadi")

    if tx["state"] == 2:
        return ok(req_id, {
            "transaction": str(tx["id"]),
            "perform_time": tx["perform_time"],
            "state": 2
        })

    if tx["state"] != 1:
        return err(req_id, ERR_CANT_PERFORM, "Tranzaksiya holati xato")

    perform_time = int(time.time() * 1000)
    amount_uzs = tx["amount"] / 100

    user_id_str = str(tx["user_id"])  # 🔥 MUHIM FIX

    async with database.transaction():
        # ✅ Wallet balansni yangilash
        await database.execute(
            """
            UPDATE wallets
            SET balance = balance + :a, updated_at = NOW()
            WHERE user_id = :uid
            """,
            {"a": amount_uzs, "uid": user_id_str}
        )

        # ✅ Agar wallet yo‘q bo‘lsa yaratib yuboradi (MUHIM)
        await database.execute(
            """
            INSERT INTO wallets (user_id, balance)
            SELECT :uid, :a
            WHERE NOT EXISTS (
                SELECT 1 FROM wallets WHERE user_id = :uid
            )
            """,
            {"uid": user_id_str, "a": amount_uzs}
        )

        # ✅ Transactions yozish
        await database.execute(
            """
            INSERT INTO transactions
            (receiver_id, amount, type, status, description, reference)
            VALUES (:uid, :a, 'topup', 'completed', 'Payme orqali toldirish', :ref)
            """,
            {"uid": user_id_str, "a": amount_uzs, "ref": payme_tx_id}
        )

        # ✅ Payme transaction update
        await database.execute(
            """
            UPDATE payme_transactions
            SET state=2, perform_time=:pt
            WHERE payme_id=:pid
            """,
            {"pt": perform_time, "pid": payme_tx_id}
        )

    return ok(req_id, {
        "transaction": str(tx["id"]),
        "perform_time": perform_time,
        "state": 2
    })