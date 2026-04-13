import unittest
from datetime import date
from app import app
from models import db, User, Student, Class, AcademicYear
from models import User, Student, Class, AcademicYear
from constants import ROLE_ADMIN

class ExportRoutesTestCase(unittest.TestCase):
    def setUp(self):
        self.app = app
        self.app.config['TESTING'] = True
        self.app.config['WTF_CSRF_ENABLED'] = False
        self.client = self.app.test_client()
        with self.app.app_context():
            db.create_all()
            # Create a test admin user and student
            admin = User(email='admin@test.com', full_name='Admin User', role=ROLE_ADMIN)
            admin.set_password('password')
            db.session.add(admin)
            test_class = Class(name='Test Class', description='Demo class', teacher_id=None)
            db.session.add(test_class)
            test_year = AcademicYear(name='2025–2026', start_date=date(2025, 1, 1), end_date=date(2026, 1, 1), is_active=True, created_by=admin.id)
            db.session.add(test_year)
            db.session.flush()  # get test_class.id and test_year.id
            student = Student(student_id='S001', first_name='Test', last_name='Student', dob=date(2005, 1, 1), gender='M', klass_id=test_class.id, academic_year_id=test_year.id)
            db.session.add(student)
            db.session.commit()
            self.admin_id = admin.id

    def tearDown(self):
        with self.app.app_context():
            db.session.remove()
            db.drop_all()

    def login(self):
        self.client.post('/login', data={
            'email': 'admin@test.com',
            'password': 'password'
        })

    def test_export_students_csv(self):
        self.login()
        with self.app.app_context():
            response = self.client.get('/export/students', follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b'Student ID,First Name,Last Name', response.data)
            self.assertIn(b'Test,Student', response.data)

if __name__ == '__main__':
    unittest.main()
