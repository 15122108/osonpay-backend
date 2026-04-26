from app.database import database

async def run_migrations():
    print("Running migrations...")

    await database.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            phone VARCHAR(20) UNIQUE NOT NULL,
            full_name VARCHAR(100),
            pin_hash VARCHAR(255),
            is_verified BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            is_blocked BOOLEAN DEFAULT FALSE,
            language VARCHAR(5) DEFAULT 'uz',
            avatar_url VARCHAR(255),
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS otps (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            phone VARCHAR(20) NOT NULL,
            code VARCHAR(6) NOT NULL,
            expires_at TIMESTAMP NOT NULL,
            is_used BOOLEAN DEFAULT FALSE,
            attempts INT DEFAULT 0,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS wallets (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            balance DECIMAL(15,2) DEFAULT 0.00,
            currency VARCHAR(3) DEFAULT 'UZS',
            is_frozen BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS cards (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            card_number_masked VARCHAR(20) NOT NULL,
            card_number_token VARCHAR(64) NOT NULL,
            card_holder VARCHAR(100) NOT NULL,
            expiry_month VARCHAR(2) NOT NULL,
            expiry_year VARCHAR(4) NOT NULL,
            card_type VARCHAR(20) DEFAULT 'uzcard',
            is_default BOOLEAN DEFAULT FALSE,
            is_active BOOLEAN DEFAULT TRUE,
            color_from VARCHAR(20) DEFAULT '#7B2FBE',
            color_to VARCHAR(20) DEFAULT '#FF6B00',
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sender_id UUID REFERENCES users(id),
            receiver_id UUID REFERENCES users(id),
            amount DECIMAL(15,2) NOT NULL,
            fee DECIMAL(15,2) DEFAULT 0.00,
            type VARCHAR(20) NOT NULL,
            status VARCHAR(20) DEFAULT 'completed',
            description TEXT,
            reference VARCHAR(50) UNIQUE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            token VARCHAR(500) NOT NULL,
            device_info VARCHAR(255),
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS kyc_data (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID UNIQUE REFERENCES users(id) ON DELETE CASCADE,
            passport_series VARCHAR(20),
            passport_number VARCHAR(20),
            birth_date VARCHAR(20),
            full_name VARCHAR(100),
            status VARCHAR(20) DEFAULT 'pending',
            reviewed_by VARCHAR(100),
            reviewed_at TIMESTAMP,
            reject_reason TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS fcm_tokens (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id) ON DELETE CASCADE,
            token TEXT NOT NULL,
            platform VARCHAR(10) DEFAULT 'android',
            created_at TIMESTAMP DEFAULT NOW(),
            UNIQUE(user_id, token)
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS audit_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id),
            action VARCHAR(100) NOT NULL,
            entity_type VARCHAR(50),
            entity_id VARCHAR(100),
            details TEXT,
            ip_address VARCHAR(45),
            user_agent TEXT,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS rate_limits (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            key VARCHAR(100) NOT NULL,
            count INT DEFAULT 1,
            window_start TIMESTAMP DEFAULT NOW(),
            UNIQUE(key)
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS fraud_logs (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            sender_id UUID REFERENCES users(id),
            receiver_id UUID REFERENCES users(id),
            amount DECIMAL(15,2),
            risk_score INT DEFAULT 0,
            risk_level VARCHAR(20) DEFAULT 'low',
            action VARCHAR(20) DEFAULT 'allow',
            reasons TEXT,
            blocked BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS pending_payments (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            user_id UUID REFERENCES users(id),
            amount DECIMAL(15,2) NOT NULL,
            reference VARCHAR(50) UNIQUE,
            paytech_payment_id VARCHAR(100),
            status VARCHAR(20) DEFAULT 'pending',
            created_at TIMESTAMP DEFAULT NOW(),
            updated_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("""
        CREATE TABLE IF NOT EXISTS payme_transactions (
            id SERIAL PRIMARY KEY,
            payme_id VARCHAR(255) UNIQUE NOT NULL,
            user_id UUID NOT NULL REFERENCES users(id),
            amount BIGINT NOT NULL,
            state INTEGER DEFAULT 1,
            create_time BIGINT,
            perform_time BIGINT,
            cancel_time BIGINT,
            reason INTEGER,
            created_at TIMESTAMP DEFAULT NOW()
        )
    """)

    await database.execute("CREATE INDEX IF NOT EXISTS idx_pending_pid ON pending_payments(paytech_payment_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_otps_phone ON otps(phone)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_tx_sender ON transactions(sender_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_tx_receiver ON transactions(receiver_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_sessions_token ON sessions(token)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_audit_user ON audit_logs(user_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_fcm_user ON fcm_tokens(user_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_fraud_sender ON fraud_logs(sender_id)")
    await database.execute("CREATE INDEX IF NOT EXISTS idx_payme_tx ON payme_transactions(payme_id)")

    print("Migrations done!")