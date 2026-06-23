#!/usr/bin/env python3
"""Quick smoke test: academic year isolation for dashboard queries."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import app, db, students_for_academic_year
from models import AcademicYear, Student, Grade, StudentPayment, Assessment


def _year_ids():
    return [y.id for y in AcademicYear.query.order_by(AcademicYear.name.desc()).all()]


def _assert_no_cross_year_bleed(year_a_id, year_b_id):
    a_students = {s.id for s in students_for_academic_year(year_a_id).all()}
    b_students = {s.id for s in students_for_academic_year(year_b_id).all()}
    overlap = a_students & b_students
    assert not overlap, f"Student overlap between years {year_a_id} and {year_b_id}: {overlap}"

    for model, col in (
        (Grade, Grade.academic_year_id),
        (StudentPayment, StudentPayment.academic_year_id),
        (Assessment, Assessment.academic_year_id),
    ):
        a_ids = {
            r.id for r in model.query.filter(col == year_a_id).with_entities(model.id).all()
        }
        b_ids = {
            r.id for r in model.query.filter(col == year_b_id).with_entities(model.id).all()
        }
        assert not (a_ids & b_ids), f"{model.__name__} overlap between years"


def main():
    failures = []
    with app.app_context():
        years = _year_ids()
        print(f"Found {len(years)} academic year(s): {years}")
        if len(years) < 2:
            print("SKIP: need at least 2 years to test cross-year bleed")
            return 0

        for i, yid in enumerate(years):
            null_bleed = Student.query.filter(
                Student.academic_year_id.is_(None),
                Student.klass_id.isnot(None),
            ).count()
            if null_bleed:
                print(f"WARN: {null_bleed} active-class student(s) with NULL academic_year_id")

            scoped = students_for_academic_year(yid).count()
            raw = Student.query.filter(Student.academic_year_id == yid).count()
            if scoped != raw:
                failures.append(f"students_for_academic_year mismatch for year {yid}")

        for a, b in zip(years, years[1:]):
            try:
                _assert_no_cross_year_bleed(a, b)
                print(f"OK: no bleed between year {a} and {b}")
            except AssertionError as exc:
                failures.append(str(exc))

        from app import (
            resolve_dashboard_academic_year,
            all_academic_years,
            _build_academic_year_links,
            REGISTRAR_YEAR_SESSION_KEY,
            ADMIN_YEAR_SESSION_KEY,
        )

        listed = all_academic_years()
        if len(listed) != len(years):
            failures.append("all_academic_years() count mismatch")

        links = _build_academic_year_links(listed)
        if not isinstance(links, dict):
            failures.append("year_links not built as dict")
        else:
            print(f"OK: year_links built for {len(links)} year(s)")

        with app.test_request_context("/dashboard?academic_year_id=%s" % years[0]):
            from flask import session
            session[REGISTRAR_YEAR_SESSION_KEY] = years[-1]
            display, active, all_y, archived = resolve_dashboard_academic_year(
                session_key=REGISTRAR_YEAR_SESSION_KEY
            )
            if display.id != years[0]:
                failures.append("resolve_dashboard_academic_year ignored query param")
            else:
                print("OK: query param overrides session for year resolution")

        with app.test_request_context("/dashboard"):
            from flask import session
            session.clear()
            session[ADMIN_YEAR_SESSION_KEY] = years[0]
            display, active, all_y, archived = resolve_dashboard_academic_year(
                session_key=ADMIN_YEAR_SESSION_KEY
            )
            if display.id != years[0]:
                failures.append("resolve_dashboard_academic_year ignored session key")
            else:
                print("OK: session key persists selected year")

    if failures:
        print("\nFAILED:")
        for msg in failures:
            print(f"  - {msg}")
        return 1

    print("\nAll year isolation smoke checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
