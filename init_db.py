from app import app, db
from sqlalchemy import text, exc

# Comprehensive schema updates for all new models and fields
SCHEMA_UPDATES = (
    # User model updates
    {"table": "users", "description": "Add photo column", "sql": "ALTER TABLE users ADD COLUMN photo VARCHAR(255);"},
    {"table": "users", "description": "Add totp_secret column", "sql": "ALTER TABLE users ADD COLUMN totp_secret VARCHAR(32);"},
    {"table": "users", "description": "Add username column", "sql": "ALTER TABLE users ADD COLUMN username VARCHAR(80);"},
    
    # Grade model updates
    {"table": "grade", "description": "Add period column", "sql": "ALTER TABLE grade ADD COLUMN period INTEGER;"},
    {"table": "grade", "description": "Add activity_type column", "sql": "ALTER TABLE grade ADD COLUMN activity_type VARCHAR(50);"},
    {"table": "grade", "description": "Add ca_score column", "sql": "ALTER TABLE grade ADD COLUMN ca_score FLOAT;"},
    {"table": "grade", "description": "Add exam_score column", "sql": "ALTER TABLE grade ADD COLUMN exam_score FLOAT;"},
    {"table": "grade", "description": "Add marking_period column", "sql": "ALTER TABLE grade ADD COLUMN marking_period INTEGER;"},
    
    # Class model updates
    {"table": "class", "description": "Add yearly_fee column", "sql": "ALTER TABLE class ADD COLUMN yearly_fee FLOAT DEFAULT 0.0;"},
    
    # Student model updates
    {"table": "student", "description": "Add status column", "sql": "ALTER TABLE student ADD COLUMN status VARCHAR(20) DEFAULT 'ACTIVE';"},
    {"table": "student", "description": "Add grade_level column", "sql": "ALTER TABLE student ADD COLUMN grade_level INTEGER;"},
    
    # BusinessTransaction model updates
    {"table": "business_transaction", "description": "Add academic_year column", "sql": "ALTER TABLE business_transaction ADD COLUMN academic_year VARCHAR(32);"},
    {"table": "business_transaction", "description": "Add is_deleted column", "sql": "ALTER TABLE business_transaction ADD COLUMN is_deleted BOOLEAN DEFAULT 0;"},
    {"table": "business_transaction", "description": "Add deleted_at column", "sql": "ALTER TABLE business_transaction ADD COLUMN deleted_at DATETIME;"},
    {"table": "business_transaction", "description": "Add deleted_by_id column", "sql": "ALTER TABLE business_transaction ADD COLUMN deleted_by_id INTEGER;"},
)

if __name__ == "__main__":
    with app.app_context():
        print("Ensuring all tables exist...")
        db.create_all()  # This will create missing tables like security_logs, suspensions, etc.

        print("Applying schema updates for existing tables...")
        for update in SCHEMA_UPDATES:
            try:
                db.session.execute(text(update["sql"]))
                db.session.commit()
                print(f"Applied: {update['description']} on '{update['table']}'")
            except exc.OperationalError as e:
                db.session.rollback()
                if "duplicate column name" in str(e):
                    print(f"Skipped: {update['description']} (column already exists)")
                else:
                    print(f"Error applying '{update['description']}': {e}")
            except Exception as e:
                db.session.rollback()
                print(f"Error applying '{update['description']}': {e}")

        print("\nDatabase synchronization complete.")
