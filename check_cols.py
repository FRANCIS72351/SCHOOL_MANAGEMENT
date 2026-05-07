from app import app, db
from models import StudentPayment
with app.app_context():
    print([c.name for c in StudentPayment.__table__.columns])
