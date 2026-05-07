# app.py — Keep Track Digital School Management System
from flask import Flask, render_template, redirect, url_for, flash, request, Response, jsonify, send_file, current_app, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from datetime import datetime, timedelta, timezone
from itsdangerous import URLSafeTimedSerializer
import pyotp
from reportlab.pdfgen import canvas
from io import BytesIO, StringIO
from sqlalchemy import func
from werkzeug.utils import secure_filename
import csv
import os
import sys
from waitress import serve
try:
    import pytesseract
    from PIL import Image
except ImportError:
    pytesseract = None
    Image = None

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

# ======================== STUDENT ADMISSIONS CONFIG ========================
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'static', 'uploads', 'photos')
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}

# Ensure the folder exists
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

def allowed_file(filename):
    """Check if uploaded file has allowed extension."""
    return '.' in filename and \
           filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

# ======================== END CONFIG =======================================

from functools import wraps

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 1. Check if user is logged in
            # 2. Check if their role matches any of the provided roles
            if not current_user.is_authenticated or current_user.role not in roles:
                abort(403) # "Forbidden" error
            return f(*args, **kwargs)
        return decorated_function
    return decorator
def log_security_event(description):
    """Log security events to the database."""
    event = SecurityLog(
        ip_address=request.remote_addr,
        event=description,
        timestamp=datetime.now(timezone.utc)
    )
    db.session.add(event)
    db.session.commit()

def generate_recovery_token(user_id):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(user_id, salt='recovery-key')

def log_incident(event_type):
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    log = SecurityLog(ip_address=ip, event=event_type)
    db.session.add(log)
    db.session.commit()

def check_brute_force(ip):
    # Brute force lockout disabled temporarily.
    return False

# Local imports handled in init_db.py to avoid circular imports during app import
from models import (
    db, User, Student, Teacher, Class, Enrollment, Grade,
    Attendance, Sponsor, Announcement, Discipline, Payroll,
    Assessment, AcademicYear, BusinessTransaction, StudentPayment,
    Leader, LeaderCategory, Event, SecurityLog, Suspension, Room,
    Asset, MaintenanceTicket, Activity, Submission
)
from forms import (
    LoginForm, RegisterStudentForm, PayrollForm, AcademicYearForm, AnnouncementForm,
    BusinessTransactionForm, AssignTeacherForm, CreateClassForm, EventForm, ConfirmDeleteForm,
    LeaderForm, EnrollmentForm, PaymentForm, TransactionForm
)
from export_routes import init_export_routes

# -------------------------------------------------------------------
# SchoolEngine: Business Logic Helpers
# -------------------------------------------------------------------
class SchoolEngine:
    # --- DEAN LOGIC ---
    @staticmethod
    def suspend_student(student_id, days, reason):
        student = Student.query.get(student_id)
        if not student: return "Student not found"
        student.status = 'SUSPENDED'
        from datetime import timedelta
        end_date = datetime.now(timezone.utc) + timedelta(days=days)
        new_suspension = Suspension(student_id=student_id, reason=reason, return_date=end_date)
        db.session.add(new_suspension)
        db.session.commit()
        return f"Student locked out until {end_date.date()}"

    # --- VPA LOGIC ---
    @staticmethod
    def calculate_period_total(ca, exam):
        if ca > 60 or exam > 40:
            return None # Enforce MoE standards
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
        return sum(grade_points) / len(grade_points) if grade_points else 0.0

    @staticmethod
    def get_grade_letter(score):
        if score >= 90:
            return 'A'
        elif score >= 80:
            return 'B'
        elif score >= 70:
            return 'C'
        elif score >= 60:
            return 'D'
        else:
            return 'F'

    @staticmethod
    def get_remarks(score):
        if score >= 90:
            return 'Excellent'
        elif score >= 80:
            return 'Very Good'
        elif score >= 70:
            return 'Good'
        elif score >= 60:
            return 'Satisfactory'
        else:
            return 'Failing'

    # --- VPI LOGIC ---
    @staticmethod
    def get_dashboard_stats():
        from models import Class
        return {
            "total_students": Student.query.count(),
            "active_suspensions": Student.query.filter_by(status='SUSPENDED').count(),
            "facility_utilization": "85%" # Calculated vs total capacity
        }


class DeanManager:
    """The 'Heart' of the school - logic for the Dean of Students"""

    @staticmethod
    def issue_suspension(student_id, reason, duration_days):
        """
        Records a suspension and sets the return date.
        """
        from datetime import timedelta
        return_date = datetime.now(timezone.utc) + timedelta(days=duration_days)
        
        suspension = Suspension(
            student_id=student_id,
            reason=reason,
            return_date=return_date
        )
        
        # Flag the student as 'Suspended' in the main Student table
        student = Student.query.get(student_id)
        student.status = 'SUSPENDED'
        
        db.session.add(suspension)
        db.session.commit()
        return f"Student {student.full_name} is suspended until {return_date.date()}"

    @staticmethod
    def check_suspension_status(student_id):
        """
        Security check: Is the student allowed on campus/in the system today?
        """
        suspension = Suspension.query.filter_by(
            student_id=student_id
        ).filter(Suspension.return_date > datetime.now(timezone.utc)).first()
        
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
        room = Room.query.get(room_id)
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
        from models import MaintenanceTicket
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

    @staticmethod
    def calculate_capacity_utilization(room_capacity, current_students):
        """Logic to ensure classrooms are not overcrowded"""
        utilization = (current_students / room_capacity) * 200
        if utilization > 200:
            return f"OVERCROWDED: {utilization}% capacity"
        return f"Healthy: {utilization}% capacity"


class AcademicManager:
    """The 'Brain' of the school - logic for the VPA"""

    @staticmethod
    def enter_grade(student_id, course_id, period_id, ca_score, exam_score):
        """
        Calculates and saves a grade for a specific marking period.
        Standard: CA (60pts) + Exam (40pts) = 100pts
        """
        from models import MarkingPeriod, Course
        # 1. Validate the Marking Period is actually OPEN
        period = MarkingPeriod.query.get(period_id)
        if not period or not period.is_active:
            raise PermissionError("This marking period is closed for entry.")

        # 2. Enforce Liberian National Standards
        if ca_score > 60 or exam_score > 40:
            raise ValueError("Scores exceed the 60/40 MoE limit.")

        total = ca_score + exam_score
        
        # 3. Update or Create the grade record
        grade_record = Grade.query.filter_by(
            student_id=student_id, 
            course_id=course_id, 
            period_id=period_id
        ).first()

        if not grade_record:
            grade_record = Grade(
                student_id=student_id, 
                course_id=course_id, 
                period_id=period_id
            )

        grade_record.ca_score = ca_score
        grade_record.exam_score = exam_score
        grade_record.score = total  # Assuming score is the total
        grade_record.remarks = f"CA: {ca_score}, Exam: {exam_score}"

        db.session.add(grade_record)
        db.session.commit()
        return grade_record

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
# Security Functions
# -------------------------------------------------------------------
def track_failed_attempt(ip_address, username):
    """Logs a failed attempt and checks if we should block the IP."""
    new_log = SecurityLog(
        ip_address=ip_address,
        username=username,
        event_type='FAILED_LOGIN'
    )
    db.session.add(new_log)
    db.session.commit()
    
    # Check if this IP has failed 5 times in the last hour
    recent_fails = SecurityLog.query.filter_by(ip_address=ip_address, event_type='FAILED_LOGIN').count()
    if recent_fails >= 5:
        # Here you would trigger a 'lockdown' for this IP
        return True 
    return False

# -------------------------------------------------------------------
# Recovery Token Functions
# -------------------------------------------------------------------
def generate_recovery_token(user_id):
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    return s.dumps(user_id, salt='password-recovery-salt')

def verify_recovery_token(token, expiration=600): # 10 minutes
    s = URLSafeTimedSerializer(current_app.config['SECRET_KEY'])
    try:
        user_id = s.loads(token, salt='password-recovery-salt', max_age=expiration)
    except:
        return None
    return user_id

def calculate_period_score(ca_score, exam_score):
    """
    Logic to ensure the weights follow MoE standards.
    CA is usually out of 60, Exam out of 40.
    """
    if ca_score > 60 or exam_score > 40:
        raise ValueError("Score exceeds Liberian national standard limits.")
    
    return ca_score + exam_score # Returns a score out of 100

# -------------------------------------------------------------------
# App Configuration
# -------------------------------------------------------------------
app = Flask(__name__)

# Use env vars for security & PythonAnywhere deployment
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'change_this_secret_key')

# Set absolute path for SQLite to work on both Local and PythonAnywhere
INSTANCE_PATH = os.path.join(BASE_DIR, 'instance')
os.makedirs(INSTANCE_PATH, exist_ok=True)
db_path = os.path.join(INSTANCE_PATH, 'keeptrack_full.db')
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('DATABASE_URL', f'sqlite:///{db_path}')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
migrate = Migrate(app, db)

csrf = CSRFProtect(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"


# Custom Jinja filters
@app.template_filter('grade_letter')
def grade_letter_filter(score):
    return SchoolEngine.get_grade_letter(score)

@app.template_filter('remarks')
def remarks_filter(score):
    return SchoolEngine.get_remarks(score)

@app.context_processor
def inject_nav_flags():
    role = getattr(current_user, "role", None)
    return {"announcements_link": role in {"admin", "teacher"}}

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
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))

    # Show the next three upcoming events on the public homepage
    upcoming_events = (
        Event.query.order_by(Event.date.asc())
        .filter(Event.date >= datetime.now(timezone.utc).date())
        .limit(3)
        .all()
    )
    total_events = Event.query.count()
    return render_template(
        'index.html',
        events=upcoming_events,
        highlighted_event=upcoming_events[0] if upcoming_events else None,
        current_year=datetime.now(timezone.utc).year,
        total_events=total_events
    )

# ----------------------------- LOGIN -------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash('Login successful.', 'success')
            log_incident('SUCCESSFUL_LOGIN')
            return redirect(url_for('dashboard'))
        
        log_incident('FAILED_LOGIN')
        flash('Invalid email or password.', 'danger')
    return render_template('login.html', form=form)

@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have logged out.', 'info')
    return redirect(url_for('login'))

# --------------------------- DASHBOARD -----------------------------
@app.route('/dashboard', methods=['GET', 'POST'])
@login_required
def dashboard():
    
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    if not active_year:
        flash("No active academic year set. Please contact administrator.", "warning")
        return redirect(url_for('index'))

    stats = {
        'students': Student.query.filter_by(academic_year_id=active_year.id).count(),
        'new_students': Student.query.filter_by(registration_type='New', academic_year_id=active_year.id).count(),
        'returning_students': Student.query.filter_by(registration_type='Returning', academic_year_id=active_year.id).count(),
        'teachers': Teacher.query.count(),
        'classes': Class.query.count(),
        'payments': StudentPayment.query.count()
    }

    template_map = {
        "admin": "dashboard_admin.html",
        "teacher": "dashboard_teacher.html",
        "student": "dashboard_student.html",
        "registrar": "dashboard_registrar.html",
        "parent": "dashboard_parent.html",
        "business": "dashboard_business.html",
        "Principal": "principal_dashboard.html",
        "VPA": "vpa_dashboard.html",
        "VPI": "vpi_dashboard.html",
        "Dean": "dean_dashboard.html"
    }

    template_name = template_map.get(current_user.role)
    selected_year_name = active_year.name
    selected_year = active_year

    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    
    # Redirect leadership roles to their specific dashboards
    if current_user.role == "Principal":
        return redirect(url_for('principal_dashboard'))
    elif current_user.role == "VPI":
        return redirect(url_for('vpi_dashboard'))
    elif current_user.role == "VPA":
        return redirect(url_for('vpa_dashboard'))
    elif current_user.role == "Dean":
        return redirect(url_for('dean_dashboard'))
    
    if not template_name:
        flash("No dashboard is configured for your role.", "warning")
        return redirect(url_for('index'))

    announcements_link = current_user.role in {"admin", "teacher"}

    if template_name == "dashboard_teacher.html":
        teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()

        # Classes where this teacher is the assigned teacher
        teaching_class_ids = []
        if teacher_profile:
            teaching_class_ids = [klass.id for klass in Class.query.filter_by(teacher_id=teacher_profile.id)]
        
        # Classes where this teacher is the sponsor
        sponsored_classes = Class.query.filter_by(sponsor_id=current_user.id).all()
        sponsored_class_ids = [klass.id for klass in sponsored_classes]
        
        # All classes this teacher is involved with (teaching or sponsoring)
        all_class_ids = list(set(teaching_class_ids + sponsored_class_ids))
        
        students = (
            Student.query.filter(Student.klass_id.in_(all_class_ids)).order_by(Student.first_name, Student.last_name).all()
            if all_class_ids
            else []
        )
        grades = (
            Grade.query.filter_by(teacher_id=teacher_profile.id).order_by(Grade.period.desc()).all()
            if teacher_profile
            else []
        )

        return render_template(
            template_name,
            stats=stats,
            counts=stats,
            announcements_link=announcements_link,
            selected_year=selected_year_name,
            selected_year_obj=selected_year,
            years=years,
            students=students,
            grades=grades,
            teacher_profile=teacher_profile,
            teaching_classes=Class.query.filter_by(teacher_id=teacher_profile.id).all() if teacher_profile else [],
            sponsored_classes=sponsored_classes,
            current_user=current_user,
            active_year=active_year
        )

    if template_name == "dashboard_student.html":
        student_profile = Student.query.filter_by(user_id=current_user.id).first()
        grades = (
            Grade.query.filter_by(student_id=student_profile.id).order_by(Grade.period.desc()).all()
            if student_profile
            else []
        )
        activities = (
            Activity.query.filter_by(klass_id=student_profile.klass_id).all()
            if student_profile and student_profile.klass_id
            else []
        )
        # Assuming fees are related to StudentPayment or some general fees
        fees = student_profile.payments if student_profile else []

        return render_template(
            template_name,
            stats=stats,
            counts=stats,
            announcements_link=announcements_link,
            selected_year=selected_year_name,
            selected_year_obj=selected_year,
            years=years,
            student=student_profile,
            grades=grades,
            activities=activities,
            fees=fees,
            current_user=current_user,
            active_year=active_year
        )

    if template_name == "dashboard_parent.html":
        # For parents, find their linked student (assuming one parent per student for simplicity)
        student_profile = Student.query.filter_by(parent_email=current_user.email).first()

        return render_template(
            template_name,
            stats=stats,
            counts=stats,
            announcements_link=announcements_link,
            selected_year=selected_year_name,
            selected_year_obj=selected_year,
            years=years,
            student=student_profile,
            current_user=current_user,
            active_year=active_year
        )

    if template_name == "dashboard_registrar.html":
        form = RegisterStudentForm()
        form.klass.choices = [
            (klass.id, klass.name)
            for klass in Class.query.order_by(Class.name.asc()).all()
        ]
        form.academic_year.choices = [(year.id, year.name) for year in years]
        if request.method == 'GET' and active_year:
            form.academic_year.data = active_year.id

        if form.validate_on_submit():
            existing_student = Student.query.filter_by(student_id=form.student_id.data).first()
            if existing_student:
                student = existing_student
                student.first_name = form.first_name.data
                student.last_name = form.last_name.data
                student.dob = form.dob.data
                student.gender = form.gender.data
                student.parent_email = form.parent_email.data
                student.klass_id = form.klass.data
                student.academic_year_id = form.academic_year.data
                student.level = form.level.data
                student.registrar = current_user.full_name
                student.registration_fees = form.registration_fees.data or 0.0
                student.registration_type = 'Returning'
                flash(f"Returning student {student.first_name} {student.last_name} updated for the new academic year.", "info")
            else:
                student = Student(
                    first_name=form.first_name.data,
                    last_name=form.last_name.data,
                    dob=form.dob.data,
                    gender=form.gender.data,
                    student_id=form.student_id.data,
                    parent_email=form.parent_email.data,
                    klass_id=form.klass.data,
                    academic_year_id=form.academic_year.data,
                    level=form.level.data,
                    registrar=current_user.full_name,
                    registration_fees=form.registration_fees.data or 0.0,
                    registration_type='New'
                )
                db.session.add(student)
                flash("New student registered successfully.", "success")

            if form.email.data and form.password.data and not student.user_id:
                user = User(
                    email=form.email.data,
                    role="student",
                    full_name=f"{form.first_name.data} {form.last_name.data}",
                )
                user.set_password(form.password.data)
                db.session.add(user)
                db.session.flush()
                student.user_id = user.id

            try:
                db.session.commit()
                flash("Student registered successfully.", "success")
            except Exception as e:
                db.session.rollback()
                flash(f"Database error: {str(e)}", "danger")
                return redirect(url_for('dashboard'))
            return redirect(url_for('dashboard'))
        else:
            # Debug: Show form validation errors
            if request.method == 'POST':
                for field, errors in form.errors.items():
                    for error in errors:
                        flash(f"Error in {field}: {error}", "danger")
                flash(f"Form data: student_id={form.student_id.data}, first_name={form.first_name.data}, level={form.level.data}", "info")

        students_query = Student.query.order_by(Student.first_name.asc(), Student.last_name.asc())
        if selected_year:
            students_query = students_query.filter(Student.academic_year_id == selected_year.id)

        students = []
        for student in students_query.all():
            photo_path = student.photo or (student.user.photo if student.user and student.user.photo else None)
            if photo_path:
                if photo_path.startswith("static/"):
                    photo_url = url_for("static", filename=photo_path[len("static/"):])
                elif photo_path.startswith("/static/"):
                    photo_url = url_for("static", filename=photo_path[len("/static/"):])
                else:
                    photo_url = photo_path
            else:
                photo_url = url_for("static", filename="images/default-user.png")

            students.append({
                "id": student.id,
                "student_id": student.student_id,
                "first_name": student.first_name,
                "last_name": student.last_name,
                "klass": student.klass.name if student.klass else "-",
                "email": student.user.email if student.user else "",
                "photo_url": photo_url,
                "registration": student.registration_fees if student.registration_fees else 0,
                "current_year": student.academic_year.name if student.academic_year else None,
            })

        search_class = request.args.get('search_class', '').strip()
        class_query = Class.query.order_by(Class.name.asc())
        if search_class:
            class_query = class_query.filter(Class.name.ilike(f"%{search_class}%"))

        classes = []
        for klass in class_query.all():
            class_student_count = Student.query.filter_by(
                klass_id=klass.id,
                academic_year_id=active_year.id if active_year else None
            ).count()
            classes.append({
                "id": klass.id,
                "name": klass.name,
                "description": klass.description,
                "student_count": class_student_count,
            })

        return render_template(
            template_name,
            stats=stats,
            counts=stats,
            announcements_link=announcements_link,
            selected_year=selected_year_name,
            selected_year_obj=selected_year,
            years=years,
            form=form,
            students=students,
            classes=classes,
            search_class=search_class,
            current_user=current_user,
            active_year=active_year
        )

    if template_name == "dashboard_business.html":
        # Financial Overview for Business Manager
        total_revenue = db.session.query(func.sum(BusinessTransaction.amount)).filter_by(type='income', is_deleted=False).scalar() or 0
        total_expenses = db.session.query(func.sum(BusinessTransaction.amount)).filter_by(type='expense', is_deleted=False).scalar() or 0
        net_profit = total_revenue - total_expenses
        
        recent_transactions = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(10).all()
        
        income_categories = db.session.query(BusinessTransaction.category, func.sum(BusinessTransaction.amount)).filter_by(type='income', is_deleted=False).group_by(BusinessTransaction.category).all()
        expense_categories = db.session.query(BusinessTransaction.category, func.sum(BusinessTransaction.amount)).filter_by(type='expense', is_deleted=False).group_by(BusinessTransaction.category).all()

        # Get classes with student counts and payment info
        search_class = request.args.get('search_class', '').strip()
        class_query = Class.query.order_by(Class.name.asc())
        if search_class:
            class_query = class_query.filter(Class.name.ilike(f"%{search_class}%"))
            
        classes = []
        for klass in class_query.all():
            student_count = Student.query.filter_by(klass_id=klass.id).count()
            total_paid = db.session.query(func.sum(StudentPayment.amount_paid)).join(Student).filter(
                Student.klass_id == klass.id
            ).scalar() or 0
            
            classes.append({
                "id": klass.id,
                "name": klass.name,
                "description": klass.description,
                "student_count": student_count,
                "total_paid": total_paid,
            })

        return render_template(
            template_name,
            total_revenue=total_revenue,
            total_expenses=total_expenses,
            net_profit=net_profit,
            recent_transactions=recent_transactions,
            income_categories=income_categories,
            expense_categories=expense_categories,
            stats=stats,
            counts=stats,
            years=years,
            selected_year=selected_year_name,
            current_user=current_user,
            students=Student.query.order_by(Student.last_name, Student.first_name).all(),
            classes=classes,
            search_class=search_class,
            active_year=active_year
        )

    payments = StudentPayment.query.order_by(StudentPayment.paid_on.desc()).limit(5).all()
    announcements = Announcement.query.order_by(Announcement.created_at.desc()).limit(10).all()

    return render_template(
        template_name,
        stats=stats,
        counts=stats,
        announcements_link=announcements_link,
        selected_year=selected_year_name,
        selected_year_obj=selected_year,
        years=years,
        payments=payments,
        announcements=announcements,
        current_user=current_user,
        active_year=active_year
    )


@app.route('/registrar/class/<int:class_id>/students')
@login_required
def registrar_class_students(class_id):
    if current_user.role not in ['admin', 'registrar']:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    students_query = Student.query.filter_by(klass_id=class_id)
    if active_year:
        students_query = students_query.filter(Student.academic_year_id == active_year.id)

    students = []
    for student in students_query.order_by(Student.last_name, Student.first_name).all():
        photo_path = student.photo or (student.user.photo if student.user and student.user.photo else None)
        if photo_path:
            if photo_path.startswith("static/"):
                photo_url = url_for("static", filename=photo_path[len("static/"):])
            elif photo_path.startswith("/static/"):
                photo_url = url_for("static", filename=photo_path[len("/static/"):])
            else:
                photo_url = photo_path
        else:
            photo_url = url_for("static", filename="images/default-user.png")

        students.append({
            "student_id": student.student_id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "email": student.user.email if student.user else (student.parent_email or "-"),
            "academic_year": student.academic_year.name if student.academic_year else "-",
            "registration_fees": student.registration_fees if student.registration_fees else 0,
            "photo_url": photo_url,
        })

    return render_template(
        'registrar_class_students.html',
        klass=klass,
        students=students,
        active_year=active_year,
        current_user=current_user
    )


@app.route('/business/class/<int:class_id>/students')
@login_required
def business_class_students(class_id):
    if current_user.role not in ['admin', 'business', 'VPI']:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    students_query = Student.query.filter_by(klass_id=class_id)
    if active_year:
        students_query = students_query.filter(Student.academic_year_id == active_year.id)

    students = []
    for student in students_query.order_by(Student.last_name, Student.first_name).all():
        # Get payment information
        total_paid = db.session.query(func.sum(StudentPayment.amount_paid)).filter_by(student_id=student.id).scalar() or 0
        payment_count = StudentPayment.query.filter_by(student_id=student.id).count()
        
        photo_path = student.photo or (student.user.photo if student.user and student.user.photo else None)
        if photo_path:
            if photo_path.startswith("static/"):
                photo_url = url_for("static", filename=photo_path[len("static/"):])
            elif photo_path.startswith("/static/"):
                photo_url = url_for("static", filename=photo_path[len("/static/"):])
            else:
                photo_url = photo_path
        else:
            photo_url = url_for("static", filename="images/default-user.png")

        students.append({
            "id": student.id,
            "student_id": student.student_id,
            "first_name": student.first_name,
            "last_name": student.last_name,
            "email": student.user.email if student.user else (student.parent_email or "-"),
            "academic_year": student.academic_year.name if student.academic_year else "-",
            "registration_fees": student.registration_fees if student.registration_fees else 0,
            "total_paid": total_paid,
            "payment_count": payment_count,
            "photo_url": photo_url,
        })

    return render_template(
        'business_class_students.html',
        klass=klass,
        students=students,
        active_year=active_year,
        current_user=current_user
    )


@app.route('/grade_entry_class')
@login_required
def grade_entry():
    if current_user.role != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Get classes taught by this teacher
    taught_classes = Class.query.filter_by(teacher_id=teacher.id).all()
    # Get sponsored classes
    sponsored_classes = Class.query.filter_by(sponsor_id=current_user.id).all()
    all_classes = list(set(taught_classes + sponsored_classes))
    
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    
    return render_template('grade_entry.html', classes=all_classes, active_year=active_year)

@app.route('/grade-entry/<int:class_id>', methods=['GET', 'POST'])
@login_required
def grade_entry_class(class_id):
    # --- 1. Authorization Logic ---
    if current_user.role != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    klass = Class.query.get_or_404(class_id)
    
    # Verify teacher permissions for this specific class
    if not teacher or (klass.teacher_id != teacher.id and klass.sponsor_id != current_user.id):
        flash('Access denied or profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    # --- 2. Handle Saving Grades (POST) ---
    if request.method == 'POST':
        try:
            for key, value in request.form.items():
                if key.startswith('grade_') and value.strip() != '':
                    # Expected key format from HTML: grade_{student_id}_{period}
                    parts = key.split('_')
                    student_id = int(parts[1])
                    period = parts[2]
                    score = float(value)

                    # Check if grade record already exists
                    grade_record = Grade.query.filter_by(
                        student_id=student_id, 
                        period=period,
                        class_id=class_id # Ensure your Grade model has class_id
                    ).first()

                    if grade_record:
                        grade_record.score = score
                    else:
                        new_grade = Grade(
                            student_id=student_id,
                            period=period,
                            score=score,
                            class_id=class_id,
                            teacher_id=teacher.id
                        )
                        db.session.add(new_grade)
            
            db.session.commit()
            flash('Grades saved successfully!', 'success')
        except Exception as e:
            db.session.rollback()
            flash(f'Error saving grades: {str(e)}', 'danger')
        
        return redirect(url_for('grade_entry_class', class_id=class_id))

    # --- 3. Display Entry Form (GET) ---
    students = Student.query.filter_by(klass_id=class_id).order_by(Student.last_name, Student.first_name).all()
    
    # Efficiently fetch existing grades into a dictionary
    grades = {}
    for student in students:
        student_grades = Grade.query.filter_by(student_id=student.id, class_id=class_id).all()
        grades[student.id] = {g.period: g for g in student_grades}
    
    return render_template('grade_entry_class.html', klass=klass, students=students, grades=grades)
@app.route('/save-grades/<int:class_id>', methods=['POST'])
@login_required
def save_grades(class_id):
    if current_user.role != 'teacher':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    teacher = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher:
        flash('Teacher profile not found.', 'danger')
        return redirect(url_for('dashboard'))
    
    klass = Class.query.get_or_404(class_id)
    if klass.teacher_id != teacher.id and klass.sponsor_id != current_user.id:
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    students = Student.query.filter_by(klass_id=class_id).all()
    
    for student in students:
        for period in range(1, 7):  # 6 periods
            ca_key = f'ca_{student.id}_{period}'
            exam_key = f'exam_{student.id}_{period}'
            ca_score = request.form.get(ca_key, type=float)
            exam_score = request.form.get(exam_key, type=float)
            
            if ca_score is not None and exam_score is not None:
                total = SchoolEngine.calculate_period_total(ca_score, exam_score)
                if total is not None:
                    # Check if grade exists
                    grade = Grade.query.filter_by(student_id=student.id, period=period, teacher_id=teacher.id).first()
                    if not grade:
                        grade = Grade(
                            student_id=student.id,
                            teacher_id=teacher.id,
                            period=period,
                            subject=klass.name,  # Assuming class name as subject
                            ca_score=ca_score,
                            exam_score=exam_score,
                            score=total
                        )
                        db.session.add(grade)
                    else:
                        grade.ca_score = ca_score
                        grade.exam_score = exam_score
                        grade.score = total
    
    db.session.commit()
    flash('Grades saved successfully.', 'success')
    return redirect(url_for('grade_entry_class', class_id=class_id))

@app.route('/download-grades/<int:class_id>')
@login_required
def download_grades(class_id):
    if current_user.role != 'registrar' and current_user.role != 'teacher' or (current_user.role != 'student'):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
@app.route('/download/<path:filename>')
@login_required
def download_file(filename):
    # This assumes activity files are in static/uploads/activities
    directory = os.path.join(BASE_DIR, 'static', 'uploads', 'activities')
    return send_from_directory(directory, filename)

@app.route('/student/upload/<int:activity_id>', methods=['POST'])
@login_required
def student_upload(activity_id):
    if current_user.role != 'student':
        flash('Only students can upload activities.', 'danger')
        return redirect(url_for('dashboard'))

    student = Student.query.filter_by(user_id=current_user.id).first()
    if not student:
        flash('Student profile not found.', 'danger')
        return redirect(url_for('dashboard'))

    if 'assignment' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('dashboard'))
    
    file = request.files['assignment']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('dashboard'))

    if file:
        activity = Activity.query.get_or_404(activity_id)
        
        # Ensure student is in the class for this activity
        if student.klass_id != activity.klass_id:
            flash('You are not authorized to submit for this class activity.', 'danger')
            return redirect(url_for('dashboard'))

        # Save the file
        upload_dir = os.path.join(BASE_DIR, 'static', 'uploads', 'submissions')
        os.makedirs(upload_dir, exist_ok=True)
        
        filename = f"sub_{activity_id}_{student.id}_{file.filename}"
        file_path = os.path.join(upload_dir, filename)
        file.save(file_path)
        
        # Record submission
        submission_db_path = os.path.join('uploads', 'submissions', filename).replace('\\', '/')
        new_submission = Submission(
            activity_id=activity_id,
            student_id=student.id,
            file_path=submission_db_path
        )
        db.session.add(new_submission)
        db.session.commit()
        
        flash('Activity uploaded successfully!', 'success')
        return redirect(url_for('dashboard'))

    return redirect(url_for('dashboard'))
    klass = Class.query.get_or_404(class_id)

    # Get all grades for this class
    grades = Grade.query.filter_by(teacher_id=klass.teacher_id).filter(Grade.student.has(klass_id=class_id)).all()

    # Generate CSV
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(['Student Name', 'Period', 'CA Score', 'Exam Score', 'Total Score'])

    for grade in grades:
        student = Student.query.get(grade.student_id)
        writer.writerow([f"{student.first_name} {student.last_name}", grade.period, grade.ca_score, grade.exam_score, grade.score])

    csv_data = output.getvalue()
    output.close()

    return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename=grades_{klass.name}.csv'})

@app.route('/finalize-grades/<int:class_id>', methods=['POST'])
@login_required
def finalize_grades(class_id):
    if current_user.role != 'registrar':
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    klass = Class.query.get_or_404(class_id)
    
    # Finalize all grades for this class
    grades = Grade.query.filter_by(teacher_id=klass.teacher_id).filter(Grade.student.has(klass_id=class_id)).all()
    for grade in grades:
        grade.is_finalized = True
    
    db.session.commit()
    flash('Grades finalized for this class.', 'success')
    return redirect(url_for('dashboard'))
#--------------------------------------------
#Report card generation and download
#--------------------------------------------
@app.route('/report-card/<int:student_id>')
@login_required
def report_card(student_id):
    student = Student.query.get_or_404(student_id)
    
    # Permission Check
    if (current_user.role not in ['admin', 'teacher', 'registrar'] and 
        student.user_id != current_user.id and 
        (current_user.role == 'parent' and student.parent_email != current_user.email)):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Get all grades for this student
    grades_records = Grade.query.filter_by(student_id=student_id).all()
    
    # Organize grades by subject for the Liberian 6-Period format
    # We create a dictionary where keys are subject names
    subjects_list = ["English", "Mathematics", "Social Studies", "General Science", "Literature", "French"]
    structured_subjects = []

    for sub_name in subjects_list:
        # Find all grades for this specific subject
        sub_grades = [g for g in grades_records if g.subject == sub_name]
        
        # Map periods 1-6
        scores = {}
        total = 0
        count = 0
        for p in range(1, 7):
            # Find grade for period p
            match = next((g for g in sub_grades if g.period == p), None)
            val = match.score if match else ""
            scores[f'P{p}'] = val
            if val != "":
                total += val
                count += 1
        
        avg = round(total / count, 1) if count > 0 else ""
        
        structured_subjects.append({
            'name': sub_name,
           'p1': scores.get('P1', 0),
        'p2': scores.get('P2', 0),
        'p3': scores.get('P3', 0),
        'exam': scores.get('P3', 0),    # Mapping P3 to exam as per your logic
        'avg': scores.get('avg', 0),
        'p4': scores.get('P4', 0),
        'p5': scores.get('P5', 0),
        'p6': scores.get('P6', 0),
        'final_exam': scores.get('P6', 0), # Corrected typo from socre[P6]
        'final_avg': scores.get('avg', 0)
        })

    # Prepare the 'data' dictionary for the HTML
    data = {
        'student_name': student.full_name,
        'student_id': student.student_id,
        'level': student.grade_level,
        'subjects': structured_subjects
    }
    
    return render_template('report_card.html', student=student, data=data)

@app.route('/download-report-card/<int:student_id>')
@login_required
def download_report_card(student_id):
    student = Student.query.get_or_404(student_id)
    
    # Same permission checks
    if (current_user.role not in ['admin', 'teacher', 'registrar'] and 
        student.user_id != current_user.id and 
        (current_user.role == 'parent' and student.parent_email != current_user.email)):
        abort(403)
    
    if not student.tuition_cleared and current_user.role not in ['admin', 'teacher', 'registrar']:
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

@app.route('/update-tuition/<int:student_id>', methods=['POST'])
@login_required
def update_tuition(student_id):
    if current_user.role != 'business':
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

@app.route('/transcript/<int:student_id>')
@login_required
def transcript(student_id):
    student = Student.query.get_or_404(student_id)
    
    # Check permissions
    if (current_user.role not in ['admin', 'registrar'] and 
        student.user_id != current_user.id and 
        (current_user.role == 'parent' and student.parent_email != current_user.email)):
        flash('Access denied.', 'danger')
        return redirect(url_for('dashboard'))
    
    # Get all grades for the student
    all_grades = Grade.query.filter_by(student_id=student_id).order_by(Grade.period).all()
    
    # Group by year (assuming academic_year_id indicates year)
    grades_by_year = {}
    for grade in all_grades:
        year_name = grade.student.academic_year.name if grade.student.academic_year else 'Unknown'
        if year_name not in grades_by_year:
            grades_by_year[year_name] = []
        grades_by_year[year_name].append(grade)
    
    # Calculate overall GPA
    all_scores = [g.score for g in all_grades if g.score is not None]
    overall_gpa = SchoolEngine.calculate_gpa(all_scores) if all_scores else 0.0
    
    # Generate QR code for verification
    import qrcode
    import base64
    from io import BytesIO
    
    qr_data = f"Student ID: {student.student_id}, GPA: {overall_gpa:.2f}, Verified: {datetime.now().isoformat()}"
    qr = qrcode.QRCode(version=1, box_size=10, border=5)
    qr.add_data(qr_data)
    qr.make(fit=True)
    img = qr.make_image(fill='black', back_color='white')
    
    buffer = BytesIO()
    img.save(buffer, format="PNG")
    qr_code = base64.b64encode(buffer.getvalue()).decode('utf-8')
    
    return render_template('transcript.html', student=student, grades_by_year=grades_by_year, overall_gpa=overall_gpa, qr_code=qr_code)

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
    if not current_user.is_authenticated or current_user.role != "admin":
        flash("Administrator access required.", "danger")
        return redirect(url_for("dashboard"))
    return None


@app.route("/admin/events")
@login_required
def admin_events_list():
    redirect_resp = _require_admin()
    if redirect_resp:
        return redirect_resp

    events = Event.query.order_by(Event.date.desc()).all()
    return render_template("admin/events/list.html", events=events)


@app.route("/admin/events/create", methods=["GET", "POST"])
@login_required
def admin_events_create():
    redirect_resp = _require_admin()
    if redirect_resp:
        return redirect_resp

    form = EventForm()
    if form.validate_on_submit():
        event = Event(
            title=form.title.data.strip(),
            description=form.description.data.strip(),
            location=form.location.data.strip() if form.location.data else None,
            date=form.date.data
        )
        db.session.add(event)
        db.session.commit()
        flash("Event created successfully.", "success")
        return redirect(url_for("admin_events_list"))

    return render_template("admin/events/create.html", form=form)


@app.route("/admin/events/<int:event_id>/edit", methods=["GET", "POST"])
@login_required
def admin_events_edit(event_id):
    redirect_resp = _require_admin()
    if redirect_resp:
        return redirect_resp

    event = Event.query.get_or_404(event_id)
    form = EventForm(obj=event)
    if form.validate_on_submit():
        event.title = form.title.data.strip()
        event.description = form.description.data.strip()
        event.location = form.location.data.strip() if form.location.data else None
        event.date = form.date.data
        db.session.commit()
        flash("Event updated successfully.", "success")
        return redirect(url_for("admin_events_list"))

    return render_template("admin/events/edit.html", form=form, event=event)

@app.route("/admin/events/<int:event_id>/delete", methods=["GET", "POST"])
@login_required
def admin_events_delete(event_id):
    redirect_resp = _require_admin()
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

#------------------------------------------------------------
# Admin Profille Management
#------------------------------------------------------------
@app.route('/api/profile')
def api_profile():
    if not current_user.is_authenticated:
        return jsonify({"error": "Unauthorized"})
    return jsonify({
        "profile-img": url_for('static', filename='images/pro.PNG'),
        "name": "FRANCIS BROWNELL",
        "title": "CYBER SECURITY STUDENT",
        "skill": "Python, Flask, Numpy, Cryptography,Network Security",
        "Experience":"(ERP)Enterprise Resource Planning at 3GDESIGNS PRINTING \n(Benson & Newport Street) One year of Adminstrating Linux Servers at Hostinger.<br> Computer Teacher at the Muslin High School\n Full Stack Developer at Personal School Management System",
        "Education": "Currently pursuind Degree in Cyber Security at the BlueCrest University College",
        "DoB": "February 22, 2003",
        "contact":"+231 0889358194",
        "email": "xhangochar@gmail.com"
    }) 
# --------------------------------------------------------------
# PUBLIC: Events
# --------------------------------------------------


@app.route('/events')
def events_list():
    events = Event.query.order_by(Event.date.asc()).all()
    return render_template('events/list.html', events=events)


# --------------------------------------------------------------
# ADMIN: Manage Leaders
# --------------------------------------------------------------

@app.route('/admin/leaders')
@login_required
def manage_leaders():
    redirect_resp = _require_admin()
    if redirect_resp:
        return redirect_resp

    leaders = Leader.query.all()
    return render_template('admin/manage_leaders.html', leaders=leaders)



@app.route('/admin/leaders/add', methods=['GET', 'POST'])
@login_required
def add_leader():
    redirect_resp = _require_admin()
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
def edit_leader(leader_id):
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
def delete_leader(leader_id):
    leader = Leader.query.get_or_404(leader_id)
    db.session.delete(leader)
    db.session.commit()
    flash('Leader deleted successfully!', 'danger')
    return redirect(url_for('manage_leaders'))


@app.route('/grades/add', methods=['POST'])
@login_required
def add_grade():
    if current_user.role != "teacher":
        flash("Unauthorized.", "danger")
        return redirect(url_for('dashboard'))

    student_id = request.form.get("student_id", type=int)
    period = request.form.get("period", type=int)
    activity_type = request.form.get("activity_type")
    score = request.form.get("score", type=float)
    submitted = bool(request.form.get("submitted"))

    if not student_id or period is None or not activity_type or score is None:
        flash("All grade fields are required.", "danger")
        return redirect(url_for('dashboard'))

    # If this is an exam or CA score, validate with SchoolEngine
    ca_score = request.form.get("ca_score", type=float)
    exam_score = request.form.get("exam_score", type=float)
    if ca_score is not None and exam_score is not None:
        total = SchoolEngine.calculate_period_total(ca_score, exam_score)
        if total is None:
            flash("Invalid scores! CA must be ≤ 60 and Exam ≤ 40 (MoE Standard).", "danger")
            return redirect(url_for('dashboard'))
        score = total

    teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()
    if not teacher_profile:
        flash("Teacher profile not found.", "danger")
        return redirect(url_for('dashboard'))

    grade = Grade(
        student_id=student_id,
        teacher_id=teacher_profile.id,
        period=period,
        activity_type=activity_type,
        score=score,
        submitted=submitted,
    )

    db.session.add(grade)
    db.session.commit()

    flash("Grade saved successfully.", "success")
    return redirect(url_for('dashboard'))

@app.route('/teacher/scan-assignment/<int:student_id>', methods=['POST'])
@login_required
def scan_assignment(student_id):
    if pytesseract is None or Image is None:
        return jsonify({
            "status": "Error",
            "message": "AI Scanning dependencies (pytesseract/Pillow) not installed."
        }), 500
        
    if 'assignment' not in request.files:
        return jsonify({"status": "Error", "message": "No file uploaded"}), 400
        
    file = request.files['assignment']
    try:
        img = Image.open(file.stream)
        # Convert image to text (OCR)
        student_text = pytesseract.image_to_string(img)
        
        # Simple scoring logic based on user snippet
        score = 0
        answer_key = ["Liberia", "Monrovia", "1847"]
        for word in answer_key:
            if word.lower() in student_text.lower():
                score += 10
                
        return jsonify({
            "status": "Success",
            "suggested_grade": f"{score}/30",
            "detected_text_snippet": student_text[:100] + "..."
        })
    except Exception as e:
        return jsonify({"status": "Error", "message": str(e)}), 500
@app.route('/teacher/dashboard')
@login_required
def teacher_dashboard():
    all_students = Student.query.all()
    return render_template('teacher_dashboard.html',Students=all_students)
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
@app.route('/class/create', methods=['GET', 'POST'])
@login_required
def class_create():
    if current_user.role != "admin/principal":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = CreateClassForm()
    teachers = Teacher.query.order_by(Teacher.first_name, Teacher.last_name).all()
    
    # Teachers can now serve as sponsors too
    teacher_choices = [
        (t.id, (f"{(t.first_name or '').strip()} {(t.last_name or '').strip()}".strip() or (t.user.full_name if t.user else f"Teacher {t.id}")))
        for t in teachers
    ]
    # Sponsors are now teachers who can take on sponsor responsibilities
    sponsor_choices = [(0, "— No Sponsor —")] + teacher_choices

    form.teacher_id.choices = [(0, "— Select Teacher —")] + teacher_choices
    form.sponsor_id.choices = sponsor_choices

    classes = Class.query.options(db.joinedload(Class.teacher), db.joinedload(Class.sponsor)).order_by(Class.name).all()

    assign_form = AssignTeacherForm()
    assign_form.class_id.choices = [(c.id, c.name) for c in classes]
    assign_form.teacher_id.choices = teacher_choices

    if form.validate_on_submit():
        teacher_id = form.teacher_id.data or None
        sponsor_teacher_id = form.sponsor_id.data or None

        if teacher_id == 0:
            teacher_id = None
        if sponsor_teacher_id == 0:
            sponsor_id = None
        else:
            # Convert teacher ID to user ID for sponsor
            sponsor_teacher = Teacher.query.get(sponsor_teacher_id)
            sponsor_id = sponsor_teacher.user_id if sponsor_teacher else None

        new_class = Class(
            name=form.name.data,
            description=form.description.data,
            yearly_fee=form.yearly_fee.data or 0.0,
            teacher_id=teacher_id,
            sponsor_id=sponsor_id
        )
        db.session.add(new_class)
        db.session.commit()

        flash(f"Class '{new_class.name}' created successfully.", "success")
        return redirect(url_for('class_create'))

    return render_template(
        'class_form.html',
        form=form,
        assign_form=assign_form,
        classes=classes,
        teacher_choices=teacher_choices,
        sponsor_choices=sponsor_choices
    )

@app.route('/class/assign', methods=['POST'])
@login_required
def class_assign():
    if current_user.role != "admin/principal":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = AssignTeacherForm()
    teachers = Teacher.query.order_by(Teacher.first_name, Teacher.last_name).all()
    classes = Class.query.order_by(Class.name).all()

    form.class_id.choices = [(c.id, c.name) for c in classes]
    form.teacher_id.choices = [
        (t.id, f"{(t.first_name or '').strip()} {(t.last_name or '').strip()}".strip() or (t.user.full_name if t.user else f"Teacher {t.id}"))
        for t in teachers
    ]

    if form.validate_on_submit():
        klass = db.session.get(Class, form.class_id.data)
        selected_teacher = db.session.get(Teacher, form.teacher_id.data)

        if not klass or not selected_teacher:
            flash("Invalid class or teacher selection.", "danger")
            return redirect(url_for('class_create'))

        klass.teacher_id = selected_teacher.id
        db.session.commit()

        teacher_display = (
            f"{selected_teacher.first_name or ''} {selected_teacher.last_name or ''}".strip()
            or (selected_teacher.user.full_name if selected_teacher.user else f"Teacher {selected_teacher.id}")
        )

        flash(f"Assigned {teacher_display} to {klass.name}.", "success")
    else:
        for field, errors in form.errors.items():
            for err in errors:
                flash(f"{field}: {err}", "danger")

    return redirect(url_for('class_create'))

@app.route('/class/edit/<int:class_id>', methods=['GET', 'POST'])
@login_required
def class_edit(class_id):
    if current_user.role != "admin/principal":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

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
        teacher_id = form.teacher_id.data or None
        sponsor_teacher_id = form.sponsor_id.data or None

        if teacher_id == 0:
            teacher_id = None
        if sponsor_teacher_id == 0:
            sponsor_id = None
        else:
            sponsor_teacher = Teacher.query.get(sponsor_teacher_id)
            sponsor_id = sponsor_teacher.user_id if sponsor_teacher else None

        klass.name = form.name.data
        klass.description = form.description.data
        klass.yearly_fee = form.yearly_fee.data or 0.0
        klass.teacher_id = teacher_id
        klass.sponsor_id = sponsor_id
        
        db.session.commit()
        flash(f"Class '{klass.name}' updated successfully.", "success")
        return redirect(url_for('class_create'))

    return render_template('class_edit.html', form=form, klass=klass)

@app.route('/class/<int:class_id>/sponsor', methods=['POST'])
@login_required
def class_set_sponsor(class_id):
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    klass = Class.query.get_or_404(class_id)
    sponsor_id = request.form.get('sponsor_id', type=int)

    if sponsor_id:
        sponsor = db.session.get(User, sponsor_id)
        # Check if the sponsor is a teacher (since sponsors are now teachers)
        teacher_profile = Teacher.query.filter_by(user_id=sponsor_id).first()
        if not sponsor or not teacher_profile:
            flash("Invalid sponsor selection. Only teachers can be sponsors.", "danger")
            return redirect(url_for('class_create'))
        klass.sponsor_id = sponsor.id
        flash(f"Assigned teacher {sponsor.full_name} as sponsor to {klass.name}.", "success")
    else:
        klass.sponsor_id = None
        flash(f"Sponsor cleared for {klass.name}.", "info")

    db.session.commit()
    return redirect(url_for('class_create'))

# ------------------------ ACADEMIC YEARS ---------------------------
@app.route('/academic-years', methods=['GET', 'POST'])
@login_required
def academic_years():
    if current_user.role != "admin":
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
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    year = AcademicYear.query.get_or_404(year_id)
    if not year.is_active:
        flash("Academic year is already inactive.", "info")
        return redirect(url_for('academic_years'))

    year.is_active = False
    year.end_date = year.end_date or datetime.now(timezone.utc).date()
    db.session.commit()
    flash(f"Academic year {year.name} has been marked as ended.", "success")
    return redirect(url_for('academic_years'))


@app.route('/academic-years/edit/<int:year_id>', methods=['GET', 'POST'])
@login_required
def edit_academic_year(year_id):
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    year = AcademicYear.query.get_or_404(year_id)
    form = AcademicYearForm(obj=year)

    if form.validate_on_submit():
        if form.is_active.data and not year.is_active:
            # Deactivate all other years
            AcademicYear.query.filter(AcademicYear.id != year_id).update({'is_active': False})
        
        year.name = form.name.data
        year.start_date = form.start_date.data
        year.end_date = form.end_date.data
        year.is_active = form.is_active.data
        
        db.session.commit()
        flash(f"Academic year '{year.name}' updated successfully.", "success")
        return redirect(url_for('academic_years'))

    return render_template('academy_year_edit.html', form=form, year=year)

@app.route('/academic-years/reregister-students', methods=['POST'])
@login_required
def reregister_students():
    if current_user.role != "admin":
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

# ------------------------ STUDENT REGISTER -------------------------
@app.route('/register-student', methods=['GET', 'POST'])
@login_required
def register_student():
    if current_user.role not in ["admin", "registrar"]:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = RegisterStudentForm()
    form.klass.choices = [(klass.id, klass.name) for klass in Class.query.order_by(Class.name).all()]
    active_year = AcademicYear.query.filter_by(is_active=True).first()
    form.academic_year.choices = [
        (year.id, year.name) for year in AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    ]
    if active_year:
        form.academic_year.default = active_year.id
        form.process()  # To set the default

    if form.validate_on_submit():
        # Check if student already exists (Returning Student)
        existing_student = Student.query.filter_by(student_id=form.student_id.data).first()
        
        if existing_student:
            student = existing_student
            student.first_name = form.first_name.data
            student.last_name = form.last_name.data
            student.dob = form.dob.data
            student.gender = form.gender.data
            student.parent_email = form.parent_email.data
            student.klass_id = form.klass.data
            student.academic_year_id = form.academic_year.data
            student.level = form.level.data
            student.registrar = current_user.full_name
            student.registration_fees = form.registration_fees.data or 0.0
            student.registration_type = 'Returning'
            flash(f"Returning student {student.full_name} updated for the new academic year.", "info")
        else:
            student = Student(
                first_name=form.first_name.data,
                last_name=form.last_name.data,
                dob=form.dob.data,
                gender=form.gender.data,
                student_id=form.student_id.data,
                parent_email=form.parent_email.data,
                klass_id=form.klass.data,
                academic_year_id=form.academic_year.data,
                level=form.level.data,
                registrar=current_user.full_name,
                registration_fees=form.registration_fees.data or 0.0,
                registration_type='New'
            )
            db.session.add(student)
            flash("New student registered successfully.", "success")

        if form.photo.data:
            photo_file = form.photo.data
            filename = secure_filename(photo_file.filename)
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            filename = f"{timestamp}_{filename}"
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'students')
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, filename)
            photo_file.save(file_path)
            student.photo = os.path.join('uploads', 'students', filename).replace('\\', '/')

        # Optional user account creation
        if form.email.data and form.password.data and not student.user_id:
            user = User(
                email=form.email.data,
                role="student",
                full_name=f"{form.first_name.data} {form.last_name.data}",
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()  # obtain generated user ID
            student.user_id = user.id

        db.session.commit()
        return redirect(url_for('register_student'))

    students = Student.query.all()
    return render_template('register_student.html', form=form, students=students)

@app.route('/edit-student/<int:student_id>', methods=['GET', 'POST'])
@login_required
def edit_student(student_id):
    if current_user.role not in ["admin", "registrar"]:
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    student = Student.query.get_or_404(student_id)
    form = RegisterStudentForm(obj=student)
    form.klass.choices = [(klass.id, klass.name) for klass in Class.query.order_by(Class.name).all()]
    form.academic_year.choices = [
        (year.id, year.name) for year in AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    ]

    if form.validate_on_submit():
        existing_student = Student.query.filter_by(student_id=form.student_id.data).first()
        if existing_student and existing_student.id != student_id:
            flash("That student ID is already assigned to another student.", "danger")
            return redirect(url_for('edit_student', student_id=student_id))

        student.first_name = form.first_name.data
        student.last_name = form.last_name.data
        student.dob = form.dob.data
        student.gender = form.gender.data
        student.student_id = form.student_id.data
        student.parent_email = form.parent_email.data
        student.klass_id = form.klass.data
        student.academic_year_id = form.academic_year.data
        student.level = form.level.data
        student.registration_fees = form.registration_fees.data or 0.0
        student.registrar = current_user.full_name

        if form.photo.data:
            photo_file = form.photo.data
            filename = secure_filename(photo_file.filename)
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            filename = f"{timestamp}_{filename}"
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads', 'students')
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, filename)
            photo_file.save(file_path)
            student.photo = os.path.join('uploads', 'students', filename).replace('\\', '/')

        if form.email.data and form.password.data and not student.user_id:
            user = User(
                email=form.email.data,
                role="student",
                full_name=f"{form.first_name.data} {form.last_name.data}",
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()
            student.user_id = user.id

        db.session.commit()
        flash(f"Student {student.full_name} has been updated.", "success")
        return redirect(url_for('register_student'))

    students = Student.query.all()
    return render_template('register_student.html', form=form, students=students)

# -------------------------- ANNOUNCEMENTS -------------------------
@app.route('/announcements', methods=['GET', 'POST'])
@login_required
def announcements():
    if current_user.role not in {"admin", "teacher"}:
        flash("Unauthorized access to announcements.", "danger")
        return redirect(url_for('dashboard'))

    form = AnnouncementForm()
    items = Announcement.query.order_by(Announcement.id.desc()).all()

    if form.validate_on_submit():
        announcement = Announcement(
            title=form.title.data,
            body=form.body.data,
            audience=form.audience.data,
            author=current_user.full_name,
            created_at=datetime.now(timezone.utc).isoformat(timespec="minutes")
        )
        db.session.add(announcement)
        db.session.commit()
        flash("Announcement posted successfully.", "success")
        return redirect(url_for('announcements'))

    return render_template('announcements.html', form=form, items=items)

# ---------------------- BUSINESS MANAGEMENT -----------------------
@app.route('/business-management', methods=['GET', 'POST'])
@role_required('VPI', 'business', 'admin') # Allow VPI, Business manager, and Admin
def business_management():
    # 1. Initialize Forms
    form = TransactionForm()
    enroll_form = EnrollmentForm()
    payment_form = PaymentForm()
    
    # Populate choices
    payment_form.student.choices = [(s.id, f"{s.first_name} {s.last_name} ({s.student_id})") for s in Student.query.order_by(Student.last_name).all()]
    all_years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    payment_form.academic_year.choices = [(y.id, y.name) for y in all_years]
    
    selected_year = request.args.get('year', all_years[0].name if all_years else '2025-2026')
    selected_year_obj = next((y for y in all_years if y.name == selected_year), None)
    
    # 2. Handle Daily Transaction Posting
    if 'submit_transaction' in request.form and form.validate_on_submit():
        new_tx = BusinessTransaction(
            date=form.date.data,
            type=form.type.data,
            amount=form.amount.data,
            category=form.category.data,
            description=form.description.data,
            academic_year=selected_year
        )
        db.session.add(new_tx)
        db.session.commit()
        flash('Transaction recorded successfully!', 'success')
        return redirect(url_for('business_management', year=selected_year))

    # 3. Handle Student Payment Posting
    if 'submit_payment' in request.form and payment_form.validate_on_submit():
        new_payment = StudentPayment(
            student_id=payment_form.student.data,
            academic_year_id=payment_form.academic_year.data,
            term=payment_form.term.data,
            installment=payment_form.installment.data,
            description=payment_form.description.data,
            amount_paid=payment_form.amount_paid.data,
            paid_on=datetime.now(timezone.utc)
        )
        db.session.add(new_payment)
        
        # Also record as income in business transactions
        student = Student.query.get(payment_form.student.data)
        income_tx = BusinessTransaction(
            date=datetime.now(timezone.utc).strftime('%Y-%m-%d'),
            type='income',
            amount=payment_form.amount_paid.data,
            category='Student Fees',
            description=f"Fee payment from {student.full_name}: {payment_form.description.data or 'Tuition'}",
            balance_after=0,  # This will be updated after the transaction is committed
            current_fee=payment_form.amount_paid.data,
            academic_year=selected_year
        )
        db.session.add(income_tx)
        
        db.session.commit()
        flash('Student payment recorded successfully!', 'success')
        return redirect(url_for('business_management', year=selected_year))

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
    
    # Get fee for the selected year
    from models import SchoolFee
    fee_obj = SchoolFee.query.filter_by(academic_year_id=selected_year_obj.id).first() if selected_year_obj else None
    yearly_fee_default = fee_obj.amount if fee_obj else 0

    for k in classes:
        # Use class-specific fee if set, otherwise global year fee
        current_fee = k.yearly_fee if k.yearly_fee and k.yearly_fee > 0 else yearly_fee_default
        
        student_count = Student.query.filter_by(klass_id=k.id, status='ACTIVE').count()
        total_expected = student_count * current_fee
        
        total_collected = db.session.query(func.sum(StudentPayment.amount_paid)).join(Student).filter(
            Student.klass_id == k.id,
            StudentPayment.academic_year_id == (selected_year_obj.id if selected_year_obj else 0)
        ).scalar() or 0
        
        class_analytics.append({
            'name': k.name,
            'students_count': student_count,
            'yearly_fee': current_fee,
            'total_collected': total_collected,
            'balance': total_expected - total_collected
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
    if current_user.role not in ["admin", "business"]:
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
    if current_user.role not in ["admin", "business"]:
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
    if current_user.role not in ["admin", "business"]:
        flash("Unauthorized access to financial reports.", "danger")
        return redirect(url_for('dashboard'))

    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()

    return render_template(
        'financial_reports.html',
        years=years
    )

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
    
    # Simple average grade logic for now (mocking it if not exists)
    def get_avg(s):
        grades = Grade.query.filter_by(student_id=s.id).all()
        if not grades: return 0
        return sum(g.score for g in grades if g.score) / len(grades)

    failing_students = [s for s in all_students if get_avg(s) < 70]
    
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
    # 5. Student List with Search/Filters
    search_query = request.args.get('search', '')
    status_filter = request.args.get('status', '')
    query = Student.query
    if search_query:
        query = query.filter(Student.first_name.contains(search_query) | Student.last_name.contains(search_query))
    
    students_list = query.all()
    # Filter by average grade if needed
    if status_filter == 'failing':
        students_list = [s for s in students_list if get_avg(s) < 70]
    elif status_filter == 'suspended':
        students_list = [s for s in students_list if s.status == 'SUSPENDED']
        
    # 6. Recent Activity Feeds
    recent_transactions = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(5).all()
    security_events = SecurityLog.query.order_by(SecurityLog.timestamp.desc()).limit(5).all()
    
    # Decorate students with average for template
    for s in students_list:
        s.average = get_avg(s)
        
    return render_template('principal_dashboard.html', 
                            financial_stats=financial_stats,
                           academic_stats=academic_stats,
                           disciplinary_stats=disciplinary_stats,
                           security_stats=security_stats,
                           students=students_list,
                           recent_transactions=recent_transactions,
                           security_events=security_events,
                           current_user=current_user,
                           active_year=active_year)

# -------------------------- VPI DASHBOARD -------------------------------
@app.route('/vpi/dashboard')
@role_required('VPI', 'business', 'admin')
def vpi_dashboard():
    # Financial overview for VPI
    total_revenue = db.session.query(db.func.sum(BusinessTransaction.amount)).filter_by(type='income', is_deleted=False).scalar() or 0
    total_expenses = db.session.query(db.func.sum(BusinessTransaction.amount)).filter_by(type='expense', is_deleted=False).scalar() or 0
    net_profit = total_revenue - total_expenses
    
    # Recent transactions
    recent_transactions = BusinessTransaction.query.filter_by(is_deleted=False).order_by(BusinessTransaction.date.desc()).limit(10).all()
    
    # Transaction summary by category
    income_categories = db.session.query(BusinessTransaction.category, db.func.sum(BusinessTransaction.amount)).filter_by(type='income', is_deleted=False).group_by(BusinessTransaction.category).all()
    expense_categories = db.session.query(BusinessTransaction.category, db.func.sum(BusinessTransaction.amount)).filter_by(type='expense', is_deleted=False).group_by(BusinessTransaction.category).all()
    
    return render_template('vpi_dashboard.html',
                          total_revenue=total_revenue,
                          total_expenses=total_expenses,
                          net_profit=net_profit,
                          recent_transactions=recent_transactions,
                          income_categories=income_categories,
                          expense_categories=expense_categories)

# -------------------------- DEAN DASHBOARD -------------------------------
@app.route('/dean/dashboard')
@role_required('Dean')
def dean_dashboard():
    # Disciplinary overview for Dean
    active_suspensions = Suspension.query.filter(Suspension.return_date > datetime.now(timezone.utc)).count()
    total_suspensions = Suspension.query.count()
    
    # Recent suspensions
    recent_suspensions = Suspension.query.order_by(Suspension.id.desc()).limit(10).all()
    
    # Discipline incidents
    discipline_incidents = Discipline.query.order_by(Discipline.id.desc()).limit(10).all()
    
    # Students with disciplinary records
    students_with_discipline = db.session.query(Student, db.func.count(Discipline.id)).join(Discipline).group_by(Student.id).all()
    
    return render_template('dean_dashboard.html',
                          active_suspensions=active_suspensions,
                          total_suspensions=total_suspensions,
                          recent_suspensions=recent_suspensions,
                          discipline_incidents=discipline_incidents,
                          students_with_discipline=students_with_discipline,
                          current_user=current_user)

# -------------------------- VPA DASHBOARD -------------------------------
@app.route('/vpa/dashboard')
@role_required('VPA')
def vpa_dashboard():
    # Academic overview for VPA
    total_students = Student.query.count()
    total_assessments = Assessment.query.count()
    
    # Recent assessments
    recent_assessments = Assessment.query.order_by(Assessment.id.desc()).limit(10).all()
    
    # Grade distribution
    grade_distribution = db.session.query(Grade.score, db.func.count(Grade.id)).group_by(Grade.score).all()
    
    # Students by performance level
    excellent_students = db.session.query(Student).join(Grade).filter(Grade.score >= 90).distinct().count()
    good_students = db.session.query(Student).join(Grade).filter(Grade.score.between(80, 89)).distinct().count()
    average_students = db.session.query(Student).join(Grade).filter(Grade.score.between(70, 79)).distinct().count()
    needs_improvement = db.session.query(Student).join(Grade).filter(Grade.score < 70).distinct().count()
    
    return render_template('vpa_dashboard.html',
                          total_students=total_students,
                          total_assessments=total_assessments,
                          recent_assessments=recent_assessments,
                          grade_distribution=grade_distribution,
                          excellent_students=excellent_students,
                          good_students=good_students,
                          average_students=average_students,
                          needs_improvement=needs_improvement,
                          current_user=current_user)

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
            salary_amount=form.salary_amount.data,
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
    male_count = Student.query.filter_by(gender='M').count()
    female_count = Student.query.filter_by(gender='F').count()
    other_count = Student.query.filter(~Student.gender.in_(['M', 'F'])).count()
    return jsonify({
        'male': male_count,
        'female': female_count,
        'other': other_count
    })

@app.route('/analytics/enrollment')
@login_required
def analytics_enrollment():
    from collections import defaultdict
    class_counts = defaultdict(int)
    enrollments = Enrollment.query.all()
    for e in enrollments:
        class_counts[str(e.class_id)] += 1
    return jsonify({
        'total': len(enrollments),
        'by_class': class_counts
    })

@app.route('/delete-user/<int:user_id>', methods=['POST'])
@login_required
def delete_user(user_id):
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))
    user = User.query.get_or_404(user_id)
    db.session.delete(user)
    db.session.commit()
    flash("User deleted successfully.", "success")
    return redirect(url_for('admin_users'))

@app.route('/analytics/payments')
@login_required
def analytics_payments():
    from collections import defaultdict
    year_counts = defaultdict(float)
    payments = StudentPayment.query.all()
    for p in payments:
        year_counts[str(p.academic_year_id)] += p.amount_paid
    return jsonify({
        'total': sum(year_counts.values()),
        'by_year': year_counts
    })

@app.route('/analytics/grades')
@login_required
def analytics_grades():
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
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    from forms import CreateUserForm
    form = CreateUserForm()
    users = User.query.order_by(User.id.desc()).all()

    classes = Class.query.order_by(Class.name).all()

    if form.validate_on_submit():
        user = User(
            email=form.email.data,
            full_name=form.full_name.data,
            role=form.role.data,
            home_address=form.home_address.data,
            telephone_number=form.telephone_number.data
        )
        user.set_password(form.password.data)

        photo_file = form.photo.data
        if photo_file:
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            filename = secure_filename(photo_file.filename)
            timestamp = datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S%f')
            filename = f"{timestamp}_{filename}"
            file_path = os.path.join(upload_dir, filename)
            photo_file.save(file_path)
            user.photo = os.path.join('uploads', filename).replace('\\', '/')

        db.session.add(user)
        db.session.flush()

        if user.role == 'teacher':
            name_parts = (user.full_name or '').split()
            first_name = name_parts[0] if name_parts else None
            last_name = ' '.join(name_parts[1:]) if len(name_parts) > 1 else None
            teacher_profile = Teacher(user_id=user.id, first_name=first_name, last_name=last_name)
            db.session.add(teacher_profile)

        db.session.commit()
        flash(f"User {user.full_name} ({user.role}) created successfully.", "success")
        return redirect(url_for('admin_users'))

    return render_template('admin_users.html', form=form, users=users, classes=classes)


@app.route('/admin/users/<int:user_id>/unlock', methods=['POST'])
@login_required
def unlock_user(user_id):
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    user = User.query.get_or_404(user_id)

    # Clear recent failed login attempts (last 15 minutes to be safe)
    fifteen_minutes_ago = datetime.now(timezone.utc) - timedelta(minutes=15)
    deleted_count = SecurityLog.query.filter(
        SecurityLog.event == 'FAILED_LOGIN',
        SecurityLog.timestamp >= fifteen_minutes_ago
    ).delete()

    db.session.commit()
    flash(f"Account unlocked for {user.full_name}. Cleared {deleted_count} recent failed login attempts.", "success")
    return redirect(url_for('admin_users'))


if __name__ == '__main__':
    print("🚀 Starting server with Waitress...")
    print("Visit http://localhost:3000 in your browser")
    try:
        serve(app, host='0.0.0.0', port=3000, threads=6)
    except Exception as e:
        print(f"Waitress failed: {e}")
        print("Falling back to Flask dev server...")
        app.run(debug=True, host='0.0.0.0', port=3000)
