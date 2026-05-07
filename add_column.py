from app import app, db
from sqlalchemy import text

with app.app_context():
    try:
        # Check if column exists
        result = db.session.execute(text('PRAGMA table_info(student)'))
        columns = [row[1] for row in result.fetchall()]

        if 'photo_filename' not in columns:
            # Add the column
            db.session.execute(text("ALTER TABLE student ADD COLUMN photo_filename VARCHAR(200) DEFAULT 'default_student.png'"))
            db.session.commit()
            print('photo_filename column added successfully')
        else:
            print('photo_filename column already exists')

    except Exception as e:
        print(f'Error: {e}')