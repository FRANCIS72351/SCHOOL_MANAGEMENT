import sqlite3
import os

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
db_path = os.path.join(BASE_DIR, 'instance', 'keeptrack_full.db')

def migrate():
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    print("Adding registration_type and created_at to student table...")
    
    try:
        cursor.execute("ALTER TABLE student ADD COLUMN registration_type VARCHAR(20) DEFAULT 'New'")
        print("Column registration_type added.")
    except sqlite3.OperationalError as e:
        print(f"Error adding registration_type: {e}")

    try:
        cursor.execute("ALTER TABLE student ADD COLUMN created_at DATETIME")
        print("Column created_at added.")
        # Set default value for existing records
        cursor.execute("UPDATE student SET created_at = datetime('now') WHERE created_at IS NULL")
    except sqlite3.OperationalError as e:
        print(f"Error adding created_at: {e}")

    conn.commit()
    conn.close()
    print("Migration complete.")

if __name__ == "__main__":
    migrate()
