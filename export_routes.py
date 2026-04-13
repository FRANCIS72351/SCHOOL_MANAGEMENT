from flask import Response, request, redirect, url_for, flash
from flask_login import login_required, current_user
from io import StringIO, BytesIO
import csv
from datetime import datetime
from reportlab.pdfgen import canvas
from models import Student, Grade, Attendance, StudentPayment, Sponsor, BusinessTransaction
from constants import ROLE_ADMIN, ROLE_REGISTRAR, ROLE_TEACHER

def init_export_routes(app):
    @app.route('/export/students')
    @login_required
    def export_students():
        # allow admins and registrars
        if current_user.role not in [ROLE_ADMIN, ROLE_REGISTRAR]:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        from sqlalchemy.orm import joinedload
        year = request.args.get('year')
        query = Student.query.options(joinedload(Student.klass), joinedload(Student.academic_year))
        if year:
            query = query.filter_by(current_year=year)
        students = query.order_by(Student.last_name, Student.first_name).all()

        # Stream CSV to avoid large memory usage
        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Student ID', 'First Name', 'Last Name', 'Class', 'Gender', 'Parent Email', 'Year'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)

            for student in students:
                writer.writerow([
                    student.student_id,
                    student.first_name,
                    student.last_name,
                    getattr(student.klass, 'name', '') if student.klass else '',
                    student.gender,
                    student.parent_email or '',
                    getattr(student.academic_year, 'name', '') if getattr(student, 'academic_year', None) else ''
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)

        return Response(
            generate(),
            mimetype='text/csv',
            headers={'Content-Disposition': 'attachment; filename=students.csv'}
        )

    @app.route('/export/grades')
    @login_required
    def export_grades():
        if current_user.role not in [ROLE_ADMIN, ROLE_TEACHER]:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        grades = Grade.query.all()
        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Student ID', 'Subject', 'Score', 'Remarks'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for grade in grades:
                writer.writerow([
                    grade.student_id,
                    grade.subject,
                    grade.score,
                    grade.remarks or ''
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=grades.csv'})

    @app.route('/export/attendance')
    @login_required
    def export_attendance():
        if current_user.role not in [ROLE_ADMIN, ROLE_TEACHER]:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        attendance = Attendance.query.all()
        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Student ID', 'Date', 'Status', 'Notes'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for record in attendance:
                writer.writerow([
                    record.student_id,
                    record.date,
                    record.status,
                    record.notes or ''
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=attendance.csv'})

    @app.route('/export/payments')
    @login_required
    def export_payments():
        if current_user.role != ROLE_ADMIN:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        payments = StudentPayment.query.all()
        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Student ID', 'Amount', 'Term', 'Paid On'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for payment in payments:
                writer.writerow([
                    payment.student_id,
                    payment.amount_paid,
                    payment.term,
                    payment.paid_on.strftime('%Y-%m-%d') if payment.paid_on else ''
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=payments.csv'})

    @app.route('/export/sponsors')
    @login_required
    def export_sponsors():
        if current_user.role not in [ROLE_ADMIN, ROLE_REGISTRAR]:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        sponsors = Sponsor.query.all()

        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Sponsor ID', 'Name', 'Amount'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for s in sponsors:
                writer.writerow([
                    s.id,
                    getattr(s, 'name', ''),
                    getattr(s, 'amount', '')
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        return Response(generate(), mimetype='text/csv', headers={'Content-Disposition': 'attachment; filename=sponsors.csv'})

    @app.route('/export/business')
    @login_required
    def export_business():
        if current_user.role != ROLE_ADMIN:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        year = request.args.get('year')
        query = BusinessTransaction.query.order_by(BusinessTransaction.date.desc())
        if year:
            query = query.filter(BusinessTransaction.date.startswith(year))
        transactions = query.all()

        def generate():
            buf = StringIO()
            writer = csv.writer(buf)
            writer.writerow(['Date', 'Type', 'Amount', 'Category', 'Description'])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
            for txn in transactions:
                writer.writerow([
                    txn.date,
                    txn.type,
                    txn.amount,
                    txn.category or '',
                    txn.description or ''
                ])
                yield buf.getvalue()
                buf.seek(0)
                buf.truncate(0)
        filename = f"business_transactions_{year or 'all'}.csv"
        return Response(
            generate(),
            mimetype='text/csv',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    @app.route('/report/business/pdf')
    @login_required
    def report_business_pdf():
        if current_user.role != ROLE_ADMIN:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        year = request.args.get('year')
        query = BusinessTransaction.query.order_by(BusinessTransaction.date.asc())
        if year:
            query = query.filter(BusinessTransaction.date.startswith(year))
        transactions = query.all()

        buffer = BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.setFont('Helvetica-Bold', 16)
        pdf.drawString(50, 800, 'Business Transactions Report')
        pdf.setFont('Helvetica', 12)
        title_suffix = f" - {year}" if year else ''
        pdf.drawString(50, 780, f"Academic Year: {year if year else 'All'}{title_suffix}")

        y = 750
        pdf.setFont('Helvetica', 11)
        if not transactions:
            pdf.drawString(50, y, 'No transactions found for the selected period.')
        else:
            for txn in transactions:
                if y < 60:
                    pdf.showPage()
                    y = 800
                    pdf.setFont('Helvetica', 11)
                pdf.drawString(50, y, f"Date: {txn.date} | Type: {txn.type.capitalize()} | Amount: ₱{txn.amount:.2f}")
                y -= 16
                if txn.category or txn.description:
                    details = []
                    if txn.category:
                        details.append(f"Category: {txn.category}")
                    if txn.description:
                        details.append(f"Description: {txn.description}")
                    pdf.drawString(70, y, ' | '.join(details))
                    y -= 16
                y -= 8

        pdf.showPage()
        pdf.save()
        buffer.seek(0)
        filename = f"business_report_{year or 'all'}.pdf"
        return Response(
            buffer,
            mimetype='application/pdf',
            headers={'Content-Disposition': f'attachment; filename={filename}'}
        )

    @app.route('/report-students/pdf')
    @login_required
    def report_students_pdf():
        # Small students listing PDF for admins/registrars
        if current_user.role not in ['admin', 'registrar']:
            flash('Access denied.', 'danger')
            return redirect(url_for('dashboard'))

        year = request.args.get('year')
        query = Student.query
        if year:
            query = query.filter_by(current_year=year)
        students = query.order_by(Student.last_name, Student.first_name).all()

        buffer = BytesIO()
        p = canvas.Canvas(buffer)
        p.setFont('Helvetica-Bold', 14)
        p.drawString(50, 800, f"Students List{(' - ' + year) if year else ''}")
        y = 780
        p.setFont('Helvetica', 11)
        for s in students:
            if y < 50:
                p.showPage()
                y = 800
                p.setFont('Helvetica', 11)
            p.drawString(50, y, f"{s.student_id} - {s.last_name}, {s.first_name} ({s.klass or ''})")
            y -= 16

        p.showPage()
        p.save()
        buffer.seek(0)
        return Response(buffer, mimetype='application/pdf', headers={
            'Content-Disposition': f'attachment; filename=students_{year or "all"}.pdf'
        })

    return app