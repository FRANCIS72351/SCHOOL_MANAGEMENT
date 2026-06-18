import unittest
import uuid
from datetime import date

from app import (
    app,
    check_promotion_criteria,
    execute_academic_rollover,
    get_class_registration_fee,
    preview_moe_academic_rollover,
    promotion_pass_score,
    max_failing_subjects_for_promotion,
    save_class_registration_fees,
)
from constants import ROLE_ADMIN
from models import (
    AcademicYear, BusinessTransaction, Class, Grade, RolloverLog,
    SchoolFee, Student, StudentPayment, User, db,
)


class AcademicRolloverTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config.update({
            'TESTING': True,
            'WTF_CSRF_ENABLED': False,
            'PROMOTION_PASS_SCORE': 70,
            'MAX_FAILING_SUBJECTS': 2,
        })
        self.test_email = f'rollover-test-{uuid.uuid4().hex}@test.com'
        self.created_ids = {
            'users': [], 'classes': [], 'years': [], 'students': [], 'grades': [],
            'school_fees': [], 'payments': [], 'transactions': [],
        }
        self.client = self.app.test_client()
        with self.app.app_context():
            admin = User(email=self.test_email, full_name='Rollover Admin', role=ROLE_ADMIN)
            admin.set_password('password')
            db.session.add(admin)
            db.session.flush()
            self.created_ids['users'].append(admin.id)
            self.admin_id = admin.id

            test_class = Class(name=f'Rollover Class {uuid.uuid4().hex[:8]}', grade_level=10)
            db.session.add(test_class)
            db.session.flush()
            self.created_ids['classes'].append(test_class.id)
            self.class_id = test_class.id

            test_year = AcademicYear(
                name=f'20{uuid.uuid4().hex[:2]}-20{uuid.uuid4().hex[:2]}',
                start_date=date(2025, 9, 1),
                end_date=date(2026, 6, 30),
                is_active=True,
                created_by=admin.id,
            )
            db.session.add(test_year)
            db.session.flush()
            self.created_ids['years'].append(test_year.id)
            self.year_id = test_year.id

            student = Student(
                student_id=f'ROL{uuid.uuid4().hex[:6].upper()}',
                first_name='Promo',
                last_name='Student',
                dob=date(2008, 1, 1),
                gender='M',
                klass_id=test_class.id,
                grade_level=10,
                academic_year_id=test_year.id,
                status='ACTIVE',
            )
            db.session.add(student)
            db.session.flush()
            self.created_ids['students'].append(student.id)
            self.student_id = student.id

            for subject, score in [('Mathematics', 80), ('English', 75), ('Science', 72)]:
                grade = Grade(
                    student_id=student.id,
                    academic_year_id=test_year.id,
                    class_id=test_class.id,
                    subject=subject,
                    subject_name=subject,
                    score=score,
                    marking_period=1,
                )
                db.session.add(grade)
                db.session.flush()
                self.created_ids['grades'].append(grade.id)

            db.session.commit()

    def tearDown(self):
        with self.app.app_context():
            RolloverLog.query.filter(RolloverLog.user_id.in_(self.created_ids['users'])).delete(
                synchronize_session=False
            )
            for tx_id in self.created_ids['transactions']:
                BusinessTransaction.query.filter_by(id=tx_id).delete(synchronize_session=False)
            for payment_id in self.created_ids['payments']:
                StudentPayment.query.filter_by(id=payment_id).delete(synchronize_session=False)
            for fee_id in self.created_ids['school_fees']:
                SchoolFee.query.filter_by(id=fee_id).delete(synchronize_session=False)
            for grade_id in self.created_ids['grades']:
                Grade.query.filter_by(id=grade_id).delete(synchronize_session=False)
            for student_id in self.created_ids['students']:
                Student.query.filter_by(id=student_id).delete(synchronize_session=False)
            for year_id in self.created_ids['years']:
                AcademicYear.query.filter_by(id=year_id).delete(synchronize_session=False)
            for class_id in self.created_ids['classes']:
                Class.query.filter_by(id=class_id).delete(synchronize_session=False)
            for user_id in self.created_ids['users']:
                User.query.filter_by(id=user_id).delete(synchronize_session=False)
            db.session.commit()

    def login(self):
        with self.client.session_transaction() as sess:
            sess['_user_id'] = str(self.admin_id)
            sess['_fresh'] = True

    def test_promotion_config_defaults(self):
        with self.app.app_context():
            self.assertEqual(promotion_pass_score(), 70)
            self.assertEqual(max_failing_subjects_for_promotion(), 2)

    def test_check_promotion_criteria_passes(self):
        with self.app.app_context():
            student = db.session.get(Student, self.student_id)
            year = db.session.get(AcademicYear, self.year_id)
            self.assertTrue(check_promotion_criteria(student, year))

    def test_preview_page_requires_login(self):
        response = self.client.get('/admin/academic-rollover')
        self.assertIn(response.status_code, (302, 401))

    def test_preview_page_shows_counts(self):
        self.login()
        response = self.client.get('/admin/academic-rollover')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Promoted', response.data)
        self.assertIn(b'Execute Rollover', response.data)

    def test_preview_post_json(self):
        self.login()
        response = self.client.post(
            '/admin/academic-rollover',
            data={'preview': '1', 'format': 'json'},
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertIn('promoted', payload)
        self.assertEqual(payload['student_total'], 1)

    def test_save_class_registration_fees(self):
        with self.app.app_context():
            saved = save_class_registration_fees(
                self.year_id,
                {self.class_id: 150.0},
                included_class_ids={self.class_id},
            )
            db.session.commit()
            fee = SchoolFee.query.filter_by(
                academic_year_id=self.year_id,
                class_id=self.class_id,
                fee_type='registration',
            ).first()
            self.assertIsNotNone(fee)
            self.created_ids['school_fees'].append(fee.id)
            self.assertEqual(saved, 1)
            self.assertEqual(float(fee.amount), 150.0)
            self.assertEqual(get_class_registration_fee(self.class_id, self.year_id), 150.0)

    def test_wizard_rollover_posts_per_class_registration_income(self):
        with self.app.app_context():
            admin = db.session.get(User, self.admin_id)
            second_class = Class(name=f'Fee Class {uuid.uuid4().hex[:6]}', grade_level=11)
            db.session.add(second_class)
            db.session.flush()
            self.created_ids['classes'].append(second_class.id)

            target_year = AcademicYear(
                name=f'Target {uuid.uuid4().hex[:6]}',
                start_date=date(2026, 9, 1),
                end_date=date(2027, 6, 30),
                is_active=False,
                created_by=admin.id,
            )
            db.session.add(target_year)
            db.session.flush()
            self.created_ids['years'].append(target_year.id)

            with self.client.session_transaction() as sess:
                sess['_user_id'] = str(self.admin_id)
                sess['_fresh'] = True

            from flask_login import login_user
            with self.app.test_request_context():
                login_user(admin)
                results = execute_academic_rollover(
                    end_current_year=False,
                    target_mode='existing',
                    target_year_id=target_year.id,
                    new_year_name=None,
                    new_year_start=None,
                    new_year_end=None,
                    apply_promotions=False,
                    promotion_map={},
                    reset_tuition_cleared=False,
                    charge_registration_fee=True,
                    class_registration_fees={self.class_id: 200.0, second_class.id: 300.0},
                    included_class_ids={self.class_id, second_class.id},
                    exclude_statuses=set(),
                )

            self.assertEqual(results['fees_configured'], 2)
            self.assertEqual(results['fees_recorded'], 1)

            payment = StudentPayment.query.filter_by(
                student_id=self.student_id,
                academic_year_id=target_year.id,
            ).first()
            self.assertIsNotNone(payment)
            self.created_ids['payments'].append(payment.id)
            self.assertEqual(float(payment.amount_paid), 200.0)
            self.assertIn('registration', (payment.description or '').lower())

            income = BusinessTransaction.query.filter(
                BusinessTransaction.description.like(f'%[SP-{payment.id}]%'),
                BusinessTransaction.is_deleted.is_(False),
            ).first()
            self.assertIsNotNone(income)
            self.created_ids['transactions'].append(income.id)
            self.assertEqual(income.type, 'income')
            self.assertEqual(income.category, 'Registration Fees')
            self.assertEqual(float(income.amount), 200.0)

    def test_wizard_page_lists_class_fee_table(self):
        self.login()
        response = self.client.get('/academic-years/rollover')
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'Classes &amp; Registration Fees', response.data)
        self.assertIn(b'reg_fee_', response.data)
        self.assertIn(b'include_class_', response.data)


if __name__ == '__main__':
    unittest.main()
