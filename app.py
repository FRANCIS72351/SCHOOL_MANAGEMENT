# app.py — Keep Track Digital School Management System
from flask import Flask, render_template, redirect, url_for, flash, request, Response, jsonify, send_file, current_app, abort
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect
from datetime import datetime
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

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from functools import wraps

def role_required(role_name):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            # 1. Check if user is logged in
            # 2. Check if their role matches (VPI, VPA, Dean, etc.)
            if not current_user.is_authenticated or current_user.role != role_name:
                abort(403) # "Forbidden" error
            return f(*args, **kwargs)
        return decorated_function
    return decorator
def log_security_event(description):
    """Log security events to the database."""
    event = SecurityLog(
        ip_address=request.remote_addr,
        event=description,
        timestamp=datetime.utcnow()
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
    # If more than 5 fails in 10 mins, block
    fails = SecurityLog.query.filter_by(ip_address=ip, event='FAILED_LOGIN').count()
    return fails >= 5

# Local imports handled in init_db.py to avoid circular imports during app import
from models import (
    db, User, Student, Teacher, Class, Enrollment, Grade,
    Attendance, Sponsor, Announcement, Discipline, Payroll,
    Assessment, AcademicYear, BusinessTransaction, StudentPayment,
    Leader, LeaderCategory, Event, SecurityLog, Suspension
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
        end_date = datetime.utcnow() + timedelta(days=days)
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

    # --- VPI LOGIC ---
    @staticmethod
    def get_dashboard_stats():
        from models import Class
        return {
            "total_students": Student.query.count(),
            "active_suspensions": Student.query.filter_by(status='SUSPENDED').count(),
            "facility_utilization": "85%" # Calculated vs total capacity
        }

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
        .filter(Event.date >= datetime.utcnow().date())
        .limit(3)
        .all()
    )
    total_events = Event.query.count()
    return render_template(
        'index.html',
        events=upcoming_events,
        highlighted_event=upcoming_events[0] if upcoming_events else None,
        current_year=datetime.utcnow().year,
        total_events=total_events
    )

# ----------------------------- LOGIN -------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    form = LoginForm()
    ip = request.headers.get('X-Forwarded-For', request.remote_addr)
    
    if check_brute_force(ip):
        flash('Account locked temporarily due to multiple failed login attempts. Please contact Administrator.', 'danger')
        log_incident('BRUTE_FORCE_BLOCK')
        return render_template('login.html', form=form)

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
@app.route('/dashboard')
@login_required
def dashboard():
    latest_year = AcademicYear.query.order_by(AcademicYear.start_date.desc()).first()

    stats = {
        'students': Student.query.count(),
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
        "business": "dashboard_business.html"
    }

    template_name = template_map.get(current_user.role)
    selected_year_name = request.args.get("year") or (latest_year.name if latest_year else None)

    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    selected_year = next((y for y in years if y.name == selected_year_name), years[0] if years else None)
    if not template_name:
        flash("No dashboard is configured for your role.", "warning")
        return redirect(url_for('index'))

    announcements_link = current_user.role in {"admin", "teacher"}

    if template_name == "dashboard_teacher.html":
        teacher_profile = Teacher.query.filter_by(user_id=current_user.id).first()

        class_ids = []
        if teacher_profile:
            class_ids = [klass.id for klass in Class.query.filter_by(teacher_id=teacher_profile.id)]
        students = (
            Student.query.filter(Student.klass_id.in_(class_ids)).order_by(Student.first_name, Student.last_name).all()
            if class_ids
            else Student.query.order_by(Student.first_name, Student.last_name).all()
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
        )

    if template_name == "dashboard_registrar.html":
        form = RegisterStudentForm()
        form.klass.choices = [
            (klass.id, klass.name)
            for klass in Class.query.order_by(Class.name.asc()).all()
        ]
        form.academic_year.choices = [(year.id, year.name) for year in years]

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
                "current_year": student.academic_year.name if student.academic_year else None,
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
        )

    return render_template(
        template_name,
        stats=stats,
        counts=stats,
        announcements_link=announcements_link,
        selected_year=selected_year_name,
        selected_year_obj=selected_year,
        years=years
    )
@app.route("/about")
def about():
    categories = LeaderCategory.query.order_by(LeaderCategory.name.asc()).all()
    return render_template("about.html", categories=categories)


@app.route("/contact")
def contact():
    return render_template("contact.html")


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


# --------------------------------------------------------------
# PUBLIC: Events
# --------------------------------------------------------------


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
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    form = CreateClassForm()
    teachers = Teacher.query.order_by(Teacher.first_name, Teacher.last_name).all()
    sponsors = User.query.filter_by(role="sponsor").order_by(User.full_name.asc()).all()

    teacher_choices = [
        (t.id, (f"{(t.first_name or '').strip()} {(t.last_name or '').strip()}".strip() or (t.user.full_name if t.user else f"Teacher {t.id}")))
        for t in teachers
    ]
    sponsor_choices = [(0, "— No Sponsor —")] + [
        (s.id, s.full_name or f"Sponsor {s.id}")
        for s in sponsors
    ]

    form.teacher_id.choices = [(0, "— Select Teacher —")] + teacher_choices
    form.sponsor_id.choices = sponsor_choices

    classes = Class.query.options(db.joinedload(Class.teacher), db.joinedload(Class.sponsor)).order_by(Class.name).all()

    assign_form = AssignTeacherForm()
    assign_form.class_id.choices = [(c.id, c.name) for c in classes]
    assign_form.teacher_id.choices = teacher_choices

    if form.validate_on_submit():
        teacher_id = form.teacher_id.data or None
        sponsor_id = form.sponsor_id.data or None

        if teacher_id == 0:
            teacher_id = None
        if sponsor_id == 0:
            sponsor_id = None

        new_class = Class(
            name=form.name.data,
            description=form.description.data,
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
    if current_user.role != "admin":
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
        if not sponsor or sponsor.role != "sponsor":
            flash("Invalid sponsor selection.", "danger")
            return redirect(url_for('class_create'))
        klass.sponsor_id = sponsor.id
        flash(f"Assigned sponsor {sponsor.full_name} to {klass.name}.", "success")
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
    year.end_date = year.end_date or datetime.utcnow().date()
    db.session.commit()
    flash(f"Academic year {year.name} has been marked as ended.", "success")
    return redirect(url_for('academic_years'))


@app.route('/academic-years/reregister', methods=['POST'])
@login_required
def reregister_students():
    if current_user.role != "admin":
        flash("Unauthorized access.", "danger")
        return redirect(url_for('dashboard'))

    active_year = AcademicYear.query.filter_by(is_active=True).order_by(AcademicYear.start_date.desc()).first()
    if not active_year:
        flash("No active academic year found. Please create one before re-registering students.", "warning")
        return redirect(url_for('academic_years'))

    for student in Student.query.all():
        student.academic_year_id = active_year.id

    db.session.commit()
    flash("Students have been re-registered to the current academic year.", "success")
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
    form.academic_year.choices = [
        (year.id, year.name) for year in AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    ]

    if form.validate_on_submit():
        student = Student(
            first_name=form.first_name.data,
            last_name=form.last_name.data,
            dob=form.dob.data,
            gender=form.gender.data,
            student_id=form.student_id.data,
            parent_email=form.parent_email.data,
            klass_id=form.klass.data,
            academic_year_id=form.academic_year.data
        )

        # Optional user account creation
        if form.email.data and form.password.data:
            user = User(
                email=form.email.data,
                role="student",
                full_name=f"{form.first_name.data} {form.last_name.data}",
            )
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.flush()  # obtain generated user ID
            student.user_id = user.id

        db.session.add(student)
        db.session.commit()
        flash("Student registered successfully.", "success")
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
            created_at=datetime.utcnow().isoformat(timespec="minutes")
        )
        db.session.add(announcement)
        db.session.commit()
        flash("Announcement posted successfully.", "success")
        return redirect(url_for('announcements'))

    return render_template('announcements.html', form=form, items=items)

# ---------------------- BUSINESS MANAGEMENT -----------------------
@app.route('/business-management', methods=['GET', 'POST'])
@role_required('VPI')
def business_management():
    # 1. Initialize Forms
    form = TransactionForm()
    enroll_form = EnrollmentForm()
    payment_form = PaymentForm()
    payment_form.student.choices = [(s.id, f"{s.first_name} {s.last_name}") for s in Student.query.order_by(Student.first_name).all()]
    
    latest_year = AcademicYear.query.order_by(AcademicYear.start_date.desc()).first()
    selected_year = request.args.get('year', latest_year.name if latest_year else '2025-2026')

    # 2. Handle Daily Transaction Posting (VPI Expenses/Income)
    if form.validate_on_submit() and 'amount' in request.form:
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

    # Handle Student Payment Posting
    if payment_form.validate_on_submit() and 'amount_paid' in request.form:
        payment = StudentPayment(
            student_id=payment_form.student.data,
            academic_year_id=year_obj.id if year_obj else None,
            term=payment_form.term.data,
            amount_paid=payment_form.amount_paid.data,
            paid_on=datetime.utcnow()
        )
        db.session.add(payment)
        db.session.commit()
        flash('Payment recorded successfully!', 'success')
        return redirect(url_for('business_management', year=selected_year))

    # 3. Calculate Global Financial KPIs
    income_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(
        BusinessTransaction.type == 'income', 
        BusinessTransaction.academic_year == selected_year,
        BusinessTransaction.is_deleted == False
    ).scalar() or 0
    expense_total = db.session.query(func.sum(BusinessTransaction.amount)).filter(
        BusinessTransaction.type == 'expense', 
        BusinessTransaction.academic_year == selected_year,
        BusinessTransaction.is_deleted == False
    ).scalar() or 0

    # 4. Institutional Analytics (Class-by-Class Financial Health)
    classes = Class.query.all()
    class_analytics = []
    
    # Get fee for the selected year
    year_obj = AcademicYear.query.filter_by(name=selected_year).first()
    from models import SchoolFee
    fee_obj = SchoolFee.query.filter_by(academic_year_id=year_obj.id).first() if year_obj else None
    yearly_fee = fee_obj.amount if fee_obj else 0

    for k in classes:
        # Calculate total fee expected for this class
        student_count = Student.query.filter_by(klass_id=k.id, status='ACTIVE').count()
        total_expected = student_count * yearly_fee

        # Sum all payments made by students in this class
        total_collected = db.session.query(func.sum(StudentPayment.amount_paid)).join(Student).filter(
            Student.klass_id == k.id
        ).scalar() or 0
        
        class_analytics.append({
            'name': k.name,
            'students_count': student_count,
            'yearly_fee': yearly_fee,
            'total_collected': total_collected,
            'balance': total_expected - total_collected
        })

    # 5. Search Logic for Student Payments
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
    
    years = AcademicYear.query.order_by(AcademicYear.start_date.desc()).all()
    payrolls = Payroll.query.order_by(Payroll.created_on.desc()).all()

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
        years=years,
        payrolls=payrolls
    )

@app.route('/delete-transaction/<int:id>', methods=['POST'])
@role_required('VPI')
def soft_delete_transaction(id):
    tx = BusinessTransaction.query.get_or_404(id)
    # Logic: Mark as deleted instead of removing from DB
    tx.is_deleted = True
    tx.deleted_at = datetime.utcnow()
    tx.deleted_by_id = current_user.id
    # Log this for your Cybersecurity Audit
    log_security_event(f"Transaction ID {id} was soft-deleted by {current_user.username or current_user.full_name}")
    db.session.commit()
    flash('Transaction removed from view. Audit log updated.', 'info')
    return redirect(url_for('business_management'))

@app.route('/business-overview')
@login_required
def business_overview():
    if current_user.role != "admin":
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
    if current_user.role != "admin":
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
    if current_user.role != "admin":
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
    active_suspensions = Suspension.query.filter(Suspension.return_date > datetime.utcnow()).count()
    disciplinary_stats = {
        "active_suspensions": active_suspensions
    }
    # 4. Security Stats (Admin Data)
    # Count unique IPs blocked for brute force in last 24 hours
    yesterday = datetime.utcnow() - timedelta(days=1)
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
                           security_events=security_events)

@app.route('/payroll', methods=['GET', 'POST'])
@login_required
def payroll():
    if current_user.role != "admin":
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
            created_on=datetime.utcnow()
        )
        db.session.add(record)
        db.session.commit()
        flash("Payroll record added successfully.", "success")
        return redirect(url_for('payroll'))

    payrolls = Payroll.query.order_by(Payroll.created_on.desc()).all()
    return render_template('payroll.html', form=form, payrolls=payrolls)

# -------------------------- REPORT CARD ---------------------------
@app.route('/report-card/<int:student_id>')
@login_required
def report_card(student_id):
    student = Student.query.get_or_404(student_id)
    grades = Grade.query.filter_by(student_id=student_id).all()
    return render_template('report_card.html', student=student, grades=grades)

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
            role=form.role.data
        )
        user.set_password(form.password.data)

        photo_file = form.photo.data
        if photo_file:
            upload_dir = os.path.join(current_app.root_path, 'static', 'uploads')
            os.makedirs(upload_dir, exist_ok=True)
            filename = secure_filename(photo_file.filename)
            timestamp = datetime.utcnow().strftime('%Y%m%d%H%M%S%f')
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


if __name__ == '__main__':
    print("🚀 Server starting on http://127.0.0.1:3000")
    print("Visit http://localhost:3000 in your browser")
    serve(app, host='0.0.0.0', port=3000, threads=6)
