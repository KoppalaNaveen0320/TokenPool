from backend.app.core.database import SessionLocal, engine
from sqlalchemy import text, inspect

def run_migration():
    insp = inspect(engine)
    cols = [c["name"] for c in insp.get_columns("payment_proofs")]
    db = SessionLocal()
    try:
        if "extracted_transaction_id" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN extracted_transaction_id VARCHAR(100);"))
        if "ocr_status" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN ocr_status VARCHAR(50) DEFAULT 'PENDING';"))
        if "verification_status" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN verification_status VARCHAR(50) DEFAULT 'PENDING';"))
        if "verification_reason" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN verification_reason TEXT;"))
        if "verified_at" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN verified_at TIMESTAMP;"))
        if "file_hash" not in cols:
            db.execute(text("ALTER TABLE payment_proofs ADD COLUMN file_hash VARCHAR(64);"))
        # Partial unique index to enforce unique verified transaction IDs at DB level
        db.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS uq_verified_transaction_id ON payment_proofs (LOWER(transaction_id)) WHERE verification_status = 'VERIFIED';"))
        db.commit()
        print("Payment proof columns and index migrated successfully.")
    except Exception as e:
        db.rollback()
        print("Migration error:", e)
    finally:
        db.close()

if __name__ == "__main__":
    run_migration()
