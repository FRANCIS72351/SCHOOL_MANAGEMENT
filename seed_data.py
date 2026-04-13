from app import app, db
from models import User, Student, Teacher, Class, Payment, Sponsor, Grade, Announcement
from werkzeug.security import generate_password_hash

def seed():
    with app.app_context():
        db.drop_all()
        db.create_all()
        admin = User(email='admin@example.com', role='admin', password_hash=generate_password_hash('adminpass'), full_name='Admin User')
        teacher = User(email='teacher@example.com', role='teacher', password_hash=generate_password_hash('teacherpass'), full_name='Alice Teacher')
        student_user = User(email='student@example.com', role='student', password_hash=generate_password_hash('studentpass'), full_name='Bob Student')
        parent = User(email='parent@example.com', role='parent', password_hash=generate_password_hash('parentpass'), full_name='Parent User')
        sponsor = User(email='sponsor@example.com', role='sponsor', password_hash=generate_password_hash('sponsorpass'), full_name='Sponsor User')
        db.session.add_all([admin, teacher, student_user, parent, sponsor])
        db.session.commit()
        t = Teacher(user_id=teacher.id, first_name='Alice', last_name='Teacher', subject='Mathematics')
        s = Student(user_id=student_user.id, student_id='S1001', first_name='Bob', last_name='Student', dob='2007-05-10', gender='Male', parent_email='parent@example.com', klass='Form 1')
        db.session.add_all([t,s])
        db.session.commit()
        c = Class(name='Form 1 - Math', description='Form 1 mathematics class', teacher_id=t.id)
        db.session.add(c); db.session.commit()
        g = Grade(student_id=s.id, teacher_id=teacher.id, subject='Math', score=88.5, remarks='Good work')
        p = Payment(student_id=s.id, amount=150.00, paid_on='2025-01-15', note='Tuition term 1')
        sp = Sponsor(user_id=sponsor.id, student_id=s.id, amount=200.00)
        ann = Announcement(title='Welcome', body='Welcome to the new term', audience='all', author='admin@example.com', created_at='2025-01-01')
        db.session.add_all([g,p,sp,ann])
        db.session.commit()
        print('Seeded data. Admin: admin@example.com / adminpass')

if __name__ == '__main__':
    seed()
