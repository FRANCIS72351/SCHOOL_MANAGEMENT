"""Verify VPA archived class roster fix."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("FLASK_ENV", "development")

from app import (
    app,
    User,
    AcademicYear,
    Class,
    _principal_students_for_class,
    _year_uses_history,
)


def main():
    with app.app_context():
        y = AcademicYear.query.filter_by(name="2025-2026").first()
        klass = Class.query.get(5)
        assert _year_uses_history(y, viewing_archived=False), "inactive year must use history"
        assert len(_principal_students_for_class(klass, y, viewing_archived=False)) == 1

        vpa = User.query.get(16)
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["_user_id"] = str(vpa.id)
            sess["_fresh"] = True
            sess.pop("vpa_display_year_id", None)

        html = client.get("/vpa/dashboard?academic_year_id=1&class_id=5").get_data(as_text=True)
        assert "Handsin Doma" in html, "archived class roster missing in HTML"
        assert "Historical academic view" in html
        print("PASS: archived year class drill-down shows historical roster")


if __name__ == "__main__":
    main()
