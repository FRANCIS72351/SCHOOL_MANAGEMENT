from flask_wtf import FlaskForm
from wtforms import (
    StringField, PasswordField, SubmitField, SelectField,
    FloatField, TextAreaField, DateField, IntegerField,
    BooleanField, FileField
)
from wtforms.validators import DataRequired, Email, Optional
from flask_wtf.file import FileAllowed


# --------------------------------------------------------------
# AUTHENTICATION FORMS
# --------------------------------------------------------------

class LeaderForm(FlaskForm):
    name = StringField('Full Name', validators=[DataRequired()])
    role = StringField('Position/Role', validators=[DataRequired()])
    bio = TextAreaField('Short Bio', validators=[DataRequired()])
    contact = StringField('Contact Email or Link', validators=[Optional()])
    category = SelectField('Category', coerce=int, validators=[Optional()])
    photo = FileField('Photo', validators=[FileAllowed(['jpg', 'png', 'jpeg'], 'Images only!')])
    submit = SubmitField('Add Leader')

class LoginForm(FlaskForm):
    email = StringField("Email", validators=[DataRequired(), Email()])
    password = PasswordField("Password", validators=[DataRequired()])
    submit = SubmitField("Login")


class ChangeDetailsForm(FlaskForm):
    email = StringField("New Email", validators=[DataRequired(), Email()])
    password = PasswordField("New Password", validators=[Optional()])
    submit = SubmitField("Update")


# --------------------------------------------------------------
# STUDENT & CLASS FORMS
# --------------------------------------------------------------
class EnrollmentForm(FlaskForm):
    student = SelectField("Student", coerce=int, validators=[DataRequired()])
    klass = SelectField("Class", coerce=int, validators=[DataRequired()])
    submit = SubmitField("Enroll Student")


class RegisterStudentForm(FlaskForm):
    first_name = StringField("First Name", validators=[DataRequired()])
    last_name = StringField("Last Name", validators=[DataRequired()])
    email = StringField("Email", validators=[Optional(), Email()])
    password = PasswordField("Password (Optional)", validators=[Optional()])
    dob = DateField("Date of Birth", validators=[Optional()])
    gender = SelectField(
        "Gender",
        choices=[("Male", "Male"), ("Female", "Female"), ("Other", "Other")],
        validators=[DataRequired()]
    )
    student_id = StringField("Student ID", validators=[DataRequired()])
    parent_email = StringField("Parent Email", validators=[Optional(), Email()])
    photo = FileField("Photo", validators=[FileAllowed(["jpg", "png", "jpeg"], "Images only!")])
    klass = SelectField("Assign Class", coerce=int, validators=[Optional()])
    academic_year = SelectField("Academic Year", coerce=int, validators=[Optional()])
    submit = SubmitField("Register")


class ClassForm(FlaskForm):
    name = StringField("Class Name", validators=[DataRequired()])
    description = TextAreaField("Description", validators=[Optional()])
    teacher_id = IntegerField("Teacher Internal ID", validators=[Optional()])
    submit = SubmitField("Save Class")


class CreateClassForm(FlaskForm):
    name = StringField('Class Name', validators=[DataRequired()])
    description = TextAreaField('Description', validators=[Optional()])
    teacher_id = SelectField('Teacher', coerce=int, validators=[Optional()])
    sponsor_id = SelectField('Sponsor', coerce=int, validators=[Optional()])
    submit = SubmitField('Save Class')


class AssignTeacherForm(FlaskForm):
    class_id = SelectField('Class', coerce=int, validators=[DataRequired()])
    teacher_id = SelectField('Teacher', coerce=int, validators=[DataRequired()])
    submit = SubmitField('Assign Teacher')


# --------------------------------------------------------------
# FINANCE & PAYROLL FORMS
# --------------------------------------------------------------
class SetFeeForm(FlaskForm):
    academic_year = SelectField("Academic Year", coerce=int, validators=[DataRequired()])
    amount = FloatField("School Fee Amount", validators=[DataRequired()])
    submit = SubmitField("Set Fees")


class PaymentForm(FlaskForm):
    student = SelectField("Student", coerce=int, validators=[DataRequired()])
    academic_year = SelectField("Academic Year", coerce=int, validators=[DataRequired()])
    term = SelectField(
        "Term",
        choices=[(1, "Term 1"), (2, "Term 2"), (3, "Term 3"), (4, "Term 4")],
        coerce=int,
        validators=[DataRequired()]
    )
    amount_paid = FloatField("Amount Paid", validators=[DataRequired()])
    submit = SubmitField("Record Payment")


class PayrollForm(FlaskForm):
    staff_id = SelectField("Select Staff", coerce=int, validators=[DataRequired()])
    occupation = StringField("Occupation", validators=[DataRequired()])
    month = StringField("Month (e.g. January 2025)", validators=[DataRequired()])
    salary_amount = FloatField("Salary Amount", validators=[DataRequired()])
    paid = BooleanField("Paid")
    submit = SubmitField("Save Payroll Record")


class BusinessTransactionForm(FlaskForm):
    date = StringField("Date (YYYY-MM-DD)", validators=[DataRequired()])
    type = SelectField(
        "Type",
        choices=[("income", "Income"), ("expense", "Expense")],
        validators=[DataRequired()]
    )
    amount = FloatField("Amount", validators=[DataRequired()])
    description = TextAreaField("Description", validators=[Optional()])
    category = StringField("Category", validators=[Optional()])
    submit = SubmitField("Save Transaction")


# --------------------------------------------------------------
# ACADEMIC YEAR & SPONSORSHIP
# --------------------------------------------------------------
class AcademicYearForm(FlaskForm):
    name = StringField("Academic Year (e.g. 2025–2026)", validators=[DataRequired()])
    start_date = DateField("Start Date", validators=[DataRequired()])
    end_date = DateField("End Date", validators=[Optional()])
    is_active = BooleanField("Active", default=True)
    submit = SubmitField("Save")


class SponsorForm(FlaskForm):
    user_id = IntegerField("User ID (if admin creating)", validators=[Optional()])
    student_id = IntegerField("Student ID", validators=[DataRequired()])
    amount = FloatField("Amount", validators=[DataRequired()])
    submit = SubmitField("Sponsor")


# --------------------------------------------------------------
# COMMUNICATION & DISCIPLINE
# --------------------------------------------------------------
class AnnouncementForm(FlaskForm):
    title = StringField("Title", validators=[DataRequired()])
    body = TextAreaField("Body", validators=[DataRequired()])
    audience = SelectField(
        "Audience",
        choices=[
            ("all", "All"),
            ("parents", "Parents"),
            ("students", "Students"),
            ("teachers", "Teachers"),
            ("sponsors", "Sponsors")
        ],
        validators=[DataRequired()]
    )
    submit = SubmitField("Post")


class EventForm(FlaskForm):
    title = StringField("Event Title", validators=[DataRequired()])
    description = TextAreaField("Description", validators=[DataRequired()])
    location = StringField("Location", validators=[Optional()])
    date = DateField("Date", validators=[DataRequired()])
    submit = SubmitField("Save Event")


class ConfirmDeleteForm(FlaskForm):
    submit = SubmitField("Delete")


class DisciplineForm(FlaskForm):
    student_id = IntegerField("Student Internal ID", validators=[DataRequired()])
    offense = StringField("Offense", validators=[DataRequired()])
    action_taken = StringField("Action Taken", validators=[Optional()])
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Report")


# --------------------------------------------------------------
# ATTENDANCE & GRADING
# --------------------------------------------------------------
class AttendanceForm(FlaskForm):
    student_id = IntegerField("Student ID", validators=[DataRequired()])
    date = DateField("Date", validators=[DataRequired()])
    status = SelectField(
        "Status",
        choices=[("present", "Present"), ("absent", "Absent"), ("late", "Late")],
        validators=[DataRequired()]
    )
    notes = TextAreaField("Notes", validators=[Optional()])
    submit = SubmitField("Record")


class GradeForm(FlaskForm):
    student_id = IntegerField("Student ID", validators=[DataRequired()])
    teacher_id = IntegerField("Teacher ID", validators=[DataRequired()])
    activity_type = SelectField(
        "Activity Type",
        choices=[
            ("assignment", "Assignment"),
            ("test", "Test"),
            ("classwork", "Class Work")
        ],
        validators=[DataRequired()]
    )
    score = FloatField("Score", validators=[DataRequired()])
    period = IntegerField("Period", validators=[DataRequired()])
    submitted = BooleanField("Submit Grade", default=False)
    submit = SubmitField("Save")


class CreateUserForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    full_name = StringField('Full Name', validators=[DataRequired()])
    password = PasswordField('Password', validators=[DataRequired()])
    role = SelectField('Role', choices=[
        ('admin', 'Admin'),
        ('teacher', 'Teacher'),
        ('student', 'Student'),
        ('parent', 'Parent'),
        ('sponsor', 'Sponsor'),
        ('registrar', 'Registrar')
    ], validators=[DataRequired()])
    photo = FileField('Profile Photo', validators=[FileAllowed(['jpg', 'jpeg', 'png', 'gif'], 'Images only!')])
    submit = SubmitField('Create User')

TransactionForm = BusinessTransactionForm
