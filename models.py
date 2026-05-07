from datetime import datetime, timezone
from flask_login import UserMixin
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash


db = SQLAlchemy()

# --------------------------------------------------------------
# USER MODEL
# --------------------------------------------------------------



class User(db.Model, UserMixin):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=True)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(50), nullable=False)  # admin, teacher, student, parent, sponsor
    full_name = db.Column(db.String(120), nullable=False)
    photo = db.Column(db.String(200))
    totp_secret = db.Column(db.String(32)) # 2FA
    home_address = db.Column(db.String(255))
    telephone_number = db.Column(db.String(20))
                        
    def set_password(self, password):
        """Hash and store the provided plaintext password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Verify the provided password against the stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f"<User {self.full_name}>"


class SecurityLog(db.Model):
    __tablename__ = "security_logs"
    id = db.Column(db.Integer, primary_key=True)
    ip_address = db.Column(db.String(45))
    event = db.Column(db.String(100))  # e.g., "FAILED_LOGIN", "SUSPENDED_ENTRY"
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Suspension(db.Model):
    __tablename__ = "suspensions"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'))
    reason = db.Column(db.Text)
    return_date = db.Column(db.DateTime)

    student = db.relationship("Student", backref=db.backref("suspensions", lazy="dynamic"))

class Room(db.Model):
    __tablename__ = "rooms"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50))
    capacity = db.Column(db.Integer)

# --------------------------------------------------------------
# ACADEMIC YEAR
# --------------------------------------------------------------
class AcademicYear(db.Model):
    __tablename__ = "academic_year"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(32), unique=True, nullable=False)  # e.g. "2025–2026"
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    is_active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_on = db.Column(db.DateTime, default=db.func.now())
    current_year = db.Column(db.String(20))  # e.g. "2025" or "Grade 12"
    klass_id = db.Column(db.Integer, db.ForeignKey("class.id"))

    def __repr__(self):
        return f"<AcademicYear {self.name}>"


# --------------------------------------------------------------
# CLASS MODEL
# --------------------------------------------------------------
class Class(db.Model):
    __tablename__ = "class"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(250))
    yearly_fee = db.Column(db.Float, default=0.0)
    teacher_id = db.Column(db.Integer, db.ForeignKey("teacher.id"))
    sponsor_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    teacher = db.relationship("Teacher", backref=db.backref("classes", lazy="dynamic"))
    sponsor = db.relationship(
        "User",
        foreign_keys=[sponsor_id],
        backref=db.backref("sponsored_classes", lazy="dynamic")
    )

    def __repr__(self):
        return f"<Class {self.name}>"


# --------------------------------------------------------------
# STUDENT MODEL
# --------------------------------------------------------------
class Student(db.Model):
    __tablename__ = "student"
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    student_id = db.Column(db.String(50), unique=True, nullable=False)
    first_name = db.Column(db.String(100), nullable=False)
    last_name = db.Column(db.String(100), nullable=False)
    dob = db.Column(db.Date, nullable=False)
    gender = db.Column(db.String(10), nullable=False)
    parent_email = db.Column(db.String(120))
    photo = db.Column(db.String(200))
    photo_filename = db.Column(db.String(200), default='default_student.png')  # For admissions photos
    status = db.Column(db.String(20), default='ACTIVE')  # ACTIVE, SUSPENDED, REPEAT
    grade_level = db.Column(db.Integer)  # 1-12
    level = db.Column(db.String(50))  # Elementary, Junior High, Senior High
    student_id_code = db.Column(db.String(20), unique=True)
    klass_id = db.Column(db.Integer, db.ForeignKey("class.id"))
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_year.id"))
    registration_type = db.Column(db.String(20), default='New')  # 'New' or 'Returning'
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    tuition_cleared = db.Column(db.Boolean, default=False)  # Financial clearance for report cards
    registrar = db.Column(db.String(100))  # Registrar who registered the student
    registration_fees = db.Column(db.Float, default=0.0)  # Registration fees paid

    user = db.relationship("User", backref=db.backref("student_profile", uselist=False))
    klass = db.relationship("Class", backref=db.backref("students", lazy="dynamic"))
    academic_year = db.relationship("AcademicYear", backref=db.backref("students", lazy="dynamic"))

    def __repr__(self):
        return f"<Student {self.first_name} {self.last_name}>"

    @property
    def full_name(self):
        """Return the user's full name if available, otherwise combine first and last name."""
        if self.user and getattr(self.user, 'full_name', None):
            return self.user.full_name
        return f"{self.first_name} {self.last_name}"


# --------------------------------------------------------------
# LEADERS (FOR ABOUT PAGE)
# --------------------------------------------------------------
class LeaderCategory(db.Model):
    __tablename__ = "leader_category"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)

    leaders = db.relationship("Leader", backref="category", lazy=True)

    def __repr__(self):
        return f"<LeaderCategory {self.name}>"


class Leader(db.Model):
    __tablename__ = "leader"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    role = db.Column(db.String(120))
    bio = db.Column(db.Text)
    contact = db.Column(db.String(120))
    photo = db.Column(db.String(200))
    category_id = db.Column(db.Integer, db.ForeignKey("leader_category.id"))

    def __repr__(self):
        return f"<Leader {self.name}>"


# --------------------------------------------------------------
# TEACHER MODEL
# --------------------------------------------------------------
class Teacher(db.Model):
    __tablename__ = "teacher"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    first_name = db.Column(db.String(80))
    last_name = db.Column(db.String(80))
    subject = db.Column(db.String(80))

    user = db.relationship("User", backref=db.backref("teacher_profile", uselist=False))

    def __repr__(self):
        return f"<Teacher {self.first_name} {self.last_name}>"


# --------------------------------------------------------------
# PAYROLL
# --------------------------------------------------------------
class Payroll(db.Model):
    __tablename__ = "payroll"

    id = db.Column(db.Integer, primary_key=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    occupation = db.Column(db.String(100), nullable=False)
    month = db.Column(db.String(20), nullable=False)  # e.g. "January 2025"
    salary_amount = db.Column(db.Float, nullable=False)
    paid = db.Column(db.Boolean, default=False)
    created_on = db.Column(db.DateTime, default=datetime.utcnow)

    staff = db.relationship("User", backref="payroll_records")

    def __repr__(self):
        return f"<Payroll {self.occupation} - {self.month}>"


# --------------------------------------------------------------
# SCHOOL FEE AND PAYMENTS
# --------------------------------------------------------------
class SchoolFee(db.Model):
    __tablename__ = "school_fee"

    id = db.Column(db.Integer, primary_key=True)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_year.id"), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    academic_year = db.relationship("AcademicYear", backref="fees")


class StudentPayment(db.Model):
    __tablename__ = "student_payment"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    academic_year_id = db.Column(db.Integer, db.ForeignKey("academic_year.id"), nullable=False)
    term = db.Column(db.Integer, nullable=False)
    installment = db.Column(db.Integer, nullable=True)  # 1, 2, 3
    amount_paid = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(250), nullable=True) # "Tuition", "Uniform", etc.
    paid_on = db.Column(db.DateTime, default=datetime.utcnow)

    student = db.relationship("Student", backref="payments")
    academic_year = db.relationship("AcademicYear", backref="payments")
# --------------------------------------------------------------
# BUSINESS TRANSACTIONS
# --------------------------------------------------------------
class BusinessTransaction(db.Model):
    __tablename__ = "business_transaction"

    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.String(20), nullable=False)
    type = db.Column(db.String(20), nullable=False)  # 'income' or 'expense'
    amount = db.Column(db.Float, nullable=False)
    description = db.Column(db.String(250))
    category = db.Column(db.String(120))
    academic_year = db.Column(db.String(32))  # e.g. "2025-2026"
    is_deleted = db.Column(db.Boolean, default=False)
    deleted_at = db.Column(db.DateTime, nullable=True)
    deleted_by_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=True)

    deleted_by = db.relationship("User", foreign_keys=[deleted_by_id], backref="deleted_transactions")

    def __repr__(self):
        return f"<Transaction {self.type} - {self.amount}>"


# --------------------------------------------------------------
# ENROLLMENT
# --------------------------------------------------------------
class Enrollment(db.Model):
    __tablename__ = "enrollment"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("class.id"), nullable=False)

    student = db.relationship("Student", backref="enrollments")
    klass = db.relationship("Class", backref="enrollments")


# --------------------------------------------------------------
# ASSESSMENTS & GRADES
# --------------------------------------------------------------
class Assessment(db.Model):
    __tablename__ = "assessment"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120))
    description = db.Column(db.String(250))
    date = db.Column(db.String(20))
    max_score = db.Column(db.Float)
    klass_id = db.Column(db.Integer, db.ForeignKey("class.id"))


class Grade(db.Model):
    __tablename__ = "grade"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    teacher_id = db.Column(db.Integer, db.ForeignKey("teacher.id"))
    subject = db.Column(db.String(120))
    subject_name = db.Column(db.String(100))
    score = db.Column(db.Float)
    ca_score = db.Column(db.Float)  # 60%
    exam_score = db.Column(db.Float)  # 40%
    marking_period = db.Column(db.Integer)  # 1-6
    period = db.Column(db.Integer)
    # Liberia MoE 6-Period System
    p1 = db.Column(db.Integer, default=0)
    p2 = db.Column(db.Integer, default=0)
    p3 = db.Column(db.Integer, default=0)
    p4 = db.Column(db.Integer, default=0)
    p5 = db.Column(db.Integer, default=0)
    p6 = db.Column(db.Integer, default=0)
    activity_type = db.Column(db.String(50))
    submitted = db.Column(db.Boolean, default=False)
    remarks = db.Column(db.String(200))
    is_finalized = db.Column(db.Boolean, default=False)  # Locked by Registrar

    student = db.relationship("Student", backref="grades")
    teacher = db.relationship("Teacher", backref="grades")

    @property
    def final_average(self):
        scores = [self.p1, self.p2, self.p3, self.p4, self.p5, self.p6]
        # Only average periods that have a score > 0
        valid_scores = [s for s in scores if s > 0]
        return round(sum(valid_scores) / len(valid_scores), 1) if valid_scores else 0


# --------------------------------------------------------------
# ATTENDANCE, ANNOUNCEMENTS, SPONSORS, DISCIPLINE
# --------------------------------------------------------------
class Attendance(db.Model):
    __tablename__ = "attendance"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    date = db.Column(db.String(20))
    status = db.Column(db.String(20))  # present, absent, late
    notes = db.Column(db.String(200))


class Sponsor(db.Model):
    __tablename__ = "sponsor"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    amount = db.Column(db.Float)


class Event(db.Model):
    __tablename__ = "event"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=False)
    location = db.Column(db.String(200))
    date = db.Column(db.Date, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Event {self.title} ({self.date})>"


class Announcement(db.Model):
    __tablename__ = "announcement"

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200))
    body = db.Column(db.Text)
    audience = db.Column(db.String(50))  # all, parents, students, teachers, sponsors
    author = db.Column(db.String(120))
    created_at = db.Column(db.String(50), default='')


class Discipline(db.Model):
    __tablename__ = "discipline"

    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    offense = db.Column(db.String(250))
    action_taken = db.Column(db.String(250))
    notes = db.Column(db.Text)
    created_at = db.Column(db.String(50), default='')


# --- VPI (INSTITUTIONAL) LAYER ---
class Asset(db.Model):
    __tablename__ = "asset"
    id = db.Column(db.Integer, primary_key=True)
    item_name = db.Column(db.String(100))
    status = db.Column(db.String(50))  # 'Functional', 'Broken'


class MaintenanceTicket(db.Model):
    __tablename__ = "maintenance_ticket"
    id = db.Column(db.Integer, primary_key=True)
    asset_id = db.Column(db.Integer, db.ForeignKey("asset.id"))
    description = db.Column(db.Text)
    priority = db.Column(db.String(20))  # Low, Medium, High
    status = db.Column(db.String(20), default='Open')  # Open, In Progress, Closed
    reported_at = db.Column(db.DateTime, default=datetime.utcnow)


class SchoolPermit(db.Model):
    __tablename__ = "school_permit"
    id = db.Column(db.Integer, primary_key=True)
    permit_type = db.Column(db.String(100))
    expiry_date = db.Column(db.Date)


# --- VPA (ACADEMIC) LAYER ---
class Course(db.Model):
    __tablename__ = "course"
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100))
    code = db.Column(db.String(20))


class MarkingPeriod(db.Model):
    __tablename__ = "marking_period"
    id = db.Column(db.Integer, primary_key=True)
    period_number = db.Column(db.Integer)  # 1st Period through 6th Period
    is_active = db.Column(db.Boolean, default=False)
    weight_ca = db.Column(db.Float, default=60.0)  # Continuous Assessment %
    weight_exam = db.Column(db.Float, default=40.0)  # Exam %


# --- DEAN (DISCIPLINE) LAYER ---
class DisciplinaryLog(db.Model):
    __tablename__ = "disciplinary_log"
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    infraction = db.Column(db.String(200))
    action_taken = db.Column(db.String(100))
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Activity(db.Model):
    __tablename__ = "activity"
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text)
    file_name = db.Column(db.String(200)) # The task file for students to download
    klass_id = db.Column(db.Integer, db.ForeignKey("class.id"))
    created_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    klass = db.relationship("Class", backref=db.backref("activities", lazy="dynamic"))

class Submission(db.Model):
    __tablename__ = "submission"
    id = db.Column(db.Integer, primary_key=True)
    activity_id = db.Column(db.Integer, db.ForeignKey("activity.id"))
    student_id = db.Column(db.Integer, db.ForeignKey("student.id"))
    file_path = db.Column(db.String(200), nullable=False)
    submitted_at = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

    activity = db.relationship("Activity", backref=db.backref("submissions", lazy="dynamic"))
    student = db.relationship("Student", backref=db.backref("submissions", lazy="dynamic"))

