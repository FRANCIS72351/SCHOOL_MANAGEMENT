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
    {"table": "student", "description": "Add level column", "sql": "ALTER TABLE student ADD COLUMN level VARCHAR(50);"},
    {"table": "student", "description": "Add student_id_code column", "sql": "ALTER TABLE student ADD COLUMN student_id_code VARCHAR(20);"},
    {"table": "student", "description": "Add klass_id column", "sql": "ALTER TABLE student ADD COLUMN klass_id INTEGER REFERENCES class(id);"},
    {"table": "student", "description": "Add academic_year_id column", "sql": "ALTER TABLE student ADD COLUMN academic_year_id INTEGER REFERENCES academic_year(id);"},
    {"table": "student", "description": "Add registration_type column", "sql": "ALTER TABLE student ADD COLUMN registration_type VARCHAR(20) DEFAULT 'New';"},
    {"table": "student", "description": "Add created_at column", "sql": "ALTER TABLE student ADD COLUMN created_at DATETIME DEFAULT CURRENT_TIMESTAMP;"},
    {"table": "student", "description": "Add tuition_cleared column", "sql": "ALTER TABLE student ADD COLUMN tuition_cleared BOOLEAN DEFAULT 0;"},
    {"table": "student", "description": "Add registrar column", "sql": "ALTER TABLE student ADD COLUMN registrar VARCHAR(100);"},
    {"table": "student", "description": "Add registration_fees column", "sql": "ALTER TABLE student ADD COLUMN registration_fees FLOAT DEFAULT 0.0;"},
    
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
