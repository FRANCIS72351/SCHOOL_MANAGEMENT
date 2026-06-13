import getpass
import os
import sys


current_dir = os.path.dirname(os.path.abspath(__file__))
if current_dir not in sys.path:
    sys.path.insert(0, current_dir)

try:
    from app import app, db, User
except ModuleNotFoundError as exc:
    print("=" * 70)
    print(f"Initialization Error: {exc}")
    print("Make sure this script is beside app.py inside the SCHOOL_MANAGEMENT folder.")
    print("=" * 70)
    sys.exit(1)


ADMIN_EMAIL = os.environ.get("ADMIN_EMAIL", "admin@school.com")
ADMIN_NAME = os.environ.get("ADMIN_NAME", "Francis Brownell")


def resolve_admin_password():
    env_password = os.environ.get("ADMIN_PASSWORD", "").strip()
    if env_password:
        return env_password
    password = getpass.getpass("Enter master administrator password: ").strip()
    if not password:
        print("Password is required.")
        sys.exit(1)
    return password


print("=" * 70)
print("INITIALIZING MASTER ADMINISTRATOR")
print("=" * 70)

admin_password = resolve_admin_password()

with app.app_context():
    try:
        user = User.query.filter_by(email=ADMIN_EMAIL).first()
        if user:
            user.full_name = user.full_name or ADMIN_NAME
            user.role = "admin"
            user.username = user.username or "admin"
            user.set_password(admin_password)
            print("SUCCESS: Master administrator account reset.")
        else:
            user = User(
                email=ADMIN_EMAIL,
                full_name=ADMIN_NAME,
                role="admin",
                username="admin",
            )
            user.set_password(admin_password)
            db.session.add(user)
            print("SUCCESS: Master administrator account created.")

        db.session.commit()
        print(f"Email: {ADMIN_EMAIL}")
        if os.environ.get("ADMIN_PASSWORD"):
            print("Password: (from ADMIN_PASSWORD environment variable)")
        else:
            print("Password: (hidden — entered at prompt)")
    except Exception as exc:
        db.session.rollback()
        print(f"Error creating admin account: {exc}")
        sys.exit(1)

print("=" * 70)
