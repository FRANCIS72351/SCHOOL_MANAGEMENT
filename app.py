# app.py — Keep Track Digital School Management System
from flask import Flask, render_template, redirect, url_for, flash, request, Response, jsonify, send_file, send_from_directory, current_app, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from models import db, User, Student, Teacher, Class, Announcement, Grade, AcademicYear, ClassSubjectTeacher, Room, Suspension, Discipline, StudentPayment
from itsdangerous import URLSafeTimedSerializer
import pyotp
from reportlab.pdfgen import canvas
from decorators import role_required  # Adjust this import to match your layout
from constants import ROLE_ADMIN
from utils import (
    build_student_financials,
    parse_currency_amount,
    parse_currency_amount_optional,
    currency_to_float,
)
from io import BytesIO, StringIO
from sqlalchemy import func, text, or_
from werkzeug.utils import secure_filename
from models import Student, AcademicYear, Class, BusinessTransaction, StudentPayment, SchoolFee
from decimal import Decimal, ROUND_HALF_UP
from datetime import datetime, date, timezone, timedelta
import csv
import os
import sys
from functools import wraps
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass
from deployment import configure_app, configure_sqlite_performance
from flask_migrate import Migrate
try:
    import pytesseract
    from PIL import Image, ImageEnhance, ImageFilter
except ImportError:
    pytesseract = None
    Image = None
    ImageEnhance = None
    ImageFilter = None

import logging

logger = logging.getLogger(__name__)


def normalize_role(user):
    """Return lowercase stripped role string for consistent access checks."""
    return (getattr(user, 'role', None) or '').strip().lower()

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ======================== STUDENT ADMISSIONS CONFIG ========================
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'uploads')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Ensure the folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    """Check if uploaded file has allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ======================== END CONFIG =======================================

def resolve_static_upload_path(rel_path):
    """Resolve DB-relative upload path (uploads/...) to an absolute file under static/."""
    rel_path = (rel_path or '').replace('\\', '/').lstrip('/')
    if rel_path.startswith('static/'):
        rel_path = rel_path[len('static/'):]
    return os.path.join(BASE_DIR, 'static', rel_path.replace('/', os.sep))


def safe_send_upload_file(subdirectory, filename):
    """Serve a file from a static upload subdirectory with path-traversal protection."""
    safe_name = os.path.basename((filename or '').replace('\\', '/'))
    if not safe_name or safe_name in ('.', '..'):
        abort(404)
    base_dir = os.path.realpath(os.path.join(BASE_DIR, 'static', subdirectory))
    file_path = os.path.realpath(os.path.join(base_dir, safe_name))
    if not file_path.startswith(base_dir) or not os.path.isfile(file_path):
        abort(404)
    return send_from_directory(base_dir, safe_name, as_attachment=True)


def log_security_event(description):
    """Log security events to the database."""
    from models import db, SecurityLog
    
    event = SecurityLog(
        ip_address=request.remote_addr,
        event=description,
        timestamp=datetime.now(timezone.utc)
    )
    db.session.add(event)
    db.session.commit()

def log_incident(event_type):
    # We import SecurityLog and db locally inside the function
    # to prevent circular dependency lookup blocks during runtime initialization.
    from models import db, SecurityLog
    
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    log = SecurityLog(
        ip_address=ip, 
        event=event_type,
        timestamp=datetime.now(timezone.utc)
    )
    db.session.add(log)
    db.session.commit()

def check_brute_force(ip):
    """Block login after 5 failed attempts from the same IP within 15 minutes."""
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=15)
    recent_fails = SecurityLog.query.filter(
        SecurityLog.ip_address == ip,
        SecurityLog.event == 'FAILED_LOGIN',
        SecurityLog.timestamp >= cutoff,
    ).count()
    return recent_fails >= 5

# Local imports handled in init_db.py to avoid circular imports during app import
from models import (
    db, User, Student, Teacher, Class, ClassSubject, Enrollment, Grade,
    Attendance, Sponsor, Announcement, Discipline, Payroll,
    Assessment, AcademicYear, BusinessTransaction, StudentPayment,
    Leader, LeaderCategory, Event, SchoolMedia, SecurityLog, Suspension, Room,
    Asset, MaintenanceTicket, Activity, Submission, RolloverLog,
    SponsorWelfareNote, ClassAnnouncement,
)
from forms import (
    LoginForm, RegisterStudentForm, PayrollForm, AcademicYearForm, RolloverWizardForm,
    AnnouncementForm, BusinessTransactionForm, AssignTeacherForm, CreateClassForm,
    EventForm, ConfirmDeleteForm, LeaderForm, EnrollmentForm, PaymentForm, TransactionForm,
    DisciplineForm, SchoolMediaForm,
)

COMMUNICATIONS_MANAGER_ROLES = frozenset({"admin", "principal", "vpa"})
SCHOOL_MEDIA_MANAGER_ROLES = frozenset({"admin", "principal", "vpa", "registrar"})
REGISTRAR_MEDIA_CATEGORIES = frozenset({"entrance", "info_sheet"})
DOCUMENT_ONLY_MEDIA_CATEGORIES = frozenset({"entrance", "info_sheet"})
HOMEPAGE_FEATURED_MEDIA_CATEGORIES = frozenset({"general", "gallery", "advertisement"})
from flask_wtf import FlaskForm
from export_routes import init_export_routes

# -------------------------------------------------------------------
# SchoolEngine: Business Logic Helpers
# -------------------------------------------------------------------
class SchoolEngine:
    # --- DEAN LOGIC ---
    @staticmethod
    def suspend_student(student_id, days, reason):
        # Updated to standard modern SQLAlchemy get pattern
        student = db.session.get(Student, student_id)
        if not student: 
            return "Student not found"
            
        student.status = 'SUSPENDED'
        
        # Removed the redundant local 'from datetime import timedelta' statement
        end_date = datetime.now(timezone.utc) + timedelta(days=days)
        new_suspension = Suspension(student_id=student_id, reason=reason, return_date=end_date)
        
        db.session.add(new_suspension)
        db.session.commit()
        return f"Student locked out until {end_date.date()}"

    # --- VPA LOGIC ---
    @staticmethod
    def calculate_period_total(ca, exam):
        if ca > 60 or exam > 40:
            return None  # Enforce MoE standards
        return ca + exam

    @staticmethod
    def calculate_gpa(scores):
        """Calculate GPA based on Liberian MoE scale"""
        grade_points = []
        for score in scores:
            if score >= 90:
                grade_points.append(4.0)  # A
            elif score >= 80:
                grade_points.append(3.0)  # B
            elif score >= 70:
                grade_points.append(2.0)  # C
            elif score >= 60:
                grade_points.append(1.0)  # D
            else:
                grade_points.append(0.0)  # F
        gpa = sum(grade_points) / len(grade_points) if grade_points else 0.0
        return gpa

    @staticmethod
    def get_grade_letter(score):
        if score is None:
            return '-'
        try:
            score = float(score)
        except (TypeError, ValueError):
            return '-'
        if score >= 90:
            return 'A'
        if score >= 80:
            return 'B'
        if score >= 70:
            return 'C'
        if score >= 60:
            return 'D'
        return 'F'

    @staticmethod
    def get_remarks(score):
        if score is None:
            return ''
        try:
            score = float(score)
        except (TypeError, ValueError):
            return ''
        if score >= 90:
            return 'Excellent'
        if score >= 80:
            return 'Very Good'
        if score >= 70:
            return 'Good'
        if score >= 60:
            return 'Satisfactory'
        return 'Failing'


# ----------------------------------------------------------------------
# Helper utilities: centralize teacher -> classes -> students resolution
# ----------------------------------------------------------------------
def get_teacher_class_ids(teacher_profile, user=None):
    """Return class ids the teacher may access (allocations, homeroom, sponsorship)."""
    ids = set()
    if not teacher_profile:
        return ids

    user_id = user.id if user else getattr(teacher_profile, 'user_id', None)

    try:
        allocs = ClassSubjectTeacher.query.filter_by(teacher_id=teacher_profile.id).all()
        for alloc in allocs:
            if getattr(alloc, 'class_id', None):
                ids.add(alloc.class_id)
    except Exception:
        pass

    try:
        for klass in Class.query.filter_by(teacher_id=teacher_profile.id).all():
            ids.add(klass.id)
    except Exception:
        pass

    try:
        if user_id:
            for klass in Class.query.filter_by(sponsor_id=user_id).all():
                ids.add(klass.id)
    except Exception:
        pass

    return ids


def get_teacher_classes(teacher_profile, user=None):
    """Return Class rows the teacher is authorized to work with."""
    class_ids = get_teacher_class_ids(teacher_profile, user)
    if not class_ids:
        return []
    return Class.query.filter(Class.id.in_(class_ids)).order_by(Class.name.asc()).all()


def teacher_can_access_class(teacher_profile, user, class_id):
    if not teacher_profile or not class_id:
        return False
    return int(class_id) in get_teacher_class_ids(teacher_profile, user)


def teacher_can_access_student(teacher_profile, user, student):
    if not teacher_profile or not student:
        return False
    class_ids = get_teacher_class_ids(teacher_profile, user)
    if not class_ids:
        return False
    if student.klass_id and student.klass_id in class_ids:
        return True
    try:
        return Enrollment.query.filter(
            Enrollment.student_id == student.id,
            Enrollment.class_id.in_(class_ids),
        ).first() is not None
    except Exception:
        return False


def get_teacher_class_cards(teacher_profile, user):
    """Build class folder cards with subjects and student counts for the teacher UI."""
    if not teacher_profile or not user:
        return []

    class_ids = get_teacher_class_ids(teacher_profile, user)
    if not class_ids:
        return []

    subject_map = {}
    for alloc in ClassSubjectTeacher.query.filter_by(teacher_id=teacher_profile.id).all():
        subject_map.setdefault(alloc.class_id, set()).add(alloc.subject_name)

    cards = []
    for klass in Class.query.filter(Class.id.in_(class_ids)).order_by(
        Class.grade_level.asc(), Class.name.asc()
    ):
        role_labels = []
        if klass.teacher_id == teacher_profile.id:
            role_labels.append('Homeroom')
        if klass.sponsor_id == user.id:
            role_labels.append('Sponsor')
        if not role_labels:
            role_labels.append('Subject Teacher')

        cards.append({
            'id': klass.id,
            'name': klass.name,
            'grade_level': klass.grade_level,
            'stream': klass.stream,
            'subjects': sorted(subject_map.get(klass.id, [])),
            'student_count': Student.query.filter_by(klass_id=klass.id).count(),
            'role_labels': role_labels,
            'klass': klass,
        })
    return cards


def build_teacher_dashboard_context(teacher_profile, user):
    """Assemble roster-limited teacher dashboard data."""
    class_ids = get_teacher_class_ids(teacher_profile, user)
    teaching_classes = get_teacher_classes(teacher_profile, user)
    class_cards = get_teacher_class_cards(teacher_profile, user)
    sponsored_classes = (
        Class.query.filter_by(sponsor_id=user.id).order_by(Class.name.asc()).all()
        if user else []
    )
    students = get_students_for_class_ids(list(class_ids)) if class_ids else []
    student_ids = {s.id for s in students}

    activities = []
    recent_submissions = []
    if class_ids:
        try:
            activities = (
                Assessment.query.filter(Assessment.klass_id.in_(class_ids))
                .order_by(Assessment.id.desc())
                .limit(20)
                .all()
            )
        except Exception:
            activities = []
        try:
            recent_submissions = (
                Submission.query.join(Assessment)
                .filter(Assessment.klass_id.in_(class_ids))
                .order_by(Submission.submitted_at.desc())
                .limit(25)
                .all()
            )
        except Exception:
            recent_submissions = []

    grades = []
    if teacher_profile:
        grade_query = Grade.query.filter_by(teacher_id=teacher_profile.id)
        if student_ids:
            grade_query = grade_query.filter(Grade.student_id.in_(student_ids))
        grades = grade_query.order_by(Grade.id.desc()).all()

    ai_scan_queue = []
    if class_ids:
        try:
            ai_scan_queue = (
                Submission.query.join(Assessment)
                .filter(
                    Assessment.klass_id.in_(class_ids),
                    Submission.file_path.isnot(None),
                    Submission.file_path != '',
                    Submission.is_graded.is_(False),
                )
                .order_by(Submission.submitted_at.desc())
                .all()
            )
        except Exception:
            ai_scan_queue = []

    assigned_subjects = sorted(
        {
            subject
            for card in class_cards
            for subject in card.get('subjects', [])
        },
        key=str.lower,
    )

    pending_grading_count = sum(
        1 for s in recent_submissions if not s.is_graded
    )
    activities_with_pending = 0
    if class_ids:
        try:
            for act in activities:
                subs = Submission.query.filter_by(assessment_id=act.id).all()
                if any(
                    (sub.file_path or sub.submission_text) and not sub.is_graded
                    for sub in subs
                ):
                    activities_with_pending += 1
        except Exception:
            pass

    return {
        'teaching_classes': teaching_classes,
        'class_cards': class_cards,
        'sponsored_classes': sponsored_classes,
        'students': students,
        'activities': activities,
        'recent_submissions': recent_submissions,
        'ai_scan_queue': ai_scan_queue,
        'grades': grades,
        'assigned_subjects': assigned_subjects,
        'grading_periods': MOE_GRADING_PERIODS,
        'pending_grading_count': pending_grading_count,
        'activities_with_pending': activities_with_pending,
    }


def get_students_for_class_ids(class_ids):
    """Return unique list of Student objects for given class ids.
    Considers both Student.klass_id and Enrollment rows.
    """
    if not class_ids:
        return []

    students_map = {}
    try:
        for s in Student.query.filter(Student.klass_id.in_(class_ids)).all():
            students_map[s.id] = s
    except Exception:
        pass

    try:
        enrolls = Enrollment.query.filter(Enrollment.class_id.in_(class_ids)).all()
        for e in enrolls:
            if e.student_id and e.student:
                students_map[e.student.id] = e.student
    except Exception:
        pass

    # Return sorted list
    return sorted(students_map.values(), key=lambda s: (s.last_name or '', s.first_name or ''))


MOE_GRADING_PERIODS = [
    (1, 'Period 1'),
    (2, 'Period 2'),
    (3, 'Period 3'),
    (4, 'Period 4'),
    (5, 'Period 5'),
    (6, 'Period 6'),
    (7, '1st Semester Exam'),
    (8, '2nd Semester Final Exam'),
]


def get_teacher_subjects_for_class(teacher_profile, class_id):
    """Subjects this teacher is allocated to teach in the given class."""
    if not teacher_profile or not class_id:
        return []
    subjects = {
        alloc.subject_name
        for alloc in ClassSubjectTeacher.query.filter_by(
            teacher_id=teacher_profile.id,
            class_id=class_id,
        ).all()
        if alloc.subject_name
    }
    return sorted(subjects, key=str.lower)


def get_class_subject_catalog(class_id):
    """All subject names configured for a class (catalog + teacher allocations)."""
    if not class_id:
        return []
    subjects = set()
    for row in ClassSubject.query.filter_by(class_id=class_id).all():
        if row.subject_name:
            subjects.add(row.subject_name)
    for row in ClassSubjectTeacher.query.filter_by(class_id=class_id).all():
        if row.subject_name:
            subjects.add(row.subject_name)
    return sorted(subjects, key=str.lower)


def get_assignable_subjects_for_class(teacher_profile, user, class_id):
    """Subjects a teacher may use when assigning activities in a class."""
    allocated = get_teacher_subjects_for_class(teacher_profile, class_id)
    if allocated:
        return allocated
    if not teacher_can_access_class(teacher_profile, user, class_id):
        return []
    return get_class_subject_catalog(class_id)


SPONSOR_RESPONSIBILITIES = [
    {'icon': 'bi-calendar-check', 'title': 'Daily Attendance', 'detail': 'Mark present, late, or absent and track punctuality.'},
    {'icon': 'bi-heart-pulse', 'title': 'Student Welfare', 'detail': 'Log pastoral notes, concerns, and follow-ups.'},
    {'icon': 'bi-telephone', 'title': 'Parent Liaison', 'detail': 'Primary contact for guardians on class matters.'},
    {'icon': 'bi-shield-exclamation', 'title': 'Conduct Monitoring', 'detail': 'Record incidents and refer serious cases to the Dean.'},
    {'icon': 'bi-megaphone', 'title': 'Class Communication', 'detail': 'Post reminders and notices for your class.'},
    {'icon': 'bi-graph-up', 'title': 'Academic Oversight', 'detail': 'Monitor MoE standings, tasks, and report cards.'},
    {'icon': 'bi-cash-coin', 'title': 'Fee Awareness', 'detail': 'View tuition status — payments are handled by Business.'},
    {'icon': 'bi-clipboard-check', 'title': 'Activities & Grades', 'detail': 'Assign classwork and support MoE grade entry.'},
]


def teacher_is_class_sponsor(teacher_profile, user, class_id):
    """True when this teacher is the assigned class sponsor or homeroom teacher."""
    if not teacher_profile or not class_id:
        return False
    klass = db.session.get(Class, class_id)
    if not klass:
        return False
    if user and klass.sponsor_id == user.id:
        return True
    return klass.teacher_id == teacher_profile.id


def _sponsor_hub_redirect(class_id, **kwargs):
    params = {k: v for k, v in kwargs.items() if v is not None}
    return redirect(url_for('sponsor_class_hub', class_id=class_id, **params))


def _student_attendance_rate(student, days=30):
    """Return attendance percentage over the last N calendar days."""
    if not student:
        return None
    today = date.today()
    start = today - timedelta(days=days - 1)
    records = []
    for row in student.attendance_ledger.all():
        if not row.date:
            continue
        try:
            row_date = datetime.strptime(row.date, '%Y-%m-%d').date()
        except ValueError:
            continue
        if start <= row_date <= today:
            records.append(row)
    if not records:
        return None
    present = sum(1 for r in records if (r.status or '').lower() in {'present', 'late'})
    return round(present / len(records) * 100, 1)


def _student_period_average(student, academic_year):
    if not student or not academic_year:
        return None
    grades = Grade.query.filter_by(
        student_id=student.id,
        academic_year_id=academic_year.id,
        submitted=True,
    ).all()
    scores = [g.score for g in grades if g.score is not None]
    if not scores:
        draft = Grade.query.filter_by(
            student_id=student.id,
            academic_year_id=academic_year.id,
            submitted=False,
        ).all()
        scores = [g.score for g in draft if g.score is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def build_sponsor_hub_context(teacher_profile, user, klass, active_year, attendance_date=None):
    """Assemble sponsor command center data for one class."""
    attendance_date = attendance_date or date.today().strftime('%Y-%m-%d')
    students = (
        Student.query.filter_by(klass_id=klass.id)
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )
    student_ids = [s.id for s in students]

    today_attendance = {}
    if student_ids:
        for row in Attendance.query.filter(
            Attendance.student_id.in_(student_ids),
            Attendance.date == attendance_date,
        ).all():
            today_attendance[row.student_id] = row

    discipline_counts = {}
    if student_ids:
        rows = (
            db.session.query(Discipline.student_id, db.func.count(Discipline.id))
            .filter(Discipline.student_id.in_(student_ids))
            .group_by(Discipline.student_id)
            .all()
        )
        discipline_counts = {sid: cnt for sid, cnt in rows}

    roster = []
    at_risk = 0
    fee_alerts = 0
    for student in students:
        fin = build_student_financials(student, active_year) if active_year else {}
        balance = float(fin.get('tuition_balance', 0) or 0)
        att_rate = _student_attendance_rate(student)
        incidents = discipline_counts.get(student.id, 0)
        avg = _student_period_average(student, active_year)
        att_row = today_attendance.get(student.id)
        status = (att_row.status if att_row else 'present').lower()

        flags = []
        if att_rate is not None and att_rate < 75:
            flags.append('Low attendance')
            at_risk += 1
        if incidents >= 2:
            flags.append('Conduct concern')
            at_risk += 1
        if balance > 0:
            flags.append('Fee balance')
            fee_alerts += 1
        if avg is not None and avg < MOE_PASSING_SCORE:
            flags.append('Below MoE 70%')

        roster.append({
            'student': student,
            'attendance_status': status,
            'attendance_rate': att_rate,
            'incidents': incidents,
            'avg_score': avg,
            'tuition_balance': balance,
            'parent_email': student.parent_email,
            'flags': flags,
        })

    present_today = sum(
        1 for r in roster if r['attendance_status'] in {'present', 'late'}
    )
    absent_today = sum(1 for r in roster if r['attendance_status'] == 'absent')
    late_today = sum(1 for r in roster if r['attendance_status'] == 'late')

    recent_incidents = (
        Discipline.query.filter(Discipline.student_id.in_(student_ids))
        .order_by(Discipline.created_at.desc())
        .limit(8)
        .all()
    ) if student_ids else []

    welfare_notes = (
        SponsorWelfareNote.query.filter_by(class_id=klass.id)
        .order_by(SponsorWelfareNote.created_at.desc())
        .limit(10)
        .all()
    )

    class_notices = (
        ClassAnnouncement.query.filter_by(class_id=klass.id)
        .order_by(ClassAnnouncement.created_at.desc())
        .limit(8)
        .all()
    )

    open_tasks = Assessment.query.filter_by(klass_id=klass.id).count() if klass else 0
    is_sponsor = user and klass.sponsor_id == user.id
    is_homeroom = teacher_profile and klass.teacher_id == teacher_profile.id

    return {
        'klass': klass,
        'students': students,
        'roster': roster,
        'attendance_date': attendance_date,
        'today_attendance': today_attendance,
        'kpis': {
            'total_students': len(students),
            'present_today': present_today,
            'absent_today': absent_today,
            'late_today': late_today,
            'at_risk': at_risk,
            'fee_alerts': fee_alerts,
            'open_activities': open_tasks,
        },
        'recent_incidents': recent_incidents,
        'welfare_notes': welfare_notes,
        'class_notices': class_notices,
        'is_sponsor': is_sponsor,
        'is_homeroom': is_homeroom,
        'role_title': 'Class Sponsor' if is_sponsor and not is_homeroom else (
            'Form Teacher' if is_homeroom and not is_sponsor else 'Sponsor & Form Teacher'
        ),
        'responsibilities': SPONSOR_RESPONSIBILITIES,
        'active_year': active_year,
    }


def normalize_grade_period(value):
    """Return an int period (1-8) from stored grade period fields."""
    if value is None:
        return None
    if isinstance(value, int):
        return value if 1 <= value <= 8 else None
    text = str(value).strip().lower()
    if text.isdigit():
        num = int(text)
        return num if 1 <= num <= 8 else None
    if 'final' in text and 'exam' in text:
        return 8
    if 'semester' in text and 'exam' in text:
        return 7
    if text.startswith('period'):
        digits = ''.join(ch for ch in text if ch.isdigit())
        if digits:
            num = int(digits)
            return num if 1 <= num <= 8 else None
    return None


def find_grade_record(student_id, subject_name, period, class_id=None, academic_year_id=None):
    """Find an existing grade row for a student/subject/period."""
    query = Grade.query.filter(
        Grade.student_id == student_id,
        or_(Grade.subject == subject_name, Grade.subject_name == subject_name),
    )
    if class_id is not None:
        query = query.filter_by(class_id=class_id)
    if academic_year_id is not None:
        query = query.filter_by(academic_year_id=academic_year_id)

    for grade in query.all():
        stored_period = grade.marking_period or normalize_grade_period(grade.period)
        if stored_period == period:
            return grade
    return None


MOE_ACTIVITY_TYPES = ['Assignment', 'Class Work', 'Quiz', 'Test', 'Exam']

STREAM_SUBJECT_PRESETS = {
    'science': [
        'Mathematics', 'English', 'Biology', 'Chemistry', 'Physics',
        'Geography', 'History', 'French', 'Physical Education', 'Computer Science',
    ],
    'arts': [
        'Mathematics', 'English', 'Literature', 'History', 'Geography',
        'Economics', 'French', 'Religious Education', 'Physical Education', 'Art',
    ],
    'commercial': [
        'Mathematics', 'English', 'Accounting', 'Economics', 'Business Studies',
        'Geography', 'History', 'French', 'Physical Education', 'Computer Science',
    ],
    'general': [
        'Mathematics', 'English', 'Science', 'Social Studies', 'Geography',
        'History', 'French', 'Physical Education', 'Religious Education', 'Computer Science',
    ],
}

MOE_PASSING_SCORE = int(os.environ.get('PROMOTION_PASS_SCORE', '70'))


def promotion_pass_score():
    """Configurable MoE promotion average threshold (default 70%)."""
    try:
        return int(current_app.config.get('PROMOTION_PASS_SCORE', MOE_PASSING_SCORE))
    except RuntimeError:
        return MOE_PASSING_SCORE


def max_failing_subjects_for_promotion():
    """Maximum failing subjects allowed for promotion (default 2)."""
    try:
        return int(current_app.config.get('MAX_FAILING_SUBJECTS', 2))
    except RuntimeError:
        return 2


def _subject_key(name):
    return (name or '').strip().lower()


def subjects_match(left, right):
    """Case-insensitive subject name comparison."""
    if not left or not right:
        return False
    return _subject_key(left) == _subject_key(right)


def get_student_class_id(student):
    """Resolve the class a student belongs to (direct assignment or enrollment)."""
    if not student:
        return None
    if student.klass_id:
        return student.klass_id
    try:
        enrollment = (
            Enrollment.query.filter_by(student_id=student.id)
            .order_by(Enrollment.id.desc())
            .first()
        )
        if enrollment and enrollment.class_id:
            return enrollment.class_id
    except Exception:
        pass
    return None


def get_class_subjects_for_student(student):
    """Return subject names allocated to the student's class."""
    class_id = get_student_class_id(student)
    if not student or not class_id:
        return []

    subjects = set()
    for row in ClassSubjectTeacher.query.filter_by(class_id=class_id).all():
        if row.subject_name:
            subjects.add(row.subject_name)
    for row in ClassSubject.query.filter_by(class_id=class_id).all():
        if row.subject_name:
            subjects.add(row.subject_name)
    for assessment in Assessment.query.filter_by(klass_id=class_id).all():
        if assessment.subject_name:
            subjects.add(assessment.subject_name)
    for grade in Grade.query.filter_by(student_id=student.id).all():
        name = grade.subject_name or grade.subject
        if name:
            subjects.add(name)
    return sorted(subjects, key=str.lower)


def get_student_assessments(student, display_year):
    """Assessments for the student's class, scoped to the selected academic year."""
    class_id = get_student_class_id(student)
    if not student or not class_id:
        return []
    query = Assessment.query.filter(Assessment.klass_id == class_id)
    if display_year:
        query = query.filter(
            or_(Assessment.academic_year_id == display_year.id, Assessment.academic_year_id.is_(None))
        )
    return query.order_by(Assessment.id.desc()).all()


def calculate_ca_from_assessments(student_id, subject_name, marking_period, class_id, academic_year_id):
    """
    Build MoE CA score (0-60) from graded class activities in the same subject/period.
    Each graded activity contributes proportionally; result is scaled to 60.
    """
    assessments = Assessment.query.filter_by(
        klass_id=class_id,
        subject_name=subject_name,
        marking_period=marking_period,
        academic_year_id=academic_year_id,
    ).all()
    ca_assessments = [a for a in assessments if not a.is_exam_component]
    if not ca_assessments:
        return None

    ratios = []
    for assessment in ca_assessments:
        submission = Submission.query.filter_by(
            assessment_id=assessment.id,
            student_id=student_id,
            is_graded=True,
        ).first()
        if not submission or submission.score is None:
            continue
        max_score = assessment.max_score or 100.0
        if max_score <= 0:
            continue
        ratios.append(min(submission.score / max_score, 1.0))

    if not ratios:
        return None
    average_ratio = sum(ratios) / len(ratios)
    return round(average_ratio * 60, 1)


def calculate_exam_from_assessments(student_id, subject_name, marking_period, class_id, academic_year_id):
    """Return exam score (0-40) from graded Exam-type activities, scaled to 40."""
    if not class_id:
        return None
    assessments = Assessment.query.filter_by(
        klass_id=class_id,
        subject_name=subject_name,
        marking_period=marking_period,
        academic_year_id=academic_year_id,
    ).all()
    exam_assessments = [a for a in assessments if a.is_exam_component]
    if not exam_assessments:
        return None

    ratios = []
    for assessment in exam_assessments:
        submission = Submission.query.filter_by(
            assessment_id=assessment.id,
            student_id=student_id,
            is_graded=True,
        ).first()
        if not submission or submission.score is None:
            continue
        max_score = assessment.max_score or 100.0
        if max_score <= 0:
            continue
        ratios.append(min(submission.score / max_score, 1.0))

    if not ratios:
        return None
    return round((sum(ratios) / len(ratios)) * 40, 1)


def sync_draft_period_grade(student, subject_name, marking_period, teacher_id=None, preserve_manual_exam=True):
    """
    Recalculate draft period grade from activity scores.
    Keeps teacher-entered exam unless preserve_manual_exam=False.
    """
    if not student or not subject_name or marking_period not in range(1, 7):
        return None

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        return None

    student_class_id = get_student_class_id(student)
    ca_score = calculate_ca_from_assessments(
        student.id, subject_name, marking_period, student_class_id, active_year.id
    )
    auto_exam = calculate_exam_from_assessments(
        student.id, subject_name, marking_period, student_class_id, active_year.id
    )

    grade = find_grade_record(
        student.id,
        subject_name,
        marking_period,
        class_id=student_class_id,
        academic_year_id=active_year.id,
    )

    if ca_score is None and auto_exam is None and not grade:
        return None

    if grade and grade.is_finalized:
        return grade

    if not grade:
        grade = Grade(
            student_id=student.id,
            teacher_id=teacher_id,
            class_id=student_class_id,
            academic_year_id=active_year.id,
            subject=subject_name,
            subject_name=subject_name,
            marking_period=marking_period,
            period=marking_period,
            submitted=False,
        )
        db.session.add(grade)

    if teacher_id:
        grade.teacher_id = teacher_id

    if ca_score is not None:
        grade.ca_score = ca_score

    if auto_exam is not None:
        grade.exam_score = auto_exam
    elif not preserve_manual_exam and grade.exam_score is None:
        grade.exam_score = 0.0

    ca_val = grade.ca_score or 0.0
    exam_val = grade.exam_score or 0.0
    total = SchoolEngine.calculate_period_total(ca_val, exam_val)
    if total is not None:
        grade.score = total
        grade.remarks = SchoolEngine.get_remarks(total)
        setattr(grade, f'p{marking_period}', int(round(total)))

    grade.activity_type = 'Period Assessment'
    grade.submitted = False
    db.session.flush()
    return grade


def build_student_academic_portal(student, display_year, selected_subject=None, selected_period=None):
    """Assemble student-facing activity scores and draft/published standings."""
    if not student or not display_year:
        return {
            'subjects': [],
            'selected_subject': None,
            'selected_period': selected_period or 1,
            'activity_scores': [],
            'draft_standing': None,
            'published_standing': None,
            'activity_feed': [],
        }

    if selected_period is None:
        selected_period = 1

    assessments = get_student_assessments(student, display_year)
    subjects = set(get_class_subjects_for_student(student))
    for assessment in assessments:
        if assessment.subject_name:
            subjects.add(assessment.subject_name)
    subjects = sorted(subjects, key=str.lower)

    if not selected_subject:
        period_assessments = [
            a for a in assessments if (a.marking_period or 1) == selected_period
        ]
        if period_assessments:
            selected_subject = period_assessments[0].subject_name
        elif subjects:
            selected_subject = subjects[0]

    filtered_assessments = list(assessments)
    if selected_subject:
        filtered_assessments = [
            a for a in filtered_assessments if subjects_match(a.subject_name, selected_subject)
        ]
    if selected_period:
        filtered_assessments = [
            a for a in filtered_assessments if (a.marking_period or 1) == selected_period
        ]

    activity_feed = []
    for assessment in filtered_assessments:
        submission = Submission.query.filter_by(
            assessment_id=assessment.id,
            student_id=student.id,
        ).first()
        activity_feed.append({
            'assessment': assessment,
            'submission': submission,
            'type_label': assessment.activity_type or 'Assignment',
            'score': submission.score if submission and submission.is_graded else None,
            'max_score': assessment.max_score or 100,
            'status': (
                'Graded' if submission and submission.is_graded
                else 'Submitted' if submission
                else 'Pending'
            ),
        })

    draft_grade = find_grade_record(
        student.id,
        selected_subject,
        selected_period,
        class_id=get_student_class_id(student),
        academic_year_id=display_year.id,
    ) if selected_subject else None

    published_grade = draft_grade if draft_grade and draft_grade.submitted else None
    draft_standing = draft_grade if draft_grade and not draft_grade.submitted else None

    if draft_standing:
        draft_standing = {
            'ca_score': draft_standing.ca_score,
            'exam_score': draft_standing.exam_score,
            'total': draft_standing.score,
            'grade_letter': SchoolEngine.get_grade_letter(draft_standing.score),
            'remarks': draft_standing.remarks,
        }

    if published_grade:
        published_grade = {
            'ca_score': published_grade.ca_score,
            'exam_score': published_grade.exam_score,
            'total': published_grade.score,
            'grade_letter': SchoolEngine.get_grade_letter(published_grade.score),
            'remarks': published_grade.remarks,
        }

    return {
        'subjects': [{'name': name} for name in subjects],
        'selected_subject': selected_subject,
        'selected_period': selected_period,
        'activity_feed': activity_feed,
        'draft_standing': draft_standing,
        'published_standing': published_grade,
        'grading_periods': MOE_GRADING_PERIODS[:6],
    }


def official_grade_records(student_id, academic_year_id=None):
    """Grades that may appear on official report cards."""
    query = Grade.query.filter_by(student_id=student_id, submitted=True)
    if academic_year_id:
        query = query.filter_by(academic_year_id=academic_year_id)
    return query.all()


def provision_student_portal_account(student, email, password=None, full_name=None):
    """
    Create or update the login User for a student and link student.user_id.
    Email is required; password defaults to 'student123' when omitted.
    """
    if not student:
        return None

    email = (email or '').strip()
    if not email:
        return None

    display_name = (full_name or student.full_name or '').strip() or email
    user = db.session.get(User, student.user_id) if student.user_id else None

    if not user:
        user = User.query.filter(func.lower(User.email) == email.lower()).first()

    if user and user.id != student.user_id:
        existing = Student.query.filter_by(user_id=user.id).first()
        if existing and existing.id != student.id:
            return None

    if not user:
        user = User(
            email=email,
            role='student',
            full_name=display_name,
        )
        user.set_password(password or 'student123')
        db.session.add(user)
        db.session.flush()
    else:
        user.role = 'student'
        user.full_name = display_name
        if password:
            user.set_password(password)

    student.user_id = user.id
    if student.photo and not user.photo:
        user.photo = student.photo
    return user


def get_student_for_user(user, auto_link=True):
    """
    Resolve the Student record for a logged-in user.
    Attempts auto-linking when a student portal account exists without user_id.
    """
    if not user:
        return None

    student = Student.query.filter_by(user_id=user.id).first()
    if student:
        return student

    if getattr(user, 'student_profile', None):
        profile = user.student_profile
        if profile.user_id != user.id:
            profile.user_id = user.id
            db.session.commit()
        return profile

    if not auto_link or (user.role or '').lower() != 'student':
        return None

    email_local = (user.email or '').split('@')[0].lower()
    if email_local:
        id_matches = [
            s for s in Student.query.filter_by(user_id=None).all()
            if s.student_id and s.student_id.lower() == email_local
        ]
        if len(id_matches) == 1:
            id_matches[0].user_id = user.id
            db.session.commit()
            return id_matches[0]

    name_key = (user.full_name or '').strip().lower()
    if name_key:
        matches = [
            s for s in Student.query.filter_by(user_id=None).all()
            if f"{s.first_name} {s.last_name}".strip().lower() == name_key
        ]
        if len(matches) == 1:
            matches[0].user_id = user.id
            db.session.commit()
            return matches[0]

    return None


def repair_student_portal_links():
    """Link student login accounts to registrar records when a unique match exists."""
    student_users = User.query.filter(func.lower(User.role) == 'student').all()
    repaired = 0
    for user in student_users:
        if Student.query.filter_by(user_id=user.id).first():
            continue
        if get_student_for_user(user, auto_link=True):
            repaired += 1
    return repaired


def link_student_portal_from_form(student, email, password=None):
    """Helper used by registrar registration flows."""
    if not email:
        return None
    return provision_student_portal_account(
        student,
        email,
        password=password,
        full_name=f"{student.first_name} {student.last_name}".strip(),
    )


def _academic_year_id_prefix(academic_year):
    """Build a short stable prefix for auto-generated student IDs."""
    if not academic_year:
        return str(datetime.now(timezone.utc).year)

    year_name = (academic_year.name or '').strip()
    if not year_name:
        return str(academic_year.id)

    parts = [part.strip() for part in year_name.replace('/', '-').split('-') if part.strip()]
    if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
        return f"{parts[0][-2:]}{parts[1][-2:]}"

    digits = ''.join(char for char in year_name if char.isdigit())
    return digits[:4] or str(academic_year.id)


def generate_next_student_id(academic_year=None):
    """Generate the next unique student ID (supports 99,999+ students per academic year)."""
    if academic_year is None:
        academic_year = AcademicYear.query.filter_by(is_active=True).first()

    prefix = _academic_year_id_prefix(academic_year)
    pattern = f"{prefix}-"
    existing_ids = [
        row[0]
        for row in db.session.query(Student.student_id).filter(Student.student_id.like(f"{pattern}%")).all()
    ]

    max_seq = 0
    for student_id in existing_ids:
        suffix = student_id[len(pattern):]
        if suffix.isdigit():
            max_seq = max(max_seq, int(suffix))

    if existing_ids or not Student.query.count():
        return f"{pattern}{max_seq + 1:05d}"

    legacy_numeric = []
    for (student_id,) in db.session.query(Student.student_id).all():
        if student_id and student_id.isdigit():
            legacy_numeric.append(int(student_id))

    if legacy_numeric:
        return str(max(legacy_numeric) + 1)

    return f"{pattern}00001"


def _same_student_identity(existing_student, form):
    """Return True when the submitted form matches an existing student record."""
    return (
        (existing_student.first_name or '').strip().lower() == (form.first_name.data or '').strip().lower()
        and (existing_student.last_name or '').strip().lower() == (form.last_name.data or '').strip().lower()
        and existing_student.dob == form.dob.data
    )


def compile_student_dashboard_context(student, display_year, request_args=None):
    """Build the full template context for the student dashboard."""
    request_args = request_args or {}
    if hasattr(request_args, 'getlist'):
        selected_subject = request_args.get('subject') or request_args.get('subject_name')
        selected_period = request_args.get('period', type=int) or 1
    else:
        selected_subject = request_args.get('subject') or request_args.get('subject_name')
        raw_period = request_args.get('period')
        try:
            selected_period = int(raw_period) if raw_period else 1
        except (TypeError, ValueError):
            selected_period = 1

    portal = build_student_academic_portal(
        student,
        display_year,
        selected_subject=selected_subject,
        selected_period=selected_period,
    ) if student and display_year else {
        'subjects': [],
        'selected_subject': None,
        'selected_period': 1,
        'activity_feed': [],
        'draft_standing': None,
        'published_standing': None,
        'grading_periods': MOE_GRADING_PERIODS[:6],
    }

    assessments = get_student_assessments(student, display_year) if student and display_year else []
    student_submissions = {}
    if student:
        for sub in Submission.query.filter_by(student_id=student.id).all():
            student_submissions[sub.assessment_id] = sub

    pending_tasks = []
    for assessment in assessments:
        submission = student_submissions.get(assessment.id)
        if submission and submission.is_graded:
            continue
        pending_tasks.append({
            'assessment': assessment,
            'submission': submission,
            'needs_submit': submission is None,
            'needs_grade': submission is not None and not submission.is_graded,
        })

    sel_subject = portal.get('selected_subject')
    sel_period = portal.get('selected_period', 1)
    task_assessments = list(assessments)
    if sel_subject:
        task_assessments = [
            a for a in task_assessments if subjects_match(a.subject_name, sel_subject)
        ]
    if sel_period:
        task_assessments = [
            a for a in task_assessments if (a.marking_period or 1) == sel_period
        ]

    pending_ids = {task['assessment'].id for task in pending_tasks}
    visible_pending = [a for a in task_assessments if a.id in pending_ids]
    if pending_tasks and not visible_pending:
        first_pending = pending_tasks[0]['assessment']
        sel_subject = first_pending.subject_name
        sel_period = first_pending.marking_period or 1
        portal = build_student_academic_portal(
            student,
            display_year,
            selected_subject=sel_subject,
            selected_period=sel_period,
        )
        task_assessments = list(assessments)
        if sel_subject:
            task_assessments = [
                a for a in task_assessments if subjects_match(a.subject_name, sel_subject)
            ]
        if sel_period:
            task_assessments = [
                a for a in task_assessments if (a.marking_period or 1) == sel_period
            ]

    klass = student.assigned_class if student else None
    financials = build_student_financials(student, display_year) if student else None
    if isinstance(financials, dict):
        financials = type('StudentFinancials', (), financials)()
    fees = []
    if student:
        if hasattr(student, 'payment_records'):
            all_fees = list(student.payment_records.order_by(StudentPayment.paid_on.desc()).all())
        else:
            all_fees = StudentPayment.query.filter_by(student_id=student.id).order_by(
                StudentPayment.paid_on.desc()
            ).all()
        if display_year:
            fees = [p for p in all_fees if p.academic_year_id == display_year.id]
        if not fees:
            fees = all_fees

    grades = []
    if student and display_year:
        grades = Grade.query.filter_by(
            student_id=student.id,
            academic_year_id=display_year.id,
        ).all()

    class_notices = []
    if student and get_student_class_id(student):
        class_notices = (
            ClassAnnouncement.query.filter_by(class_id=get_student_class_id(student))
            .order_by(ClassAnnouncement.created_at.desc())
            .limit(6)
            .all()
        )

    return {
        'student': student,
        'display_year': display_year,
        'klass': klass,
        'grades': grades,
        'financials': financials,
        'fees': fees,
        'activities': assessments,
        'assessments': assessments,
        'task_assessments': task_assessments,
        'pending_tasks': pending_tasks,
        'student_submissions': student_submissions,
        'subjects': portal.get('subjects', []),
        'selected_subject': portal.get('selected_subject'),
        'selected_period': portal.get('selected_period', 1),
        'activity_feed': portal.get('activity_feed', []),
        'draft_standing': portal.get('draft_standing'),
        'published_standing': portal.get('published_standing'),
        'grading_periods': portal.get('grading_periods', MOE_GRADING_PERIODS[:6]),
        'current_active_period': portal.get('selected_period', 1),
        'class_notices': class_notices,
    }


class DeanManager:
    """The 'Heart' of the school - logic for the Dean of Students"""

    @staticmethod
    def issue_suspension(student_id, reason, duration_days):
        """
        Records a suspension and sets the return date.
        """
        # Removed redundant local timedelta import statement
        return_date = datetime.now(timezone.utc) + timedelta(days=duration_days)
        
        suspension = Suspension(
            student_id=student_id,
            reason=reason,
            return_date=return_date
        )
        
        # Updated to safe, modern session.get syntax pattern
        student = db.session.get(Student, student_id)
        if student:
            student.status = 'SUSPENDED'
            db.session.add(suspension)
            db.session.commit()
            return f"Student {student.full_name} is suspended until {return_date.date()}"
        return "Student context error: Record not found"

    @staticmethod
    def check_suspension_status(student_id):
        """
        Security check: Is the student allowed on campus/in the system today?
        """
        suspension = Suspension.query.filter(
            Suspension.student_id == student_id,
            Suspension.return_date > datetime.now(timezone.utc)
        ).first()
        
        if suspension:
            return False  # Student is still banned
        return True  # Student is clear


class InstitutionalManager:
    """The 'Engine' of the school - logic for the VPI"""

    @staticmethod
    def check_room_availability(room_id, new_student_count):
        """
        Ensures the school is compliant with capacity standards.
        """
        # Updated to safe, modern session.get syntax pattern
        room = db.session.get(Room, room_id)
        if not room:
            return {"status": "ERROR", "message": "Room not found"}
            
        current_total = room.current_occupancy + new_student_count
        
        if current_total > room.capacity:
            return {
                "status": "OVERCROWDED", 
                "shortfall": current_total - room.capacity
            }
        return {"status": "OK", "remaining_seats": room.capacity - current_total}

    @staticmethod
    def log_maintenance(asset_id, issue_description, priority):
        """
        VPI's tool to manage school repairs (generators, roofs, etc.)
        """
        # Cleaned up redundant local 'MaintenanceTicket' import statement
        ticket = MaintenanceTicket(
            asset_id=asset_id,
            description=issue_description,
            priority=priority,  # Low, Medium, High
            status="Open",
            reported_at=datetime.now(timezone.utc)
        )
        db.session.add(ticket)
        db.session.commit()

    @staticmethod
    def check_accreditation_status(permit_expiry_date):
        """Calculates days until permit needs renewal"""
        today = datetime.now(timezone.utc).date()
        days_left = (permit_expiry_date - today).days
        
        if days_left < 0:
            return "EXPIRED - Urgent Action Required"
        elif days_left < 90:
            return f"WARNING: {days_left} days left for renewal"
        return "Compliant"

  # -------------------------------------------------------------------
# 1. FLASK APPLICATION INITIALIZATION & CONFIGURATION
# -------------------------------------------------------------------
# Calculate the absolute root folder where app.py resides
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# Inject current directory layout context into the system path for safe module imports
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

app = Flask(__name__)

# Security parameters configuration
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change_this_secret_key')

# ABSOLUTE DATABASE PATHING RESOLUTION
INSTANCE_PATH = os.path.join(BASE_DIR, 'instance')
os.makedirs(INSTANCE_PATH, exist_ok=True)

# Generate an absolute path to ensure desynchronization bugs are impossible
db_path = os.path.abspath(os.path.join(INSTANCE_PATH, 'keeptrack_full.db'))

# Assign SQLALCHEMY configuration parameters
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f'sqlite:///{db_path}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['PROMOTION_PASS_SCORE'] = int(os.environ.get('PROMOTION_PASS_SCORE', '70'))
app.config['MAX_FAILING_SUBJECTS'] = int(os.environ.get('MAX_FAILING_SUBJECTS', '2'))
MOE_PASSING_SCORE = app.config['PROMOTION_PASS_SCORE']
configure_app(app)

# =====================================================================
# ENGINE PLUGINS & EXTENSIONS INITIALIZATION
# =====================================================================

# 1. Bind SQLAlchemy to the Flask app instance FIRST
db.init_app(app)
configure_sqlite_performance(app, db)

# 2. Safely initialize Migrate now that db is bound to app
migrate = Migrate(app, db)
csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"

class AcademicManager:
    """The 'Brain' of the school - logic for the VPA"""

    @staticmethod
    def calculate_annual_result(student_id):
        """
        Logic for Promotion/Retention at year-end.
        Criteria: Must have 70+ average AND fail no more than 2 core subjects.
        """
        # Fetch all grades for the year
        all_grades = Grade.query.filter_by(student_id=student_id).all()
        
        # Group by course to get the average per subject
        subject_averages = {}
        for g in all_grades:
            course_id = getattr(g, 'course_id', None) or getattr(g, 'subject', None)
            if course_id not in subject_averages:
                subject_averages[course_id] = []
            subject_averages[course_id].append(g.score)

        failed_count = 0
        grand_total = 0
        subject_count = 0

        for course_id, scores in subject_averages.items():
            avg = sum(scores) / len(scores)
            grand_total += avg
            subject_count += 1
            if avg < 70:  # Liberian passing mark
                failed_count += 1

        final_gpa = grand_total / subject_count if subject_count > 0 else 0

        # PROMOTION LOGIC
        if failed_count > 2 or final_gpa < 70:
            return {"status": "RETAINED", "gpa": final_gpa, "failed_subjects": failed_count}
        else:
            return {"status": "PROMOTED", "gpa": final_gpa, "failed_subjects": failed_count}


# -------------------------------------------------------------------
# 3. SECURITY UTILITY LOGIC
# -------------------------------------------------------------------
def track_failed_attempt(ip_address, username=None):
    """Logs a failed login attempt and returns True if the IP should be blocked."""
    log = SecurityLog(
        ip_address=ip_address,
        event='FAILED_LOGIN',
        timestamp=datetime.now(timezone.utc),
    )
    db.session.add(log)
    db.session.commit()
    return check_brute_force(ip_address)


def generate_recovery_token(user_id):
    s = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    return s.dumps(user_id, salt='password-recovery-salt')


def verify_recovery_token(token, expiration=600): # 10 minutes
    s = URLSafeTimedSerializer(app.config['SECRET_KEY'])
    try:
        user_id = s.loads(token, salt='password-recovery-salt', max_age=expiration)
    except:
        return None
    return user_id


def generate_transcript_verify_token(student_id):
    """Signed token embedded in transcript QR codes (does not expire)."""
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return serializer.dumps({'sid': student_id}, salt='transcript-verify-salt')


def decode_transcript_verify_token(token):
    """Resolve a transcript QR token to an internal student id."""
    serializer = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        payload = serializer.loads(token, salt='transcript-verify-salt')
    except Exception:
        return None
    return payload.get('sid') if isinstance(payload, dict) else None


def calculate_period_score(ca_score, exam_score):
    """
    Logic to ensure the weights follow MoE standards.
    CA is usually out of 60, Exam out of 40.
    """
    if ca_score > 60 or exam_score > 40:
        raise ValueError("Score exceeds Liberian national standard limits.")
    
    return ca_score + exam_score 


# -------------------------------------------------------------------
# 4. ACCOUNTING & FINANCIAL MATRICES
# -------------------------------------------------------------------
def money(value):
    """Return a two-decimal float using Decimal math for fee totals."""
    if value is None or value == "":
        return 0.0
    if isinstance(value, str) and any(ch in value for ch in (",", "$", "₱", "₦", "€", "£")):
        return currency_to_float(value)
    return float((Decimal(str(value))).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def get_yearly_fee_for_student(student, academic_year=None):
    if not student:
        return money(0.0)

    klass = getattr(student, 'klass', None)
    if not klass and getattr(student, 'klass_id', None):
        klass = db.session.get(Class, student.klass_id)

    if klass is not None:
        raw_fee = getattr(klass, 'yearly_fees', None) or getattr(klass, 'yearly_fee', None)
        if raw_fee:
            return money(raw_fee)

    if academic_year:
        global_fee = SchoolFee.query.filter_by(academic_year_id=academic_year.id).first()
        if global_fee and global_fee.amount:
            return money(global_fee.amount)

    return money(0.0)


def is_yearly_fee_payment(description):
    desc = (description or "").strip().lower()
    if not desc:
        return True

    non_yearly_keywords = ("uniform", "graduation", "utility", "transport", "bus", "lunch", "book", "activity")
    if any(keyword in desc for keyword in non_yearly_keywords):
        return False

    yearly_keywords = ("tuition", "school fee", "yearly", "annual", "registration", "fee", "fees")
    return any(keyword in desc for keyword in yearly_keywords)


def build_student_financials(student, academic_year=None):
    """
    Computes precise financial metrics for an individual student.
    Aggregates metrics directly from the student_payments table using 
    the dynamic relationship backref to preserve multi-tenancy sandboxing.
    """
    financials = {
        'yearly_fee': 0.0, 'tuition_paid': 0.0, 'tuition_balance': 0.0,
        'utility_paid': 0.0, 'registration_paid': 0.0, 'total_paid': 0.0
    }

    if not student:
        return financials

    # 1. Fetch all payment records safely via dynamic relationship or fallback query
    if hasattr(student, 'payment_records'):
        all_payments = student.payment_records.all()
    else:
        # Fallback direct lookup if model metadata hasn't completed mapping
        all_payments = StudentPayment.query.filter_by(student_id=student.id).all()

    # 2. Filter payments by the selected academic year if provided
    year_id = academic_year.id if academic_year else None
    if year_id:
        payments = [p for p in all_payments if p.academic_year_id == year_id]
    else:
        payments = all_payments

    # 3. Totals come only from recorded StudentPayment rows — never from
    # student.registration_fees (that field is registrar metadata, not a ledger entry).
    yearly_fee = Decimal(str(get_yearly_fee_for_student(student, academic_year)))
    yearly_paid = Decimal("0")
    other_paid = Decimal("0")
    total_paid = Decimal("0")
    registration_payment_paid = Decimal("0")

    for payment in payments:
        amount = Decimal(str(payment.amount_paid or 0))
        total_paid += amount

        desc = (payment.description or "").lower()
        if "registration" in desc:
            registration_payment_paid += amount

        if is_yearly_fee_payment(payment.description):
            yearly_paid += amount
        else:
            other_paid += amount

    # 4. Final aggregation updates
    registration_paid = registration_payment_paid
    tuition_balance = max(Decimal("0"), yearly_fee - yearly_paid)

    financials.update({
        'yearly_fee': money(yearly_fee),
        'tuition_paid': money(yearly_paid),
        'tuition_balance': money(tuition_balance),
        'utility_paid': money(other_paid),
        'registration_paid': money(registration_paid),
        'total_paid': money(total_paid)
    })
    
    return financials


# -------------------------------------------------------------------
# 5. SCHEMA REPAIRS TOOLKIT
# -------------------------------------------------------------------
def ensure_legacy_sqlite_schema():
    """Add model columns that db.create_all() cannot add to existing SQLite tables."""
    repairs = {
        "class": {
            "yearly_fee": "FLOAT DEFAULT 0.0",
            "sponsor_id": "INTEGER",
        },
        "student": {
            "photo_filename": "VARCHAR(200) DEFAULT 'default_student.png'",
            "status": "VARCHAR(20) DEFAULT 'ACTIVE'",
            "grade_level": "INTEGER",
            "level": "VARCHAR(50)",
            "student_id_code": "VARCHAR(20)",
            "registration_type": "VARCHAR(20) DEFAULT 'New'",
            "created_at": "DATETIME",
            "tuition_cleared": "BOOLEAN DEFAULT 0",
            "registrar": "VARCHAR(100)",
            "registration_fees": "FLOAT DEFAULT 0.0",
        },
        "student_payment": {
            "installment": "INTEGER",
            "description": "VARCHAR(250)",
        },
        "grades": {
            "academic_year_id": "INTEGER",
            "class_id": "INTEGER",
            "marking_period": "INTEGER",
        },
        "assessments": {
            "subject_name": "VARCHAR(100)",
            "activity_type": "VARCHAR(50) DEFAULT 'Assignment'",
            "submission_mode": "VARCHAR(30) DEFAULT 'file_upload'",
            "marking_period": "INTEGER DEFAULT 1",
            "academic_year_id": "INTEGER",
            "teacher_id": "INTEGER",
            "file_name": "VARCHAR(255)",
        },
        "business_transaction": {
            "balance_after": "FLOAT DEFAULT 0.0",
            "is_deleted": "BOOLEAN DEFAULT 0",
            "deleted_at": "DATETIME",
            "deleted_by_id": "INTEGER",
        },
        "academic_year": {
            "current_year": "VARCHAR(20)",
            "klass_id": "INTEGER",
        },
        "events": {
            "event_type": "VARCHAR(50) DEFAULT 'general'",
            "updated_at": "DATETIME",
        },
        "discipline_records": {
            "logged_by_id": "INTEGER",
        },
        "suspensions": {
            "start_date": "DATE",
        },
        "users": {
            "username": "VARCHAR(80)",
            "photo": "VARCHAR(200)",
            "totp_secret": "VARCHAR(32)",
            "home_address": "VARCHAR(255)",
            "telephone_number": "VARCHAR(20)",
        },
        "announcements": {
            "content": "TEXT DEFAULT ''",
            "target_role": "TEXT DEFAULT 'all'",
            "category": "VARCHAR(50)",
        },
        "submissions": {
            "assessment_id": "INTEGER",
            "submission_text": "TEXT",
            "score": "FLOAT",
            "teacher_feedback": "TEXT",
            "is_graded": "BOOLEAN DEFAULT 0",
        },
    }

    for table_name, columns in repairs.items():
        existing = {
            row[1]
            for row in db.session.execute(text(f'PRAGMA table_info("{table_name}")')).fetchall()
        }
        if not existing:
            continue

        for column_name, column_type in columns.items():
            if column_name not in existing:
                db.session.execute(
                    text(f'ALTER TABLE "{table_name}" ADD COLUMN {column_name} {column_type}')
                )

    db.session.commit()

    try:
        db.session.execute(
            text('UPDATE events SET updated_at = created_at WHERE updated_at IS NULL')
        )
        db.session.commit()
    except Exception:
        db.session.rollback()


def repair_submission_legacy_links():
    """Backfill legacy activity_id values from newer assessment_id rows."""
    existing = {
        row[1]
        for row in db.session.execute(text('PRAGMA table_info("submissions")')).fetchall()
    }
    if not existing or "activity_id" not in existing:
        return 0
    if "assessment_id" in existing:
        db.session.execute(
            text(
                "UPDATE submissions SET activity_id = assessment_id "
                "WHERE (activity_id IS NULL OR activity_id = 0) AND assessment_id IS NOT NULL"
            )
        )
    db.session.commit()
    return 1


def normalize_misplaced_school_media():
    """Move photos/videos out of document-only entrance/info_sheet categories."""
    misplaced = SchoolMedia.query.filter(
        SchoolMedia.category.in_(DOCUMENT_ONLY_MEDIA_CATEGORIES),
        SchoolMedia.media_type.in_(("video", "photo")),
    ).all()
    if not misplaced:
        return 0
    for item in misplaced:
        item.category = "advertisement" if item.media_type == "video" else "gallery"
    db.session.commit()
    return len(misplaced)

# =====================================================================
# ✅ PLACE IMPORTS HERE (Right after extensions init, before anything else)
# =====================================================================
from models import (
    User, Student, Teacher, Class, ClassSubject, ClassSubjectTeacher, Enrollment, Grade,
    Attendance, Sponsor, Announcement, Discipline, Payroll,
    Assessment, AcademicYear, BusinessTransaction, StudentPayment,
    Leader, LeaderCategory, Event, SecurityLog, Suspension, Room,
    Asset, MaintenanceTicket, Activity, Submission, SystemSetting, SchoolMedia,
)
from forms import (
    LoginForm, RegisterStudentForm, PayrollForm, AcademicYearForm, RolloverWizardForm,
    AnnouncementForm, BusinessTransactionForm, AssignTeacherForm, CreateClassForm,
    EventForm, ConfirmDeleteForm, LeaderForm, EnrollmentForm, PaymentForm, TransactionForm,
    DisciplineForm,
)
from export_routes import init_export_routes
#
# Custom Jinja filters
@app.template_filter('grade_letter')
def grade_letter_filter(score):
    return SchoolEngine.get_grade_letter(score)

@app.template_filter('remarks')
def remarks_filter(score):
    return SchoolEngine.get_remarks(score)

SYSTEM_ARCHITECT_NAME = os.environ.get('SYSTEM_ARCHITECT_NAME', 'Francis Brownell')


def _whatsapp_digits(phone):
    if not phone:
        return ''
    return ''.join(ch for ch in str(phone) if ch.isdigit())


def get_system_settings():
    """Return the singleton system license row, creating defaults if needed."""
    settings = SystemSetting.query.order_by(SystemSetting.id.asc()).first()
    if not settings:
        settings = SystemSetting(system_active=True)
        db.session.add(settings)
        db.session.commit()
    return settings


def _system_hold_exempt_endpoints():
    return {
        'static', 'login', 'logout', 'index', 'about', 'contact',
        'events_list', 'school_media_gallery', 'school_media_download',
        'admin_system_control', 'admin_system_activate', 'admin_system_deactivate',
    }


@app.before_request
def enforce_system_license():
    """Block non-admin users when the platform is on hold."""
    endpoint = request.endpoint
    if not endpoint or endpoint in _system_hold_exempt_endpoints():
        return None
    if endpoint and endpoint.startswith('export_'):
        return None
    try:
        settings = get_system_settings()
    except Exception:
        return None
    if settings.system_active:
        return None
    if current_user.is_authenticated and normalize_role(current_user) == 'admin':
        return None
    if current_user.is_authenticated:
        logout_user()
    return render_template(
        'system_unavailable.html',
        settings=settings,
        system_architect_name=SYSTEM_ARCHITECT_NAME,
        system_admin_whatsapp=_whatsapp_digits(settings.admin_contact_phone),
    ), 503


@app.context_processor
def inject_nav_flags():
    role = getattr(current_user, "role", None)
    role_lower = (role or "").lower()
    try:
        settings = get_system_settings()
        system_is_active = settings.system_active
    except Exception:
        settings = None
        system_is_active = True
    return {
        "announcements_link": role_lower in {"admin", "teacher", "principal", "vpa"},
        "can_manage_leaders": role_lower in {"admin", "principal"},
        "can_manage_events": role_lower in COMMUNICATIONS_MANAGER_ROLES,
        "can_manage_school_media": role_lower in SCHOOL_MEDIA_MANAGER_ROLES,
        "registrar_media_only": role_lower == "registrar",
        "system_settings": settings,
        "system_is_active": system_is_active,
        "system_on_hold": not system_is_active,
        "system_architect_name": SYSTEM_ARCHITECT_NAME,
        "system_admin_whatsapp": _whatsapp_digits(
            settings.admin_contact_phone if settings else ''
        ),
    }

# -------------------------------------------------------------------
# Login Manager
# -------------------------------------------------------------------
@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))

# -------------------------------------------------------------------
# Routes
# -------------------------------------------------------------------

@app.route('/')
def index():
    # Show the next three upcoming events on the public homepage
    upcoming_events = (
        Event.query.order_by(Event.date.asc())
        .filter(Event.date >= datetime.now(timezone.utc).date())
        .limit(3)
        .all()
    )
    total_events = Event.query.count()
    featured_media = (
        SchoolMedia.query.filter(
            SchoolMedia.is_published.is_(True),
            SchoolMedia.category.in_(HOMEPAGE_FEATURED_MEDIA_CATEGORIES),
        )
        .order_by(SchoolMedia.created_at.desc())
        .limit(6)
        .all()
    )
    entrance_highlights = (
        SchoolMedia.query.filter(
            SchoolMedia.is_published.is_(True),
            SchoolMedia.category == "entrance",
            SchoolMedia.media_type == "document",
        )
        .order_by(SchoolMedia.created_at.desc())
        .limit(2)
        .all()
    )
    info_sheet_highlights = (
        SchoolMedia.query.filter(
            SchoolMedia.is_published.is_(True),
            SchoolMedia.category == "info_sheet",
            SchoolMedia.media_type == "document",
        )
        .order_by(SchoolMedia.created_at.desc())
        .limit(3)
        .all()
    )
    latest_announcements = (
        Announcement.query.order_by(Announcement.created_at.desc())
        .limit(6)
        .all()
    )
    return render_template(
        'index.html',
        events=upcoming_events,
        highlighted_event=upcoming_events[0] if upcoming_events else None,
        featured_media=featured_media,
        entrance_highlights=entrance_highlights,
        info_sheet_highlights=info_sheet_highlights,
        latest_announcements=latest_announcements,
        current_year=datetime.now(timezone.utc).year,
        total_events=total_events,
        youtube_embed_url=_youtube_embed_url,
    )

# ----------------------------- LOGIN -------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if form.validate_on_submit():
        if check_brute_force(ip):
            flash('Too many failed login attempts. Please try again in 15 minutes.', 'danger')
            return render_template('login.html', form=form)

        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            settings = get_system_settings()
            if not settings.system_active and normalize_role(user) != 'admin':
                flash('The system is on hold. Contact the administrator to renew service.', 'danger')
                return render_template('login.html', form=form)
            login_user(user)
            flash('Login successful.', 'success')
            log_incident('SUCCESSFUL_LOGIN')
            if (user.role or '').strip().lower() == 'teacher':
                return redirect(url_for('teacher_dashboard'))
            return redirect(url_for('dashboard'))
        
        track_failed_attempt(ip, form.email.data)
        flash('Invalid email or password.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have logged out.', 'info')
    return redirect(url_for('login'))


@app.route('/admin/system-control', methods=['GET', 'POST'])
@login_required
def admin_system_control():
    if normalize_role(current_user) != 'admin':
        flash('Administrator access required.', 'danger')
        return redirect(url_for('dashboard'))

    settings = get_system_settings()
    if request.method == 'POST':
        action = (request.form.get('action') or '').strip().lower()
        if action == 'save_contact':
            settings.admin_contact_email = (request.form.get('admin_contact_email') or '').strip()
            settings.admin_contact_phone = (request.form.get('admin_contact_phone') or '').strip() or None
            settings.hold_message = (request.form.get('hold_message') or '').strip() or settings.hold_message
            db.session.commit()
            flash('Hold page contact details saved.', 'success')
            return redirect(url_for('admin_system_control'))
        if action == 'activate':
            settings.system_active = True
            settings.deactivated_at = None
            settings.deactivated_by_id = None
            db.session.commit()
            flash('System activated. All users may sign in again.', 'success')
            return redirect(url_for('admin_system_control'))

    return render_template(
        'admin_system_control.html',
        settings=settings,
        system_architect_name=SYSTEM_ARCHITECT_NAME,
    )


@app.route('/admin/system-control/activate', methods=['POST'])
@login_required
def admin_system_activate():
    if normalize_role(current_user) != 'admin':
        flash('Administrator access required.', 'danger')
        return redirect(url_for('dashboard'))
    settings = get_system_settings()
    settings.system_active = True
    settings.deactivated_at = None
    settings.deactivated_by_id = None
    db.session.commit()
    flash('System activated for all users.', 'success')
    return redirect(url_for('admin_system_control'))


@app.route('/admin/system-control/deactivate', methods=['POST'])
@login_required
def admin_system_deactivate():
    if normalize_role(current_user) != 'admin':
        flash('Administrator access required.', 'danger')
        return redirect(url_for('dashboard'))
    settings = get_system_settings()
    settings.system_active = False
    settings.deactivated_at = datetime.now(timezone.utc)
    settings.deactivated_by_id = current_user.id
    db.session.commit()
    flash('System placed on hold. Only administrators can sign in.', 'warning')
    return redirect(url_for('admin_system_control'))


# --------------------------- DASHBOARD -----------------------------
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    """
    Centralized Core Routing Gateway for School Management Platform.
    Dynamically orchestrates backend telemetry state processing and safely dispatches
    role-based permissions contexts across 10 operational profiles.
    
    SAFE-FALLBACK VERSION: Gracefully accommodates freshly initialized/empty databases.
    """
    # ----------------------------------------------------------------------
    # 1. CORE TELEMETRY & SYSTEM STATE INITIALIZATION
    # ----------------------------------------------------------------------
    # Extract the foundational active terminal anchor
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    
    # SAFE FALLBACK: If no academic year exists yet, we store None instead of crashing or looping
    active_year_id = active_year.id if active_year else None
    selected_year_name = active_year.name if active_year else "No Active Year Setup"
    selected_year = active_year

    # Query all_students globally to satisfy template view layout requirements
    all_students = Student.query.all()

    # Build standardized administrative analytic stats metrics tracking matrix
    # If active_year_id is None, student counts safely fallback to 0 instead of crashing
    stats = {
        'students': Student.query.filter_by(academic_year_id=active_year_id).count() if active_year_id else 0,
        'new_students': Student.query.filter_by(registration_type='New', academic_year_id=active_year_id).count() if active_year_id else 0,
        'returning_students': Student.query.filter_by(registration_type='Returning', academic_year_id=active_year_id).count() if active_year_id else 0,
        'teachers': Teacher.query.count(),
        'classes': Class.query.count(),
        'payments': StudentPayment.query.count()
    }

    # Global template view model layer anchors
    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    current_role = (current_user.role or "").lower()
    announcements_link = current_role in {"admin", "principal"}

    # ======================================================================
    # 1.5 SPECIALIZED FACULTY / INSTRUCTOR DIRECT ROUTING (EARLY DISPATCH)
    # ======================================================================
    # Redirect specialized leadership roles to their dedicated dashboard functions
    if current_role == "principal":
        return principal_dashboard()
    elif current_role == "teacher":
        return redirect(url_for('teacher_dashboard', _external=False))
    elif current_role == "vpi":
        return redirect(url_for('vpi_dashboard', _external=False))
    elif current_role == "vpa":
        return redirect(url_for('vpa_dashboard', _external=False))
    elif current_role == "dean":
        return redirect(url_for('dean_dashboard', _external=False))
    elif current_role == "student":
        return redirect(url_for('student_dashboard', _external=False))
    elif current_role == "sponsor":
        return redirect(url_for('teacher_dashboard', _external=False))

    # Define strict interface mapping layout dictionary
    template_map = {
        "admin": "dashboard_admin.html",
        "student": "dashboard_student.html",
        "registrar": "dashboard_registrar.html",
        "parent": "dashboard_parent.html",
        "business": "dashboard_business.html"
    }

    template_name = template_map.get(current_role)

    # ----------------------------------------------------------------------
    # 2. ROLE-BASED DISPATCH ROUTING AND SAFELY RENDERING
    # ----------------------------------------------------------------------
    if not template_name:
        # Security fallback: If user role is unrecognized or blank, abort safely
        return "Access Denied: Invalid System Role Configuration.", 403

    # Prepare optional context used by specific dashboard templates
    classes = Class.query.order_by(Class.name.asc()).all()
    search_class = request.args.get('search_class')

    extra_context = {}
    if template_name == 'dashboard_registrar.html':
        registrar_ctx = build_registrar_dashboard_context(search_class=search_class)
        return render_template(
            template_name,
            announcements_link=announcements_link,
            all_students=all_students,
            **registrar_ctx,
        )

    if template_name == 'dashboard_business.html':
        payment_form = PaymentForm()
        if request.method == 'POST' and 'submit_payment' in request.form:
            populate_business_payment_form(
                payment_form,
                active_year,
                years,
                class_id=request.form.get('class_id', type=int),
                student_id=request.form.get('student_id', type=int),
            )
            if payment_form.validate_on_submit():
                try:
                    student = db.session.get(Student, payment_form.student.data)
                    if not student:
                        flash('Student not found.', 'danger')
                    else:
                        paid_amount = parse_currency_amount(payment_form.amount_paid.data)
                        record_student_payment_with_income(
                            student,
                            payment_form.academic_year.data,
                            payment_form.term.data,
                            paid_amount,
                            (payment_form.description.data or 'Tuition Payment').strip(),
                            installment=payment_form.installment.data,
                        )
                        db.session.commit()
                        flash(
                            f"Payment of ${float(paid_amount):,.2f} recorded for {student.full_name}.",
                            'success',
                        )
                        return redirect(url_for(
                            'dashboard',
                            tab='tuition',
                            class_id=request.form.get('class_id', type=int),
                            student_id=payment_form.student.data,
                        ))
                except Exception as exc:
                    db.session.rollback()
                    flash(f'Could not record payment: {exc}', 'danger')
            else:
                for field, errors in payment_form.errors.items():
                    for err in errors:
                        flash(f'Payment ({field}): {err}', 'danger')

        business_ctx = build_business_dashboard_context(
            active_year=active_year,
            stats=stats,
            years=years,
            selected_year_name=selected_year_name,
            search_class=search_class,
            payment_form=payment_form,
        )
        return render_template(
            template_name,
            announcements_link=announcements_link,
            all_students=all_students,
            selected_year_name=selected_year_name,
            **business_ctx,
        )

    if template_name == 'dashboard_parent.html':
        children = []
        if current_user.email:
            parent_email = current_user.email.strip().lower()
            children = (
                Student.query.filter(func.lower(Student.parent_email) == parent_email)
                .order_by(Student.last_name.asc(), Student.first_name.asc())
                .all()
            )
        student = children[0] if children else None
        return render_template(
            template_name,
            stats=stats,
            active_year=active_year,
            student=student,
            children=children,
            years=years,
            selected_year=selected_year,
            selected_year_name=selected_year_name,
            announcements_link=announcements_link,
        )

    if template_name == 'dashboard_admin.html':
        recent_payments = (
            StudentPayment.query.order_by(StudentPayment.paid_on.desc())
            .limit(8)
            .all()
        )
        return render_template(
            template_name,
            stats=stats,
            counts=stats,
            active_year=active_year,
            all_students=all_students,
            years=years,
            selected_year=selected_year_name or (active_year.name if active_year else ''),
            selected_year_name=selected_year_name,
            announcements_link=announcements_link,
            payments=recent_payments,
        )

    # Safely forward all critical layout variables down to the UI views
    return render_template(
        template_name,
        stats=stats,                        # Supports templates that reference stats directly
        counts=stats,                      # Admin dashboard expects counts
        active_year=active_year,           # Required for dashboard header display
        all_students=all_students,
        years=years,
        selected_year=selected_year,
        selected_year_name=selected_year_name,
        announcements_link=announcements_link,
        **extra_context
    )


@app.route('/registrar/class/<int:class_id>/students', methods=['GET'])
@login_required
def registrar_class_students(class_id):
    """
    Renders the authorized class roster for an assigned classroom block.
    Supports granular role evaluation, eager-loading optimizations, and 
    defensive image asset pathway matching.
    """
    # 1. Broadened Authorization Guard
    authorized_roles = {'admin', 'registrar', 'business', 'principal', 'vpa', 'vpi', 'dean'}

    if not current_user.is_authenticated or normalize_role(current_user) not in authorized_roles:
        flash("Access Denied: Your system authority profile cannot read this database leaf.", "danger")
        return redirect(url_for('dashboard' if hasattr(current_user, 'role') else 'login'))

    # 2. Defensive Structural Query Resolution
    # Look up the specific class directory block or handle failure safely
    klass = db.session.get(Class, class_id)
    if not klass:
        flash(f"System Matrix Failure: Class record node #{class_id} could not be resolved.", "warning")
        return redirect(url_for('dashboard'))

    # 3. Eager Loading Student Roster Payload
    # Assuming your Student model has a 'klass_id' relationship foreign key
    students = Student.query.filter_by(klass_id=class_id).all()

    # 4. Secure Context Response Delivery
    # Photo URLs are now handled via the photo_url property on the Student model
    return render_template(
        'registrar_class_students.html',
        klass=klass,
        students=students,
        current_user=current_user,
        active_year=AcademicYear.query.filter_by(is_active=True).first()
    )


@app.route('/business/class/<int:class_id>/students', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'business', 'VPI', 'principal')
def business_class_students(class_id):
    klass = Class.query.get_or_404(class_id)
    
    # 1. Look up the active session context needed for both viewing and paying
    active_year = AcademicYear.query.filter_by(is_active=True).first()

    # ==========================================
    # HANDLE POST REQUEST: RECORDING A PAYMENT
    # ==========================================
    if request.method == 'POST':
        if not active_year:
            flash("Cannot accept payments without an active session configuration.", "danger")
            return redirect(url_for('business_class_students', class_id=class_id))
            
        student_id = request.form.get('student_id', type=int)
        term = request.form.get('term', type=int)
        raw_amount_paid = request.form.get('amount_paid')
        description = request.form.get('description', 'Tuition Payment')
        installment = request.form.get('installment', type=int) # Optional installment tracking

        try:
            amount_paid = parse_currency_amount(raw_amount_paid)
        except ValueError:
            amount_paid = None

        # Basic input validation
        if not student_id or term is None or amount_paid is None:
            flash("All required payment fields (Student, Term, Amount) must be provided.", "danger")
            return redirect(url_for('business_class_students', class_id=class_id))

        student = db.session.get(Student, student_id)
        if not student:
            flash("Student record not found.", "danger")
            return redirect(url_for('business_class_students', class_id=class_id))

        try:
            record_student_payment_with_income(
                student,
                active_year.id,
                term,
                amount_paid,
                description,
                installment=installment,
            )
            db.session.commit()
            flash("Payment recorded and posted to business income.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"Could not record payment: {exc}", "danger")
        return redirect(url_for('business_class_students', class_id=class_id))

    # ==========================================
    # HANDLE GET REQUEST: RENDER STUDENT LEDGER
    # ==========================================
    students_query = Student.query.filter_by(klass_id=class_id)
    if active_year:
        students_query = students_query.filter(Student.academic_year_id == active_year.id)

    students_data = []
    for student in students_query.order_by(Student.last_name, Student.first_name).all():
        financials = build_student_financials(student, active_year)
        payment_query = StudentPayment.query.filter_by(student_id=student.id)
        if active_year:
            payment_query = payment_query.filter_by(academic_year_id=active_year.id)
        payment_count = payment_query.count()
        
        students_data.append({
            "id": student.id,
            "student_id": student.student_id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "full_name": student.full_name,
            "email": student.user.email if student.user else (student.parent_email or "-"),
            "academic_year": student.academic_year.name if student.academic_year else "-",
            "academic_year_id": student.academic_year_id,
            "registration_fees": financials["registration_paid"],
            "yearly_fee": financials["yearly_fee"],
            "total_paid": financials["total_paid"],
            "balance": financials["tuition_balance"],
            "payment_count": payment_count,
            "photo_url": student.photo_url,
        })

    return render_template(
        'business_class_students.html',
        klass=klass,
        students=students_data,
        active_year=active_year,
        current_user=current_user
    )

@app.route('/grade_entry_class')
@login_required
def grade_entry():
    """
    Landing Page: Shows the list of ALL classes this specific teacher has clearance
    to enter grades for (both taught classes and sponsored classes).
    """
    if normalize_role(current_user) != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    all_classes = get_teacher_classes(teacher, current_user)
    if not all_classes:
        flash('No classes assigned yet.', 'warning')
        return redirect(url_for('teacher_dashboard'))
    if len(all_classes) == 1:
        return redirect(url_for('grade_entry_class', class_id=all_classes[0].id))
    return redirect(url_for('teacher_dashboard'))


@app.route('/grade-entry/<int:class_id>', methods=['GET'])
@login_required
def grade_entry_class(class_id):
    """
    MoE-standard grade entry sheet for one class, subject, and marking period.
    """
    if normalize_role(current_user) != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    klass = Class.query.get_or_404(class_id)

    if not teacher or not teacher_can_access_class(teacher, current_user, class_id):
        flash('Access mapping violation: You do not possess clearance for this room.', 'danger')
        return redirect(url_for('dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash('No active academic year found. Please contact the administrator.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    subjects = get_assignable_subjects_for_class(teacher, current_user, class_id)
    selected_subject = (request.args.get('subject') or '').strip()
    if selected_subject not in subjects:
        selected_subject = subjects[0] if subjects else ''

    selected_period = request.args.get('period', 1, type=int)
    if selected_period not in range(1, 9):
        selected_period = 1

    students = (
        Student.query.filter_by(klass_id=klass.id)
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )

    grade_rows = {}
    if selected_subject:
        existing_grades = Grade.query.filter_by(
            class_id=class_id,
            subject=selected_subject,
            academic_year_id=active_year.id,
        ).all()
        for grade in existing_grades:
            period_num = grade.marking_period or normalize_grade_period(grade.period)
            if period_num == selected_period and grade.student_id in {s.id for s in students}:
                grade_rows[grade.student_id] = grade

    period_label = dict(MOE_GRADING_PERIODS).get(selected_period, f'Period {selected_period}')
    is_semester_exam = selected_period in (7, 8)

    teacher_class_tabs = [
        {
            'id': card['id'],
            'name': card['name'],
            'grade_level': card['grade_level'],
            'stream': card.get('stream'),
            'active': card['id'] == class_id,
        }
        for card in get_teacher_class_cards(teacher, current_user)
    ]

    return render_template(
        'grade_entry_class.html',
        klass=klass,
        students=students,
        grade_rows=grade_rows,
        active_year=active_year,
        subjects=subjects,
        selected_subject=selected_subject,
        selected_period=selected_period,
        period_label=period_label,
        grading_periods=MOE_GRADING_PERIODS,
        is_semester_exam=is_semester_exam,
        teacher_name=teacher.full_name,
        teacher_class_tabs=teacher_class_tabs,
    )

@app.route('/save-grades/<int:class_id>', methods=['POST'])
@login_required
def save_grades(class_id):
    if normalize_role(current_user) != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    if not teacher_can_access_class(teacher, current_user, class_id):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash('No active academic year found. Please contact the administrator.', 'danger')
        return redirect(url_for('grade_entry_class', class_id=class_id))

    subject_name = (request.form.get('subject') or '').strip()
    period = request.form.get('period', type=int)
    publish_action = (request.form.get('publish_action') or 'draft').strip().lower()
    publish_to_report = publish_action == 'publish'
    if not subject_name:
        flash('Please select a subject before saving grades.', 'danger')
        return redirect(url_for('grade_entry_class', class_id=class_id))
    if period not in range(1, 9):
        flash('Invalid marking period selected.', 'danger')
        return redirect(url_for('grade_entry_class', class_id=class_id, subject=subject_name))

    allowed_subjects = get_assignable_subjects_for_class(teacher, current_user, class_id)
    if subject_name not in allowed_subjects:
        flash('You are not assigned to teach that subject in this class.', 'danger')
        return redirect(url_for('grade_entry_class', class_id=class_id))

    students = Student.query.filter_by(klass_id=class_id).all()
    period_label = dict(MOE_GRADING_PERIODS).get(period, f'Period {period}')
    saved_count = 0

    for student in students:
        if period in range(1, 7):
            ca_key = f'ca_{student.id}'
            exam_key = f'exam_{student.id}'
            ca_raw = request.form.get(ca_key, '').strip()
            exam_raw = request.form.get(exam_key, '').strip()
            if ca_raw == '' and exam_raw == '':
                continue

            ca_score = float(ca_raw or 0)
            exam_score = float(exam_raw or 0)
            total = SchoolEngine.calculate_period_total(ca_score, exam_score)
            if total is None:
                flash(
                    f'Invalid scores for {student.full_name}. CA must be ≤ 60 and Exam ≤ 40.',
                    'danger',
                )
                return redirect(
                    url_for(
                        'grade_entry_class',
                        class_id=class_id,
                        subject=subject_name,
                        period=period,
                    )
                )
        else:
            exam_raw = request.form.get(f'exam_{student.id}', '').strip()
            if exam_raw == '':
                continue
            exam_score = float(exam_raw)
            if exam_score < 0 or exam_score > 100:
                flash(
                    f'Invalid exam score for {student.full_name}. Score must be between 0 and 100.',
                    'danger',
                )
                return redirect(
                    url_for(
                        'grade_entry_class',
                        class_id=class_id,
                        subject=subject_name,
                        period=period,
                    )
                )
            ca_score = 0.0
            total = exam_score

        grade = find_grade_record(
            student.id,
            subject_name,
            period,
            class_id=class_id,
            academic_year_id=active_year.id,
        )

        if grade and grade.is_finalized:
            flash(f'Grades for {student.full_name} are finalized and cannot be changed.', 'warning')
            continue

        if not grade:
            grade = Grade(
                student_id=student.id,
                teacher_id=teacher.id,
                class_id=class_id,
                academic_year_id=active_year.id,
                subject=subject_name,
                subject_name=subject_name,
            )
            db.session.add(grade)

        grade.teacher_id = teacher.id
        grade.class_id = class_id
        grade.academic_year_id = active_year.id
        grade.subject = subject_name
        grade.subject_name = subject_name
        grade.marking_period = period
        grade.period = period
        grade.activity_type = 'Semester Exam' if period in (7, 8) else 'Period Assessment'
        grade.ca_score = ca_score
        grade.exam_score = exam_score if period in range(1, 7) else total
        grade.score = total
        grade.remarks = SchoolEngine.get_remarks(total)
        grade.submitted = publish_to_report

        if 1 <= period <= 6:
            setattr(grade, f'p{period}', int(round(total)))

        saved_count += 1

    db.session.commit()
    if saved_count:
        if publish_to_report:
            flash(f'{period_label} grades published to report cards for {subject_name}.', 'success')
        else:
            flash(f'{period_label} draft saved for {subject_name}. Students can see their standing; report cards unchanged.', 'success')
    else:
        flash('No grade values were entered.', 'warning')

    if request.form.get('return_to') == 'grading_hub':
        return redirect(
            url_for(
                'class_grading_hub',
                class_id=class_id,
                subject=subject_name,
                period=period,
                hub_tab='moe',
            )
        )
    return redirect(
        url_for(
            'grade_entry_class',
            class_id=class_id,
            subject=subject_name,
            period=period,
        )
    )

@app.route('/download-grades/<int:class_id>')
@login_required
def download_grades(class_id):
    role = normalize_role(current_user)
    if role not in ('registrar', 'teacher', 'admin'):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    if role == 'teacher':
        teacher = Teacher.query.filter_by(user_id=current_user.id).first()
        if not teacher or not teacher_can_access_class(teacher, current_user, class_id):
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

    year_name = request.args.get('year')
    active_year = AcademicYear.query.filter_by(name=year_name).first() if year_name else None
    if not active_year:
        active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash('No academic year found for export.', 'warning')
        return redirect(url_for('dashboard'))

    subject = (request.args.get('subject') or '').strip()
    period = request.args.get('period', type=int)

    grades_query = Grade.query.filter_by(
        class_id=class_id,
        academic_year_id=active_year.id,
    )
    if subject:
        grades_query = grades_query.filter(Grade.subject == subject)
    if period:
        grades_query = grades_query.filter(Grade.marking_period == period)

    grades = grades_query.order_by(
        Grade.subject.asc(),
        Grade.marking_period.asc(),
        Grade.student_id.asc(),
    ).all()
    student_map = {s.id: s for s in Student.query.filter_by(klass_id=class_id).all()}

    def generate():
        buf = StringIO()
        writer = csv.writer(buf)
        writer.writerow([
            'Student ID', 'Student Name', 'Subject', 'Period',
            'CA Score', 'Exam Score', 'Total', 'Grade Letter', 'Published',
        ])
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        for grade in grades:
            student = student_map.get(grade.student_id)
            writer.writerow([
                student.student_id if student else grade.student_id,
                student.full_name if student else '',
                grade.subject or grade.subject_name or '',
                grade.marking_period or grade.period or '',
                grade.ca_score if grade.ca_score is not None else '',
                grade.exam_score if grade.exam_score is not None else '',
                grade.score if grade.score is not None else '',
                SchoolEngine.get_grade_letter(grade.score or 0),
                'Yes' if grade.submitted else 'No',
            ])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

    safe_class = (klass.name or f'class_{class_id}').replace(' ', '_')[:40]
    filename = f'grades_{safe_class}_{active_year.name}.csv'
    return Response(
        generate(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'},
    )

@app.route('/download/<path:filename>')
@login_required
def download_file(filename):
    return safe_send_upload_file('uploads/activities', filename)


@app.route('/teacher/download-activity/<int:assessment_id>')
@login_required
def teacher_download_activity(assessment_id):
    """Let teachers download the assignment file they attached to an activity."""
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can download activity resources.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    klass = assessment.klass
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to access this activity.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    if not assessment.file_name:
        flash('No assignment file was attached to this activity.', 'warning')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    return safe_send_upload_file('uploads/activities', assessment.file_name)


@app.route('/student/download-activity/<int:assessment_id>')
@login_required
def student_download_activity(assessment_id):
    """Let a student download the teacher's assignment file for an activity."""
    if normalize_role(current_user) != 'student':
        flash('Only students can download class activities.', 'danger')
        return redirect(url_for('dashboard'))

    student = get_student_for_user(current_user)
    if not student:
        flash('Student profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    class_id = get_student_class_id(student)
    if class_id != assessment.klass_id:
        flash('You are not authorized to access this activity.', 'danger')
        return redirect(url_for('student_dashboard'))

    if not assessment.file_name:
        flash('No assignment file was attached to this activity.', 'warning')
        return redirect(url_for('student_dashboard'))

    return safe_send_upload_file('uploads/activities', assessment.file_name)


@app.route('/student/download-submission/<int:assessment_id>')
@login_required
def student_download_submission(assessment_id):
    """Let a student download their own submitted work."""
    if normalize_role(current_user) != 'student':
        flash('Only students can download submissions.', 'danger')
        return redirect(url_for('dashboard'))

    student = get_student_for_user(current_user)
    if not student:
        flash('Student profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    class_id = get_student_class_id(student)
    if class_id != assessment.klass_id:
        flash('You are not authorized to access this submission.', 'danger')
        return redirect(url_for('student_dashboard'))

    submission = Submission.query.filter_by(
        assessment_id=assessment_id,
        student_id=student.id,
    ).first()
    if not submission or not submission.file_path:
        flash('You have not uploaded a file for this activity yet.', 'warning')
        return redirect(url_for('student_dashboard'))

    rel_path = submission.file_path.replace('\\', '/').lstrip('/')
    if rel_path.startswith('static/'):
        rel_path = rel_path[len('static/'):]
    return safe_send_upload_file(os.path.dirname(rel_path), os.path.basename(rel_path))

@app.route('/student/upload/<int:assessment_id>', methods=['POST'])
@app.route('/student/upload/activity/<int:assessment_id>', methods=['POST'])
@login_required
def student_upload(assessment_id):
    if normalize_role(current_user) != 'student':
        flash('Only students can upload activities.', 'danger')
        return redirect(url_for('dashboard'))

    student = get_student_for_user(current_user)
    if not student:
        flash('Student profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    if 'assignment' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('student_dashboard'))
    
    file = request.files['assignment']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('student_dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    if get_student_class_id(student) != assessment.klass_id:
        flash('You are not authorized to submit for this class activity.', 'danger')
        return redirect(url_for('student_dashboard'))

    upload_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'submissions')
    os.makedirs(upload_dir, exist_ok=True)

    filename = f"sub_{assessment_id}_{student.id}_{secure_filename(file.filename)}"
    file_path = os.path.join(upload_dir, filename)
    file.save(file_path)

    submission_db_path = os.path.join('uploads', 'submissions', filename).replace('\\', '/')
    submission = Submission.query.filter_by(assessment_id=assessment_id, student_id=student.id).first()
    if submission:
        submission.file_path = submission_db_path
        submission.submitted_at = datetime.now(timezone.utc)
    else:
        submission = Submission(
            assessment_id=assessment_id,
            student_id=student.id,
            file_path=submission_db_path
        )
        db.session.add(submission)

    db.session.commit()
    flash('Activity uploaded successfully!', 'success')
    return redirect(url_for('student_dashboard', tab='tasks'))

@app.route('/student/submit-activity/<int:assessment_id>', methods=['POST'])
@login_required
def submit_activity(assessment_id):
    if normalize_role(current_user) != 'student':
        flash('Only students can submit activities.', 'danger')
        return redirect(url_for('dashboard'))

    student = get_student_for_user(current_user)
    if not student:
        flash('Student profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    if get_student_class_id(student) != assessment.klass_id:
        flash('You are not authorized to submit for this class activity.', 'danger')
        return redirect(url_for('student_dashboard'))

    quiz_answers = request.form.get('quiz_answers', '').strip()
    if not quiz_answers:
        flash('Please provide answers before submitting.', 'danger')
        return redirect(url_for('student_dashboard'))

    if assessment.submission_mode != 'text_entry':
        flash('This activity does not accept text submissions.', 'danger')
        return redirect(url_for('student_dashboard'))

    submission = Submission.query.filter_by(assessment_id=assessment_id, student_id=student.id).first()
    if submission:
        submission.submission_text = quiz_answers
        submission.submitted_at = datetime.now(timezone.utc)
    else:
        submission = Submission(
            assessment_id=assessment_id,
            student_id=student.id,
            submission_text=quiz_answers
        )
        db.session.add(submission)

    db.session.commit()
    flash('Text submission received successfully!', 'success')
    return redirect(url_for('student_dashboard', tab='tasks'))

@app.route('/teacher/activity/<int:assessment_id>')
@app.route('/teacher/assessment/<int:assessment_id>')
@login_required
def activity_detail(assessment_id):
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can view activity review pages.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    klass = assessment.klass
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to review this activity.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    class_students = sorted(
        get_students_for_class_ids([klass.id]),
        key=lambda s: ((s.last_name or '').lower(), (s.first_name or '').lower()),
    )
    submissions = Submission.query.filter_by(assessment_id=assessment.id).all()
    submission_by_student = {sub.student_id: sub for sub in submissions}
    pending_grades = sum(
        1 for sub in submissions if not sub.is_graded and (sub.file_path or sub.submission_text)
    )
    active_year = AcademicYear.query.filter_by(is_active=True).first()

    return render_template(
        'activity_detail.html',
        activity=assessment,
        assessment=assessment,
        submissions=submissions,
        class_students=class_students,
        submission_by_student=submission_by_student,
        pending_grades=pending_grades,
        current_user=current_user,
        active_year=active_year
    )

@app.route('/finalize-grades/<int:class_id>', methods=['POST'])
@login_required
def finalize_grades(class_id):
    if normalize_role(current_user) != 'registrar':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    klass = Class.query.get_or_404(class_id)
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    grade_query = Grade.query.filter_by(class_id=class_id)
    if active_year:
        grade_query = grade_query.filter_by(academic_year_id=active_year.id)
    grades = grade_query.all()
    for grade in grades:
        grade.is_finalized = True
    
    db.session.commit()
    flash('Grades finalized for this class.', 'success')
    return redirect(url_for('dashboard'))
#--------------------------------------------
#Report card generation and download
#--------------------------------------------
def format_student_school_level(student):
    """Return the academic level label shown on report cards."""
    if student.level:
        return student.level
    grade = student.grade_level
    if grade is None and student.klass:
        grade = student.klass.grade_level
    if grade is not None:
        return f"Grade {grade}"
    return "Not Set"


@app.route('/report-card/<int:student_id>')
@login_required
def report_card(student_id):
    student = Student.query.get_or_404(student_id)
    
    staff_roles = {'admin', 'teacher', 'registrar', 'principal', 'vpa', 'vpi', 'dean', 'business'}
    user_role = (current_user.role or '').lower()
    if user_role == 'student':
        linked_student = get_student_for_user(current_user)
        if not linked_student or linked_student.id != student.id:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))
    elif user_role == 'parent':
        if student.parent_email != current_user.email:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))
    elif user_role not in staff_roles:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    if user_role == 'teacher':
        teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
        if not teacher_profile or not teacher_can_access_student(teacher_profile, current_user, student):
            flash('You may only view report cards for students in your assigned classes.', 'danger')
            return redirect(url_for('teacher_dashboard'))
    
    # Get official (published) grades for this student only
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    year_id = request.args.get('academic_year_id', type=int) or (active_year.id if active_year else None)
    grades_records = official_grade_records(student_id, year_id)
    
    # Organize grades by subject and build a period matrix for the report card
    subjects_list = sorted({g.subject for g in grades_records if g.subject}, key=lambda x: x.lower())
    structured_subjects = []

    for sub_name in subjects_list:
        sub_grades = [g for g in grades_records if g.subject == sub_name]
        
        # Extract individual period scores
        p1 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 1), '')
        p2 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 2), '')
        p3 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 3), '')
        p4 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 4), '')
        p5 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 5), '')
        p6 = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 6), '')
        
        # Semester exam columns align with MoE report card layout
        sem1_exam = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 7), '')
        sem2_exam = next((g.score for g in sub_grades if (g.marking_period or normalize_grade_period(g.period)) == 8), '')
        
        # Calculate Semester 1 Average
        sem1_scores = [s for s in [p1, p2, p3, sem1_exam] if isinstance(s, (int, float))]
        sem1_avg = round(sum(sem1_scores) / len(sem1_scores), 1) if sem1_scores else ''
        
        # Calculate Semester 2 Average
        sem2_scores = [s for s in [p4, p5, p6, sem2_exam] if isinstance(s, (int, float))]
        sem2_avg = round(sum(sem2_scores) / len(sem2_scores), 1) if sem2_scores else ''
        
        # Calculate Final Average
        all_scores = sem1_scores + sem2_scores
        final_avg = round(sum(all_scores) / len(all_scores), 1) if all_scores else ''
        
        remarks = '; '.join({g.remarks for g in sub_grades if g.remarks})

        structured_subjects.append({
            'name': sub_name,
            'p1': p1,
            'p2': p2,
            'p3': p3,
            'exam': sem1_exam,
            'avg': sem1_avg,
            'p4': p4,
            'p5': p5,
            'p6': p6,
            'final_exam': sem2_exam,
            'final_avg': final_avg,
            'remarks': remarks
        })

    # Prepare the 'data' dictionary for the HTML
    data = {
        'student_name': student.full_name,
        'student_id': student.student_id,
        'level': format_student_school_level(student),
        'subjects': structured_subjects
    }
    
    return render_template('report_card.html', student=student, data=data)

@app.route('/download-report-card/<int:student_id>')
@login_required
def download_report_card(student_id):
    student = Student.query.get_or_404(student_id)
    
    staff_roles = {'admin', 'teacher', 'registrar', 'principal', 'vpa', 'vpi', 'dean', 'business'}
    user_role = (current_user.role or '').lower()
    if (user_role not in staff_roles and 
        student.user_id != current_user.id and 
        (user_role != 'parent' or student.parent_email != current_user.email)):
        abort(403)

    if user_role == 'teacher':
        teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
        if not teacher_profile or not teacher_can_access_student(teacher_profile, current_user, student):
            abort(403)
    
    if not student.tuition_cleared and user_role not in {'admin', 'teacher', 'registrar', 'principal'}:
        # flash('Report card access blocked due to outstanding tuition.', 'warning')
        # abort(403)
        pass
    
    # Generate PDF
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.styles import getSampleStyleSheet
    from io import BytesIO
    
    buffer = BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    styles = getSampleStyleSheet()
    elements = []
    
    # Title
    elements.append(Paragraph("STANDERD SCHOOL MANAGEMENT", styles['Heading1']))
    elements.append(Paragraph("REPORT CARD", styles['Heading2']))
    elements.append(Spacer(1, 12))
    
    # Student Info
    elements.append(Paragraph(f"Student Name: {student.full_name}", styles['Normal']))
    elements.append(Paragraph(f"Student ID: {student.student_id}", styles['Normal']))
    elements.append(Paragraph(f"Class: {student.klass.name if student.klass else 'N/A'}", styles['Normal']))
    elements.append(Spacer(1, 12))
    
    # Query all grades for the student
    grades = Grade.query.filter_by(student_id=student_id).order_by(Grade.period).all()

    # Table header
    data = [
        ['Period', 'Subject', 'CA Score', 'Exam Score', 'Total Score', 'Grade Letter', 'Remarks']
    ]

    # Loop through grades and build rows
    for grade in grades:
        grade_letter = SchoolEngine.get_grade_letter(grade.score or 0)
        remarks = SchoolEngine.get_remarks(grade.score or 0)

        data.append([
            grade.period,
            grade.subject or 'N/A',
            grade.ca_score or '',
            grade.exam_score or '',
            grade.score or '',
            grade_letter,
            remarks
        ])

    table = Table(data)
    table.setStyle(TableStyle([
        ('BACKGROUND', (0, 0), (-1, 0), colors.grey),
        ('TEXTCOLOR', (0, 0), (-1, 0), colors.whitesmoke),
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
        ('FONTNAME', (0, 0), (-1, 0), 'Helvetica-Bold'),
        ('FONTSIZE', (0, 0), (-1, 0), 14),
        ('BOTTOMPADDING', (0, 0), (-1, 0), 12),
        ('BACKGROUND', (0, 1), (-1, -1), colors.beige),
        ('GRID', (0, 0), (-1, -1), 1, colors.black),
    ]))
    elements.append(table)

    # GPA
    scores = [g.score for g in grades if g.score is not None]
    gpa = SchoolEngine.calculate_gpa(scores) if scores else 0.0
    elements.append(Spacer(1, 12))
    elements.append(Paragraph(f"GPA: {gpa:.2f}", styles['Normal']))
    
    doc.build(elements)
    buffer.seek(0)
    
    return send_file(buffer, as_attachment=True, download_name=f'report_card_{student.student_id}.pdf', mimetype='application/pdf')

@app.route('/student/dashboard', methods=['GET'])
@login_required
def student_dashboard():
    """
    Unified Student Dashboard Engine
    Handles historical continuity filtering, academic term matrix routing,
    financial contextual data loads, and localized assignment state tracking.
    """
    # Strict role verification barrier
    if (current_user.role or '').lower() != 'student':
        abort(403, description="Access restricted to student ledger signatures only.")

    try:
        # 1. Fetch core student identity profile
        student_profile = get_student_for_user(current_user)
        
        # 2. Extract full chronological record catalog for dropdown historical tracking
        past_years = AcademicYear.query.order_by(AcademicYear.name.desc()).all()

        # 3. Handle historical continuity selection logic
        selected_year_id = request.args.get('academic_year_id', type=int)
        
        if selected_year_id:
            display_year = db.session.get(AcademicYear, selected_year_id)
        else:
            # Fallback seamlessly to system primary active year
            display_year = AcademicYear.query.filter_by(is_active=True).first()
            # Emergency fallback rule if no terms are active in database
            if not display_year and past_years:
                display_year = past_years[0]

        if student_profile and not display_year and student_profile.academic_year_id:
            display_year = db.session.get(AcademicYear, student_profile.academic_year_id)

        if student_profile and display_year:
            ctx = compile_student_dashboard_context(
                student_profile,
                display_year,
                request.args,
            )
        else:
            ctx = compile_student_dashboard_context(None, display_year, request.args)

        active_tab = request.args.get('tab', 'grades')
        if active_tab not in ('grades', 'tasks', 'account'):
            active_tab = 'grades'
        if ctx.get('pending_tasks') and request.args.get('tab') is None:
            active_tab = 'tasks'

        return render_template(
            'dashboard_student.html',
            past_years=past_years,
            current_user=current_user,
            active_tab=active_tab,
            **ctx,
        )

    except Exception as e:
        logger.error(f"Critical error rendering student dashboard: {str(e)}", exc_info=True)
        abort(500, description="Internal Data Layer Synthesis Failure.")

@app.route('/update-tuition/<int:student_id>', methods=['POST'])
@login_required
def update_tuition(student_id):
    if (current_user.role or '').lower() != 'business':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    if student_id == 0:
        student_id = request.form.get('student_id', type=int)
    
    student = Student.query.get_or_404(student_id)
    cleared = request.form.get('tuition_cleared') == 'on'
    student.tuition_cleared = cleared
    db.session.commit()
    
    flash('Tuition status updated.', 'success')
    return redirect(url_for('dashboard'))

@app.route('/verify/transcript/<token>')
def verify_transcript(token):
    """Public transcript authenticity check — opened when the QR code is scanned."""
    student_pk = decode_transcript_verify_token(token)
    if not student_pk:
        return render_template(
            'verify_transcript.html',
            valid=False,
            error='This verification link is invalid or has been tampered with.',
        )

    student = db.session.get(Student, student_pk)
    if not student:
        return render_template(
            'verify_transcript.html',
            valid=False,
            error='No matching student record was found for this verification code.',
        )

    official_grades = official_grade_records(student.id)
    all_scores = [g.score for g in official_grades if g.score is not None]
    overall_gpa = SchoolEngine.calculate_gpa(all_scores) if all_scores else 0.0
    active_year = AcademicYear.query.filter_by(is_active=True).first()

    return render_template(
        'verify_transcript.html',
        valid=True,
        student=student,
        overall_gpa=overall_gpa,
        grade_count=len(all_scores),
        active_year=active_year,
        verified_at=datetime.now(timezone.utc),
    )


@app.route('/transcript/<int:student_id>')
@login_required
def transcript(student_id):
    student = Student.query.get_or_404(student_id)
    
    # Check permissions
    user_role = (current_user.role or '').lower()
    is_owner = student.user_id == current_user.id
    is_parent = user_role == 'parent' and student.parent_email == current_user.email
    if user_role not in ['admin', 'registrar'] and not is_owner and not is_parent:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    all_grades = official_grade_records(student_id)

    grades_by_year = {}
    for grade in all_grades:
        year_record = grade.academic_year
        if not year_record and grade.academic_year_id:
            year_record = db.session.get(AcademicYear, grade.academic_year_id)
        year_name = year_record.name if year_record else 'Unknown'
        grades_by_year.setdefault(year_name, []).append(grade)

    all_scores = [g.score for g in all_grades if g.score is not None]
    overall_gpa = SchoolEngine.calculate_gpa(all_scores) if all_scores else 0.0
    
    # QR must encode a URL so phone scanners open our verify page (not a web search).
    import qrcode
    import base64

    verify_token = generate_transcript_verify_token(student.id)
    verify_url = url_for('verify_transcript', token=verify_token, _external=True)
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(verify_url)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode('utf-8')
    
    return render_template(
        'transcript.html',
        student=student,
        grades_by_year=grades_by_year,
        overall_gpa=overall_gpa,
        qr_code=qr_code,
        verify_url=verify_url,
    )

@app.route("/about")
def about():
    categories = LeaderCategory.query.order_by(LeaderCategory.name.asc()).all()
    return render_template("about.html", categories=categories)


@app.route("/contact")
def contact():
    return render_template("contact.html")


@app.route('/api/stats')
@login_required
def api_stats():
    import random
    # Mock system stats
    cpu = random.randint(10, 90)
    memory = random.randint(20, 80)
    # db_wave: perhaps student count or recent activity
    db_wave = Student.query.count() + random.randint(-10, 10)
    return jsonify({'cpu': cpu, 'memory': memory, 'db_wave': db_wave})


# --------------------------------------------------------------
# ADMIN: Manage Events
# --------------------------------------------------------------


def _require_admin():
    if not current_user.is_authenticated or normalize_role(current_user) != "admin":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


def _require_communications_manager():
    """Admin, Principal, and VPA can manage events and announcements."""
    if not current_user.is_authenticated:
        flash("Please log in to continue.", "danger")
        return redirect(url_for("login"))
    if normalize_role(current_user) not in COMMUNICATIONS_MANAGER_ROLES:
        flash("Communications management access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


def _require_school_media_manager():
    """Admin, Principal, VPA, and Registrar can manage school media."""
    if not current_user.is_authenticated:
        flash("Please log in to continue.", "danger")
        return redirect(url_for("login"))
    if normalize_role(current_user) not in SCHOOL_MEDIA_MANAGER_ROLES:
        flash("School media management access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


def _registrar_media_restricted():
    return normalize_role(current_user) == "registrar"


def _school_media_category_choices():
    all_choices = [
        ("general", "General Update"),
        ("advertisement", "Advertisement / Promo Video"),
        ("gallery", "School Gallery"),
        ("entrance", "Entrance Exam Notice (documents only)"),
        ("info_sheet", "Academic Year Info Sheet (documents only)"),
    ]
    if _registrar_media_restricted():
        return [choice for choice in all_choices if choice[0] in REGISTRAR_MEDIA_CATEGORIES]
    return all_choices


def _user_can_manage_media_item(item):
    if not _registrar_media_restricted():
        return True
    return item.category in REGISTRAR_MEDIA_CATEGORIES


def _school_media_query_for_user():
    query = SchoolMedia.query
    if _registrar_media_restricted():
        query = query.filter(SchoolMedia.category.in_(REGISTRAR_MEDIA_CATEGORIES))
    return query.order_by(SchoolMedia.created_at.desc())


def _school_media_upload_dir():
    upload_dir = os.path.join(current_app.root_path, "static", "uploads", "school_media")
    os.makedirs(upload_dir, exist_ok=True)
    return upload_dir


def _save_school_media_file(upload_file):
    filename = secure_filename(upload_file.filename)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    stored_name = f"{timestamp}_{filename}"
    upload_dir = _school_media_upload_dir()
    full_path = os.path.join(upload_dir, stored_name)
    upload_file.save(full_path)
    return os.path.join("uploads", "school_media", stored_name).replace("\\", "/")


def _school_media_allowed_file(filename, media_type):
    if not filename or "." not in filename:
        return False
    ext = filename.rsplit(".", 1)[-1].lower()
    allowed = {
        "photo": {"jpg", "jpeg", "png", "webp", "gif"},
        "video": {"mp4", "webm", "mov"},
        "document": {"pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt"},
    }
    return ext in allowed.get(media_type, set())


def _youtube_embed_url(url):
    if not url:
        return None
    url = url.strip()
    if "youtu.be/" in url:
        video_id = url.rsplit("/", 1)[-1].split("?")[0]
        return f"https://www.youtube.com/embed/{video_id}"
    if "watch?v=" in url:
        video_id = url.split("watch?v=", 1)[-1].split("&")[0]
        return f"https://www.youtube.com/embed/{video_id}"
    if "youtube.com/embed/" in url:
        return url
    return None


def _require_analytics_access():
    """Restrict analytics APIs to staff roles."""
    if normalize_role(current_user) not in ('admin', 'registrar', 'principal', 'business'):
        abort(403)


def _require_leader_manager():
    """About-page leadership profiles — admin and principal."""
    if not current_user.is_authenticated:
        flash("Please log in to continue.", "danger")
        return redirect(url_for("dashboard"))
    if (current_user.role or "").lower() not in {"admin", "principal"}:
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


@app.route("/admin/events")
@login_required
def admin_events_list():
    redirect_resp = _require_communications_manager()
    if redirect_resp:
        return redirect_resp

    events = Event.query.order_by(Event.date.desc()).all()
    return render_template("admin/events/list.html", events=events)


@app.route("/admin/events/create", methods=["GET", "POST"])
@login_required
def admin_events_create():
    redirect_resp = _require_communications_manager()
    if redirect_resp:
        return redirect_resp

    form = EventForm()
    if form.validate_on_submit():
        event = Event(
            title=form.title.data.strip(),
            description=form.description.data.strip(),
            location=form.location.data.strip() if form.location.data else None,
            date=form.date.data,
            event_type=form.event_type.data or "general",
        )
        db.session.add(event)
        db.session.commit()
        flash("Event created successfully and is now visible on the homepage.", "success")
        return redirect(url_for("index") + "#upcoming-schedule")

    return render_template("admin/events/create.html", form=form)


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def admin_events_edit(event_id):
    redirect_resp = _require_communications_manager()
    if redirect_resp:
        return redirect_resp

    event = Event.query.get_or_404(event_id)
    form = EventForm(obj=event)
    if form.validate_on_submit():
        event.title = form.title.data.strip()
        event.description = form.description.data.strip()
        event.location = form.location.data.strip() if form.location.data else None
        event.date = form.date.data
        event.event_type = form.event_type.data or "general"
        db.session.commit()
        flash("Event updated successfully.", "success")
        return redirect(url_for("admin_events_list"))

    return render_template("admin/events/edit.html", form=form, event=event)

@app.route("/admin/events/<int:event_id>/delete", methods=["GET", "POST"])
@login_required
def admin_events_delete(event_id):
    redirect_resp = _require_communications_manager()
    if redirect_resp:
        return redirect_resp

    event = Event.query.get_or_404(event_id)
    form = ConfirmDeleteForm()
    if form.validate_on_submit():
        db.session.delete(event)
        db.session.commit()
        flash("Event deleted.", "success")
        return redirect(url_for("admin_events_list"))

    return render_template("admin/events/delete.html", form=form, event=event)


# --------------------------------------------------------------
# PUBLIC: Events
# --------------------------------------------------


@app.route('/events')
def events_list():
    events = Event.query.order_by(Event.date.asc()).all()
    return render_template('events/list.html', events=events)


def _populate_school_media_form(form):
    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    form.academic_year.choices = [(0, "-- Any / All Years --")] + [(y.id, y.name) for y in years]
    form.category.choices = _school_media_category_choices()
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if request.method == "GET":
        if active_year:
            form.academic_year.default = active_year.id
        if not _registrar_media_restricted():
            form.category.default = "gallery"
        form.process()


def _validate_school_media_submission(form):
    category = form.category.data or "general"
    media_type = form.media_type.data or "document"

    if _registrar_media_restricted() and category not in REGISTRAR_MEDIA_CATEGORIES:
        flash("Registrars can only post Entrance Exam and Academic Year Info Sheet items.", "danger")
        return False

    if category in DOCUMENT_ONLY_MEDIA_CATEGORIES and media_type != "document":
        flash(
            "Entrance Exam and Info Sheet categories are for downloadable documents only. "
            "Use Advertisement, Gallery, or General for photos and videos.",
            "danger",
        )
        return False

    if _registrar_media_restricted() and media_type != "document":
        flash(
            "Registrars can only upload documents for entrance notices and info sheets. "
            "Ask Principal or VPA to publish advertisement videos.",
            "danger",
        )
        return False

    return True


@app.route("/school-media")
def school_media_gallery():
    category = (request.args.get("category") or "").strip()
    media_type = (request.args.get("type") or "").strip()
    year_id = request.args.get("year_id", type=int)

    query = SchoolMedia.query.filter_by(is_published=True)
    if category:
        query = query.filter_by(category=category)
    if media_type:
        query = query.filter_by(media_type=media_type)
    if year_id:
        query = query.filter_by(academic_year_id=year_id)

    items = query.order_by(SchoolMedia.created_at.desc()).all()
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    return render_template(
        "school_media/gallery.html",
        items=items,
        active_year=active_year,
        years=years,
        selected_category=category,
        selected_type=media_type,
        selected_year_id=year_id,
        youtube_embed_url=_youtube_embed_url,
    )


@app.route("/school-media/manage")
@login_required
def school_media_manage():
    redirect_resp = _require_school_media_manager()
    if redirect_resp:
        return redirect_resp

    items = _school_media_query_for_user().all()
    return render_template("school_media/manage.html", items=items)


@app.route("/school-media/create", methods=["GET", "POST"])
@login_required
def school_media_create():
    redirect_resp = _require_school_media_manager()
    if redirect_resp:
        return redirect_resp

    form = SchoolMediaForm()
    _populate_school_media_form(form)

    if form.validate_on_submit():
        if not _validate_school_media_submission(form):
            return render_template("school_media/form.html", form=form, item=None)

        media_type = form.media_type.data
        has_file = form.media_file.data and getattr(form.media_file.data, "filename", None)
        external_url = (form.external_url.data or "").strip() or None

        if media_type == "video" and not has_file and not external_url:
            flash("Upload a video file or provide a YouTube/Vimeo link.", "danger")
            return render_template("school_media/form.html", form=form, item=None)

        if media_type in {"photo", "document"} and not has_file:
            flash("Please upload a file for this media type.", "danger")
            return render_template("school_media/form.html", form=form, item=None)

        file_path = None
        if has_file:
            if not _school_media_allowed_file(form.media_file.data.filename, media_type):
                flash("That file type is not allowed for the selected media type.", "danger")
                return render_template("school_media/form.html", form=form, item=None)
            file_path = _save_school_media_file(form.media_file.data)

        item = SchoolMedia(
            title=form.title.data.strip(),
            description=(form.description.data or "").strip() or None,
            media_type=media_type,
            category=form.category.data or "general",
            file_path=file_path,
            external_url=external_url,
            academic_year_id=form.academic_year.data or None,
            is_published=bool(form.is_published.data),
            author_id=current_user.id,
        )
        db.session.add(item)
        db.session.commit()
        flash("School media published successfully.", "success")
        return redirect(url_for("school_media_manage"))

    return render_template("school_media/form.html", form=form, item=None)


@app.route("/school-media/<int:item_id>/edit", methods=["GET", "POST"])
@login_required
def school_media_edit(item_id):
    redirect_resp = _require_school_media_manager()
    if redirect_resp:
        return redirect_resp

    item = SchoolMedia.query.get_or_404(item_id)
    if not _user_can_manage_media_item(item):
        flash("You are not authorized to edit this media item.", "danger")
        return redirect(url_for("school_media_manage"))

    form = SchoolMediaForm(obj=item)
    _populate_school_media_form(form)
    if request.method == "GET":
        form.academic_year.data = item.academic_year_id or 0

    if form.validate_on_submit():
        if not _validate_school_media_submission(form):
            return render_template("school_media/form.html", form=form, item=item)

        media_type = form.media_type.data
        has_file = form.media_file.data and getattr(form.media_file.data, "filename", None)
        external_url = (form.external_url.data or "").strip() or None

        if media_type == "video" and not has_file and not external_url and not item.file_path:
            flash("Upload a video file or provide a video link.", "danger")
            return render_template("school_media/form.html", form=form, item=item)

        if media_type in {"photo", "document"} and not has_file and not item.file_path:
            flash("Please upload a file for this media type.", "danger")
            return render_template("school_media/form.html", form=form, item=item)

        if has_file:
            if not _school_media_allowed_file(form.media_file.data.filename, media_type):
                flash("That file type is not allowed for the selected media type.", "danger")
                return render_template("school_media/form.html", form=form, item=item)
            item.file_path = _save_school_media_file(form.media_file.data)

        item.title = form.title.data.strip()
        item.description = (form.description.data or "").strip() or None
        item.media_type = media_type
        item.category = form.category.data or "general"
        item.external_url = external_url
        item.academic_year_id = form.academic_year.data or None
        item.is_published = bool(form.is_published.data)
        db.session.commit()
        flash("School media updated successfully.", "success")
        return redirect(url_for("school_media_manage"))

    return render_template("school_media/form.html", form=form, item=item)


@app.route("/school-media/<int:item_id>/delete", methods=["GET", "POST"])
@login_required
def school_media_delete(item_id):
    redirect_resp = _require_school_media_manager()
    if redirect_resp:
        return redirect_resp

    item = SchoolMedia.query.get_or_404(item_id)
    if not _user_can_manage_media_item(item):
        flash("You are not authorized to delete this media item.", "danger")
        return redirect(url_for("school_media_manage"))
    form = ConfirmDeleteForm()
    if form.validate_on_submit():
        db.session.delete(item)
        db.session.commit()
        flash("School media removed.", "success")
        return redirect(url_for("school_media_manage"))

    return render_template("school_media/delete.html", form=form, item=item)


@app.route("/school-media/<int:item_id>/download")
def school_media_download(item_id):
    item = SchoolMedia.query.get_or_404(item_id)
    if not item.is_published:
        if not current_user.is_authenticated or normalize_role(current_user) not in SCHOOL_MEDIA_MANAGER_ROLES:
            flash("This file is not available.", "danger")
            return redirect(url_for("school_media_gallery"))

    if not item.file_path:
        flash("No file attached to this item.", "warning")
        return redirect(url_for("school_media_gallery"))

    rel_path = item.static_file_path
    if not rel_path:
        flash("File not found.", "danger")
        return redirect(url_for("school_media_gallery"))

    full_path = os.path.join(current_app.root_path, "static", rel_path.replace("/", os.sep))
    if not os.path.isfile(full_path):
        flash("File not found on disk.", "danger")
        return redirect(url_for("school_media_gallery"))

    directory = os.path.dirname(full_path)
    filename = os.path.basename(full_path)
    return send_from_directory(directory, filename, as_attachment=True)


# --------------------------------------------------------------
# ADMIN: Manage Leaders
# --------------------------------------------------------------

@app.route('/admin/leaders')
@login_required
def manage_leaders():
    redirect_resp = _require_leader_manager()
    if redirect_resp:
        return redirect_resp

    leaders = Leader.query.all()
    return render_template('admin/manage_leaders.html', leaders=leaders)

@app.route('/admin/leaders/add', methods=['GET', 'POST'])
@login_required
def add_leader():
    redirect_resp = _require_leader_manager()
    if redirect_resp:
        return redirect_resp

    form = LeaderForm()
    form.category.choices = [(c.id, c.name) for c in LeaderCategory.query.order_by(LeaderCategory.name.asc())]

    if form.validate_on_submit():
        leader = Leader(
            name=form.name.data,
            role=form.role.data,
            bio=form.bio.data,
            contact=form.contact.data,
            category_id=form.category.data if form.category.data else None
        )

        if form.photo.data:
            photo_file = form.photo.data
            filename = secure_filename(photo_file.filename)
            photo_path = os.path.join('static/uploads/leaders', filename)
            os.makedirs(os.path.dirname(photo_path), exist_ok=True)
            photo_file.save(photo_path)
            leader.photo = photo_path

        db.session.add(leader)
        db.session.commit()
        flash('Leader added successfully!', 'success')
        return redirect(url_for('manage_leaders'))

    return render_template('admin/add_leader.html', form=form)


@app.route('/admin/edit_leader/<int:leader_id>', methods=['GET', 'POST'])
@login_required
def edit_leader(leader_id):
    redirect_resp = _require_leader_manager()
    if redirect_resp:
        return redirect_resp

    leader = Leader.query.get_or_404(leader_id)
    form = LeaderForm(obj=leader)
    form.category.choices = [(c.id, c.name) for c in LeaderCategory.query.order_by(LeaderCategory.name.asc())]

    if form.validate_on_submit():
        leader.name = form.name.data
        leader.role = form.role.data
        leader.bio = form.bio.data
        leader.contact = form.contact.data
        leader.category_id = form.category.data if form.category.data else None

        if form.photo.data:
            photo_file = form.photo.data
            filename = secure_filename(photo_file.filename)
            photo_path = os.path.join('static/uploads/leaders', filename)
            os.makedirs(os.path.dirname(photo_path), exist_ok=True)
            photo_file.save(photo_path)
            leader.photo = photo_path

        db.session.commit()
        flash('Leader updated successfully!', 'success')
        return redirect(url_for('manage_leaders'))

    return render_template('admin/edit_leader.html', form=form, leader=leader)


@app.route('/admin/delete_leader/<int:leader_id>', methods=['POST'])
@login_required
def delete_leader(leader_id):
    redirect_resp = _require_leader_manager()
    if redirect_resp:
        return redirect_resp

    leader = Leader.query.get_or_404(leader_id)
    db.session.delete(leader)
    db.session.commit()
    flash('Leader deleted successfully!', 'danger')
    return redirect(url_for('manage_leaders'))


@app.route('/grades/add', methods=['POST'])
@login_required
def add_grade():
    # 1. Access Control: Restrict to teachers only
    if normalize_role(current_user) != 'teacher':
        flash("Unauthorized.", "danger")
        return redirect(url_for('dashboard'))

    # 2. Strict Session Safety Check: Verify an active academic year exists
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash("No active academic year found. Please contact the administrator.", "danger")
        return redirect(url_for('dashboard'))

    # 3. Extract and parse incoming form data safely
    student_id = request.form.get("student_id", type=int)
    subject_name = (request.form.get("subject_name") or request.form.get("subject") or "").strip()
    period_str = request.form.get("period")
    marking_period = request.form.get("marking_period", type=int)
    if marking_period is None:
        marking_period = request.form.get("period", type=int)
    activity_type = request.form.get("activity_type")
    score = request.form.get("score", type=float)
    submitted = bool(request.form.get("submitted"))

    # Extract continuous assessment and exam components
    ca_score = request.form.get("ca_score", type=float)
    exam_score = request.form.get("exam_score", type=float)

    # 4. Input Validation
    if not student_id or not subject_name or marking_period is None:
        flash("Student, subject, and marking period are required.", "danger")
        return redirect(url_for('teacher_dashboard'))

    if marking_period not in range(1, 9):
        flash("Invalid marking period selected.", "danger")
        return redirect(url_for('teacher_dashboard'))

    if not activity_type:
        activity_type = 'Semester Exam' if marking_period in (7, 8) else 'Period Assessment'

    student = db.session.get(Student, student_id)
    if not student:
        flash("Student not found.", "danger")
        return redirect(url_for('teacher_dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('teacher_dashboard'))

    if not teacher_can_access_student(teacher_profile, current_user, student):
        flash("You may only enter grades for students in your assigned classes.", "danger")
        return redirect(url_for('teacher_dashboard'))

    student_class_id = get_student_class_id(student)
    allowed_subjects = get_teacher_subjects_for_class(teacher_profile, student_class_id)
    if subject_name not in allowed_subjects:
        flash("You are not assigned to teach that subject for this student's class.", "danger")
        return redirect(url_for('teacher_dashboard'))

    # 5. MoE Standard Calculations & Constraints Validation via SchoolEngine
    if marking_period in range(1, 7):
        if ca_score is None and exam_score is None and score is not None:
            ca_score = 0.0
            exam_score = score
        if ca_score is None or exam_score is None:
            flash("CA and Exam scores are required for period grades.", "danger")
            return redirect(url_for('teacher_dashboard'))
        total = SchoolEngine.calculate_period_total(ca_score, exam_score)
        if total is None:
            flash("Invalid scores! CA must be ≤ 60 and Exam ≤ 40 (MoE Standard).", "danger")
            return redirect(url_for('teacher_dashboard'))
        score = total
    else:
        exam_only = exam_score if exam_score is not None else score
        if exam_only is None:
            flash("Semester exam score is required.", "danger")
            return redirect(url_for('teacher_dashboard'))
        if exam_only < 0 or exam_only > 100:
            flash("Semester exam score must be between 0 and 100.", "danger")
            return redirect(url_for('teacher_dashboard'))
        ca_score = 0.0
        exam_score = exam_only
        score = exam_only

    grade = find_grade_record(
        student_id,
        subject_name,
        marking_period,
        class_id=student_class_id,
        academic_year_id=active_year.id,
    )
    if not grade:
        grade = Grade(
            student_id=student_id,
            teacher_id=teacher_profile.id,
            class_id=student_class_id,
            academic_year_id=active_year.id,
            subject_name=subject_name,
            subject=subject_name,
        )
        db.session.add(grade)

    if grade and grade.is_finalized:
        flash('This grade is finalized and cannot be changed.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    grade.teacher_id = teacher_profile.id
    grade.class_id = student_class_id
    grade.academic_year_id = active_year.id
    grade.subject_name = subject_name
    grade.subject = subject_name
    grade.period = marking_period
    grade.marking_period = marking_period
    grade.activity_type = activity_type
    grade.ca_score = ca_score if ca_score is not None else 0.0
    grade.exam_score = exam_score if exam_score is not None else 0.0
    grade.score = score
    grade.remarks = SchoolEngine.get_remarks(score)
    grade.submitted = submitted

    if 1 <= marking_period <= 6:
        setattr(grade, f"p{marking_period}", int(round(score)))

    db.session.commit()

    flash("Grade saved successfully.", "success")
    return_class_id = request.form.get('grade_class_id', type=int)
    if return_class_id:
        return redirect(url_for('teacher_dashboard', tab='grades', grade_class_id=return_class_id))
    return redirect(url_for('teacher_dashboard', tab='grades'))

@app.route('/teacher/assign-activity', methods=['POST'])
@login_required
def assign_activity():
    if (current_user.role or '').strip().lower() != 'teacher':
        flash('Only teachers can assign activities.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    klass_id = request.form.get('klass_id', type=int)
    title = request.form.get('title', '').strip()
    description = request.form.get('description', '').strip()
    submission_mode = (request.form.get('submission_mode') or request.form.get('activity_type') or '').strip()
    subject_name = (request.form.get('subject_name') or request.form.get('subject') or '').strip()
    marking_period = request.form.get('marking_period', type=int) or request.form.get('period', type=int) or 1
    evaluation_type = (request.form.get('evaluation_type') or 'Assignment').strip()

    if not klass_id or not title or not submission_mode or not subject_name:
        flash('Class, subject, title, and submission type are required.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    if submission_mode not in {'file_upload', 'text_entry', 'in_class'}:
        flash('Choose how students should submit this activity.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    if evaluation_type not in MOE_ACTIVITY_TYPES:
        evaluation_type = 'Assignment'
    if marking_period not in range(1, 7):
        marking_period = 1

    if not teacher_can_access_class(teacher_profile, current_user, klass_id):
        flash('You may only assign activities to your own classes.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    allowed_subjects = get_assignable_subjects_for_class(teacher_profile, current_user, klass_id)
    if not allowed_subjects:
        flash('No subjects are available for this class. Ask the principal to set up class subjects.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))
    if subject_name not in allowed_subjects:
        flash(f'Choose a subject you teach in this class: {", ".join(allowed_subjects)}.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash('No active academic year found.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    klass = Class.query.get_or_404(klass_id)

    task_file_name = None
    if 'task_file' in request.files:
        task_file = request.files['task_file']
        if task_file and task_file.filename:
            upload_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'activities')
            os.makedirs(upload_dir, exist_ok=True)
            file_name = f"activity_{klass_id}_{int(datetime.now(timezone.utc).timestamp())}_{secure_filename(task_file.filename)}"
            task_file.save(os.path.join(upload_dir, file_name))
            task_file_name = file_name

    try:
        assessment = Assessment(
            title=title,
            description=description,
            activity_type=evaluation_type,
            submission_mode=submission_mode,
            subject_name=subject_name,
            marking_period=marking_period,
            academic_year_id=active_year.id,
            teacher_id=teacher_profile.id,
            klass_id=klass_id,
            file_name=task_file_name,
            max_score=100.0,
        )
        db.session.add(assessment)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.error('Failed to assign classroom activity: %s', exc, exc_info=True)
        flash('Could not save the activity. Please try again or contact support.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    flash(f'"{title}" assigned to {klass.name} — {subject_name} (Period {marking_period}).', 'success')
    return redirect(url_for('teacher_dashboard', tab='activities'))


@app.route('/teacher/dashboard', methods=['GET'])
@login_required
def teacher_dashboard():
    """
    Assembles data for the comprehensive cyber-themed instructor panel,
    encompassing student rosters, active terms, and system historical ledgers.
    """
    # Strict role verification barrier
    if (current_user.role or '').strip().lower() != 'teacher':
        logger.warning(f"Unauthorized dashboard access attempt by User ID: {current_user.id}")
        abort(403, description="Access restricted to authorized faculty members only.")

    try:
        # Retrieve the currently active academic term configuration
        active_year = AcademicYear.query.filter_by(is_active=True).first()
        if not active_year:
            active_year = type('obj', (object,), {
                'name': 'No Active Year',
                'start_date': 'N/A',
                'end_date': 'N/A'
            })()

        # Fetch active faculty roster for display contexts that need it
        teachers_list = Teacher.query.filter_by(status='ACTIVE').order_by(Teacher.first_name.asc(), Teacher.last_name.asc()).all()

        # Get or create teacher profile for current user
        teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
        
        if not teacher_profile:
            logger.warning(f"Auto-creating teacher profile for user {current_user.id}")
            try:
                name_parts = (current_user.full_name or '').strip().split(None, 1)
                first_name = name_parts[0].strip() if name_parts else current_user.full_name
                last_name = name_parts[1].strip() if len(name_parts) > 1 else ''
                
                teacher_profile = Teacher(
                    user_id=current_user.id,
                    first_name=first_name or 'Unknown',
                    last_name=last_name or current_user.full_name,
                    status='ACTIVE'
                )
                db.session.add(teacher_profile)
                db.session.commit()
            except Exception as e:
                logger.error(f"Failed to auto-create teacher profile: {e}")
                db.session.rollback()

        dashboard_ctx = build_teacher_dashboard_context(teacher_profile, current_user)
        class_cards = dashboard_ctx['class_cards']
        grade_class_id = request.args.get('grade_class_id', type=int)
        valid_class_ids = {card['id'] for card in class_cards}
        if class_cards:
            if grade_class_id not in valid_class_ids:
                grade_class_id = class_cards[0]['id']
        else:
            grade_class_id = None

        selected_grade_class = next(
            (card for card in class_cards if card['id'] == grade_class_id),
            None,
        )
        grade_entry_students = []
        grade_entry_subjects = []
        if grade_class_id:
            grade_entry_students = (
                Student.query.filter_by(klass_id=grade_class_id)
                .order_by(Student.last_name.asc(), Student.first_name.asc())
                .all()
            )
            grade_entry_subjects = get_teacher_subjects_for_class(teacher_profile, grade_class_id)

        class_subjects_by_id = {
            str(card['id']): get_assignable_subjects_for_class(
                teacher_profile, current_user, card['id']
            )
            for card in class_cards
        }

        active_tab = request.args.get('tab', 'grades')
        if active_tab not in ('classes', 'grades', 'activities'):
            active_tab = 'grades'
        if dashboard_ctx['pending_grading_count'] and request.args.get('tab') is None:
            active_tab = 'activities'

        return render_template(
            'dashboard_teacher.html',
            students=dashboard_ctx['students'],
            teachers=teachers_list,
            active_year=active_year,
            teaching_classes=dashboard_ctx['teaching_classes'],
            class_cards=class_cards,
            sponsored_classes=dashboard_ctx['sponsored_classes'],
            grades=dashboard_ctx['grades'],
            activities=dashboard_ctx['activities'],
            recent_submissions=dashboard_ctx['recent_submissions'],
            ai_scan_queue=dashboard_ctx['ai_scan_queue'],
            assigned_subjects=dashboard_ctx['assigned_subjects'],
            class_subjects_by_id=class_subjects_by_id,
            grading_periods=dashboard_ctx['grading_periods'],
            grade_class_id=grade_class_id,
            selected_grade_class=selected_grade_class,
            grade_entry_students=grade_entry_students,
            grade_entry_subjects=grade_entry_subjects,
            pending_grading_count=dashboard_ctx['pending_grading_count'],
            activities_with_pending=dashboard_ctx['activities_with_pending'],
            active_tab=active_tab,
        )
    
    except Exception as e:
        logger.error(f"Teacher dashboard error: {str(e)}", exc_info=True)
        abort(500, description="Internal Data Layer Synthesis Failure.")    
    return render_template(
        'dashboard_teacher.html',
        students=[],
        teachers=[],
        active_year=None,
        teaching_classes=[],
        class_cards=[],
        sponsored_classes=[],
        grades=[],
        activities=[],
        recent_submissions=[],
        ai_scan_queue=[],
        assigned_subjects=[],
        class_subjects_by_id={},
        grading_periods=MOE_GRADING_PERIODS,
        grade_class_id=None,
        selected_grade_class=None,
        grade_entry_students=[],
        grade_entry_subjects=[],
        pending_grading_count=0,
        activities_with_pending=0,
        active_tab='grades',
    )


@app.route('/teacher/class/<int:class_id>')
@login_required
def teacher_class_folder(class_id):
    """Class folder view: roster and quick links for an assigned class."""
    if (current_user.role or '').strip().lower() != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_can_access_class(teacher_profile, current_user, class_id):
        flash('You are not assigned to this class.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    klass = db.session.get(Class, class_id)
    if not klass:
        flash('Class not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    subjects = [
        a.subject_name
        for a in ClassSubjectTeacher.query.filter_by(
            teacher_id=teacher_profile.id, class_id=class_id
        ).all()
    ]
    students = (
        Student.query.filter_by(klass_id=class_id)
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )
    active_year = AcademicYear.query.filter_by(is_active=True).first()

    is_sponsor = teacher_is_class_sponsor(teacher_profile, current_user, class_id)

    return render_template(
        'teacher_class_folder.html',
        klass=klass,
        students=students,
        subjects=sorted(set(subjects)),
        active_year=active_year,
        is_sponsor=is_sponsor,
    )


@app.route('/teacher/class/<int:class_id>/grading', methods=['GET'])
@login_required
def class_grading_hub(class_id):
    """Unified class grading: activities + MoE period sheet in one place."""
    if normalize_role(current_user) != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    klass = Class.query.get_or_404(class_id)
    if not teacher or not teacher_can_access_class(teacher, current_user, class_id):
        flash('You are not assigned to this class.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash('No active academic year found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    subjects = get_assignable_subjects_for_class(teacher, current_user, class_id)
    selected_subject = (request.args.get('subject') or '').strip()
    if selected_subject not in subjects:
        selected_subject = subjects[0] if subjects else ''

    selected_period = request.args.get('period', 1, type=int)
    if selected_period not in range(1, 9):
        selected_period = 1

    hub_tab = request.args.get('hub_tab', 'activities')
    if hub_tab not in ('activities', 'moe'):
        hub_tab = 'activities'

    students = (
        Student.query.filter_by(klass_id=klass.id)
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )
    student_ids = {s.id for s in students}

    grade_rows = {}
    if selected_subject:
        existing_grades = Grade.query.filter_by(
            class_id=class_id,
            subject=selected_subject,
            academic_year_id=active_year.id,
        ).all()
        for grade in existing_grades:
            period_num = grade.marking_period or normalize_grade_period(grade.period)
            if period_num == selected_period and grade.student_id in student_ids:
                grade_rows[grade.student_id] = grade

    class_activities = []
    if selected_subject:
        class_activities = (
            Assessment.query.filter_by(klass_id=class_id, subject_name=selected_subject)
            .filter(Assessment.marking_period == selected_period)
            .order_by(Assessment.id.desc())
            .all()
        )

    activity_cards = []
    for act in class_activities:
        subs = Submission.query.filter_by(assessment_id=act.id).all()
        graded = sum(1 for s in subs if s.is_graded)
        pending = sum(
            1 for s in subs
            if not s.is_graded and (s.file_path or s.submission_text)
        )
        activity_cards.append({
            'activity': act,
            'submission_count': len(subs),
            'graded_count': graded,
            'pending_count': pending,
            'roster_size': len(students),
        })

    period_label = dict(MOE_GRADING_PERIODS).get(selected_period, f'Period {selected_period}')
    is_semester_exam = selected_period in (7, 8)

    return render_template(
        'teacher_class_grading_hub.html',
        klass=klass,
        students=students,
        grade_rows=grade_rows,
        active_year=active_year,
        subjects=subjects,
        selected_subject=selected_subject,
        selected_period=selected_period,
        period_label=period_label,
        grading_periods=MOE_GRADING_PERIODS,
        is_semester_exam=is_semester_exam,
        teacher_name=teacher.full_name,
        activity_cards=activity_cards,
        hub_tab=hub_tab,
    )


@app.route('/teacher/sponsor/<int:class_id>', methods=['GET'])
@login_required
def sponsor_class_hub(class_id):
    """Class Sponsor & Form Teacher command center."""
    if normalize_role(current_user) != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_is_class_sponsor(teacher_profile, current_user, class_id):
        flash('This command center is only for assigned class sponsors and form teachers.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    klass = db.session.get(Class, class_id)
    if not klass:
        flash('Class not found.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    attendance_date = request.args.get('date') or date.today().strftime('%Y-%m-%d')
    ctx = build_sponsor_hub_context(
        teacher_profile, current_user, klass, active_year, attendance_date=attendance_date
    )
    return render_template('sponsor_class_hub.html', **ctx)


@app.route('/teacher/sponsor/<int:class_id>/attendance', methods=['POST'])
@login_required
def sponsor_save_attendance(class_id):
    if normalize_role(current_user) != 'teacher':
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_is_class_sponsor(teacher_profile, current_user, class_id):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    attendance_date = (request.form.get('attendance_date') or date.today().strftime('%Y-%m-%d')).strip()
    students = Student.query.filter_by(klass_id=class_id).all()
    saved = 0
    for student in students:
        status = (request.form.get(f'attendance_{student.id}') or 'present').strip().lower()
        if status not in {'present', 'absent', 'late'}:
            status = 'present'
        notes = (request.form.get(f'notes_{student.id}') or '').strip() or None
        row = Attendance.query.filter_by(student_id=student.id, date=attendance_date).first()
        if row:
            row.status = status
            row.notes = notes
        else:
            db.session.add(Attendance(
                student_id=student.id,
                date=attendance_date,
                status=status,
                notes=notes,
            ))
        saved += 1
    try:
        db.session.commit()
        flash(f'Attendance saved for {saved} students on {attendance_date}.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.error('Sponsor attendance save failed: %s', exc, exc_info=True)
        flash('Could not save attendance.', 'danger')
    return _sponsor_hub_redirect(class_id, date=attendance_date)


@app.route('/teacher/sponsor/<int:class_id>/incident', methods=['POST'])
@login_required
def sponsor_log_incident(class_id):
    if normalize_role(current_user) != 'teacher':
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_is_class_sponsor(teacher_profile, current_user, class_id):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    student_id = request.form.get('student_id', type=int)
    offense = (request.form.get('offense') or '').strip()
    action_taken = (request.form.get('action_taken') or '').strip() or 'Referred to Dean of Students'
    notes = (request.form.get('notes') or '').strip()

    if not student_id or not offense:
        flash('Student and offense description are required.', 'danger')
        return _sponsor_hub_redirect(class_id)

    student = db.session.get(Student, student_id)
    if not student or student.klass_id != class_id:
        flash('Student not found in this class.', 'danger')
        return _sponsor_hub_redirect(class_id)

    db.session.add(Discipline(
        student_id=student_id,
        offense=offense,
        action_taken=action_taken,
        notes=notes or None,
        logged_by_id=current_user.id,
    ))
    try:
        db.session.commit()
        flash(f'Conduct incident logged for {student.full_name}. Dean has been notified via the record.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.error('Sponsor incident log failed: %s', exc, exc_info=True)
        flash('Could not save incident.', 'danger')
    return _sponsor_hub_redirect(class_id)


@app.route('/teacher/sponsor/<int:class_id>/welfare-note', methods=['POST'])
@login_required
def sponsor_welfare_note(class_id):
    if normalize_role(current_user) != 'teacher':
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_is_class_sponsor(teacher_profile, current_user, class_id):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    student_id = request.form.get('student_id', type=int) or None
    note_type = (request.form.get('note_type') or 'welfare').strip()
    content = (request.form.get('content') or '').strip()

    if not content:
        flash('Welfare note content is required.', 'danger')
        return _sponsor_hub_redirect(class_id)

    if student_id:
        student = db.session.get(Student, student_id)
        if not student or student.klass_id != class_id:
            flash('Invalid student for this class.', 'danger')
            return _sponsor_hub_redirect(class_id)

    db.session.add(SponsorWelfareNote(
        class_id=class_id,
        student_id=student_id,
        teacher_id=teacher_profile.id,
        note_type=note_type,
        content=content,
    ))
    try:
        db.session.commit()
        flash('Welfare note recorded.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.error('Welfare note save failed: %s', exc, exc_info=True)
        flash('Could not save welfare note.', 'danger')
    return _sponsor_hub_redirect(class_id)


@app.route('/teacher/sponsor/<int:class_id>/announce', methods=['POST'])
@login_required
def sponsor_class_announce(class_id):
    if normalize_role(current_user) != 'teacher':
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_is_class_sponsor(teacher_profile, current_user, class_id):
        flash('Unauthorized.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    title = (request.form.get('title') or '').strip()
    content = (request.form.get('content') or '').strip()
    audience = (request.form.get('audience') or 'students').strip()

    if not title or not content:
        flash('Announcement title and message are required.', 'danger')
        return _sponsor_hub_redirect(class_id)

    db.session.add(ClassAnnouncement(
        class_id=class_id,
        author_id=current_user.id,
        title=title,
        content=content,
        audience=audience if audience in {'students', 'parents', 'both'} else 'students',
    ))
    try:
        db.session.commit()
        flash('Class announcement posted.', 'success')
    except Exception as exc:
        db.session.rollback()
        logger.error('Class announcement failed: %s', exc, exc_info=True)
        flash('Could not post announcement.', 'danger')
    return _sponsor_hub_redirect(class_id)


# ----------------------------------------------------------------------
# 2. NEURAL VISION INFERENCE ENGINE (OCR GRADING PIPELINE)
# ----------------------------------------------------------------------
@app.route('/teacher/scan-assignment/<int:student_id>', methods=['POST'])
@login_required
def scan_assignment(student_id):
    """
    Asynchronously streams dropped image payloads, executes adaptive OCR text
    extraction, and evaluates content structures against answer vectors.
    """
    if normalize_role(current_user) != 'teacher':
        return jsonify({
            "status": "Error", 
            "message": "Access Denied: Insufficient cryptographic authorization credentials."
        }), 403

    # Check for library dependency installation states
    if pytesseract is None or Image is None:
        logger.critical("OCR Ingestion failure: Engine stack dependencies are unlinked.")
        return jsonify({
            "status": "Error",
            "message": "AI Vision system down: Subsystem dependencies missing."
        }), 500
        
    # Verify multi-part file block envelope structures
    if 'assignment' not in request.files:
        return jsonify({
            "status": "Error", 
            "message": "Payload processing failure: Missing structural file stream multipart key."
        }), 400
        
    file = request.files['assignment']
    if file.filename == '':
        return jsonify({
            "status": "Error", 
            "message": "Rejection: Received terminal stream with an empty file name."
        }), 400

    # Match target record parameters inside the logical database perimeter
    target_student = db.session.get(Student, student_id)
    if not target_student:
        return jsonify({
            "status": "Error", 
            "message": f"Execution target context (Student ID: #{student_id}) cannot be located."
        }), 404

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile or not teacher_can_access_student(teacher_profile, current_user, target_student):
        return jsonify({
            "status": "Error",
            "message": "Access denied: student is not in your assigned classes."
        }), 403
        
    try:
        # Ingest stream directly into safe volatile memory space
        img = Image.open(file.stream)
        
        # --- Advanced OCR Pre-processing Step ---
        # Converts low-contrast snapshots into highly clear monochrome text arrays
        img = img.convert('L')  # Cast image to Grayscale spectrum matrix
        img = img.filter(ImageFilter.SHARPEN)  # Increase edge accuracy thresholds
        img = ImageEnhance.Contrast(img).enhance(2.0)  # Expand binarization separation
        
        # Parse structural string vectors out of the processed bitmap layer
        student_text = pytesseract.image_to_string(img)
        
        # Abort inference sequence cleanly if image layers return non-alphanumeric blocks
        if not student_text.strip():
            return jsonify({
                "status": "Error", 
                "message": "Scan failed: The document canvas appears unreadable, skewed, or completely blank."
            }), 422
        
        # --- Neural Score Pattern Aggregation Matrix ---
        score = 0
        answer_key = ["Liberia", "Monrovia", "1847"]
        normalized_text = student_text.lower()
        
        for keyword in answer_key:
            if keyword.lower() in normalized_text:
                score += 10
                
        # Compress visual output lines into a uniform telemetry log sequence
        sanitized_snippet = student_text[:100].strip().replace('\n', ' ').replace('\r', '')
        
        return jsonify({
            "status": "Success",
            "suggested_grade": f"{score}/30",
            "detected_text_snippet": f"{sanitized_snippet}..." if len(student_text) > 100 else sanitized_snippet
        })

    except Exception as e:
        logger.error(f"Inference pipeline crash on operational tracking frame: {str(e)}", exc_info=True)
        return jsonify({
            "status": "Error", 
            "message": f"Pipeline execution fault: {str(e)}"
        }), 500


# Scan an existing submission file and return AI-suggested grade
@app.route('/teacher/scan-submission/<int:submission_id>', methods=['POST'])
@login_required
def scan_submission(submission_id):
    if normalize_role(current_user) != 'teacher':
        return jsonify({"status": "Error", "message": "Access denied."}), 403

    submission = Submission.query.get_or_404(submission_id)
    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    assessment = submission.assessment
    if (
        not teacher_profile
        or not assessment
        or not assessment.klass_id
        or not teacher_can_access_class(teacher_profile, current_user, assessment.klass_id)
    ):
        return jsonify({"status": "Error", "message": "Access denied."}), 403

    if not submission.file_path:
        return jsonify({"status": "Error", "message": "No file attached to this submission."}), 400

    file_path = resolve_static_upload_path(submission.file_path)
    if not os.path.exists(file_path):
        return jsonify({"status": "Error", "message": "Submission file not found on disk."}), 404

    try:
        if pytesseract is None or Image is None:
            return jsonify({"status": "Error", "message": "OCR dependencies missing."}), 500

        img = Image.open(file_path)
        img = img.convert('L')
        img = img.filter(ImageFilter.SHARPEN)
        img = ImageEnhance.Contrast(img).enhance(2.0)

        student_text = pytesseract.image_to_string(img)
        if not student_text.strip():
            return jsonify({"status": "Error", "message": "OCR returned no text."}), 422

        # Simple heuristic scoring (replace with model call if available)
        score = 0
        answer_key = ["liberia", "monrovia", "1847"]
        nt = student_text.lower()
        for keyword in answer_key:
            if keyword in nt:
                score += 10

        max_score = assessment.max_score or 100.0
        _apply_activity_score(
            teacher_profile,
            assessment,
            submission.student,
            score,
            feedback=f"AI OCR scan: {student_text[:120].strip().replace(chr(10), ' ').replace(chr(13), ' ')}",
        )
        db.session.commit()

        snippet = student_text[:200].strip().replace('\n', ' ').replace('\r', '')
        return jsonify({
            "status": "Success",
            "suggested_grade": f"{score}/{int(max_score) if max_score == int(max_score) else max_score}",
            "score": score,
            "max_score": max_score,
            "detected_text_snippet": snippet,
        })
    except Exception as e:
        logger.error(f"scan_submission error: {str(e)}", exc_info=True)
        db.session.rollback()
        return jsonify({"status": "Error", "message": str(e)}), 500


def _apply_activity_score(teacher_profile, assessment, student, score, feedback=None):
    """Save or update a student's score for an activity and refresh draft period grade."""
    submission = Submission.query.filter_by(
        assessment_id=assessment.id,
        student_id=student.id,
    ).first()
    if not submission:
        submission = Submission(
            assessment_id=assessment.id,
            student_id=student.id,
        )
        db.session.add(submission)

    submission.score = score
    submission.is_graded = True
    if feedback:
        submission.teacher_feedback = feedback

    sync_draft_period_grade(
        student,
        assessment.subject_name,
        assessment.marking_period or 1,
        teacher_id=teacher_profile.id,
    )
    return submission


@app.route('/teacher/grade-submission/<int:submission_id>', methods=['POST'])
@login_required
def grade_submission(submission_id):
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can grade submissions.', 'danger')
        return redirect(url_for('dashboard'))

    submission = Submission.query.get_or_404(submission_id)
    assessment = submission.assessment
    student = submission.student
    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    klass = assessment.klass if assessment else None
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to grade this submission.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    try:
        score = float(request.form.get('score'))
    except (TypeError, ValueError):
        flash('Invalid score provided.', 'danger')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    max_score = assessment.max_score or 100.0
    if score < 0 or score > max_score:
        flash(f'Score must be between 0 and {max_score}.', 'danger')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    feedback = request.form.get('feedback', '').strip() or None
    _apply_activity_score(teacher_profile, assessment, student, score, feedback=feedback)

    db.session.commit()
    flash('Score saved. Draft period grade updated for the student dashboard.', 'success')
    return redirect(request.referrer or url_for('activity_detail', assessment_id=assessment.id))


@app.route('/teacher/grade-activity/<int:assessment_id>/<int:student_id>', methods=['POST'])
@login_required
def grade_activity_student(assessment_id, student_id):
    """Let teachers set activity scores for any student in the class roster."""
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can grade activities.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    student = Student.query.get_or_404(student_id)
    klass = assessment.klass
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to grade this activity.', 'danger')
        return redirect(url_for('teacher_dashboard'))
    if get_student_class_id(student) != klass.id:
        flash('This student is not in the activity class.', 'danger')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    try:
        score = float(request.form.get('score'))
    except (TypeError, ValueError):
        flash('Invalid score provided.', 'danger')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    max_score = assessment.max_score or 100.0
    if score < 0 or score > max_score:
        flash(f'Score must be between 0 and {max_score}.', 'danger')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    feedback = request.form.get('feedback', '').strip() or None
    _apply_activity_score(teacher_profile, assessment, student, score, feedback=feedback)

    db.session.commit()
    flash(f'Score saved for {student.full_name}.', 'success')
    return redirect(url_for('activity_detail', assessment_id=assessment.id))


@app.route('/teacher/grade-activity/<int:assessment_id>/bulk', methods=['POST'])
@login_required
def bulk_grade_activity(assessment_id):
    """Save scores for multiple students on one activity in a single submit."""
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can grade activities.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    assessment = Assessment.query.get_or_404(assessment_id)
    klass = assessment.klass
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to grade this activity.', 'danger')
        return redirect(url_for('teacher_dashboard', tab='activities'))

    max_score = assessment.max_score or 100.0
    roster_ids = {s.id for s in get_students_for_class_ids([klass.id])}
    saved_count = 0
    errors = []

    for student_id in roster_ids:
        score_raw = request.form.get(f'score_{student_id}', '').strip()
        if score_raw == '':
            continue
        try:
            score = float(score_raw)
        except (TypeError, ValueError):
            errors.append(f'Invalid score for student #{student_id}')
            continue
        if score < 0 or score > max_score:
            errors.append(f'Score for student #{student_id} must be 0–{max_score}')
            continue

        student = db.session.get(Student, student_id)
        if not student:
            continue
        feedback = request.form.get(f'feedback_{student_id}', '').strip() or None
        _apply_activity_score(teacher_profile, assessment, student, score, feedback=feedback)
        saved_count += 1

    if errors:
        for msg in errors[:3]:
            flash(msg, 'danger')
        if not saved_count:
            db.session.rollback()
            return redirect(url_for('activity_detail', assessment_id=assessment.id))

    db.session.commit()
    if saved_count:
        flash(f'Saved {saved_count} score{"s" if saved_count != 1 else ""} for {assessment.title}.', 'success')
    else:
        flash('No scores entered. Fill in at least one score field.', 'warning')
    return redirect(url_for('activity_detail', assessment_id=assessment.id))


@app.route('/teacher/download-submission/<int:submission_id>')
@login_required
def teacher_download_submission(submission_id):
    """Download a student's submitted file for review."""
    if normalize_role(current_user) != 'teacher':
        flash('Only teachers can download student submissions.', 'danger')
        return redirect(url_for('dashboard'))

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    submission = Submission.query.get_or_404(submission_id)
    assessment = submission.assessment
    klass = assessment.klass if assessment else None
    if not klass or not teacher_can_access_class(teacher_profile, current_user, klass.id):
        flash('You are not authorized to access this submission.', 'danger')
        return redirect(url_for('teacher_dashboard'))

    if not submission.file_path:
        flash('This submission has no uploaded file.', 'warning')
        return redirect(url_for('activity_detail', assessment_id=assessment.id))

    rel_path = submission.file_path.replace('\\', '/').lstrip('/')
    if rel_path.startswith('static/'):
        rel_path = rel_path[len('static/'):]
    return safe_send_upload_file(os.path.dirname(rel_path), os.path.basename(rel_path))
# -------------------------- DISCIPLINE & SUSPENSION ---------------------------
@app.route('/student/<int:student_id>/suspend', methods=['POST'])
@role_required('Dean')
def suspend_student(student_id):
    days = request.form.get('days', type=int)
    reason = request.form.get('reason')
    if not days or not reason:
        flash("Days and reason are required for suspension.", "danger")
        return redirect(url_for('dashboard'))
    
    msg = SchoolEngine.suspend_student(student_id, days, reason)
    log_security_event(f"Student {student_id} suspended. Reason: {reason}")
    flash(msg, "warning")
    return redirect(url_for('dashboard'))

# -------------------------- CLASS MANAGEMENT ---------------------------

RESET_ROLE_GROUPS = [
    {
        'key': 'teacher',
        'label': 'Teachers',
        'icon': 'fa-chalkboard-teacher',
        'browse': 'list',
        'roles': {'teacher'},
    },
    {
        'key': 'student',
        'label': 'Students',
        'icon': 'fa-user-graduate',
        'browse': 'class_folders',
        'roles': {'student'},
    },
    {
        'key': 'staff',
        'label': 'Staff',
        'icon': 'fa-briefcase',
        'browse': 'list',
        'roles': {'registrar', 'business', 'vpa', 'vpi', 'dean', 'principal', 'parent', 'sponsor'},
    },
    {
        'key': 'admin',
        'label': 'Administrators',
        'icon': 'fa-user-shield',
        'browse': 'list',
        'roles': {'admin'},
        'admin_only': True,
    },
]


def _reset_operator_can_target(operator, target_user):
    """Whether the logged-in operator may reset target_user's password."""
    if not operator or not target_user:
        return False
    op_role = (operator.role or '').lower()
    if op_role == 'admin':
        return True
    if op_role == 'principal':
        if target_user.id == operator.id:
            return False
        return (target_user.role or '').lower() != 'admin'
    return False


def _reset_role_groups_for_operator(operator):
    groups = []
    op_role = (operator.role or '').lower()
    for group in RESET_ROLE_GROUPS:
        if group.get('admin_only') and op_role != 'admin':
            continue
        groups.append(group)
    return groups


def _reset_group_by_key(role_key):
    for group in RESET_ROLE_GROUPS:
        if group['key'] == role_key:
            return group
    return RESET_ROLE_GROUPS[0]


def _reset_build_class_folders():
    folders = []
    for klass in Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all():
        students = Student.query.filter_by(klass_id=klass.id).all()
        portal_count = sum(1 for student in students if student.user_id)
        folders.append({
            'klass': klass,
            'student_count': len(students),
            'portal_count': portal_count,
        })
    return folders


def _reset_build_class_students(klass, search_q, operator):
    rows = []
    students = (
        Student.query.filter_by(klass_id=klass.id)
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )
    for student in students:
        user = db.session.get(User, student.user_id) if student.user_id else None
        if search_q:
            haystack = ' '.join(filter(None, [
                student.first_name,
                student.last_name,
                student.student_id,
                user.email if user else '',
            ])).lower()
            if search_q.lower() not in haystack:
                continue
        rows.append({
            'student': student,
            'user': user,
            'can_reset': _reset_operator_can_target(operator, user) if user else False,
        })
    return rows


def _reset_build_role_users(role_keys, search_q, operator):
    rows = []
    for user in User.query.order_by(User.full_name.asc()).all():
        if (user.role or '').lower() not in role_keys:
            continue
        if not _reset_operator_can_target(operator, user):
            continue
        if search_q:
            haystack = f"{user.full_name or ''} {user.email or ''} {user.role or ''}".lower()
            if search_q.lower() not in haystack:
                continue
        teacher = (
            Teacher.query.filter_by(user_id=user.id).first()
            if (user.role or '').lower() == 'teacher'
            else None
        )
        rows.append({'user': user, 'teacher': teacher})
    return rows


@app.route('/admin/account/override-reset', methods=['GET', 'POST'])
@login_required
def administrative_password_reset():
    if not current_user.role or current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access matrix. Insufficient clearance permissions.", "danger")
        return redirect(url_for('dashboard'))

    role_groups = _reset_role_groups_for_operator(current_user)
    selected_role = request.args.get('role') or request.form.get('return_role') or role_groups[0]['key']
    selected_group = _reset_group_by_key(selected_role)
    if selected_group.get('admin_only') and current_user.role.lower() != 'admin':
        selected_role = role_groups[0]['key']
        selected_group = role_groups[0]

    search_q = (request.args.get('q') or request.form.get('return_q') or '').strip()
    class_id = request.args.get('class_id', type=int) or request.form.get('return_class_id', type=int)
    target_user_id = request.args.get('target_user_id', type=int)
    selected_class = db.session.get(Class, class_id) if class_id else None
    selected_target = db.session.get(User, target_user_id) if target_user_id else None

    if selected_target and not _reset_operator_can_target(current_user, selected_target):
        flash("Security Exception: You cannot reset this account.", "danger")
        selected_target = None
        target_user_id = None

    if request.method == 'POST':
        post_target_id = request.form.get('target_user_id', type=int)
        new_password = (request.form.get('new_password') or '').strip()
        confirm_password = (request.form.get('confirm_password') or '').strip()
        return_role = request.form.get('return_role') or selected_role
        return_class_id = request.form.get('return_class_id', type=int)
        return_q = (request.form.get('return_q') or '').strip()

        def _reset_redirect(target_id=None):
            params = {'role': return_role}
            if return_class_id:
                params['class_id'] = return_class_id
            if return_q:
                params['q'] = return_q
            if target_id:
                params['target_user_id'] = target_id
            return redirect(url_for('administrative_password_reset', **params))

        if not post_target_id or not new_password:
            flash("Missing mandatory transmission parameters.", "danger")
            return _reset_redirect(post_target_id)

        if new_password != confirm_password:
            flash("Passwords do not match. Enter the same password in both fields.", "danger")
            return _reset_redirect(post_target_id)

        if len(new_password) < 6:
            flash("Security policy violation: Password string must be at least 6 characters.", "danger")
            return _reset_redirect(post_target_id)

        target_user = db.session.get(User, post_target_id)
        if not target_user:
            flash("Target identity record node could not be pulled from system ledger.", "danger")
            return _reset_redirect()

        if not _reset_operator_can_target(current_user, target_user):
            flash("Security Exception: You cannot reset this account.", "danger")
            return _reset_redirect()

        try:
            target_user.set_password(new_password)
            db.session.commit()
            flash(
                f"Credentials for {target_user.full_name or target_user.username} successfully overwritten. "
                f"They can sign in immediately with the new password.",
                "success",
            )
            return _reset_redirect()
        except Exception as e:
            db.session.rollback()
            logger.error("Password reset commit failed: %s", e, exc_info=True)
            flash("An internal transactional ledger exception aborted password commitment.", "danger")
            return _reset_redirect(post_target_id)

    class_folders = _reset_build_class_folders() if selected_group['browse'] == 'class_folders' else []
    class_students = (
        _reset_build_class_students(selected_class, search_q, current_user)
        if selected_class
        else []
    )
    role_users = (
        _reset_build_role_users(selected_group['roles'], search_q, current_user)
        if selected_group['browse'] == 'list'
        else []
    )

    return render_template(
        'administrative_reset.html',
        role_groups=role_groups,
        selected_role=selected_role,
        selected_group=selected_group,
        class_folders=class_folders,
        selected_class=selected_class,
        class_students=class_students,
        role_users=role_users,
        search_q=search_q,
        class_id=class_id,
        target_user_id=target_user_id,
        selected_target=selected_target,
    )

@app.route('/class/edit/<int:class_id>', methods=['GET', 'POST'])
@login_required
def class_edit(class_id):
    # --- FIXED SECURITY GUARD: CASE-INSENSITIVE MULTI-ROLE VALIDATION ---
    user_role = current_user.role.lower() if current_user.role else ""
    if user_role not in ['admin', 'principal']:
        flash("Unauthorized access. Administrative privileges required.", "danger")
        return redirect(url_for('dashboard'))

    # Modern SQLAlchemy 2.0 implementation fallback for query
    klass = Class.query.get_or_404(class_id)
    form = CreateClassForm(obj=klass)
    
    teachers = Teacher.query.order_by(Teacher.first_name, Teacher.last_name).all()
    teacher_choices = [
        (t.id, (f"{(t.first_name or '').strip()} {(t.last_name or '').strip()}".strip() or (t.user.full_name if t.user else f"Teacher {t.id}")))
        for t in teachers
    ]
    sponsor_choices = [(0, "— No Sponsor —")] + teacher_choices

    form.teacher_id.choices = [(0, "— Select Teacher —")] + teacher_choices
    form.sponsor_id.choices = sponsor_choices

    if request.method == 'GET':
        form.teacher_id.data = klass.teacher_id or 0
        if klass.sponsor_id:
            sponsor_teacher = Teacher.query.filter_by(user_id=klass.sponsor_id).first()
            form.sponsor_id.data = sponsor_teacher.id if sponsor_teacher else 0
        else:
            form.sponsor_id.data = 0

    if form.validate_on_submit():
        raw_teacher_id = form.teacher_id.data
        raw_sponsor_id = form.sponsor_id.data

        teacher_id = None if raw_teacher_id in (0, '0', '', None) else int(raw_teacher_id)
        sponsor_teacher_id = None if raw_sponsor_id in (0, '0', '', None) else int(raw_sponsor_id)

        sponsor_id = None
        if sponsor_teacher_id:
            # Modernized SQLAlchemy 2.0 Safe Lookup
            sponsor_teacher = db.session.get(Teacher, sponsor_teacher_id)
            sponsor_id = sponsor_teacher.user_id if sponsor_teacher else None

        klass.name = form.name.data
        klass.description = form.description.data
        klass.yearly_fee = parse_currency_amount_optional(form.yearly_fee.data)
        klass.teacher_id = teacher_id
        klass.sponsor_id = sponsor_id
        
        try:
            db.session.commit()
            flash(f"Class '{klass.name}' updated successfully.", "success")
            return redirect(url_for('class_create'))
        except Exception as e:
            db.session.rollback()
            print(f"[-] Database Error during class update: {str(e)}")
            flash("Database error occurred while updating class data.", "danger")

    return render_template('class_edit.html', form=form, klass=klass)


def _build_class_sponsor_matrix(classes):
    """Map each class to its assigned homeroom sponsor teacher."""
    rows = []
    for klass in classes:
        sponsor_name = None
        sponsor_user_id = None
        if klass.sponsor_id:
            sponsor_user = db.session.get(User, klass.sponsor_id)
            if sponsor_user:
                sponsor_name = sponsor_user.full_name
                sponsor_user_id = sponsor_user.id
        rows.append({
            'klass': klass,
            'sponsor_name': sponsor_name,
            'sponsor_user_id': sponsor_user_id,
        })
    return rows


@app.route('/principal/class-sponsors', methods=['GET'])
@login_required
def principal_class_sponsors():
    """Full class sponsor command center for principal/admin."""
    user_role = (current_user.role or '').lower()
    if user_role not in ['admin', 'principal']:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('dashboard'))

    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    teachers = Teacher.query.filter_by(status='ACTIVE').order_by(
        Teacher.first_name.asc(), Teacher.last_name.asc()
    ).all()
    return render_template(
        'principal_class_sponsors.html',
        classes=classes,
        teachers=teachers,
        sponsor_matrix=_build_class_sponsor_matrix(classes),
    )


@app.route('/class/<int:class_id>/sponsor', methods=['POST'])
@login_required
def class_set_sponsor(class_id):
    # --- FIXED SECURITY GUARD: CASE-INSENSITIVE CHECKING ---
    user_role = current_user.role.lower() if current_user.role else ""
    if user_role not in ['admin', 'principal']:
        flash("Unauthorized access. Administrative privileges required.", "danger")
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    sponsor_id = request.form.get('sponsor_id', type=int)
    teacher_id = request.form.get('teacher_id', type=int)
    next_page = (request.form.get('next') or 'class_create').strip()

    if not sponsor_id and teacher_id:
        teacher_profile = db.session.get(Teacher, teacher_id)
        if teacher_profile and teacher_profile.user_id:
            sponsor_id = teacher_profile.user_id

    if sponsor_id:
        # Modernized SQLAlchemy 2.0 Safe Lookup
        sponsor = db.session.get(User, sponsor_id)
        teacher_profile = Teacher.query.filter_by(user_id=sponsor_id).first()
        
        if not sponsor or not teacher_profile:
            flash("Invalid sponsor selection. Only teachers can be sponsors.", "danger")
            return redirect(url_for(next_page) if next_page in current_app.view_functions else url_for('class_create'))
            
        klass.sponsor_id = sponsor.id
        flash(f"Assigned teacher {sponsor.full_name} as sponsor to {klass.name}.", "success")
    else:
        klass.sponsor_id = None
        flash(f"Sponsor cleared for {klass.name}.", "info")

    try:
        db.session.commit()
    except Exception as e:
        db.session.rollback()
        print(f"[-] Database Error during assigning sponsor: {str(e)}")
        flash("Failed to commit database modifications.", "danger")

    if next_page in current_app.view_functions:
        return redirect(url_for(next_page))
    return redirect(url_for('class_create'))

# ------------------------ ACADEMIC YEAR ROLLOVER WIZARD ---------------------------
def _student_grade_level(student):
    """Resolve a student's grade tier from stored grade_level or assigned class."""
    if student.grade_level:
        return student.grade_level
    if student.klass_id:
        klass = student.assigned_class or db.session.get(Class, student.klass_id)
        if klass:
            return klass.grade_level
    return None


def check_promotion_criteria(student, academic_year=None):
    """
    MoE promotion standard: configurable average threshold and max failing subjects.
    Students without grades for the year are not promoted.
    """
    if not student:
        return False
    if academic_year is None:
        academic_year = AcademicYear.query.filter_by(is_active=True).first()
    if not academic_year:
        return False

    pass_score = promotion_pass_score()
    max_failing = max_failing_subjects_for_promotion()

    grades = Grade.query.filter_by(
        student_id=student.id,
        academic_year_id=academic_year.id,
    ).all()
    if not grades:
        return False

    subject_averages = {}
    for grade in grades:
        subject_key = (grade.subject_name or grade.subject or '').strip().lower()
        if not subject_key:
            continue
        score = grade.score if grade.score is not None else grade.final_average
        if score is None:
            continue
        subject_averages.setdefault(subject_key, []).append(float(score))

    if not subject_averages:
        return False

    failed_count = 0
    grand_total = 0.0
    for scores in subject_averages.values():
        avg = sum(scores) / len(scores)
        grand_total += avg
        if avg < pass_score:
            failed_count += 1

    final_average = grand_total / len(subject_averages)
    return failed_count <= max_failing and final_average >= pass_score


def _next_academic_year_name(name):
    """Advance labels like 2025-2026 to 2026-2027."""
    import re
    match = re.match(r'^(\d{4})\s*[-–/]\s*(\d{4})$', (name or '').strip())
    if match:
        start_y, end_y = int(match.group(1)), int(match.group(2))
        return f"{start_y + 1}-{end_y + 1}"
    return None


def _resolve_or_create_next_academic_year(active_year):
    """End the current year and activate (or create) the next academic year."""
    if not active_year:
        return None

    next_name = _next_academic_year_name(active_year.name)
    if not next_name:
        if active_year.start_date:
            y = active_year.start_date.year
            next_name = f"{y + 1}-{y + 2}"
        else:
            next_name = f"{active_year.name} (Next)"

    active_year.is_active = False
    if not active_year.end_date:
        active_year.end_date = datetime.now(timezone.utc).date()

    existing = AcademicYear.query.filter_by(name=next_name).first()
    if existing:
        db.session.execute(
            db.update(AcademicYear).where(AcademicYear.id != existing.id).values(is_active=False)
        )
        existing.is_active = True
        return existing

    if active_year.start_date:
        try:
            start_date = active_year.start_date.replace(year=active_year.start_date.year + 1)
        except ValueError:
            start_date = active_year.start_date + timedelta(days=365)
    else:
        start_date = datetime.now(timezone.utc).date()

    end_date = None
    if active_year.end_date:
        try:
            end_date = active_year.end_date.replace(year=active_year.end_date.year + 1)
        except ValueError:
            end_date = active_year.end_date + timedelta(days=365)

    target_year = AcademicYear(
        name=next_name,
        start_date=start_date,
        end_date=end_date,
        is_active=True,
        created_by=current_user.id,
    )
    db.session.add(target_year)
    db.session.flush()
    db.session.execute(
        db.update(AcademicYear).where(AcademicYear.id != target_year.id).values(is_active=False)
    )
    target_year.is_active = True
    return target_year


def _rollover_already_run_today(from_year_id):
    """Soft guard: block duplicate rollover for the same source year on the same UTC day."""
    if not from_year_id:
        return False
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    return RolloverLog.query.filter(
        RolloverLog.from_year_id == from_year_id,
        RolloverLog.created_at >= today_start,
    ).first() is not None


def record_rollover_audit(
    *,
    mode,
    from_year,
    to_year,
    promoted,
    retained,
    graduated,
    re_registration=0,
):
    """Persist rollover counts and a general activity audit entry."""
    log = RolloverLog(
        user_id=current_user.id,
        from_year_id=from_year.id if from_year else None,
        from_year_name=from_year.name if from_year else None,
        to_year_id=to_year.id if to_year else None,
        to_year_name=to_year.name if to_year else None,
        promoted=promoted,
        retained=retained,
        graduated=graduated,
        re_registration=re_registration,
        rollover_mode=mode,
    )
    db.session.add(log)

    from_label = from_year.name if from_year else '—'
    to_label = to_year.name if to_year else '—'
    activity = Activity(
        user_id=current_user.id,
        action=(
            f"Academic rollover ({mode}): {from_label} → {to_label} — "
            f"{promoted} promoted, {retained} retained, {graduated} graduated"
        ),
        module='Academic',
        ip_address=request.remote_addr,
    )
    db.session.add(activity)
    return log


def _compute_next_year_label(active_year):
    """Return the label the quick rollover would use for the next academic year."""
    if not active_year:
        return None
    next_name = _next_academic_year_name(active_year.name)
    if next_name:
        return next_name
    if active_year.start_date:
        y = active_year.start_date.year
        return f"{y + 1}-{y + 2}"
    return f"{active_year.name} (Next)"


def preview_moe_academic_rollover(active_year=None):
    """Dry-run summary for the one-click MoE promotion rollover."""
    if active_year is None:
        active_year = AcademicYear.query.filter_by(is_active=True).first()

    preview = {
        'active_year': active_year,
        'next_year_name': _compute_next_year_label(active_year),
        'promoted': 0,
        'retained': 0,
        'graduated': 0,
        're_registration': 0,
        'student_total': 0,
        'no_active_year': active_year is None,
        'no_students': True,
        'already_rolled_today': False,
        'pass_score': promotion_pass_score(),
        'max_failing': max_failing_subjects_for_promotion(),
        'warnings': [],
        'can_execute': False,
    }

    if not active_year:
        preview['warnings'].append(
            'No active academic year found. Create and activate a year first.'
        )
        return preview

    preview['already_rolled_today'] = _rollover_already_run_today(active_year.id)
    if preview['already_rolled_today']:
        preview['warnings'].append(
            'A rollover for this academic year was already recorded today. '
            'Proceed only if you intend to run it again.'
        )

    students = Student.query.filter_by(
        academic_year_id=active_year.id,
        status='ACTIVE',
    ).all()
    preview['student_total'] = len(students)
    preview['no_students'] = len(students) == 0
    if preview['no_students']:
        preview['warnings'].append('No active students are enrolled in the current academic year.')

    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    promotion_map = build_default_promotion_map(classes)

    for student in students:
        passed = check_promotion_criteria(student, active_year)
        grade_level = _student_grade_level(student)

        if grade_level == 12:
            if passed:
                preview['graduated'] += 1
            else:
                preview['retained'] += 1
        elif passed:
            target_class = promotion_map.get(student.klass_id) if student.klass_id else None
            if target_class == 'graduate':
                preview['graduated'] += 1
            else:
                preview['promoted'] += 1
        else:
            preview['retained'] += 1

        preview['re_registration'] += 1

    preview['can_execute'] = (
        not preview['no_active_year']
        and not preview['no_students']
    )
    return preview


def execute_moe_academic_rollover(active_year=None, *, allow_repeat_today=False):
    """
    One-click academic year rollover with MoE-based student promotion.
    Returns result dict; raises ValueError on guard failures.
    """
    if active_year is None:
        active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        raise ValueError('No active academic year found. Create and activate a year first.')

    if _rollover_already_run_today(active_year.id) and not allow_repeat_today:
        raise ValueError(
            'A rollover for this academic year was already run today. '
            'Check the audit log or confirm to run again.'
        )

    students = Student.query.filter_by(
        academic_year_id=active_year.id,
        status='ACTIVE',
    ).all()
    if not students:
        raise ValueError('No active students are enrolled in the current academic year.')

    target_year = _resolve_or_create_next_academic_year(active_year)
    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    promotion_map = build_default_promotion_map(classes)
    class_cache = {c.id: c for c in classes}

    promoted = retained = graduated = re_registration = 0

    for student in students:
        passed = check_promotion_criteria(student, active_year)
        grade_level = _student_grade_level(student)

        if grade_level == 12:
            if passed:
                student.status = 'GRADUATED'
                student.klass_id = None
                graduated += 1
            else:
                student.status = 'REPEAT'
                student.registration_type = 'Returning'
                student.tuition_cleared = False
                student.academic_year_id = target_year.id
                retained += 1
                re_registration += 1
            continue

        student.registration_type = 'Returning'
        student.tuition_cleared = False
        student.academic_year_id = target_year.id
        re_registration += 1

        if passed:
            target_class = promotion_map.get(student.klass_id) if student.klass_id else None
            if isinstance(target_class, int):
                student.klass_id = target_class
                promoted_class = class_cache.get(target_class)
                if promoted_class:
                    student.grade_level = promoted_class.grade_level
            elif grade_level:
                student.grade_level = min(12, grade_level + 1)
            promoted += 1
        else:
            student.status = 'REPEAT'
            retained += 1

    record_rollover_audit(
        mode='quick',
        from_year=active_year,
        to_year=target_year,
        promoted=promoted,
        retained=retained,
        graduated=graduated,
        re_registration=re_registration,
    )
    db.session.commit()

    return {
        'target_year_name': target_year.name,
        'promoted': promoted,
        'retained': retained,
        'graduated': graduated,
        're_registration': re_registration,
    }


def format_rollover_flash_summary(results):
    """Build a detailed post-rollover flash message."""
    retained = results.get('retained', results.get('repeat', 0))
    parts = [
        (
            f"Rollover complete: {results.get('promoted', 0)} promoted, "
            f"{retained} retained, {results.get('graduated', 0)} graduated."
        ),
        f"New year: {results.get('target_year_name', '—')}",
    ]
    if results.get('re_registration'):
        parts.append(f"{results['re_registration']} students marked for re-registration")
    if results.get('re_enrolled'):
        parts.append(f"{results['re_enrolled']} students re-enrolled")
    if results.get('ended_year_name'):
        parts.insert(0, f"Ended {results['ended_year_name']}.")
    return ' '.join(parts)


def _rollover_role_guard():
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))
    return None


def build_default_promotion_map(classes):
    """Suggest next-class targets from grade level (+1) and stream when possible."""
    by_grade = {}
    for klass in classes:
        by_grade.setdefault(klass.grade_level, []).append(klass)

    promotion_map = {}
    for klass in classes:
        next_grade = klass.grade_level + 1
        if next_grade > 12:
            promotion_map[klass.id] = 'graduate'
            continue
        candidates = by_grade.get(next_grade, [])
        if not candidates:
            promotion_map[klass.id] = 'repeat'
            continue
        if klass.stream:
            stream_matches = [c for c in candidates if c.stream == klass.stream]
            if stream_matches:
                promotion_map[klass.id] = stream_matches[0].id
                continue
        promotion_map[klass.id] = candidates[0].id
    return promotion_map


def build_rollover_preview(active_year, classes, students):
    """Summarize rollover impact before execution."""
    promotion_map = build_default_promotion_map(classes)
    counts = {
        'promote': 0,
        'graduate': 0,
        'repeat': 0,
        'unassigned': 0,
    }
    for student in students:
        if not student.klass_id:
            counts['unassigned'] += 1
            continue
        target = promotion_map.get(student.klass_id, 'repeat')
        if target == 'graduate':
            counts['graduate'] += 1
        elif target == 'repeat':
            counts['repeat'] += 1
        else:
            counts['promote'] += 1
    return {
        'promotion_map': promotion_map,
        'counts': counts,
        'student_total': len(students),
    }


def parse_rollover_promotion_map(classes):
    """Read per-class promotion targets submitted from the wizard form."""
    promotion_map = {}
    valid_class_ids = {str(c.id) for c in classes}
    for klass in classes:
        field_name = f'promotion_{klass.id}'
        raw_value = (request.form.get(field_name) or '').strip()
        if raw_value == 'graduate':
            promotion_map[klass.id] = 'graduate'
        elif raw_value == 'repeat':
            promotion_map[klass.id] = 'repeat'
        elif raw_value in valid_class_ids:
            promotion_map[klass.id] = int(raw_value)
        else:
            promotion_map[klass.id] = build_default_promotion_map(classes).get(klass.id, 'repeat')
    return promotion_map


def execute_academic_rollover(
    *,
    end_current_year,
    target_mode,
    target_year_id,
    new_year_name,
    new_year_start,
    new_year_end,
    apply_promotions,
    promotion_map,
    reset_tuition_cleared,
    charge_registration_fee,
    registration_fee_amount,
    exclude_statuses,
):
    """Run the full academic year rollover cycle in one database transaction."""
    results = {
        'ended_year_name': None,
        'target_year_name': None,
        'promoted': 0,
        'graduated': 0,
        'repeat': 0,
        're_enrolled': 0,
        'fees_recorded': 0,
        'tuition_reset': 0,
        'skipped': 0,
    }

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    source_year_id = active_year.id if active_year else None

    if end_current_year and active_year:
        active_year.is_active = False
        if not active_year.end_date:
            active_year.end_date = datetime.now(timezone.utc).date()
        results['ended_year_name'] = active_year.name

    if target_mode == 'new':
        if not new_year_name or not new_year_start:
            raise ValueError("New academic year requires a name and start date.")
        if AcademicYear.query.filter_by(name=new_year_name.strip()).first():
            raise ValueError(f"Academic year '{new_year_name.strip()}' already exists.")
        target_year = AcademicYear(
            name=new_year_name.strip(),
            start_date=new_year_start,
            end_date=new_year_end,
            is_active=True,
            created_by=current_user.id,
        )
        db.session.add(target_year)
        db.session.flush()
    else:
        if not target_year_id:
            raise ValueError("Select an existing academic year to activate.")
        target_year = db.session.get(AcademicYear, target_year_id)
        if not target_year:
            raise ValueError("Selected target academic year was not found.")

    db.session.execute(
        db.update(AcademicYear)
        .where(AcademicYear.id != target_year.id)
        .values(is_active=False)
    )
    target_year.is_active = True
    results['target_year_name'] = target_year.name

    if source_year_id:
        students_query = Student.query.filter_by(academic_year_id=source_year_id)
    else:
        students_query = Student.query.filter(
            or_(Student.academic_year_id.is_(None), Student.academic_year_id != target_year.id)
        )
    if exclude_statuses:
        students_query = students_query.filter(~Student.status.in_(list(exclude_statuses)))
    students = students_query.all()

    fee_amount = money(registration_fee_amount)
    class_cache = {c.id: c for c in Class.query.all()}

    for student in students:
        if exclude_statuses and student.status in exclude_statuses:
            results['skipped'] += 1
            continue

        if apply_promotions and student.klass_id:
            target_class = promotion_map.get(student.klass_id, 'repeat')
            if target_class == 'graduate':
                student.status = 'GRADUATED'
                results['graduated'] += 1
                continue
            if target_class == 'repeat':
                results['repeat'] += 1
            elif target_class:
                student.klass_id = int(target_class)
                promoted_class = class_cache.get(int(target_class))
                if promoted_class:
                    student.grade_level = promoted_class.grade_level
                results['promoted'] += 1

        student.academic_year_id = target_year.id
        student.registration_type = 'Returning'

        if reset_tuition_cleared:
            if student.tuition_cleared:
                results['tuition_reset'] += 1
            student.tuition_cleared = False

        if charge_registration_fee and fee_amount > 0:
            record_student_payment_with_income(
                student,
                target_year.id,
                term=1,
                amount_paid=fee_amount,
                description=f"Registration fee — {target_year.name} rollover",
            )
            results['fees_recorded'] += 1

        results['re_enrolled'] += 1

    retained_count = results.get('repeat', 0)
    record_rollover_audit(
        mode='wizard',
        from_year=db.session.get(AcademicYear, source_year_id) if source_year_id else None,
        to_year=target_year,
        promoted=results['promoted'],
        retained=retained_count,
        graduated=results['graduated'],
        re_registration=results['re_enrolled'],
    )
    db.session.commit()
    return results


@app.route('/admin/academic-rollover', methods=['GET', 'POST'])
@login_required
@role_required(ROLE_ADMIN)
def academic_rollover():
    """Preview and execute one-click academic year rollover with MoE-based promotion."""
    preview = preview_moe_academic_rollover()

    if request.method == 'GET':
        return render_template(
            'academic_rollover_preview.html',
            preview=preview,
            wizard_url=url_for('academic_year_rollover'),
        )

    if request.form.get('preview'):
        preview = preview_moe_academic_rollover()
        if request.form.get('format') == 'json' or request.accept_mimetypes.best == 'application/json':
            return jsonify({
                'promoted': preview['promoted'],
                'retained': preview['retained'],
                'graduated': preview['graduated'],
                're_registration': preview['re_registration'],
                'student_total': preview['student_total'],
                'next_year_name': preview['next_year_name'],
                'pass_score': preview['pass_score'],
                'max_failing': preview['max_failing'],
                'warnings': preview['warnings'],
                'can_execute': preview['can_execute'],
            })
        return render_template(
            'academic_rollover_preview.html',
            preview=preview,
            wizard_url=url_for('academic_year_rollover'),
            show_preview=True,
        )

    if not preview['can_execute']:
        flash(preview['warnings'][0] if preview['warnings'] else 'Rollover cannot be executed.', 'warning')
        return redirect(url_for('academic_rollover'))

    allow_repeat = request.form.get('allow_repeat_today') == '1'
    if preview['already_rolled_today'] and not allow_repeat:
        flash(
            'A rollover for this academic year was already run today. '
            'Acknowledge the warning on the preview page to proceed.',
            'warning',
        )
        return redirect(url_for('academic_rollover'))

    try:
        results = execute_moe_academic_rollover(allow_repeat_today=allow_repeat)
        flash(format_rollover_flash_summary(results), 'success')
    except Exception as exc:
        db.session.rollback()
        flash(f"Rollover failed: {exc}", "danger")

    return redirect(url_for('dashboard'))


@app.route('/academic-years/rollover', methods=['GET', 'POST'])
@login_required
def academic_year_rollover():
    denied = _rollover_role_guard()
    if denied:
        return denied

    form = RolloverWizardForm()
    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    inactive_years = AcademicYear.query.filter_by(is_active=False).order_by(
        AcademicYear.start_date.desc()
    ).all()
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    all_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    form.target_year_id.choices = [(0, '-- Select Year --')] + [
        (y.id, y.name) for y in inactive_years
    ]

    if active_year:
        source_students = Student.query.filter_by(academic_year_id=active_year.id).all()
    else:
        source_students = Student.query.filter(
            or_(Student.academic_year_id.is_(None), Student.status == 'ACTIVE')
        ).all()

    promotion_defaults = build_default_promotion_map(classes)
    preview = build_rollover_preview(active_year, classes, source_students)

    class_choices = [(c.id, c.name) for c in classes]

    if request.method == 'POST':
        if not form.validate_on_submit():
            flash("Please complete all required fields and confirm the rollover.", "warning")
        else:
            exclude_statuses = set()
            if form.exclude_graduated.data:
                exclude_statuses.add('GRADUATED')
            if form.exclude_withdrawn.data:
                exclude_statuses.add('WITHDRAWN')
            if form.exclude_suspended.data:
                exclude_statuses.add('SUSPENDED')

            if form.target_mode.data == 'existing' and not form.target_year_id.data:
                flash("Select an existing academic year to activate.", "warning")
            elif form.target_mode.data == 'new' and (
                not (form.new_year_name.data or '').strip() or not form.new_year_start.data
            ):
                flash("Provide a name and start date for the new academic year.", "warning")
            elif form.charge_registration_fee.data and money(form.registration_fee_amount.data) <= 0:
                flash("Enter a registration fee amount greater than zero, or disable fee recording.", "warning")
            else:
                promotion_map = parse_rollover_promotion_map(classes)
                try:
                    results = execute_academic_rollover(
                        end_current_year=form.end_current_year.data,
                        target_mode=form.target_mode.data,
                        target_year_id=form.target_year_id.data,
                        new_year_name=form.new_year_name.data,
                        new_year_start=form.new_year_start.data,
                        new_year_end=form.new_year_end.data,
                        apply_promotions=form.apply_promotions.data,
                        promotion_map=promotion_map,
                        reset_tuition_cleared=form.reset_tuition_cleared.data,
                        charge_registration_fee=form.charge_registration_fee.data,
                        registration_fee_amount=form.registration_fee_amount.data,
                        exclude_statuses=exclude_statuses,
                    )
                    flash(format_rollover_flash_summary(results), 'success')
                    return redirect(url_for('academic_years'))
                except Exception as exc:
                    db.session.rollback()
                    flash(f"Rollover failed: {exc}", "danger")

    stats = {
        'active_students': len(source_students),
        'active_classes': len(classes),
        'inactive_years': len(inactive_years),
        'total_years': len(all_years),
    }

    return render_template(
        'academic_rollover_wizard.html',
        form=form,
        active_year=active_year,
        classes=classes,
        class_choices=class_choices,
        promotion_defaults=promotion_defaults,
        preview=preview,
        stats=stats,
        all_years=all_years,
    )


# ------------------------ ACADEMIC YEARS ---------------------------
@app.route('/academic-years', methods=['GET', 'POST'])
@login_required
def academic_years():
    # ✨ FIX: Check if the user's role is neither Admin nor Principal
    # We use .lower() to completely eliminate case-sensitivity bugs!
    if current_user.role.lower() not in ['admin', 'principal', 'vpa']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = AcademicYearForm()

    if form.validate_on_submit():
        if form.is_active.data:
            # Deactivate all other years
            AcademicYear.query.update({'is_active': False})
        
        year = AcademicYear(
            name=form.name.data,
            start_date=form.start_date.data,
            end_date=form.end_date.data,
            is_active=form.is_active.data,
            created_by=current_user.id
        )
        db.session.add(year)
        db.session.commit()
        flash("Academic year created successfully.", "success")
        return redirect(url_for('academic_years'))

    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    return render_template(
        'academic_years.html',
        form=form,
        years=years
    )

@app.route('/academic-years/<int:year_id>/end', methods=['POST'])
@login_required
def end_academic_year(year_id):
    # ✨ Secured role authentication matrix
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    # ✨ Modern, crash-proof database query fetch
    year = db.first_or_404(db.select(AcademicYear).filter_by(id=year_id))
    
    if not year.is_active:
        flash("Academic year is already inactive.", "info")
        return redirect(url_for('academic_years'))

    # Change operational state variables
    year.is_active = False
    
    # Safely extract the date object from your tracking timestamp framework
    if not year.end_date:
        year.end_date = datetime.now(timezone.utc).date()
        
    try:
        db.session.commit()
        flash(f"Academic year {year.name} has been marked as ended successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Database sync fault: {str(e)}", "danger")
        
    return redirect(url_for('academic_years'))

@app.route('/academic-years/edit/<int:year_id>', methods=['GET', 'POST'])
@login_required
def edit_academic_year(year_id):
    # ✨ Fixed: Clean multi-role checking with lowercase protection
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    # ✨ Fixed: Modern Flask-SQLAlchemy lookup to avoid deprecation warnings
    year = db.first_or_404(db.select(AcademicYear).filter_by(id=year_id))
    
    # Initialize the form with data from the existing database object
    form = AcademicYearForm(obj=year)

    if form.validate_on_submit():
        try:
            # If this year is being switched from Inactive to Active
            if form.is_active.data and not year.is_active:
                # Safely deactivate all other academic calendars
                db.session.execute(
                    db.update(AcademicYear)
                    .where(AcademicYear.id != year_id)
                    .values(is_active=False)
                )
            
            # Form field data ingestion mappings
            year.name = form.name.data
            year.start_date = form.start_date.data
            year.end_date = form.end_date.data
            year.is_active = form.is_active.data
            
            # Commit transactional changes safely to the ledger
            db.session.commit()
            flash(f"Academic year '{year.name}' updated successfully.", "success")
            return redirect(url_for('academic_years'))
            
        except Exception as e:
            db.session.rollback()
            flash(f"System sync fault during save operations: {str(e)}", "danger")

    return render_template('academy_year_edit.html', form=form, year=year)
@app.route('/academic-years/reregister-students', methods=['POST'])
@login_required
def reregister_students():
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash("No active academic year found to re-register students to.", "warning")
        return redirect(url_for('academic_years'))

    # Update all students who are not in the current active year
    students = Student.query.filter(Student.academic_year_id != active_year.id).all()
    count = 0
    for student in students:
        student.academic_year_id = active_year.id
        student.registration_type = 'Returning'
        count += 1
    
    db.session.commit()
    flash(f"Successfully re-registered {count} students to {active_year.name}.", "success")
    return redirect(url_for('academic_years'))

@app.route('/academic-years/delete/<int:year_id>', methods=['POST'])
@login_required
def delete_academic_year(year_id):
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    year = AcademicYear.query.get_or_404(year_id)

    if year.is_active:
        flash("Cannot delete an active academic year. End it first.", "warning")
        return redirect(url_for('academic_years'))

    try:
        db.session.delete(year)
        db.session.commit()
        flash(f"Academic year '{year.name}' deleted successfully.", "success")
    except Exception as e:
        db.session.rollback()
        flash(f"Failed to delete academic year: {str(e)}", "danger")

    return redirect(url_for('academic_years'))

# ------------------------ STUDENT REGISTER -------------------------
def build_student_financials_business_summary(student, academic_year):
    """
    Aggregates all ledger payments for a student. Combines registration fees
    and tuition fees so they accurately show on the business management layout.
    """
    if not student or not academic_year:
        return {
            "tuition_paid": Decimal('0.00'),
            "registration_paid": Decimal('0.00'),
            "total_collected": Decimal('0.00'),
            "yearly_fee": Decimal('0.00'),
        }

    # Fetch the benchmark class fee safely
    yearly_fee = Decimal(str(get_yearly_fee_for_student(student, academic_year)))

    # Query all ledger payment documents associated with this student's ledger file
    payments = StudentPayment.query.filter_by(
        student_id=student.id,
        academic_year_id=academic_year.id
    ).all()

    # Distinguish transaction types via their ledger audit descriptions
    registration_paid = sum(
        Decimal(str(p.amount_paid)) for p in payments 
        if "registration" in (p.description or "").lower()
    )
    
    tuition_paid = sum(
        Decimal(str(p.amount_paid)) for p in payments 
        if "tuition" in (p.description or "").lower()
    )

    total_collected = sum(Decimal(str(p.amount_paid)) for p in payments)

    return {
        "tuition_paid": tuition_paid,
        "registration_paid": registration_paid,
        "total_collected": total_collected,
        "yearly_fee": yearly_fee,
    }


def get_running_business_balance():
    """Return the latest running balance from the business ledger."""
    last_tx = (
        BusinessTransaction.query.filter_by(is_deleted=False)
        .order_by(BusinessTransaction.id.desc())
        .first()
    )
    return money(last_tx.balance_after if last_tx and last_tx.balance_after is not None else 0.0)


def categorize_fee_payment(description):
    """Map a student payment description to a business income category."""
    desc = (description or "").strip().lower()
    if "registration" in desc:
        return "Registration Fees"
    if any(keyword in desc for keyword in ("tuition", "school fee", "yearly", "annual")):
        return "Tuition"
    if "uniform" in desc:
        return "Uniform"
    if "graduation" in desc:
        return "Graduation"
    return "Student Fees"


def sync_registration_fee_ledger(student, academic_year_id, fee_amount, *, update_existing=True):
    """Create or update the registration fee payment row for a student."""
    fee_amount = parse_currency_amount_optional(fee_amount)
    if not student or not academic_year_id or fee_amount <= 0:
        return fee_amount

    existing_reg_payment = (
        StudentPayment.query.filter_by(
            student_id=student.id,
            academic_year_id=academic_year_id,
            term=1,
        )
        .filter(StudentPayment.description.ilike('%registration%'))
        .first()
    )

    if existing_reg_payment:
        if not update_existing:
            return fee_amount
        old_amount = parse_currency_amount_optional(existing_reg_payment.amount_paid)
        if old_amount == fee_amount:
            return fee_amount
        existing_reg_payment.amount_paid = fee_amount
        marker = f"[SP-{existing_reg_payment.id}]"
        income_tx = BusinessTransaction.query.filter(
            BusinessTransaction.description.like(f"%{marker}%"),
            BusinessTransaction.is_deleted.is_(False),
        ).first()
        if income_tx:
            income_tx.amount = fee_amount
        return fee_amount

    record_student_payment_with_income(
        student,
        academic_year_id,
        term=1,
        amount_paid=fee_amount,
        description="Initial Registration Fee Payment (Auto-Generated)",
        installment=1,
    )
    return fee_amount


def record_student_payment_with_income(
    student,
    academic_year_id,
    term,
    amount_paid,
    description,
    *,
    installment=None,
    paid_on=None,
):
    """
    Record a student fee and mirror it as business income in one atomic ledger cycle.
    Returns the StudentPayment row, or None when amount is zero/invalid.
    """
    amount = parse_currency_amount_optional(amount_paid)
    if amount <= 0 or not student:
        return None

    academic_year = db.session.get(AcademicYear, academic_year_id) if academic_year_id else None
    year_name = academic_year.name if academic_year else None
    payment_description = (description or "Tuition Payment").strip()
    category = categorize_fee_payment(payment_description)
    paid_at = paid_on or datetime.now(timezone.utc)

    payment = StudentPayment(
        student_id=student.id,
        academic_year_id=academic_year_id,
        term=term,
        installment=installment,
        amount_paid=amount,
        description=payment_description,
        paid_on=paid_at,
    )
    db.session.add(payment)
    db.session.flush()

    student_name = student.full_name
    marker = f"[SP-{payment.id}]"
    prev_balance = get_running_business_balance()
    income_tx = BusinessTransaction(
        date=paid_at.strftime("%Y-%m-%d"),
        type="income",
        amount=amount,
        category=category,
        description=(
            f"{marker} Fee payment from {student_name} "
            f"({student.student_id}): {payment_description}"
        ),
        balance_after=parse_currency_amount_optional(prev_balance) + amount,
        academic_year=year_name,
    )
    db.session.add(income_tx)
    return payment


def backfill_student_payments_to_income_ledger():
    """Mirror any historical student payments missing from the business income ledger."""
    created = 0
    for payment in StudentPayment.query.order_by(StudentPayment.id.asc()).all():
        marker = f"[SP-{payment.id}]"
        existing = BusinessTransaction.query.filter(
            BusinessTransaction.description.like(f"%{marker}%"),
            BusinessTransaction.is_deleted.is_(False),
        ).first()
        if existing:
            continue

        student = db.session.get(Student, payment.student_id)
        if not student:
            continue

        academic_year = db.session.get(AcademicYear, payment.academic_year_id)
        amount = parse_currency_amount_optional(payment.amount_paid)
        if amount <= 0:
            continue

        paid_at = payment.paid_on or datetime.now(timezone.utc)
        category = categorize_fee_payment(payment.description)
        prev_balance = get_running_business_balance()
        income_tx = BusinessTransaction(
            date=paid_at.strftime("%Y-%m-%d"),
            type="income",
            amount=amount,
            category=category,
            description=(
                f"{marker} Fee payment from {student.full_name} "
                f"({student.student_id}): {payment.description or 'Student Fee'}"
            ),
            balance_after=parse_currency_amount_optional(prev_balance) + amount,
            academic_year=academic_year.name if academic_year else None,
        )
        db.session.add(income_tx)
        created += 1

    if created:
        db.session.commit()
    return created


def sum_business_ledger(tx_type, active_year=None):
    """Sum business ledger amounts, optionally scoped to the active academic year."""
    query = db.session.query(func.sum(BusinessTransaction.amount)).filter(
        BusinessTransaction.type == tx_type,
        BusinessTransaction.is_deleted.is_(False),
    )
    if active_year:
        query = query.filter(BusinessTransaction.academic_year == active_year.name)
    return money(query.scalar() or 0)


# =========================================================================
# BUSINESS DASHBOARD CONTEXT
# =========================================================================
def populate_business_payment_form(payment_form, active_year, years, class_id=None, student_id=None):
    """Fill payment form choices for the tuition collection workspace."""
    payment_form.academic_year.choices = [(y.id, y.name) for y in years]
    if class_id:
        students_q = Student.query.filter_by(klass_id=class_id)
        if active_year:
            students_q = students_q.filter_by(academic_year_id=active_year.id)
        students = students_q.order_by(Student.last_name, Student.first_name).all()
        payment_form.student.choices = [(s.id, f"{s.full_name} ({s.student_id})") for s in students] or [
            (0, "No students in this class")
        ]
    else:
        payment_form.student.choices = [(0, "Select a class first")]

    if request.method == "GET":
        if active_year:
            payment_form.academic_year.data = active_year.id
        if student_id:
            payment_form.student.data = student_id
        if not payment_form.description.data:
            payment_form.description.data = "Tuition Payment"
        if payment_form.term.data is None:
            payment_form.term.data = 1


def build_business_class_roster(klass_id, active_year):
    """Students in a class with tuition balances for the payment wizard."""
    if not klass_id:
        return []
    students_q = Student.query.filter_by(klass_id=klass_id)
    if active_year:
        students_q = students_q.filter_by(academic_year_id=active_year.id)
    roster = []
    for student in students_q.order_by(Student.last_name, Student.first_name).all():
        financials = build_student_financials(student, active_year)
        roster.append({
            "id": student.id,
            "full_name": student.full_name,
            "student_id": student.student_id,
            "yearly_fee": money(financials["yearly_fee"]),
            "total_paid": money(financials["total_paid"]),
            "balance": money(financials["tuition_balance"]),
            "tuition_cleared": student.tuition_cleared,
        })
    return roster


def build_business_dashboard_context(
    active_year,
    stats,
    years,
    selected_year_name,
    search_class=None,
    payment_form=None,
):
    """Build template context for the business role dashboard."""
    search_class = (search_class or request.args.get("search_class", "") or "").strip()
    active_tab = (request.args.get("tab") or "tuition").strip().lower()
    if active_tab not in {"tuition", "summary", "ledger"}:
        active_tab = "tuition"

    selected_class_id = request.args.get("class_id", type=int) or request.form.get("class_id", type=int)
    selected_student_id = request.args.get("student_id", type=int) or request.form.get("student_id", type=int)

    if payment_form is None:
        payment_form = PaymentForm()
    populate_business_payment_form(
        payment_form,
        active_year,
        years,
        class_id=selected_class_id,
        student_id=selected_student_id,
    )

    selected_student = None
    if selected_student_id:
        selected_student = db.session.get(Student, selected_student_id)
    selected_class = db.session.get(Class, selected_class_id) if selected_class_id else None
    class_roster = build_business_class_roster(selected_class_id, active_year)
    selected_student_summary = next(
        (row for row in class_roster if row["id"] == selected_student_id),
        None,
    )

    class_query = Class.query.order_by(Class.name.asc())
    if search_class:
        class_query = class_query.filter(Class.name.ilike(f"%{search_class}%"))

    classes_list = []
    for klass in class_query.all():
        student_query = Student.query.filter_by(klass_id=klass.id)
        if active_year:
            student_query = student_query.filter_by(academic_year_id=active_year.id)
        student_count = student_query.count()

        payment_query = (
            db.session.query(func.sum(StudentPayment.amount_paid))
            .join(Student, StudentPayment.student_id == Student.id)
            .filter(Student.klass_id == klass.id)
        )
        if active_year:
            payment_query = payment_query.filter(StudentPayment.academic_year_id == active_year.id)
        total_paid = payment_query.scalar() or 0
        yearly_fees = float(klass.yearly_fees or 0)
        expected = yearly_fees * student_count

        classes_list.append({
            "id": klass.id,
            "name": klass.name,
            "description": klass.stream or f"Grade {klass.grade_level}",
            "student_count": student_count,
            "total_paid": float(total_paid),
            "yearly_fees": yearly_fees,
            "balance": max(0.0, expected - float(total_paid)),
        })

    total_revenue = sum_business_ledger("income", active_year)
    total_expenses = sum_business_ledger("expense", active_year)
    fee_income_total = 0.0
    if active_year:
        fee_income_total = money(
            db.session.query(func.sum(StudentPayment.amount_paid))
            .filter_by(academic_year_id=active_year.id)
            .scalar()
            or 0
        )
    else:
        fee_income_total = money(
            db.session.query(func.sum(StudentPayment.amount_paid)).scalar() or 0
        )

    recent_payments_q = StudentPayment.query
    if active_year:
        recent_payments_q = recent_payments_q.filter_by(academic_year_id=active_year.id)
    recent_payments = []
    for payment in recent_payments_q.order_by(StudentPayment.paid_on.desc()).limit(12).all():
        student = db.session.get(Student, payment.student_id)
        recent_payments.append({
            "id": payment.id,
            "student_name": student.full_name if student else "Unknown",
            "student_code": student.student_id if student else "-",
            "amount": money(payment.amount_paid),
            "description": payment.description or "Tuition",
            "paid_on": payment.paid_on,
            "term": payment.term,
        })

    recent_query = BusinessTransaction.query.filter_by(is_deleted=False)
    if active_year:
        recent_query = recent_query.filter(BusinessTransaction.academic_year == active_year.name)

    return {
        "payment_form": payment_form,
        "active_tab": active_tab,
        "selected_class_id": selected_class_id,
        "selected_student_id": selected_student_id,
        "selected_class": selected_class,
        "selected_student": selected_student,
        "class_roster": class_roster,
        "selected_student_summary": selected_student_summary,
        "recent_payments": recent_payments,
        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "net_profit": total_revenue - total_expenses,
        "fee_income_total": fee_income_total,
        "recent_transactions": (
            recent_query.order_by(BusinessTransaction.date.desc(), BusinessTransaction.id.desc())
            .limit(10)
            .all()
        ),
        "students": Student.query.order_by(Student.last_name, Student.first_name).all(),
        "classes": classes_list,
        "search_class": search_class,
        "stats": stats,
        "counts": stats,
        "years": years,
        "selected_year": selected_year_name,
        "active_year": active_year,
    }


# =========================================================================
# 2. OPTIMIZED STUDENT REGISTRATION & LEDGER COMMIT ROUTE
# =========================================================================
def build_registrar_dashboard_context(form=None, search_class=None):
    """Shared template context for registrar dashboard and registration views."""
    class_rows = Class.query.order_by(Class.name.asc()).all()
    classes = [
        {
            'id': klass.id,
            'name': klass.name,
            'description': getattr(klass, 'description', None),
            'student_count': Student.query.filter_by(klass_id=klass.id).count(),
            'klass': klass,
        }
        for klass in class_rows
    ]
    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    active_year_id = active_year.id if active_year else None

    if form is None:
        form = RegisterStudentForm()

    form.klass.choices = [(0, '-- Select Class --')] + [(k['id'], k['name']) for k in classes]
    form.academic_year.choices = [(0, '-- Select Academic Year --')] + [(y.id, y.name) for y in years]
    suggested_student_id = generate_next_student_id(active_year)
    if request.method == 'GET':
        if active_year:
            form.academic_year.default = active_year.id
        form.student_id.default = suggested_student_id
        form.process()

    year_filter = request.args.get('year')
    students_query = Student.query
    if year_filter:
        students_query = students_query.join(AcademicYear).filter(AcademicYear.name == year_filter)
    elif active_year_id:
        students_query = students_query.filter(
            (Student.academic_year_id == active_year_id) | (Student.academic_year_id.is_(None))
        )

    page = request.args.get('page', 1, type=int)
    per_page = 100
    students_pagination = students_query.order_by(Student.id.desc()).paginate(
        page=page,
        per_page=per_page,
        error_out=False,
    )
    students = students_pagination.items

    stats = {
        'students': Student.query.filter_by(academic_year_id=active_year_id).count() if active_year_id else Student.query.count(),
        'new_students': Student.query.filter_by(registration_type='New', academic_year_id=active_year_id).count() if active_year_id else 0,
        'returning_students': Student.query.filter_by(registration_type='Returning', academic_year_id=active_year_id).count() if active_year_id else 0,
        'teachers': Teacher.query.count(),
        'classes': Class.query.count(),
        'payments': StudentPayment.query.count(),
    }

    selected_year = year_filter or (active_year.name if active_year else None)

    return {
        'form': form,
        'students': students,
        'students_pagination': students_pagination,
        'suggested_student_id': suggested_student_id,
        'classes': classes,
        'years': years,
        'active_year': active_year,
        'counts': stats,
        'stats': stats,
        'search_class': search_class or request.args.get('search_class'),
        'selected_year': selected_year,
        'selected_year_name': selected_year or (active_year.name if active_year else 'No Active Year Setup'),
    }


def apply_student_form_to_record(student, form, *, registrar_name, registration_type=None):
    """Persist RegisterStudentForm fields onto a Student record."""
    klass_id = form.klass.data or None
    academic_year_id = form.academic_year.data or None
    if not academic_year_id:
        active_year = AcademicYear.query.filter_by(is_active=True).first()
        academic_year_id = active_year.id if active_year else None

    registration_fee_value = parse_currency_amount_optional(form.registration_fees.data)

    student.first_name = form.first_name.data.strip()
    student.last_name = form.last_name.data.strip()
    student.dob = form.dob.data
    student.gender = form.gender.data
    student.parent_email = (form.parent_email.data or '').strip() or None
    student.klass_id = klass_id
    student.academic_year_id = academic_year_id
    student.level = form.level.data
    student.registrar = registrar_name
    student.registration_fees = registration_fee_value
    student.status = student.status or 'ACTIVE'

    if registration_type:
        student.registration_type = registration_type

    if klass_id:
        assigned_class = db.session.get(Class, klass_id)
        if assigned_class:
            student.grade_level = assigned_class.grade_level

    if form.student_id.data:
        student.student_id = form.student_id.data.strip()

    if student.student_id:
        student.student_id_code = student.student_id

    return registration_fee_value, academic_year_id


@app.route('/register-student', methods=['GET', 'POST'])
@login_required
def register_student():
    # 1. Authorize user permissions (case-insensitive)
    if (current_user.role or '').lower() not in {"admin", "principal", "registrar"}:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = RegisterStudentForm()
    context = build_registrar_dashboard_context(form=form)

    # 4. Handle POST requests: Process form submissions
    if request.method == 'POST':
        if not form.validate():
            flash("Please correct the highlighted errors before saving.", "danger")
            return render_template('dashboard_registrar.html', **context)

        try:
            academic_year = None
            if form.academic_year.data:
                academic_year = db.session.get(AcademicYear, form.academic_year.data)
            if not academic_year:
                academic_year = AcademicYear.query.filter_by(is_active=True).first()

            student_id_value = (form.student_id.data or '').strip()
            if not student_id_value:
                student_id_value = generate_next_student_id(academic_year)

            existing_student = Student.query.filter_by(student_id=student_id_value).first()

            if existing_student:
                if not _same_student_identity(existing_student, form):
                    flash(
                        f"Student ID '{student_id_value}' is already assigned to "
                        f"{existing_student.full_name}. The system generated a new ID for you; "
                        "please submit again or choose a different ID.",
                        "danger",
                    )
                    context['suggested_student_id'] = generate_next_student_id(academic_year)
                    context['form'].student_id.data = context['suggested_student_id']
                    return render_template('dashboard_registrar.html', **context)

                student = existing_student
                registration_fee_value, academic_year_id = apply_student_form_to_record(
                    student,
                    form,
                    registrar_name=current_user.full_name,
                    registration_type='Returning',
                )
                student.student_id = student_id_value
                flash(
                    f"Returning student {student.full_name} re-registered for "
                    f"{academic_year.name if academic_year else 'the selected academic year'} successfully.",
                    "success",
                )
            else:
                student = Student(
                    student_id=student_id_value,
                    registration_type='New',
                )
                registration_fee_value, academic_year_id = apply_student_form_to_record(
                    student,
                    form,
                    registrar_name=current_user.full_name,
                )
                student.student_id = student_id_value
                db.session.add(student)
                flash(
                    f"New student {student.first_name} {student.last_name} registered successfully "
                    f"(ID: {student_id_value}).",
                    "success",
                )

            if form.photo.data and hasattr(form.photo.data, 'filename') and form.photo.data.filename:
                photo_file = form.photo.data
                filename = secure_filename(photo_file.filename)
                timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
                filename = f"{timestamp}_{filename}"

                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'students')
                os.makedirs(upload_dir, exist_ok=True)

                file_path = os.path.join(upload_dir, filename)
                photo_file.save(file_path)

                student.photo = os.path.join('uploads', 'students', filename).replace('\\', '/')
                student.photo_filename = filename

            if form.email.data:
                portal_user = link_student_portal_from_form(
                    student,
                    form.email.data,
                    password=form.password.data or None,
                )
                if portal_user:
                    if not form.password.data:
                        flash(
                            "Student portal account linked. Default login password is: student123",
                            "info",
                        )
                else:
                    flash(
                        f"Could not link portal account for '{form.email.data}'. "
                        "That email may already belong to another student account.",
                        "warning",
                    )

            if registration_fee_value > 0 and academic_year_id:
                db.session.flush()
                sync_registration_fee_ledger(
                    student,
                    academic_year_id,
                    registration_fee_value,
                    update_existing=False,
                )

            db.session.commit()
            return redirect(url_for('dashboard'))

        except Exception as e:
            db.session.rollback()
            flash(f"Could not save student record: {str(e)}", "danger")

    return render_template('dashboard_registrar.html', **context)

@app.route('/edit-student/<int:student_id>', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    if (current_user.role or '').lower() not in {"admin", "principal", "registrar"}:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    student = Student.query.get_or_404(student_id)
    form = RegisterStudentForm(obj=student)
    context = build_registrar_dashboard_context(form=form)
    form.klass.data = student.klass_id or 0
    form.academic_year.data = student.academic_year_id or 0
    return_to = request.args.get('return_to') or request.form.get('return_to')

    if request.method == 'GET':
        form.student_id.data = student.student_id
        form.level.data = student.level
        if student.registration_fees is not None and float(student.registration_fees) > 0:
            form.registration_fees.data = f"{float(student.registration_fees):,.2f}"
        else:
            form.registration_fees.data = "0.00"
        if student.user and student.user.email:
            form.email.data = student.user.email

    if form.validate_on_submit():
        existing_student = Student.query.filter_by(student_id=form.student_id.data.strip()).first()
        if existing_student and existing_student.id != student_id:
            flash("That student ID is already assigned to another student.", "danger")
            return redirect(url_for('edit_student', student_id=student_id))

        registration_fee_value, academic_year_id = apply_student_form_to_record(
            student,
            form,
            registrar_name=current_user.full_name,
        )

        if form.photo.data and hasattr(form.photo.data, 'filename') and form.photo.data.filename:
            photo_file = form.photo.data
            filename = secure_filename(photo_file.filename)
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            filename = f"{timestamp}_{filename}"
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'students')
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, filename)
            photo_file.save(file_path)
            student.photo = os.path.join('uploads', 'students', filename).replace('\\', '/')
            student.photo_filename = filename

        if form.email.data:
            portal_user = link_student_portal_from_form(
                student,
                form.email.data,
                password=form.password.data or None,
            )
            if portal_user and not form.password.data:
                flash(
                    "Student portal account linked. Default login password is: student123",
                    "info",
                )
            elif not portal_user:
                flash(
                    f"Could not link portal account for '{form.email.data}'. "
                    "That email may already belong to another student account.",
                    "warning",
                )

        if registration_fee_value > 0 and academic_year_id:
            db.session.flush()
            sync_registration_fee_ledger(
                student,
                academic_year_id,
                registration_fee_value,
                update_existing=True,
            )

        db.session.commit()
        flash(f"Student {student.full_name} has been updated.", "success")
        if return_to == 'class_roster' and student.klass_id:
            return redirect(url_for('registrar_class_students', class_id=student.klass_id))
        return redirect(url_for('register_student'))

    return render_template(
        'edit_student.html',
        form=form,
        student=student,
        active_year=context.get('active_year'),
        return_to=return_to,
    )

# =========================================================================
# 1. CORE INSTITUTIONAL CLASS PROVISIONING MATRIX CONTROLLER
# =========================================================================
@app.route('/admin/classes/create', methods=['GET', 'POST'])
@login_required
def class_create():
    """
    Main structural controller to establish operational grade classrooms,
    load active entity ledgers, and manage systemic fee rates.
    """
    # Debugging / Auditing Node: Trace systemic credential clearance elevations
    print(f"--- [SECURITY AUDIT]: User ID {current_user.id} attempting layout access with role: '{current_user.role}' ---")

    # Access Authorization Protocol Layer
    if not current_user.role:
        flash("System Protection Fault: Your account lacks a defined systemic role configuration.", "danger")
        return redirect(url_for('dashboard'))

    # Case-Insensitive Hardened Role Verification Check
    user_role_clean = str(current_user.role).strip().lower()
    allowed_roles = ['admin', 'principal', 'registrar', 'vpa', 'lead developer']

    if user_role_clean not in allowed_roles:
        flash(f"Access Denied: Role operational capacity '{current_user.role}' lacks administrative elevation metrics.", "danger")
        return redirect(url_for('dashboard'))

    # Handle Class Submission Payloads
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        grade_level = request.form.get('grade_level')
        stream = request.form.get('stream', '').strip()
        yearly_fees = request.form.get('yearly_fees', '0.00').strip()
        room_id = request.form.get('room_id') or None

        # Validate Core System Variables
        if not name or not grade_level or not yearly_fees:
            flash("Data Integrity Warning: Class Name, Grade Level, and Yearly Tuition Rates are mandatory fields.", "warning")
            return redirect(url_for('class_create'))

        try:
            # Enforce Architectural Record Uniqueness
            existing_class = Class.query.filter(Class.name.ilike(name)).first()
            if existing_class:
                flash(f"Structural Collision: A classroom architecture named '{name}' already exists within the system matrix.", "danger")
                return redirect(url_for('class_create'))

            # Parse and Sanitize Financial Metric Arrays
            parsed_fees = parse_currency_amount_optional(yearly_fees)

            # Instantiate and Commit Core Class Node Structure
            new_class = Class(
                name=name,
                grade_level=int(grade_level),
                stream=stream if stream else None,
                yearly_fees=parsed_fees,
                room_id=int(room_id) if room_id else None
            )
            
            db.session.add(new_class)
            db.session.commit()
            
            flash(f"Operational Node: Class '{name}' has been successfully provisioned and committed!", "success")
            return redirect(url_for('class_create'))  # Redirect back to keep working seamlessly
            
        except ValueError:
            flash("Data Mutation Exception: Invalid input provided for numerical or currency metrics.", "warning")
            return redirect(url_for('class_create'))
        except Exception as e:
            db.session.rollback()
            flash(f"System Matrix Fault: Failed to establish structural class. Details: {str(e)}", "danger")
            return redirect(url_for('class_create'))

    # GET Request Processing: Query and load cross-functional entity pipelines
    # Ordered cleanly by name so your drop-down and list matrices always display professionally
    rooms = Room.query.order_by(Room.name.asc()).all()
    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    teachers = Teacher.query.filter_by(status='ACTIVE').order_by(Teacher.first_name.asc(), Teacher.last_name.asc()).all()
    return render_template(
        'class_create.html',
        rooms=rooms,
        classes=classes,
        teachers=teachers,
        sponsor_matrix=_build_class_sponsor_matrix(classes),
    )


# =========================================================================
# 2. DYNAMIC PHYSICAL ASSET ROOM INVENTORY CO-CONTROLLER
# =========================================================================
@app.route('/admin/rooms/quick-create', methods=['POST'])
@login_required
def room_quick_create():
    """
    Sub-routing endpoint to dynamically expand the campus physical room inventory
    directly on-the-fly without exiting active workflows.
    """
    # Enforce Mirrored Administrative Access Controls
    user_role_clean = str(current_user.role).strip().lower() if hasattr(current_user, 'role') else ''
    allowed_roles = ['admin', 'principal', 'registrar', 'vpa', 'lead developer']
    
    if user_role_clean not in allowed_roles:
        flash("Access Denied: Unauthorized infrastructure modification request.", "danger")
        return redirect(url_for('dashboard'))

    # Extract and Cleanse Asset Payload
    room_name = request.form.get('room_name', '').strip()
    room_number = request.form.get('room_number', '').strip()
    capacity_raw = request.form.get('capacity', '30').strip()

    if not room_name:
        flash("Data Integrity Violation: Physical room description/identifier is mandatory.", "warning")
        return redirect(url_for('class_create'))

    try:
        # Prevent input casting breakdowns with fallback metrics
        capacity = int(capacity_raw) if capacity_raw.isdigit() else 30

        # Run Verification Scans for Existing Asset Allocations
        if room_number and hasattr(Room, 'number'):
            existing_room = Room.query.filter(
                or_(Room.name.ilike(room_name), Room.number.ilike(room_number))
            ).first()
        else:
            existing_room = Room.query.filter(Room.name.ilike(room_name)).first()

        if existing_room:
            flash(f"Asset Namespace Collision: A room identifying as '{room_name}' is already indexed in infrastructure inventory.", "warning")
            return redirect(url_for('class_create'))

        # Append New Physical Space Architecture
        room_payload = {
            'name': room_name,
            'capacity': capacity,
            'current_occupancy': 0  # Defaults to clean state initialization
        }
        if room_number and hasattr(Room, 'number'):
            room_payload['number'] = room_number

        new_room = Room(**room_payload)
        
        db.session.add(new_room)
        db.session.commit()

        flash(f"Physical Asset Matrix: Space '{room_name}' successfully added to structural inventory data streams.", "success")

    except Exception as e:
        db.session.rollback()
        flash(f"System Matrix Fault: Failed to write room record mapping. Details: {str(e)}", "danger")

    # Refresh page layout state instantly to populate newly registered components
    return redirect(url_for('class_create'))

@app.route('/admin/classes/assign-teacher', methods=['POST'])
@login_required
def assign_teacher():
    """
    Handles form submission for allocating teachers to distinct class subjects
    via the ClassSubjectTeacher intermediate matrix mapping table.
    """
    # ✨ FIX 1: Grant permission to BOTH Admin and Principal roles (case-insensitive protection)
    user_role = current_user.role.lower() if current_user.role else ""
    if user_role not in ['admin', 'principal']:
        logger.warning(f"Unauthorized assignment manipulation attempt by User ID: {current_user.id} with role: {current_user.role}")
        flash("Unauthorized access: Restricted to administrative personnel.", "danger")
        return redirect(url_for('dashboard'))

    form = AssignTeacherForm()
    
    # Safely order fallback entries alphabetically by actual database string columns
    teachers = Teacher.query.order_by(Teacher.first_name.asc(), Teacher.last_name.asc()).all()
    classes = Class.query.order_by(Class.name.asc()).all()

    # Repopulate choices dynamically so form validation passes smoothly
    form.class_id.choices = [(c.id, c.name) for c in classes]
    form.teacher_id.choices = [
        (t.id, f"{(t.first_name or '').strip()} {(t.last_name or '').strip()}".strip() or (t.user.full_name if t.user else f"Teacher {t.id}"))
        for t in teachers
    ]

    if form.validate_on_submit():
        try:
            class_id = form.class_id.data
            teacher_id = form.teacher_id.data
            
            # Access the new subject field safely from your form string payload
            subject_name = form.subject_name.data.strip() if hasattr(form, 'subject_name') else request.form.get('subject_name', '').strip()

            if not subject_name:
                flash("Validation Error: Please provide a valid Subject Name.", "warning")
                return redirect(url_for('class_create'))

            # Verify entities exist using modern SQLAlchemy standards
            klass = db.session.get(Class, class_id)
            selected_teacher = db.session.get(Teacher, teacher_id)

            if not klass or not selected_teacher:
                flash("Invalid class or teacher selection.", "danger")
                return redirect(url_for('class_create'))

            # ✨ REVOLUTIONARY FIX: Check if this specific subject assignment is already registered
            existing_assignment = ClassSubjectTeacher.query.filter_by(
                class_id=class_id,
                subject_name=subject_name
            ).first()

            if existing_assignment:
                # Resolve relationship lookup cleanly
                assigned_t = existing_assignment.teacher_node if hasattr(existing_assignment, 'teacher_node') else existing_assignment.teacher
                t_name = f"{assigned_t.first_name or ''} {assigned_t.last_name or ''}".strip() if assigned_t else "Another teacher"
                flash(f"⚠️ Conflict: {t_name} is already assigned to teach '{subject_name}' to {klass.name}!", "warning")
                return redirect(url_for('class_create'))

            # ✨ CORE CHANGE: Instantiate a multi-assignment row instead of overwriting the Class table directly!
            new_assignment = ClassSubjectTeacher(
                class_id=klass.id,
                teacher_id=selected_teacher.id,
                subject_name=subject_name
            )
            
            db.session.add(new_assignment)
            db.session.commit()

            teacher_display = (
                f"{selected_teacher.first_name or ''} {selected_teacher.last_name or ''}".strip()
                or (selected_teacher.user.full_name if selected_teacher.user else f"Teacher {selected_teacher.id}")
            )

            flash(f"✨ Success! Assigned {teacher_display} to teach '{subject_name}' in room {klass.name}.", "success")
            
        except Exception as e:
            db.session.rollback()
            logger.error(f"[-] Critical system assignment error: {str(e)}", exc_info=True)
            flash(f"Database write error during assignment configuration: {str(e)}", "danger")
    else:
        # Flash form parsing errors if any matching token fields fail validation checks (e.g., missing CSRF token)
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"Form Validation Error [{field}]: {err}", "danger")

    # Dynamic target check: falls back smoothly to class management page layouts
    return redirect(url_for('class_create') if 'class_create' in current_app.view_functions else url_for('dashboard'))


def _stream_preset_for_class(klass):
    """Resolve MoE subject preset from a class stream label."""
    stream = (klass.stream or 'general').strip().lower()
    aliases = {
        'sci': 'science', 'sciences': 'science', 'science stream': 'science',
        'art': 'arts', 'arts stream': 'arts', 'humanities': 'arts',
        'commerce': 'commercial', 'business': 'commercial', 'commercial stream': 'commercial',
    }
    stream = aliases.get(stream, stream)
    return STREAM_SUBJECT_PRESETS.get(stream, STREAM_SUBJECT_PRESETS['general'])


@app.route('/admin/subjects', defaults={'class_id': None}, methods=['GET', 'POST'])
@app.route('/admin/subjects/<int:class_id>', methods=['GET', 'POST'])
@login_required
def subject_setup(class_id=None):
    """Curriculum catalog — define subjects offered per class (VPA / Principal / Admin)."""
    user_role = (current_user.role or '').lower()
    if user_role not in ['admin', 'principal', 'vpa']:
        flash('Unauthorized access.', 'danger')
        return redirect(url_for('dashboard'))

    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    selected_class = db.session.get(Class, class_id) if class_id else None

    if request.method == 'POST' and selected_class:
        action = (request.form.get('action') or '').strip()
        try:
            if action == 'add_subject':
                subject_name = (request.form.get('subject_name') or '').strip()
                if not subject_name:
                    flash('Enter a subject name.', 'warning')
                elif ClassSubject.query.filter_by(class_id=selected_class.id, subject_name=subject_name).first():
                    flash(f'"{subject_name}" is already in this class catalog.', 'warning')
                else:
                    db.session.add(ClassSubject(class_id=selected_class.id, subject_name=subject_name))
                    db.session.commit()
                    flash(f'Added "{subject_name}" to {selected_class.name}.', 'success')

            elif action == 'remove_subject':
                subject_row_id = request.form.get('subject_id', type=int)
                row = ClassSubject.query.filter_by(id=subject_row_id, class_id=selected_class.id).first()
                if row:
                    db.session.delete(row)
                    db.session.commit()
                    flash(f'Removed "{row.subject_name}" from {selected_class.name}.', 'success')

            elif action == 'apply_preset':
                preset = _stream_preset_for_class(selected_class)
                added = 0
                for name in preset:
                    if not ClassSubject.query.filter_by(class_id=selected_class.id, subject_name=name).first():
                        db.session.add(ClassSubject(class_id=selected_class.id, subject_name=name))
                        added += 1
                db.session.commit()
                flash(f'Loaded {added} preset subject(s) for {selected_class.name}.', 'success')

            elif action == 'sync_from_teachers':
                added = 0
                for alloc in ClassSubjectTeacher.query.filter_by(class_id=selected_class.id).all():
                    if not alloc.subject_name:
                        continue
                    if not ClassSubject.query.filter_by(class_id=selected_class.id, subject_name=alloc.subject_name).first():
                        db.session.add(ClassSubject(class_id=selected_class.id, subject_name=alloc.subject_name))
                        added += 1
                db.session.commit()
                flash(f'Synced {added} subject(s) from teacher assignments.', 'success')

        except Exception as e:
            db.session.rollback()
            flash(f'Could not update subject catalog: {e}', 'danger')

        return redirect(url_for('subject_setup', class_id=selected_class.id))

    class_subjects = []
    teacher_map = {}
    preset_subjects = []
    if selected_class:
        class_subjects = (
            ClassSubject.query.filter_by(class_id=selected_class.id)
            .order_by(ClassSubject.subject_name.asc())
            .all()
        )
        preset_subjects = _stream_preset_for_class(selected_class)
        for alloc in ClassSubjectTeacher.query.filter_by(class_id=selected_class.id).all():
            teacher = alloc.teacher_node if hasattr(alloc, 'teacher_node') else alloc.teacher
            if not alloc.subject_name:
                continue
            label = (
                f"{(teacher.first_name or '').strip()} {(teacher.last_name or '').strip()}".strip()
                if teacher
                else 'Assigned teacher'
            )
            teacher_map.setdefault(alloc.subject_name, [])
            if label not in teacher_map[alloc.subject_name]:
                teacher_map[alloc.subject_name].append(label)

    return render_template(
        'subject_setup.html',
        classes=classes,
        selected_class=selected_class,
        class_subjects=class_subjects,
        teacher_map=teacher_map,
        preset_subjects=preset_subjects,
    )


@app.route('/announcements', methods=['GET', 'POST'])
@login_required
def announcements():
    # 1. Secure case-insensitive executive gatekeeping
    if current_user.role.lower() not in ["admin", "teacher", "principal", "vpa"]:
        flash("Unauthorized access to communications management.", "danger")
        return redirect(url_for('dashboard'))

    # Explicit local import from your clean forms.py file
    from forms import AnnouncementForm
    form = AnnouncementForm()
    
    # Modernized query execution syntax
    items = Announcement.query.order_by(Announcement.id.desc()).all()

    if form.validate_on_submit():
        try:
            # ✨ Smart AI Scan: Automatically catch business classification from the title string
            title_text = form.title.data.lower()
            announcement_category = 'general'
            
            if "deadline" in title_text or "due" in title_text:
                announcement_category = 'deadline'
                flash("⏰ Academic submission deadline posted successfully.", "info")
            elif "warning" in title_text or "alert" in title_text:
                announcement_category = 'warning'
                flash("⚠️ Administrative compliance warning broadcasted.", "warning")
            elif any(word in title_text for word in ["fee", "payment", "sponsor", "business", "tuition"]):
                announcement_category = 'business_finance'
                flash("💼 Business/Financial notification published to the ledger.", "success")
            else:
                flash("General school announcement posted successfully.", "success")

            model_args = {
                "title": (form.title.data or "").strip(),
                "content": (form.content.data or "").strip(),
                "target_role": form.target_audience.data,
                "author": (current_user.full_name or current_user.email or "System").strip(),
                "category": announcement_category,
            }

            announcement = Announcement(**model_args)
            
            db.session.add(announcement)
            db.session.commit()
            
            return redirect(url_for('announcements'))

        except Exception as e:
            db.session.rollback()
            flash(f"System failure processing broadcast parameters: {str(e)}", "danger")
            return redirect(url_for('announcements'))

    # ✨ FIX: Catch any WTForms validation errors (like missing inputs) and display them
    elif request.method == 'POST':
        for field, errors in form.errors.items():
            for err in errors:
                field_label = getattr(form, field).label.text if hasattr(form, field) else field
                flash(f"{field_label}: {err}", "danger")

    return render_template('announcements.html', form=form, items=items)

# ---------------------- BUSINESS MANAGEMENT -----------------------
@app.route('/business-management', methods=['GET', 'POST'])
@role_required('VPI', 'business', 'admin', 'principal')  # Enforce administrative access permissions
def business_management():
    from forms import TransactionForm, EnrollmentForm, PaymentForm
    from models import Student, AcademicYear, Class, BusinessTransaction, StudentPayment, SchoolFee
    
    # 1. Initialize Forms
    form = TransactionForm()
    enroll_form = EnrollmentForm()
    payment_form = PaymentForm()
    
    # Populate dropdown choices dynamically
    payment_form.student.choices = [(s.id, f"{s.first_name} {s.last_name} ({s.student_id})") for s in Student.query.order_by(Student.last_name).all()]
    all_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    payment_form.academic_year.choices = [(y.id, y.name) for y in all_years]
    
    # Pre-fill data if provided via search query parameters
    prefill_student_id = request.args.get('student_id', type=int)
    prefill_year_id = request.args.get('year_id', type=int)
    
    if request.method == 'GET':
        if prefill_student_id:
            payment_form.student.data = prefill_student_id
        if prefill_year_id:
            payment_form.academic_year.data = prefill_year_id
        elif globals().get('active_year'):
            payment_form.academic_year.data = active_year.id
    
    selected_year = request.args.get('year', all_years[0].name if all_years else '2025-2026')
    selected_year_obj = next((y for y in all_years if y.name == selected_year), None)
    
    # 2. Handle Daily Expense/Income Transaction Posting
    if 'submit_transaction' in request.form:
        if form.validate_on_submit():
            try:
                # Fetch last balance metric to compute the balance_after runner delta
                last_tx = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.id.desc()).first()
                prev_balance = last_tx.balance_after if last_tx and last_tx.balance_after else 0.0
                
                amount = parse_currency_amount(form.amount.data)
                prev_balance_decimal = parse_currency_amount_optional(prev_balance)
                new_balance = (
                    prev_balance_decimal + amount
                    if form.type.data == 'income'
                    else prev_balance_decimal - amount
                )

                new_tx = BusinessTransaction(
                    date=form.date.data,
                    type=form.type.data,
                    amount=amount,
                    category=form.category.data,
                    description=form.description.data,
                    balance_after=new_balance,  # ✨ Aligned column setup
                    academic_year=selected_year
                )
                db.session.add(new_tx)
                db.session.commit()
                flash('Transaction recorded successfully!', 'success')
                return redirect(url_for('business_management', year=selected_year))
            except Exception as e:
                db.session.rollback()
                flash(f"System failure processing ledger transaction: {str(e)}", "danger")
        else:
            for field, errors in form.errors.items():
                for err in errors:
                    flash(f"Transaction Field ({field}): {err}", "danger")

    # 3. Handle Student Payment Posting Pipeline
    if 'submit_payment' in request.form:
        if payment_form.validate_on_submit():
            try:
                payment_description = (payment_form.description.data or "Tuition").strip()
                student = db.session.get(Student, payment_form.student.data)

                if not student:
                    flash("Student extraction entity record not found.", "danger")
                    return redirect(url_for('business_management', year=selected_year))

                record_student_payment_with_income(
                    student,
                    payment_form.academic_year.data,
                    payment_form.term.data,
                    parse_currency_amount(payment_form.amount_paid.data),
                    payment_description,
                    installment=payment_form.installment.data,
                )
                db.session.commit()
                flash('Student payment recorded and posted to business income.', 'success')
                return redirect(url_for('business_management', year=selected_year))
            except Exception as e:
                db.session.rollback()
                flash(f"System failure recording transaction payment configuration: {str(e)}", "danger")
        else:
            for field, errors in payment_form.errors.items():
                for err in errors:
                    flash(f"Payment Field ({field}): {err}", "danger")

    # 4. Calculate Global Financial KPIs
    income_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(
        BusinessTransaction.type == 'income', BusinessTransaction.academic_year == selected_year, BusinessTransaction.is_deleted == False
    ).scalar() or 0
    
    expense_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(
        BusinessTransaction.type == 'expense', BusinessTransaction.academic_year == selected_year, BusinessTransaction.is_deleted == False
    ).scalar() or 0

    # 5. Institutional Analytics (Class-by-Class Financial Health)
    classes = Class.query.all()
    class_analytics = []
    
    fee_obj = SchoolFee.query.filter_by(academic_year_id=selected_year_obj.id).first() if selected_year_obj else None
    yearly_fee_default = fee_obj.amount if fee_obj else 0

    for k in classes:
        current_fee = k.yearly_fees if k.yearly_fees and k.yearly_fees > 0 else yearly_fee_default
        
        students_query = Student.query.filter_by(klass_id=k.id, status='ACTIVE')
        if selected_year_obj:
            students_query = students_query.filter(Student.academic_year_id == selected_year_obj.id)
        students_in_class = students_query.all()
        student_count = len(students_in_class)
        total_expected = student_count * current_fee
        total_collected = sum(
            build_student_financials_business_summary(student, selected_year_obj)["total_collected"]
            for student in students_in_class
        )
        
        class_analytics.append({
            'name': k.name,
            'students_count': student_count,
            'yearly_fee': money(current_fee),
            'total_collected': money(total_collected),
            'balance': money(total_expected - total_collected)
        })

    # 6. Search Logic for Student Payments
    student_search = request.args.get('student_search', '')
    search_results = []
    if student_search:
        search_results = Student.query.filter(
            (Student.first_name.contains(student_search)) | 
            (Student.last_name.contains(student_search)) |
            (Student.student_id == student_search)
        ).all()

    transactions = BusinessTransaction.query.filter_by(
        academic_year=selected_year, 
        is_deleted=False
    ).order_by(BusinessTransaction.date.desc()).all()
    
    return render_template(
        'business_management.html',
        form=form,
        enroll_form=enroll_form,
        payment_form=payment_form,
        income_total=income_total,
        expense_total=expense_total,
        class_search_results=class_analytics,
        search_results=search_results,
        transactions=transactions,
        selected_year=selected_year,
        years=all_years
    )

@app.route('/delete-transaction/<int:id>', methods=['POST'])
@role_required('VPI', 'business', 'admin')
def soft_delete_transaction(id):
    tx = BusinessTransaction.query.get_or_404(id)
    # Logic: Mark as deleted instead of removing from DB
    tx.is_deleted = True
    tx.deleted_at = datetime.now(timezone.utc)
    tx.deleted_by_id = current_user.id
    # Log this for your Cybersecurity Audit
    log_security_event(f"Transaction ID {id} was soft-deleted by {current_user.username or current_user.full_name}")
    db.session.commit()
    flash('Transaction removed from view. Audit log updated.', 'info')
    return redirect(url_for('business_management'))

@app.route('/business/daily-expenses')
@role_required('VPI', 'business', 'admin')
def daily_expense_report():
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    expenses = BusinessTransaction.query.filter_by(
        date=today,
        type='expense',
        is_deleted=False
    ).all()
    total = sum(e.amount for e in expenses)
    return render_template('daily_expense_report.html', expenses=expenses, total=total, date=today)

@app.route('/business-overview')
@login_required
def business_overview():
    if (current_user.role or '').strip().lower() not in {"admin", "business", "principal", "vpi"}:
        flash("Unauthorized access to business overview.", "danger")
        return redirect(url_for('dashboard'))

    income_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(BusinessTransaction.type == "income", BusinessTransaction.is_deleted == False).scalar() or 0
    expense_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(BusinessTransaction.type == "expense", BusinessTransaction.is_deleted == False).scalar() or 0
    net_total = income_total - expense_total

    recent_transactions = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(5).all()

    return render_template(
        'business_overview.html',
        income_total=income_total,
        expense_total=expense_total,
        net_total=net_total,
        recent_transactions=recent_transactions
    )

@app.route('/payroll-summary')
@login_required
def payroll_summary():
    if normalize_role(current_user) not in {'admin', 'business'}:
        flash("Unauthorized access to payroll summary.", "danger")
        return redirect(url_for('dashboard'))

    payroll_records = Payroll.query.order_by(Payroll.created_on.desc()).all()
    total_paid = sum(record.salary_amount for record in payroll_records if record.paid)
    total_pending = sum(record.salary_amount for record in payroll_records if not record.paid)

    return render_template(
        'payroll_summary.html',
        payroll_records=payroll_records,
        total_paid=total_paid,
        total_pending=total_pending
    )

@app.route('/financial-reports')
@login_required
def financial_reports():
    if (current_user.role or '').strip().lower() not in {"admin", "business", "vpi", "principal"}:
        flash("Unauthorized access to financial reports.", "danger")
        return redirect(url_for('dashboard'))

    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    return render_template(
        'financial_reports.html',
        years=years
    )

def _principal_student_average(student, active_year=None):
    grade_query = Grade.query.filter_by(student_id=student.id)
    if active_year:
        grade_query = grade_query.filter_by(academic_year_id=active_year.id)
    grades = grade_query.all()
    scored = [g.score for g in grades if g.score]
    if not scored:
        return 0
    return round(sum(scored) / len(scored), 1)


def _principal_students_for_active_year(students, active_year=None):
    if not active_year:
        return list(students)
    return [
        student for student in students
        if not student.academic_year_id or student.academic_year_id == active_year.id
    ]


def _principal_summarize_students(students, active_year=None):
    scoped_students = _principal_students_for_active_year(students, active_year)
    optimal_count = 0
    at_risk_count = 0
    suspended_count = 0
    for student in scoped_students:
        average = _principal_student_average(student, active_year)
        status = (student.status or 'ACTIVE').upper()
        if status == 'SUSPENDED':
            suspended_count += 1
        elif average < 70:
            at_risk_count += 1
        else:
            optimal_count += 1
    total = len(scoped_students)
    health_pct = round((optimal_count / total) * 100) if total else 100
    return {
        'student_count': total,
        'optimal_count': optimal_count,
        'at_risk_count': at_risk_count,
        'suspended_count': suspended_count,
        'health_pct': health_pct,
    }


def _principal_students_for_class(klass, active_year=None):
    students = get_students_for_class_ids([klass.id])
    return _principal_students_for_active_year(students, active_year)


def _principal_unallocated_students(active_year=None):
    students = (
        Student.query.filter(Student.klass_id.is_(None))
        .order_by(Student.last_name.asc(), Student.first_name.asc())
        .all()
    )
    return _principal_students_for_active_year(students, active_year)


def _principal_build_class_portfolios(active_year=None, search_class=''):
    portfolios = []
    search_class = (search_class or '').strip().lower()
    for klass in Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all():
        if search_class:
            haystack = ' '.join(filter(None, [
                klass.name,
                klass.stream,
                str(klass.grade_level),
            ])).lower()
            if search_class not in haystack:
                continue
        summary = _principal_summarize_students(
            get_students_for_class_ids([klass.id]),
            active_year,
        )
        portfolios.append({
            'klass': klass,
            **summary,
        })
    return portfolios


def _principal_build_unallocated_portfolio(active_year=None):
    students = _principal_unallocated_students(active_year)
    if not students:
        return None
    return _principal_summarize_students(students, active_year)


def _principal_filter_students(students, search_query='', status_filter='', active_year=None):
    filtered = []
    search_query = (search_query or '').strip().lower()
    for student in students:
        average = _principal_student_average(student, active_year)
        student.average = average
        status = (student.status or 'ACTIVE').upper()
        if search_query:
            haystack = ' '.join(filter(None, [
                student.first_name,
                student.last_name,
                student.student_id,
                student.full_name,
            ])).lower()
            if search_query not in haystack:
                continue
        if status_filter == 'failing' and average >= 70:
            continue
        if status_filter == 'suspended' and status != 'SUSPENDED':
            continue
        filtered.append(student)
    return filtered


# -------------------------- PAYROLL -------------------------------
@app.route('/principal/dashboard')
@role_required('Principal') # Ensure only the Principal can see this
def principal_dashboard():
    from datetime import timedelta
    # Get active academic year
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    # 1. Financial Stats (VPI Data)
    total_revenue = db.session.query(db.func.sum(BusinessTransaction.amount)).filter_by(type='income', is_deleted=False).scalar() or 0
    total_expenses = db.session.query(db.func.sum(BusinessTransaction.amount)).filter_by(type='expense', is_deleted=False).scalar() or 0
    financial_stats = {
        "total_revenue": total_revenue,
        "total_expenses": total_expenses,
        "net_profit": total_revenue - total_expenses,
        "total_transactions": BusinessTransaction.query.filter_by(is_deleted=False).count()
    }
    # 2. Academic Stats (VPA Data - Liberian 70% Standard)
    all_students = Student.query.all()
    total_student_count = len(all_students)
    
    failing_students = [
        student for student in all_students
        if _principal_student_average(student, active_year) < 70
    ]
    
    academic_stats = {
        "passing_rate": round(((total_student_count - len(failing_students)) / total_student_count * 100), 1) if total_student_count > 0 else 0,
        "failing_count": len(failing_students)
    }
    # 3. Disciplinary Stats (Dean Data)
    active_suspensions = Suspension.query.filter(Suspension.return_date > datetime.now(timezone.utc)).count()
    disciplinary_stats = {
        "active_suspensions": active_suspensions
    }
    # 4. Security Stats (Admin Data)
    # Count unique IPs blocked for brute force in last 24 hours
    yesterday = datetime.now(timezone.utc) - timedelta(days=1)
    blocked_ips = SecurityLog.query.filter(SecurityLog.timestamp > yesterday, SecurityLog.event.contains('BLOCKED_IP')).count()
    security_stats = {
        "blocked_ips": blocked_ips
    }
    # 5. Active Student Condition Ledger (class folders + drill-down)
    search_query = request.args.get('search', '')
    status_filter = request.args.get('status', '')
    search_class = request.args.get('search_class', '')
    class_id = request.args.get('class_id', type=int)
    selected_folder = (request.args.get('folder') or '').strip().lower()
    selected_class = db.session.get(Class, class_id) if class_id else None

    class_portfolios = _principal_build_class_portfolios(active_year, search_class)
    unallocated_portfolio = _principal_build_unallocated_portfolio(active_year)

    students_list = []
    selected_class_stats = None
    if selected_folder == 'unallocated':
        roster = _principal_unallocated_students(active_year)
        students_list = _principal_filter_students(
            roster, search_query, status_filter, active_year
        )
        selected_class_stats = _principal_summarize_students(roster, active_year)
    elif selected_class:
        roster = _principal_students_for_class(selected_class, active_year)
        students_list = _principal_filter_students(
            roster, search_query, status_filter, active_year
        )
        selected_class_stats = _principal_summarize_students(roster, active_year)

    # 6. Recent Activity Feeds
    recent_transactions = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(5).all()
    security_events = SecurityLog.query.order_by(SecurityLog.timestamp.desc()).limit(5).all()
    
    return render_template('principal_dashboard.html', 
                            financial_stats=financial_stats,
                           academic_stats=academic_stats,
                           disciplinary_stats=disciplinary_stats,
                           security_stats=security_stats,
                           students=students_list,
                           class_portfolios=class_portfolios,
                           unallocated_portfolio=unallocated_portfolio,
                           selected_class=selected_class,
                           selected_folder=selected_folder if selected_folder == 'unallocated' else '',
                           selected_class_stats=selected_class_stats,
                           class_id=class_id,
                           search_class=search_class,
                           recent_transactions=recent_transactions,
                           security_events=security_events,
                           current_user=current_user,
                           active_year=active_year)

# -------------------------- VPI DASHBOARD -------------------------------
def _vpi_year_tx_query(selected_year_name):
    return BusinessTransaction.query.filter(
        BusinessTransaction.is_deleted == False,
        BusinessTransaction.academic_year == selected_year_name,
    )


def _vpi_class_collection_snapshots(active_year):
    snapshots = []
    fee_obj = SchoolFee.query.filter_by(academic_year_id=active_year.id).first() if active_year else None
    yearly_fee_default = fee_obj.amount if fee_obj else 0

    for klass in Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all():
        current_fee = klass.yearly_fees if klass.yearly_fees and klass.yearly_fees > 0 else yearly_fee_default
        students_query = Student.query.filter_by(klass_id=klass.id, status='ACTIVE')
        if active_year:
            students_query = students_query.filter(Student.academic_year_id == active_year.id)
        students = students_query.all()
        collected = sum(
            Decimal(str(build_student_financials_business_summary(student, active_year)["total_collected"]))
            for student in students
        )
        expected = Decimal(str(current_fee)) * len(students)
        balance = max(Decimal('0'), expected - collected)
        rate = round(float(collected / expected * 100), 1) if expected > 0 else 0.0
        snapshots.append({
            'klass': klass,
            'student_count': len(students),
            'expected': money(expected),
            'collected': money(collected),
            'balance': money(balance),
            'collection_rate': rate,
        })
    return snapshots


def _vpi_outstanding_students(active_year, limit=10):
    rows = []
    if not active_year:
        return rows
    for student in Student.query.filter_by(academic_year_id=active_year.id, status='ACTIVE').all():
        fin = build_student_financials(student, active_year)
        balance = Decimal(str(fin.get('tuition_balance', 0) or 0))
        if balance > 0:
            rows.append({
                'student': student,
                'yearly_fee': money(fin.get('yearly_fee', 0)),
                'total_paid': money(fin.get('total_paid', 0)),
                'balance': money(balance),
            })
    rows.sort(key=lambda row: row['balance'], reverse=True)
    return rows[:limit]


@app.route('/vpi/dashboard')
@login_required
@role_required('VPI', 'business', 'admin')
def vpi_dashboard():
    """VPI / Business Officer — tuition, ledger, collections, and fiscal oversight."""
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    all_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    selected_year_name = request.args.get('year') or (active_year.name if active_year else (all_years[0].name if all_years else ''))
    selected_year = next((y for y in all_years if y.name == selected_year_name), active_year)

    year_filter = _vpi_year_tx_query(selected_year_name) if selected_year_name else BusinessTransaction.query.filter_by(is_deleted=False)

    def _year_ledger_sum(tx_type):
        query = db.session.query(func.sum(BusinessTransaction.amount)).filter(
            BusinessTransaction.is_deleted == False,
            BusinessTransaction.type == tx_type,
        )
        if selected_year_name:
            query = query.filter(BusinessTransaction.academic_year == selected_year_name)
        return query.scalar() or 0

    total_revenue = _year_ledger_sum('income')
    total_expenses = _year_ledger_sum('expense')
    net_profit = float(total_revenue) - float(total_expenses)

    tuition_collected = Decimal('0')
    total_expected = Decimal('0')
    students_with_balance = 0
    if selected_year:
        for student in Student.query.filter_by(academic_year_id=selected_year.id, status='ACTIVE').all():
            fin = build_student_financials(student, selected_year)
            tuition_collected += Decimal(str(fin.get('total_paid', 0) or 0))
            yearly_fee = Decimal(str(fin.get('yearly_fee', 0) or 0))
            total_expected += yearly_fee
            if Decimal(str(fin.get('tuition_balance', 0) or 0)) > 0:
                students_with_balance += 1

    collection_rate = round(float(tuition_collected / total_expected * 100), 1) if total_expected > 0 else 0.0
    outstanding_total = money(max(Decimal('0'), total_expected - tuition_collected))

    today_str = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    today_income = (
        db.session.query(func.sum(BusinessTransaction.amount))
        .filter(
            BusinessTransaction.is_deleted == False,
            BusinessTransaction.type == 'income',
            BusinessTransaction.date == today_str,
        )
        .scalar()
        or 0
    )
    today_expenses = (
        db.session.query(func.sum(BusinessTransaction.amount))
        .filter(
            BusinessTransaction.is_deleted == False,
            BusinessTransaction.type == 'expense',
            BusinessTransaction.date == today_str,
        )
        .scalar()
        or 0
    )

    recent_transactions = (
        year_filter.order_by(BusinessTransaction.date.desc()).limit(10).all()
        if selected_year_name else
        BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(10).all()
    )
    recent_payments = StudentPayment.query.order_by(StudentPayment.paid_on.desc()).limit(8).all()

    income_q = db.session.query(
        BusinessTransaction.category, func.sum(BusinessTransaction.amount)
    ).filter(BusinessTransaction.is_deleted == False, BusinessTransaction.type == 'income')
    expense_q = db.session.query(
        BusinessTransaction.category, func.sum(BusinessTransaction.amount)
    ).filter(BusinessTransaction.is_deleted == False, BusinessTransaction.type == 'expense')
    if selected_year_name:
        income_q = income_q.filter(BusinessTransaction.academic_year == selected_year_name)
        expense_q = expense_q.filter(BusinessTransaction.academic_year == selected_year_name)
    income_categories = income_q.group_by(BusinessTransaction.category).all()
    expense_categories = expense_q.group_by(BusinessTransaction.category).all()

    stats = {
        'total_revenue': money(total_revenue),
        'total_expenses': money(total_expenses),
        'net_profit': money(net_profit),
        'tuition_collected': money(tuition_collected),
        'outstanding_total': outstanding_total,
        'collection_rate': collection_rate,
        'students_with_balance': students_with_balance,
        'transaction_count': year_filter.count() if selected_year_name else BusinessTransaction.query.filter_by(is_deleted=False).count(),
        'today_income': money(today_income),
        'today_expenses': money(today_expenses),
        'ledger_balance': money(get_running_business_balance()),
        'active_students': Student.query.filter_by(academic_year_id=selected_year.id, status='ACTIVE').count() if selected_year else Student.query.filter_by(status='ACTIVE').count(),
    }

    return render_template(
        'vpi_dashboard.html',
        current_user=current_user,
        active_year=active_year,
        selected_year=selected_year,
        selected_year_name=selected_year_name,
        years=all_years,
        stats=stats,
        class_snapshots=_vpi_class_collection_snapshots(selected_year),
        outstanding_students=_vpi_outstanding_students(selected_year),
        recent_transactions=recent_transactions,
        recent_payments=recent_payments,
        income_categories=income_categories,
        expense_categories=expense_categories,
        total_revenue=stats['total_revenue'],
        total_expenses=stats['total_expenses'],
        net_profit=stats['net_profit'],
    )

# -------------------------- DEAN DASHBOARD -------------------------------
def _dean_student_has_active_suspension(student, current_time=None):
    current_time = current_time or datetime.now(timezone.utc)
    if (student.status or '').upper() == 'SUSPENDED':
        return True
    return Suspension.query.filter(
        Suspension.student_id == student.id,
        Suspension.return_date > current_time,
    ).count() > 0


def _dean_build_class_snapshots():
    snapshots = []
    for klass in Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all():
        students = Student.query.filter_by(klass_id=klass.id).all()
        incident_count = (
            db.session.query(db.func.count(Discipline.id))
            .join(Student, Student.id == Discipline.student_id)
            .filter(Student.klass_id == klass.id)
            .scalar()
            or 0
        )
        snapshots.append({
            'klass': klass,
            'student_count': len(students),
            'suspended_count': sum(1 for s in students if (s.status or '').upper() == 'SUSPENDED'),
            'incident_count': incident_count,
        })
    return snapshots


@app.route('/dean/dashboard', methods=['GET'])
@login_required
@role_required('Dean')
def dean_dashboard():
    """Dean of Students — conduct, welfare, attendance, and campus oversight."""
    current_time = datetime.now(timezone.utc)
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    class_id = request.args.get('class_id', type=int)
    search_q = (request.args.get('q') or '').strip()

    rooms_list = Room.query.order_by(Room.name.asc()).all()
    classes = Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all()
    selected_class = db.session.get(Class, class_id) if class_id else None

    student_query = Student.query
    if class_id:
        student_query = student_query.filter_by(klass_id=class_id)
    if search_q:
        like = f'%{search_q}%'
        student_query = student_query.filter(
            Student.first_name.ilike(like)
            | Student.last_name.ilike(like)
            | Student.student_id.ilike(like)
        )
    students = student_query.order_by(Student.last_name.asc(), Student.first_name.asc()).all()

    incident_counts = dict(
        db.session.query(Discipline.student_id, db.func.count(Discipline.id))
        .group_by(Discipline.student_id)
        .all()
    )
    for student in students:
        student.incident_count = incident_counts.get(student.id, 0)
        student.has_active_suspension = _dean_student_has_active_suspension(student, current_time)

    month_start = current_time.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    today_str = current_time.strftime('%Y-%m-%d')
    active_suspensions = Suspension.query.filter(Suspension.return_date > current_time).count()
    total_suspensions = Suspension.query.count()
    incidents_this_month = Discipline.query.filter(Discipline.created_at >= month_start).count()
    suspended_students = Student.query.filter(func.upper(Student.status) == 'SUSPENDED').count()
    today_absences = Attendance.query.filter_by(date=today_str, status='absent').count()
    today_late = Attendance.query.filter_by(date=today_str, status='late').count()
    rooms_at_capacity = sum(1 for room in rooms_list if room.capacity and room.current_occupancy >= room.capacity)

    repeat_offenders = (
        db.session.query(Student, db.func.count(Discipline.id).label('incident_count'))
        .join(Discipline, Student.id == Discipline.student_id)
        .group_by(Student.id)
        .having(db.func.count(Discipline.id) >= 2)
        .order_by(db.func.count(Discipline.id).desc())
        .limit(8)
        .all()
    )
    at_risk_count = len(repeat_offenders)

    recent_suspensions = (
        Suspension.query.order_by(Suspension.id.desc()).limit(8).all()
    )
    discipline_incidents = (
        Discipline.query.order_by(Discipline.created_at.desc()).limit(8).all()
    )

    stats = {
        'active_suspensions': active_suspensions,
        'total_suspensions': total_suspensions,
        'incidents_this_month': incidents_this_month,
        'total_incidents': Discipline.query.count(),
        'suspended_students': suspended_students,
        'at_risk_students': at_risk_count,
        'today_absences': today_absences,
        'today_late': today_late,
        'total_students_enrolled': Student.query.count(),
        'total_monitored_rooms': len(rooms_list),
        'rooms_at_capacity': rooms_at_capacity,
    }

    return render_template(
        'dean_dashboard.html',
        current_user=current_user,
        current_time=current_time,
        active_year=active_year,
        rooms_list=rooms_list,
        classes=classes,
        selected_class=selected_class,
        class_id=class_id,
        search_q=search_q,
        students=students,
        class_snapshots=_dean_build_class_snapshots(),
        active_suspensions=active_suspensions,
        total_suspensions=total_suspensions,
        recent_suspensions=recent_suspensions,
        discipline_incidents=discipline_incidents,
        repeat_offenders=repeat_offenders,
        stats=stats,
        form=FlaskForm(),
        discipline_form=DisciplineForm(),
    )


@app.route('/dean/incident/process', methods=['POST'])
@login_required
@role_required('Dean')
def process_discipline_incident():
    student_id = request.form.get('student_id', type=int)
    offense = (request.form.get('offense') or '').strip()
    action_taken = (request.form.get('action_taken') or '').strip() or 'Logged for dean review'
    notes = (request.form.get('notes') or '').strip()
    class_id = request.form.get('return_class_id', type=int)
    search_q = (request.form.get('return_q') or '').strip()

    if not student_id or not offense:
        flash('Student and offense description are required.', 'danger')
        return redirect(url_for('dean_dashboard', class_id=class_id, q=search_q or None))

    student = db.session.get(Student, student_id)
    if not student:
        flash('Student record not found.', 'danger')
        return redirect(url_for('dean_dashboard'))

    incident = Discipline(
        student_id=student_id,
        offense=offense,
        action_taken=action_taken,
        notes=notes or None,
        logged_by_id=current_user.id,
    )
    db.session.add(incident)
    db.session.commit()
    flash(f'Conduct incident logged for {student.full_name}.', 'success')
    return redirect(url_for('dean_dashboard', class_id=class_id, q=search_q or None))


@app.route('/dean/suspension/process', methods=['POST'])
@login_required
@role_required('Dean')
def process_suspension():
    student_id = request.form.get('student_id', type=int)
    reason = (request.form.get('reason') or '').strip()
    start_date_str = request.form.get('start_date')
    return_date_str = request.form.get('return_date')
    class_id = request.form.get('return_class_id', type=int)
    search_q = (request.form.get('return_q') or '').strip()

    if not all([student_id, reason, start_date_str, return_date_str]):
        flash('All suspension fields are required.', 'danger')
        return redirect(url_for('dean_dashboard', class_id=class_id, q=search_q or None))

    try:
        start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
        return_day = datetime.strptime(return_date_str, '%Y-%m-%d').date()
        return_dt = datetime.combine(return_day, datetime.max.time(), tzinfo=timezone.utc)

        student = db.session.get(Student, student_id)
        if not student:
            flash('Student record not found.', 'danger')
            return redirect(url_for('dean_dashboard'))

        new_sanction = Suspension(
            student_id=student_id,
            reason=reason,
            start_date=start_date,
            return_date=return_dt,
        )
        student.status = 'SUSPENDED'
        db.session.add(new_sanction)
        db.session.add(Discipline(
            student_id=student_id,
            offense=f'Suspension: {reason}',
            action_taken=f'Suspended until {return_date_str}',
            logged_by_id=current_user.id,
        ))
        db.session.commit()
        flash('Suspension recorded and parent notification letter is ready to print.', 'success')
    except Exception as e:
        db.session.rollback()
        app.logger.error(f"Failed to record suspension: {e}")
        flash(f'Could not save suspension: {e}', 'danger')

    return redirect(url_for('dean_dashboard', class_id=class_id, q=search_q or None))

# -------------------------- VPA DASHBOARD -------------------------------
def _vpa_student_average(student, academic_year=None):
    """MoE student standing — mean of all entered grade scores for the year."""
    query = Grade.query.filter_by(student_id=student.id)
    if academic_year:
        query = query.filter_by(academic_year_id=academic_year.id)
    scores = [g.score for g in query.all() if g.score is not None]
    if not scores:
        return None
    return round(sum(scores) / len(scores), 1)


def _vpa_year_grade_query(academic_year):
    query = Grade.query
    if academic_year:
        query = query.filter_by(academic_year_id=academic_year.id)
    return query


def _vpa_year_assessment_query(academic_year):
    query = Assessment.query
    if academic_year:
        query = query.filter(
            or_(Assessment.academic_year_id == academic_year.id, Assessment.academic_year_id.is_(None))
        )
    return query


def _vpa_build_class_snapshots(academic_year):
    snapshots = []
    for klass in Class.query.order_by(Class.grade_level.asc(), Class.name.asc()).all():
        students_query = Student.query.filter_by(klass_id=klass.id, status='ACTIVE')
        if academic_year:
            students_query = students_query.filter(Student.academic_year_id == academic_year.id)
        students = students_query.all()

        subject_names = {
            row.subject_name
            for row in ClassSubject.query.filter_by(class_id=klass.id).all()
            if row.subject_name
        }
        for alloc in ClassSubjectTeacher.query.filter_by(class_id=klass.id).all():
            if alloc.subject_name:
                subject_names.add(alloc.subject_name)

        grade_count = _vpa_year_grade_query(academic_year).filter_by(class_id=klass.id).count()
        assessment_count = _vpa_year_assessment_query(academic_year).filter_by(klass_id=klass.id).count()

        averages = [_vpa_student_average(s, academic_year) for s in students]
        valid_avgs = [avg for avg in averages if avg is not None]
        passing = sum(1 for avg in valid_avgs if avg >= MOE_PASSING_SCORE)
        failing = sum(1 for avg in valid_avgs if avg < MOE_PASSING_SCORE)
        class_avg = round(sum(valid_avgs) / len(valid_avgs), 1) if valid_avgs else None
        passing_rate = round(passing / len(valid_avgs) * 100, 1) if valid_avgs else 0.0

        snapshots.append({
            'klass': klass,
            'student_count': len(students),
            'subject_count': len(subject_names),
            'grade_count': grade_count,
            'assessment_count': assessment_count,
            'class_avg': class_avg,
            'passing_rate': passing_rate,
            'failing_count': failing,
        })
    return snapshots


def _vpa_performance_bands(academic_year):
    students = Student.query.filter_by(status='ACTIVE')
    if academic_year:
        students = students.filter_by(academic_year_id=academic_year.id)
    bands = {'excellent': 0, 'good': 0, 'average': 0, 'needs_improvement': 0, 'no_grades': 0}
    for student in students.all():
        avg = _vpa_student_average(student, academic_year)
        if avg is None:
            bands['no_grades'] += 1
        elif avg >= 90:
            bands['excellent'] += 1
        elif avg >= 80:
            bands['good'] += 1
        elif avg >= MOE_PASSING_SCORE:
            bands['average'] += 1
        else:
            bands['needs_improvement'] += 1
    return bands


def _vpa_grade_letter_distribution(academic_year):
    buckets = {'A': 0, 'B': 0, 'C': 0, 'D': 0, 'F': 0}
    for grade in _vpa_year_grade_query(academic_year).all():
        if grade.score is None:
            continue
        letter = SchoolEngine.get_grade_letter(grade.score)
        if letter in buckets:
            buckets[letter] += 1
    return buckets


def _vpa_at_risk_students(academic_year, class_id=None, limit=10):
    rows = []
    student_query = Student.query.filter_by(status='ACTIVE')
    if academic_year:
        student_query = student_query.filter_by(academic_year_id=academic_year.id)
    if class_id:
        student_query = student_query.filter_by(klass_id=class_id)
    for student in student_query.all():
        avg = _vpa_student_average(student, academic_year)
        if avg is not None and avg < MOE_PASSING_SCORE:
            rows.append({'student': student, 'average': avg})
    rows.sort(key=lambda row: row['average'])
    return rows[:limit]


def _vpa_top_students(academic_year, class_id=None, limit=8):
    rows = []
    student_query = Student.query.filter_by(status='ACTIVE')
    if academic_year:
        student_query = student_query.filter_by(academic_year_id=academic_year.id)
    if class_id:
        student_query = student_query.filter_by(klass_id=class_id)
    for student in student_query.all():
        avg = _vpa_student_average(student, academic_year)
        if avg is not None:
            rows.append({'student': student, 'average': avg})
    rows.sort(key=lambda row: row['average'], reverse=True)
    return rows[:limit]


@app.route('/vpa/dashboard', methods=['GET'])
@login_required
@role_required('VPA')
def vpa_dashboard():
    """VPA — curriculum oversight, grade monitoring, and MoE academic standards."""
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    all_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    selected_year_name = request.args.get('year') or (active_year.name if active_year else (all_years[0].name if all_years else ''))
    selected_year = next((y for y in all_years if y.name == selected_year_name), active_year)

    class_id = request.args.get('class_id', type=int)
    search_q = (request.args.get('q') or '').strip()
    selected_class = db.session.get(Class, class_id) if class_id else None

    student_query = Student.query.filter_by(status='ACTIVE')
    if selected_year:
        student_query = student_query.filter_by(academic_year_id=selected_year.id)
    if class_id:
        student_query = student_query.filter_by(klass_id=class_id)
    if search_q:
        like = f'%{search_q}%'
        student_query = student_query.filter(
            Student.first_name.ilike(like)
            | Student.last_name.ilike(like)
            | Student.student_id.ilike(like)
        )
    students = student_query.order_by(Student.last_name.asc(), Student.first_name.asc()).all()

    for student in students:
        student.academic_average = _vpa_student_average(student, selected_year)
        student.grade_letter = (
            SchoolEngine.get_grade_letter(student.academic_average)
            if student.academic_average is not None
            else '-'
        )
        student.moe_status = (
            'Passing' if student.academic_average >= MOE_PASSING_SCORE
            else 'Below MoE Standard'
        ) if student.academic_average is not None else 'No grades'

    performance_bands = _vpa_performance_bands(selected_year)
    enrolled_count = (
        Student.query.filter_by(status='ACTIVE', academic_year_id=selected_year.id).count()
        if selected_year
        else Student.query.filter_by(status='ACTIVE').count()
    )
    graded_students = enrolled_count - performance_bands['no_grades']
    passing_count = (
        performance_bands['excellent']
        + performance_bands['good']
        + performance_bands['average']
    )
    passing_rate = round(passing_count / graded_students * 100, 1) if graded_students > 0 else 0.0

    catalog_subjects = ClassSubject.query.count()
    teacher_subjects = db.session.query(ClassSubjectTeacher.subject_name).distinct().count()
    subjects_allocated = max(catalog_subjects, teacher_subjects)

    grade_query = _vpa_year_grade_query(selected_year)
    assessment_query = _vpa_year_assessment_query(selected_year)

    recent_grades = grade_query.order_by(Grade.id.desc()).limit(10).all()
    recent_assessments = assessment_query.order_by(Assessment.id.desc()).limit(10).all()

    stats = {
        'total_students': enrolled_count,
        'total_classes': Class.query.count(),
        'total_teachers': Teacher.query.filter_by(status='ACTIVE').count(),
        'subjects_allocated': subjects_allocated,
        'total_assessments': assessment_query.count(),
        'grades_entered': grade_query.count(),
        'grades_published': grade_query.filter_by(submitted=True).count(),
        'passing_rate': passing_rate,
        'failing_count': performance_bands['needs_improvement'],
        'no_grade_data': performance_bands['no_grades'],
        'excellent_students': performance_bands['excellent'],
        'good_students': performance_bands['good'],
        'average_students': performance_bands['average'],
        'needs_improvement': performance_bands['needs_improvement'],
        'moe_standard': MOE_PASSING_SCORE,
    }

    return render_template(
        'vpa_dashboard.html',
        current_user=current_user,
        active_year=active_year,
        years=all_years,
        selected_year=selected_year,
        selected_year_name=selected_year_name,
        selected_class=selected_class,
        class_id=class_id,
        search_q=search_q,
        students=students,
        stats=stats,
        class_snapshots=_vpa_build_class_snapshots(selected_year),
        grade_letter_distribution=_vpa_grade_letter_distribution(selected_year),
        performance_bands=performance_bands,
        at_risk_students=_vpa_at_risk_students(selected_year, class_id=class_id),
        top_students=_vpa_top_students(selected_year, class_id=class_id),
        recent_grades=recent_grades,
        recent_assessments=recent_assessments,
        grading_periods=MOE_GRADING_PERIODS,
    )

@app.route('/payroll', methods=['GET', 'POST'])
@login_required
def payroll():
    if current_user.role not in ["admin", "business"]:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = PayrollForm()
    if form.validate_on_submit():
        # create a Payroll record (uses Payroll model)
        record = Payroll(
            staff_id=form.staff_id.data,
            occupation=form.occupation.data,
            month=form.month.data,
            salary_amount=parse_currency_amount(form.salary_amount.data),
            paid=bool(form.paid.data),
            created_on=datetime.now(timezone.utc)
        )
        db.session.add(record)
        db.session.commit()
        flash("Payroll record added successfully.", "success")
        return redirect(url_for('payroll'))

    payrolls = Payroll.query.order_by(Payroll.created_on.desc()).all()
    return render_template('payroll.html', form=form, payrolls=payrolls)

# -------------------------- PDF EXPORT ----------------------------
@app.route('/report-card/<int:student_id>/pdf')
@login_required
def report_card_pdf(student_id):
    student = Student.query.get_or_404(student_id)
    grades = Grade.query.filter_by(student_id=student_id).all()

    buffer = BytesIO()
    p = canvas.Canvas(buffer)
    p.setFont("Helvetica-Bold", 16)
    p.drawString(200, 800, "Student Report Card")

    y = 760
    p.setFont("Helvetica", 12)
    p.drawString(50, y, f"Student Name: {student.full_name}")
    y -= 30
    for g in grades:
        p.drawString(50, y, f"{g.subject_name} - {g.score}")
        y -= 20

    p.showPage()
    p.save()
    buffer.seek(0)

    return Response(buffer, mimetype='application/pdf',
                    headers={"Content-Disposition": f"attachment;filename={student.full_name}_report.pdf"})

# ------------------------ ANALYTICS ENDPOINTS ------------------------
from flask import jsonify

@app.route('/analytics/gender')
@login_required
def analytics_gender():
    _require_analytics_access()
    male_count = Student.query.filter(
        func.upper(Student.gender).in_(('M', 'MALE'))
    ).count()
    female_count = Student.query.filter(
        func.upper(Student.gender).in_(('F', 'FEMALE'))
    ).count()
    other_count = Student.query.filter(
        ~func.upper(Student.gender).in_(('M', 'MALE', 'F', 'FEMALE'))
    ).count()
    return jsonify({
        'male': male_count,
        'female': female_count,
        'other': other_count
    })

@app.route('/analytics/enrollment')
@login_required
def analytics_enrollment():
    _require_analytics_access()
    from collections import defaultdict
    class_counts = defaultdict(int)
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    students_q = Student.query.filter(Student.klass_id.isnot(None))
    if active_year:
        students_q = students_q.filter_by(academic_year_id=active_year.id)
    for student in students_q.all():
        class_counts[str(student.klass_id)] += 1
    return jsonify({
        'total': sum(class_counts.values()),
        'by_class': dict(class_counts),
    })

@app.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if normalize_role(current_user) != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))
    if user_id == current_user.id:
        flash("You cannot delete your own account.", "danger")
        return redirect(url_for('admin_users'))
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash("User deleted successfully.", "success")
    return redirect(url_for('admin_users'))

@app.route('/analytics/payments')
@login_required
def analytics_payments():
    _require_analytics_access()
    from collections import defaultdict
    year_counts = defaultdict(float)
    payments = StudentPayment.query.all()
    for p in payments:
        year_counts[str(p.academic_year_id)] += p.amount_paid
    return jsonify({
        'total': sum(year_counts.values()),
        'by_year': year_counts
    })

@app.route('/api/profile', methods=['GET'])
@login_required
def api_profile():
    """
    Secure Profile Extraction API Payload Endpoint
    Serializes relational database records for async DOM parsing.
    """
    try:
        # Fallback dictionary structures if properties return empty string variables
        payload = {
            "id": current_user.id,
            "full_name": (current_user.full_name or '').strip(),
            "role": (current_user.role or '').strip(),
            "email": current_user.email or '',
            "telephone": getattr(current_user, 'telephone_number', '') or '',
            "home_address": getattr(current_user, 'home_address', '') or '',
            "dob": getattr(current_user, 'date_of_birth', '') or '',
        }
        
        return jsonify(payload), 200
        
    except Exception as e:
        current_app.logger.error(f"API Profiler Exception: {str(e)}")
        return jsonify({"error": "Failed to compile background structural credentials"}), 500
    
@app.route('/analytics/grades')
@login_required
def analytics_grades():
    _require_analytics_access()
    from collections import defaultdict
    subject_counts = defaultdict(float)
    grades = Grade.query.all()
    for g in grades:
        subject_counts[str(g.subject)] += g.score
    return jsonify({
        'total': sum(subject_counts.values()),
        'by_subject': subject_counts
    })

# ---------------------------- ERROR -------------------------------
@app.errorhandler(404)
def page_not_found(e):
    return render_template('404.html'), 404

# -------------------------------------------------------------------
# Run
# -------------------------------------------------------------------
app = init_export_routes(app)

@app.route('/admin/users', methods=['GET', 'POST'])
@login_required
def admin_users():
    # ✨ FIX 1: Grant permission to BOTH Admin and Principal roles (case-insensitive protection)
    if current_user.role.lower() not in ['admin', 'principal']:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    from forms import CreateUserForm
    form = CreateUserForm()
    
    # Modernized explicit query order execution
    users = db.session.execute(db.select(User).order_by(User.id.desc())).scalars().all()
    classes = db.session.execute(db.select(Class).order_by(Class.name)).scalars().all()

    if form.validate_on_submit():
        try:
            # Create user entity schema array
            user = User(
                email=form.email.data,
                full_name=form.full_name.data,
                role=form.role.data,
                home_address=form.home_address.data,
                telephone_number=form.telephone_number.data
            )
            user.set_password(form.password.data)

            # Photo processing system pipeline
            photo_file = form.photo.data
            if photo_file:
                upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
                os.makedirs(upload_dir, exist_ok=True)
                
                # Clean and isolate filename parameters safely
                filename = secure_filename(photo_file.filename)
                timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
                filename = f"{timestamp}_{filename}"
                
                file_path = os.path.join(upload_dir, filename)
                photo_file.save(file_path)
                user.photo = os.path.join('uploads', filename).replace('\\', '/')

            db.session.add(user)
            db.session.flush()  # Generates the user.id node for matching teacher profiles

            # Profile creation tier for teacher tracking logs
            if user.role.lower() == 'teacher':
                name_parts = (user.full_name or '').strip().split(None, 1)  # Split on first whitespace only
                first_name = name_parts[0].strip() if name_parts else user.full_name
                last_name = name_parts[1].strip() if len(name_parts) > 1 else ''
                
                # Ensure teacher profile doesn't already exist
                existing_teacher = Teacher.query.filter_by(user_id=user.id).first()
                if not existing_teacher:
                    teacher_profile = Teacher(
                        user_id=user.id,
                        first_name=first_name or 'Unknown',
                        last_name=last_name or user.full_name,
                        status='ACTIVE'
                    )
                    db.session.add(teacher_profile)
                    logger.info(f"✅ Created Teacher profile for user {user.id}: {first_name} {last_name}")
                else:
                    logger.warning(f"⚠️ Teacher profile already exists for user {user.id}")

            db.session.commit()
            flash(f"User {user.full_name} ({user.role}) created successfully.", "success")
            return redirect(url_for('admin_users'))

        except Exception as e:
            db.session.rollback()
            flash(f"Database write fault occurred during enrollment processing: {str(e)}", "danger")

    return render_template('admin_users.html', form=form, users=users, classes=classes)
@app.route('/admin/users/<int:user_id>/unlock', methods=['POST'])
@login_required
def unlock_user(user_id):
    if normalize_role(current_user) not in ('admin', 'principal'):
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    # ✨ Modern Flask-SQLAlchemy lookup format
    user = db.first_or_404(db.select(User).filter_by(id=user_id))

    try:
        # ✨ FIX 2: Reset the User's core model security state fields if they exist
        if hasattr(user, 'login_attempts'):
            user.login_attempts = 0
        if hasattr(user, 'is_locked'):
            user.is_locked = False
        if hasattr(user, 'status'):
            user.status = 'ACTIVE'

        # Clear recent failed login attempts (last 15 minutes to clear brute-force threshold logs)
        fifteen_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
        
        deleted_count = db.session.execute(
            db.delete(SecurityLog).where(
                SecurityLog.event == 'FAILED_LOGIN',
                SecurityLog.timestamp >= fifteen_minutes_ago
            )
        ).rowcount

        # Log this administrative override execution
        unlock_log = SecurityLog(
            ip_address=request.remote_addr,
            event=f"ACCOUNT_UNLOCKED: User ID {user.id} manually unlocked by {current_user.full_name} ({normalize_role(current_user)}).",
        )
        db.session.add(unlock_log)
        
        db.session.commit()
        flash(f"Account successfully unlocked for {user.username if hasattr(user, 'username') else 'the user'}. Security tracking variables reset.", "success")
        
    except Exception as e:
        db.session.rollback()
        flash(f"System directory lock failure: {str(e)}", "danger")

    # Dynamic fallback check to make sure the redirect endpoint doesn't break
    target = 'admin_users' if 'admin_users' in current_app.view_functions else 'dashboard'
    return redirect(url_for(target))

with app.app_context():
    try:
        db.create_all()
        ensure_legacy_sqlite_schema()
        repair_submission_legacy_links()
        relocated_media = normalize_misplaced_school_media()
        if relocated_media:
            print(f"School media repair: moved {relocated_media} photo/video item(s) out of entrance/info sections.")
        repaired_links = repair_student_portal_links()
        if repaired_links:
            print(f"Student portal repair: linked {repaired_links} student profile(s) to login account(s).")
        synced = backfill_student_payments_to_income_ledger()
        if synced:
            print(f"Business ledger sync: posted {synced} historical student fee payment(s) as income.")
        print("Database structural integrity checked: all tables verified/created successfully.")
    except Exception as db_err:
        print(f"Warning: table auto-generation bypass encountered: {db_err}")

# Your exact Waitress and Fallback Server Configuration Engine Block
if __name__ == '__main__':
    host = os.environ.get('BIND_HOST', '0.0.0.0')
    port = int(os.environ.get('PORT', '3000'))
    threads = int(os.environ.get('WAITRESS_THREADS', '8'))
    print(f"Starting server with Waitress on {host}:{port} ...")
    print(f"Visit http://localhost:{port} in your browser")
    try:
        from waitress import serve
        serve(app, host=host, port=port, threads=threads)
    except Exception as e:
        print(f"Waitress production engine failed: {e}")
        print("Falling back to secure local development server...")
        app.run(debug=False, host='127.0.0.1', port=port)