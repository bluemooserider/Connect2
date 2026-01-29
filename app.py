import os
import functools
import sqlite3
import time
from datetime import datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_apscheduler import APScheduler
from sqlalchemy import text

# --- CONFIGURATION ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
PERSISTENT_STORAGE = os.environ.get('PERSISTENT_STORAGE_PATH', BASE_DIR)
DB_PATH = os.path.join(PERSISTENT_STORAGE, 'database.db')
ARCHIVE_DIR = os.path.join(PERSISTENT_STORAGE, 'archive')
UPLOAD_FOLDER = os.path.join(PERSISTENT_STORAGE, 'uploads')

for path in [ARCHIVE_DIR, UPLOAD_FOLDER]:
    if not os.path.exists(path): os.makedirs(path)

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{DB_PATH}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'secret_key_123')
app.config['SCHEDULER_API_ENABLED'] = True

db = SQLAlchemy(app)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()

# --- MODELS ---
task_group_association = db.Table('task_group_association',
                                  db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                                  db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))

task_user_association = db.Table('task_user_association',
                                 db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                                 db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True))

user_group_association = db.Table('user_group_association',
                                  db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                                  db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50));
    last_name = db.Column(db.String(50))
    email = db.Column(db.String(120), unique=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user', nullable=False)
    groups = db.relationship('Group', secondary=user_group_association, backref=db.backref('users', lazy='dynamic'))

    @property
    def display_name(
            self): return f"{self.first_name} {self.last_name}" if self.first_name and self.last_name else self.username


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(100), nullable=False)
    contact_name = db.Column(db.String(100));
    location = db.Column(db.String(250))
    main_contact_email = db.Column(db.String(100));
    phone_number = db.Column(db.String(20))
    projects = db.relationship('Project', backref='client', lazy=True, cascade="all, delete-orphan")


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    proposed_start_date = db.Column(db.Date);
    is_completed = db.Column(db.Boolean, default=False)
    tasks = db.relationship('Task', backref='project', lazy=True, order_by="Task.start_date",
                            cascade="all, delete-orphan")

    @property
    def completion_percentage(self):
        total = len(self.tasks);
        return int((sum(1 for t in self.tasks if t.is_completed) / total) * 100) if total > 0 else 0


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    contractor_type = db.Column(db.String(20), default='Internal')
    priority = db.Column(db.String(20), default='Medium')
    dependency_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=True)
    is_completed = db.Column(db.Boolean, default=False)
    comments = db.Column(db.Text)
    groups = db.relationship('Group', secondary=task_group_association, backref=db.backref('tasks', lazy='dynamic'))
    assignees = db.relationship('User', secondary=task_user_association,
                                backref=db.backref('assigned_tasks', lazy=True))


# --- HELPERS ---
def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if session.get('role') != 'admin': return jsonify({'success': False, 'message': 'Denied'}), 403
        return view(**kwargs)

    return wrapped_view


@app.context_processor
def inject_notifications():
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
        if user:
            my_tasks = [t for t in user.assigned_tasks if not t.is_completed]
            return dict(notification_count=len(my_tasks), my_pending_tasks=my_tasks)
    return dict(notification_count=0, my_pending_tasks=[])


# --- SMART SCHEDULING LOGIC ---
def adjust_start_date_based_on_dependency(task):
    """
    Checks if the task has a predecessor. If the task is scheduled to start BEFORE
    the predecessor ends, it bumps the start date to the day AFTER the predecessor finishes.
    """
    if task.dependency_id:
        parent = db.session.get(Task, task.dependency_id)
        if parent:
            parent_end_date = parent.start_date + timedelta(days=parent.duration_days)
            # If current task starts too early, push it
            if task.start_date < parent_end_date:
                print(f" >> Auto-adjusting Task {task.id}: Pushed from {task.start_date} to {parent_end_date}")
                task.start_date = parent_end_date
                return True
    return False


def cascade_updates(task_id):
    """Recursively updates children if a parent moves."""
    task = db.session.get(Task, task_id)
    if not task: return

    # Find children
    followers = Task.query.filter_by(dependency_id=task.id).all()
    for follower in followers:
        if adjust_start_date_based_on_dependency(follower):
            cascade_updates(follower.id)


# --- ROUTES ---
@app.route('/')
@login_required
def index():
    users_data = User.query.all() if session.get('role') == 'admin' else []
    return render_template('index.html', clients=Client.query.order_by(Client.company_name).all(),
                           all_groups=Group.query.order_by(Group.name).all(), users=users_data)


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = User.query.filter(
            (User.username == request.form['username']) | (User.email == request.form['username'])).first()
        if u and check_password_hash(u.password_hash, request.form['password']):
            session.update({'user_id': u.id, 'username': u.username, 'role': u.role});
            return redirect(url_for('index'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))


@app.route('/calendar')
@login_required
def view_calendar():
    return render_template('calendar.html', users=User.query.all(), all_groups=Group.query.all())


@app.route('/gantt/project/<int:project_id>')
@login_required
def view_gantt(project_id):
    p = db.session.get(Project, project_id)
    return render_template('gantt_view.html', project=p)


# --- API ENDPOINTS (Updated with Logic) ---
@app.route('/api/task/add', methods=['POST'])
@login_required
def add_task_api():
    d = request.json
    t = Task(project_id=d['project_id'], name=d['task_name'],
             start_date=datetime.strptime(d['start_date'], '%Y-%m-%d').date(),
             duration_days=int(d['duration_days']), priority=d.get('priority', 'Medium'),
             dependency_id=int(d['dependency_id']) if d.get('dependency_id') else None,
             comments=d.get('comments', ''))

    if d.get('group_ids'): t.groups = Group.query.filter(Group.id.in_(d['group_ids'])).all()
    if d.get('user_ids'): t.assignees = User.query.filter(User.id.in_(d['user_ids'])).all()

    db.session.add(t)

    # Apply Smart Scheduling Logic BEFORE commit
    adjust_start_date_based_on_dependency(t)

    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/task/update', methods=['POST'])
@login_required
def update_task_api():
    d = request.json
    t = db.session.get(Task, d['task_id'])
    if t:
        t.name = d['task_name'];
        t.priority = d.get('priority', 'Medium')
        t.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date()
        t.duration_days = int(d['duration_days'])
        t.dependency_id = int(d['dependency_id']) if d.get('dependency_id') else None
        t.comments = d.get('comments', '')

        t.groups = Group.query.filter(Group.id.in_(d.get('group_ids', []))).all()
        t.assignees = User.query.filter(User.id.in_(d.get('user_ids', []))).all()

        # Check logic for itself
        adjust_start_date_based_on_dependency(t)
        db.session.commit()

        # Check logic for its children
        cascade_updates(t.id)
        db.session.commit()

        return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/delete', methods=['POST'])
@login_required
def delete_task_api():
    t = db.session.get(Task, request.json.get('task_id'))
    if t: db.session.delete(t); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/complete', methods=['POST'])
@login_required
def complete_task_api():
    t = db.session.get(Task, request.json.get('task_id'))
    if t: t.is_completed = not t.is_completed; db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


# --- READ APIs ---
@app.route('/api/users')
@login_required
def get_users(): return jsonify(
    [{'id': u.id, 'username': u.username, 'display_name': u.display_name, 'group_ids': [g.id for g in u.groups]} for u
     in User.query.all()])


@app.route('/api/groups')
@login_required
def get_groups(): return jsonify([{'id': g.id, 'name': g.name} for g in Group.query.all()])


@app.route('/api/task/<int:tid>')
@login_required
def get_task_details(tid):
    t = db.session.get(Task, tid)
    if not t: return jsonify({'success': False}), 404
    return jsonify({
        'id': t.id, 'project_id': t.project_id, 'name': t.name, 'start_date': t.start_date.isoformat(),
        'duration_days': t.duration_days, 'priority': t.priority, 'dependency_id': t.dependency_id,
        'comments': t.comments, 'group_ids': [g.id for g in t.groups], 'assignee_ids': [u.id for u in t.assignees]
    })


@app.route('/api/projects/<int:client_id>')
@login_required
def get_projects(client_id):
    c = db.session.get(Client, client_id)
    if not c: return jsonify([])
    return jsonify([{
        'id': p.id, 'name': p.name, 'completion_percent': p.completion_percentage,
        'proposed_start_date': p.proposed_start_date.strftime('%Y-%m-%d') if p.proposed_start_date else '',
        'tasks': [{'id': t.id, 'name': t.name, 'start_date': t.start_date.isoformat(), 'duration_days': t.duration_days,
                   'priority': t.priority, 'contractor_type': t.contractor_type, 'is_completed': t.is_completed,
                   'dependency_id': t.dependency_id, 'comments': t.comments, 'group_ids': [g.id for g in t.groups],
                   'assignee_ids': [u.id for u in t.assignees],
                   'assignee_names': ", ".join([u.display_name for u in t.assignees])} for t in p.tasks]
    } for p in c.projects])


# --- ADMIN APIs ---
@app.route('/api/user/add', methods=['POST'])
@admin_required
def add_user():
    d = request.json
    if User.query.filter_by(email=d['email']).first(): return jsonify({'success': False, 'message': 'Exists'})
    u = User(first_name=d.get('first_name'), last_name=d.get('last_name'), email=d['email'], username=d['email'],
             password_hash=generate_password_hash(d['password']), role=d.get('role', 'user'))
    db.session.add(u);
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/user/delete', methods=['POST'])
@admin_required
def delete_user():
    u = db.session.get(User, request.json.get('user_id'))
    if u and u.id != 1: db.session.delete(u); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/group/save', methods=['POST'])
@admin_required
def save_group():
    d = request.json
    g = db.session.get(Group, d['id']) if d.get('id') else Group()
    g.name = d['name'];
    db.session.add(g)
    if 'user_ids' in d: g.users = User.query.filter(User.id.in_(d['user_ids'])).all()
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/group/delete', methods=['POST'])
@admin_required
def delete_group():
    g = db.session.get(Group, request.json.get('id'))
    if g: db.session.delete(g); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/client/save', methods=['POST'])
@login_required
def save_client():
    d = request.json
    c = db.session.get(Client, d['id']) if d.get('id') else Client()
    if not d.get('id'): db.session.add(c)
    c.company_name = d['company_name'];
    c.contact_name = d.get('contact_name');
    c.location = d.get('location');
    c.phone_number = d.get('phone_number');
    c.main_contact_email = d.get('main_contact_email')
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/client/delete', methods=['POST'])
@login_required
def delete_client():
    c = db.session.get(Client, request.json.get('id'))
    if c: db.session.delete(c); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/client/<int:cid>')
@login_required
def get_client(cid):
    c = db.session.get(Client, cid)
    return jsonify({'id': c.id, 'company_name': c.company_name, 'contact_name': c.contact_name, 'location': c.location,
                    'phone_number': c.phone_number, 'main_contact_email': c.main_contact_email}) if c else ({}, 404)


@app.route('/api/project/save', methods=['POST'])
@login_required
def save_project():
    d = request.json
    p = db.session.get(Project, d['id']) if d.get('id') else Project(client_id=d['client_id'])
    if not d.get('id'): db.session.add(p)
    p.name = d['name'];
    if d.get('proposed_start_date'): p.proposed_start_date = datetime.strptime(d['proposed_start_date'],
                                                                               '%Y-%m-%d').date()
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/project/delete', methods=['POST'])
@login_required
def delete_project():
    p = db.session.get(Project, request.json.get('id'))
    if p: db.session.delete(p); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/project/<int:pid>')
@login_required
def get_project(pid):
    p = db.session.get(Project, pid)
    return jsonify({'id': p.id, 'name': p.name, 'client_id': p.client_id,
                    'proposed_start_date': p.proposed_start_date.strftime(
                        '%Y-%m-%d') if p.proposed_start_date else ''}) if p else ({}, 404)


@app.route('/api/group/<int:gid>')
@login_required
def get_group_detail(gid):
    g = db.session.get(Group, gid)
    return jsonify({'id': g.id, 'name': g.name, 'member_ids': [u.id for u in g.users]})


@app.route('/api/calendar_events')
@login_required
def calendar_events():
    uid = request.args.get('user_id');
    gid = request.args.get('group_id')
    q = Task.query
    if uid and uid != 'null': query = query.join(task_user_association).filter(task_user_association.c.user_id == uid)
    if gid and gid != 'null': query = query.join(task_group_association).filter(
        task_group_association.c.group_id == gid)
    color_map = {'High': '#ef4444', 'Medium': '#f59e0b', 'Low': '#10b981'}
    return jsonify([{'id': t.id, 'title': f"[{t.project.name}] {t.name}", 'start': t.start_date.isoformat(),
                     'end': (t.start_date + timedelta(days=t.duration_days)).isoformat(),
                     'backgroundColor': color_map.get(t.priority, '#4f46e5')} for t in q.all()])


@app.route('/api/gantt_data/<source_type>/<int:source_id>')
@login_required
def api_gantt_data(source_type, source_id):
    rows = []
    if source_type == "project":
        p = db.session.get(Project, source_id)
        if p:
            for t in p.tasks:
                start = t.start_date;
                end = start + timedelta(days=t.duration_days or 1)
                rows.append([str(t.id), t.name, t.contractor_type, start.isoformat(), end.isoformat(), None,
                             100 if t.is_completed else 0, str(t.dependency_id) if t.dependency_id else None])
    return jsonify({'tasks': rows})


def init_db():
    db.create_all()
    try:
        with db.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(task)")).fetchall()
            cols = [row[1] for row in result]
            if 'priority' not in cols: conn.execute(text("ALTER TABLE task ADD COLUMN priority TEXT DEFAULT 'Medium'"))
            if 'dependency_id' not in cols: conn.execute(text("ALTER TABLE task ADD COLUMN dependency_id INTEGER"))
            conn.commit()
    except:
        pass
    if not User.query.filter_by(username='Admin').first():
        db.session.add(
            User(username='Admin', email='admin@connect.local', password_hash=generate_password_hash('password'),
                 role='admin'))
        db.session.commit()


if __name__ == '__main__':
    with app.app_context(): init_db()
    app.run(debug=True)