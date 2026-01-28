import os
import functools
import csv
import io
import shutil
import time
import sqlite3
from datetime import date, datetime, timedelta
from flask import Flask, render_template, request, redirect, url_for, jsonify, session, flash, Response, send_file, \
    send_from_directory
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from flask_apscheduler import APScheduler
from werkzeug.utils import secure_filename
from sqlalchemy import text

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_NAME = 'database.db'
DB_PATH = os.path.join(BASE_DIR, DB_NAME)
ARCHIVE_DIR = os.path.join(BASE_DIR, 'archive')
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')


class Config:
    SQLALCHEMY_DATABASE_URI = f'sqlite:///{DB_PATH}'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SECRET_KEY = os.environ.get('SECRET_KEY', 'secret_key_123')
    SCHEDULER_API_ENABLED = True
    UPLOAD_FOLDER = UPLOAD_FOLDER


app = Flask(__name__)
app.config.from_object(Config())

db = SQLAlchemy(app)
scheduler = APScheduler()
scheduler.init_app(app)
scheduler.start()


# --- BACKUP LOGIC ---
def backup_database_job():
    with app.app_context():
        try:
            db.session.commit();
            db.session.remove()
        except:
            pass
        if not os.path.exists(ARCHIVE_DIR): os.makedirs(ARCHIVE_DIR)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(ARCHIVE_DIR, f'backup_{timestamp}.db')
        try:
            src_conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
            dst_conn = sqlite3.connect(dst)
            with dst_conn:
                src_conn.backup(dst_conn)
            dst_conn.close();
            src_conn.close()
        except Exception as e:
            print(f">> Backup failed: {str(e)}")


# --- DECORATORS ---
def login_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        return view(**kwargs)

    return wrapped_view


def admin_required(view):
    @functools.wraps(view)
    def wrapped_view(**kwargs):
        if 'user_id' not in session: return redirect(url_for('login'))
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'message': 'Access denied'}), 403
        return view(**kwargs)

    return wrapped_view


# --- MODELS ---
task_group_association = db.Table('task_group_association',
                                  db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                                  db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))

task_user_association = db.Table('task_user_association',
                                 db.Column('task_id', db.Integer, db.ForeignKey('task.id'), primary_key=True),
                                 db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True))

project_type_association = db.Table('project_type_association',
                                    db.Column('project_id', db.Integer, db.ForeignKey('project.id'), primary_key=True),
                                    db.Column('project_type_id', db.Integer, db.ForeignKey('project_type.id'),
                                              primary_key=True))

user_group_association = db.Table('user_group_association',
                                  db.Column('user_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
                                  db.Column('group_id', db.Integer, db.ForeignKey('group.id'), primary_key=True))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    first_name = db.Column(db.String(50))
    last_name = db.Column(db.String(50))
    email = db.Column(db.String(120), unique=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='user', nullable=False)
    groups = db.relationship('Group', secondary=user_group_association, backref=db.backref('users', lazy='dynamic'))

    @property
    def display_name(self):
        if self.first_name and self.last_name:
            return f"{self.first_name} {self.last_name}"
        return self.username


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class Client(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    company_name = db.Column(db.String(100), nullable=False)
    contact_name = db.Column(db.String(100))
    location = db.Column(db.String(250))
    main_contact_email = db.Column(db.String(100))
    phone_number = db.Column(db.String(20))
    projects = db.relationship('Project', backref='client', lazy=True)


class ProjectType(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(50), unique=True, nullable=False)


class Project(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    client_id = db.Column(db.Integer, db.ForeignKey('client.id'), nullable=False)
    is_completed = db.Column(db.Boolean, default=False)
    proposed_start_date = db.Column(db.Date)
    tasks = db.relationship('Task', backref='project', lazy=True, order_by="Task.start_date",
                            cascade="all, delete-orphan")
    project_types = db.relationship('ProjectType', secondary=project_type_association,
                                    backref=db.backref('projects', lazy='dynamic'))

    @property
    def completion_percentage(self):
        total = len(self.tasks);
        if total == 0: return 0
        return int((sum(1 for t in self.tasks if t.is_completed) / total) * 100)


class Task(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('project.id'), nullable=False)
    name = db.Column(db.String(100), nullable=False)
    start_date = db.Column(db.Date, nullable=False)
    duration_days = db.Column(db.Integer, nullable=False)
    contractor_type = db.Column(db.String(20), default='Internal')
    is_completed = db.Column(db.Boolean, default=False)
    comments = db.Column(db.Text)
    groups = db.relationship('Group', secondary=task_group_association, backref=db.backref('tasks', lazy='dynamic'))
    assignees = db.relationship('User', secondary=task_user_association,
                                backref=db.backref('assigned_tasks', lazy=True))
    links = db.relationship('TaskLink', backref='task', lazy=True, cascade="all, delete-orphan")
    files = db.relationship('TaskFile', backref='task', lazy=True, cascade="all, delete-orphan")


class TaskLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    url = db.Column(db.String(500), nullable=False)
    description = db.Column(db.String(200))


class TaskFile(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    task_id = db.Column(db.Integer, db.ForeignKey('task.id'), nullable=False)
    filename = db.Column(db.String(255), nullable=False)
    filepath = db.Column(db.String(500), nullable=False)
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow)


# --- CONTEXT PROCESSOR ---
@app.context_processor
def inject_notifications():
    if 'user_id' in session:
        user = db.session.get(User, session['user_id'])
        if user:
            my_tasks = [t for t in user.assigned_tasks if not t.is_completed]
            return dict(notification_count=len(my_tasks), my_pending_tasks=my_tasks)
    return dict(notification_count=0, my_pending_tasks=[])


# --- ROUTES ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username_input = request.form['username']
        user = User.query.filter((User.username == username_input) | (User.email == username_input)).first()
        if user and check_password_hash(user.password_hash, request.form['password']):
            session.clear();
            session['user_id'] = user.id;
            session['username'] = user.username;
            session['role'] = user.role
            return redirect(url_for('index'))
        flash('Invalid credentials', 'error')
    return render_template('login.html')


@app.route('/logout')
def logout(): session.clear(); return redirect(url_for('login'))


@app.route('/')
@login_required
def index():
    users_data = []
    schedule_info = "Not Scheduled"
    if session.get('role') == 'admin':
        users_data = User.query.all()
        job = scheduler.get_job('backup_db_job')
        if job: schedule_info = str(job.trigger)

    return render_template('index.html',
                           clients=Client.query.order_by(Client.company_name).all(),
                           all_groups=Group.query.order_by(Group.name).all(),
                           users=users_data,
                           current_schedule=schedule_info,
                           current_user=session.get('username'))


@app.route('/project/<int:project_id>/task/new', methods=['GET', 'POST'])
@login_required
def add_new_task_page(project_id):
    project = db.session.get(Project, project_id)
    if not project:
        return "Project not found", 404

    if request.method == 'POST':
        name = request.form.get('task_name')
        start_date_str = request.form.get('start_date')
        duration = request.form.get('duration_days')
        contractor = request.form.get('contractor_type', 'Internal')
        comments = request.form.get('comments', '')
        group_ids = request.form.getlist('group_ids')

        if not name or not start_date_str or not duration:
            flash("Missing required fields", "error")
            return redirect(request.url)

        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date()
            duration_days = int(duration)
        except ValueError:
            flash("Invalid Date or Duration format", "error")
            return redirect(request.url)

        new_task = Task(
            project_id=project.id,
            name=name,
            start_date=start_date,
            duration_days=duration_days,
            contractor_type=contractor,
            comments=comments
        )

        if group_ids:
            g_ids = [int(x) for x in group_ids]
            new_task.groups = Group.query.filter(Group.id.in_(g_ids)).all()

        db.session.add(new_task)
        db.session.commit()

        flash(f"Task '{name}' added successfully", "success")
        return redirect(url_for('index'))

    return render_template('task_new_edit.html', project=project, all_groups=Group.query.all())


# --- USER API ---
@app.route('/api/user/change_password', methods=['POST'])
@login_required
def api_change_password():
    data = request.json
    new_pass = data.get('new_password')
    confirm_pass = data.get('confirm_password')
    if not new_pass or not confirm_pass: return jsonify({'success': False, 'message': 'Both fields required'}), 400
    if new_pass != confirm_pass: return jsonify({'success': False, 'message': 'Passwords do not match'}), 400
    user = db.session.get(User, session['user_id'])
    if user:
        user.password_hash = generate_password_hash(new_pass)
        db.session.commit()
        return jsonify({'success': True, 'message': 'Password updated successfully'})
    return jsonify({'success': False, 'message': 'User not found'}), 404


# --- ADMIN API (USERS) ---
@app.route('/api/user/add', methods=['POST'])
@admin_required
def add_user_api():
    data = request.json
    first_name = data.get('first_name')
    last_name = data.get('last_name')
    email = data.get('email')
    password = data.get('password')
    role = data.get('role', 'user')

    if not email or not password: return jsonify({'success': False, 'message': 'Email and Password required'})

    if role == 'admin' and data.get('username'):
        target_username = data.get('username')
    else:
        target_username = email

    if User.query.filter((User.username == target_username) | (User.email == email)).first():
        return jsonify({'success': False, 'message': 'User/Email already exists'})

    u = User(first_name=first_name, last_name=last_name, email=email, username=target_username,
             password_hash=generate_password_hash(password), role=role)
    if data.get('group_ids'):
        group_ids = [int(x) for x in data['group_ids']]
        u.groups = Group.query.filter(Group.id.in_(group_ids)).all()

    db.session.add(u);
    db.session.commit()
    return jsonify({'success': True})


@app.route('/api/user/delete', methods=['POST'])
@admin_required
def delete_user_api():
    uid = request.json.get('user_id')
    if uid == 1: return jsonify({'success': False, 'message': 'Cannot delete root admin'})
    db.session.delete(db.session.get(User, uid));
    db.session.commit()
    return jsonify({'success': True})


# --- ADMIN API (GROUPS) ---
@app.route('/api/group/save', methods=['POST'])
@admin_required
def save_group_api():
    data = request.json
    group_id = data.get('id')
    name = data.get('name')
    if not name: return jsonify({'success': False, 'message': 'Group name required'}), 400

    if group_id:
        group = db.session.get(Group, group_id)
        if not group: return jsonify({'success': False, 'message': 'Group not found'}), 404
    else:
        if Group.query.filter_by(name=name).first(): return jsonify(
            {'success': False, 'message': 'Group name exists'}), 400
        group = Group()
        db.session.add(group)

    group.name = name
    if 'user_ids' in data:
        current_users = User.query.all()
        target_ids = [int(uid) for uid in data['user_ids']]
        for u in current_users:
            if u.id in target_ids:
                if group not in u.groups: u.groups.append(group)
            else:
                if group in u.groups: u.groups.remove(group)

    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/group/delete', methods=['POST'])
@admin_required
def delete_group_api():
    gid = request.json.get('id')
    group = db.session.get(Group, gid)
    if group:
        db.session.delete(group);
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Group not found'}), 404


@app.route('/api/group/<int:group_id>')
@login_required
def get_group_details(group_id):
    group = db.session.get(Group, group_id)
    if not group: return jsonify({'success': False}), 404
    member_ids = [u.id for u in group.users]
    return jsonify({'id': group.id, 'name': group.name, 'member_ids': member_ids})


# --- ADMIN API (SYSTEM) ---
@app.route('/api/admin/schedule', methods=['POST'])
@admin_required
def admin_schedule_api():
    data = request.json
    interval = int(data.get('interval', 24))
    unit = data.get('unit', 'hours')
    if scheduler.get_job('backup_db_job'): scheduler.remove_job('backup_db_job')
    scheduler.add_job(id='backup_db_job', func=backup_database_job, trigger='interval', **{unit: interval})
    return jsonify({'success': True, 'message': f'Scheduled every {interval} {unit}'})


@app.route('/api/admin/restore', methods=['POST'])
@admin_required
def admin_restore_api():
    if 'db_file' not in request.files: return jsonify({'success': False, 'message': 'No file'})
    file = request.files['db_file']
    try:
        db.session.remove();
        file.save(os.path.join(BASE_DIR, 'temp_restore.db'))
        if os.path.exists(DB_PATH): os.remove(DB_PATH)
        shutil.move(os.path.join(BASE_DIR, 'temp_restore.db'), DB_PATH)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/api/admin/scrub', methods=['POST'])
@admin_required
def admin_scrub_api():
    try:
        db.session.execute(task_group_association.delete());
        db.session.execute(task_user_association.delete());
        db.session.execute(project_type_association.delete())
        Task.query.delete();
        Project.query.delete();
        db.session.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'message': str(e)})


@app.route('/admin/backup')
@admin_required
def admin_backup_download():
    try:
        db.session.commit();
        db.session.remove()
    except:
        pass
    if os.path.exists(DB_PATH): return send_file(DB_PATH, as_attachment=True,
                                                 download_name=f"backup_{datetime.now().strftime('%Y%m%d')}.db")
    return redirect(url_for('index'))


# --- APP API ---
@app.route('/api/projects/<int:client_id>')
@login_required
def get_client_projects_api(client_id):
    client = db.session.get(Client, client_id)
    if not client: return jsonify([])
    projects_data = []
    for p in client.projects:
        tasks_data = []
        for t in p.tasks:
            s = t.start_date if t.start_date else date.today()
            links = [{'id': l.id, 'url': l.url, 'description': l.description} for l in t.links]
            files = [{'id': f.id, 'filename': f.filename} for f in t.files]

            assignee_ids = [u.id for u in t.assignees]
            assignee_names = ", ".join([u.display_name for u in t.assignees])

            tasks_data.append({
                'id': t.id, 'name': t.name, 'start_date': s.strftime('%Y-%m-%d'), 'duration_days': t.duration_days,
                'contractor_type': t.contractor_type, 'is_completed': t.is_completed, 'project_id': p.id,
                'comments': t.comments or '',
                'group_ids': [g.id for g in t.groups], 'group_names': ", ".join([g.name for g in t.groups]),
                'assignee_ids': assignee_ids, 'assignee_names': assignee_names,
                'links': links, 'files': files
            })
        projects_data.append(
            {'id': p.id, 'name': p.name, 'completion_percent': p.completion_percentage, 'tasks': tasks_data,
             'proposed_start_date': p.proposed_start_date.strftime('%Y-%m-%d') if p.proposed_start_date else ''})
    return jsonify(projects_data)


@app.route('/api/project/save', methods=['POST'])
@login_required
def api_save_project():
    d = request.json;
    p = db.session.get(Project, d['id']) if d.get('id') else Project(client_id=d['client_id'])
    if not d.get('id'): db.session.add(p)
    p.name = d['name'];
    p.proposed_start_date = datetime.strptime(d['proposed_start_date'], '%Y-%m-%d').date() if d.get(
        'proposed_start_date') else None
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/project/delete', methods=['POST'])
@login_required
def delete_project_api():
    p = db.session.get(Project, request.json['project_id'])
    if p:
        db.session.delete(p)
        db.session.commit()
        return jsonify({'success': True})
    return jsonify({'success': False, 'message': 'Project not found'}), 404


@app.route('/api/task/add', methods=['POST'])
@login_required
def task_add_via_api():
    d = request.json;
    t = Task(project_id=d['project_id'], name=d['task_name'],
             start_date=datetime.strptime(d['start_date'], '%Y-%m-%d').date(), duration_days=int(d['duration_days']),
             contractor_type=d['contractor_type'], comments=d.get('comments', ''))

    if d.get('group_ids'): t.groups = Group.query.filter(Group.id.in_([int(x) for x in d['group_ids']])).all()
    if d.get('user_ids'): t.assignees = User.query.filter(User.id.in_([int(x) for x in d['user_ids']])).all()
    db.session.add(t);
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/task/update', methods=['POST'])
@login_required
def task_update_via_api():
    d = request.json;
    t = db.session.get(Task, d['task_id'])
    if t:
        t.name = d['task_name'];
        t.start_date = datetime.strptime(d['start_date'], '%Y-%m-%d').date();
        t.duration_days = int(d['duration_days']);
        t.contractor_type = d['contractor_type']
        t.comments = d.get('comments', '')

        if d.get('group_ids'):
            t.groups = Group.query.filter(Group.id.in_([int(x) for x in d['group_ids']])).all()
        else:
            t.groups = []
        if d.get('user_ids'):
            t.assignees = User.query.filter(User.id.in_([int(x) for x in d['user_ids']])).all()
        else:
            t.assignees = []
        db.session.commit();
        return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/delete', methods=['POST'])
@login_required
def task_delete_via_api():
    t = db.session.get(Task, request.json['task_id'])
    if t: db.session.delete(t); db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/complete', methods=['POST'])
@login_required
def task_complete_via_api():
    t = db.session.get(Task, request.json['task_id'])
    if t: t.is_completed = not t.is_completed; db.session.commit(); return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/<int:task_id>')
@login_required
def api_get_single_task(task_id):
    t = db.session.get(Task, task_id)
    if not t: return jsonify({'success': False}), 404
    return jsonify({
        'id': t.id, 'name': t.name, 'start_date': t.start_date.isoformat(), 'duration_days': t.duration_days,
        'contractor_type': t.contractor_type, 'comments': t.comments or '',
        'group_ids': [g.id for g in t.groups],
        'user_ids': [u.id for u in t.assignees],
        'links': [{'id': l.id, 'url': l.url, 'description': l.description} for l in t.links],
        'files': [{'id': f.id, 'filename': f.filename} for f in t.files]
    })


@app.route('/api/task/link/add', methods=['POST'])
@login_required
def add_task_link():
    d = request.json;
    db.session.add(TaskLink(task_id=d['task_id'], url=d['url'], description=d.get('description', '')));
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/task/link/delete', methods=['POST'])
@login_required
def delete_task_link():
    l = db.session.get(TaskLink, request.json['link_id']);
    if l: db.session.delete(l); db.session.commit()
    return jsonify({'success': True})


@app.route('/api/task/file/upload', methods=['POST'])
@login_required
def upload_task_file():
    f = request.files['file'];
    tid = request.form.get('task_id')
    if f:
        fn = secure_filename(f.filename);
        fp = f"{os.path.splitext(fn)[0]}_{int(time.time())}{os.path.splitext(fn)[1]}"
        if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
        f.save(os.path.join(UPLOAD_FOLDER, fp));
        db.session.add(TaskFile(task_id=tid, filename=fn, filepath=fp));
        db.session.commit();
        return jsonify({'success': True})
    return jsonify({'success': False})


@app.route('/api/task/file/delete', methods=['POST'])
@login_required
def delete_task_file():
    f = db.session.get(TaskFile, request.json['file_id'])
    if f:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, f.filepath))
        except:
            pass
        db.session.delete(f);
        db.session.commit()
    return jsonify({'success': True})


@app.route('/task/file/download/<int:file_id>')
@login_required
def download_task_file(file_id):
    f = db.session.get(TaskFile, file_id)
    if f: return send_from_directory(UPLOAD_FOLDER, f.filepath, as_attachment=True, download_name=f.filename)
    return "Not Found", 404


@app.route('/api/groups')
@login_required
def api_get_groups(): return jsonify([{'id': g.id, 'name': g.name} for g in Group.query.order_by(Group.name).all()])


@app.route('/api/users')
@login_required
def api_get_users():
    return jsonify([{'id': u.id, 'username': u.username, 'email': u.email, 'first_name': u.first_name,
                     'last_name': u.last_name, 'display_name': u.display_name, 'group_ids': [g.id for g in u.groups]}
                    for u in User.query.order_by(User.username).all()])


@app.route('/api/client/<int:client_id>')
@login_required
def api_get_client(client_id):
    c = db.session.get(Client, client_id)
    return jsonify({'id': c.id, 'company_name': c.company_name, 'contact_name': c.contact_name, 'location': c.location,
                    'phone_number': c.phone_number, 'main_contact_email': c.main_contact_email}) if c else ({}, 404)


@app.route('/api/client/save', methods=['POST'])
@login_required
def api_save_client():
    d = request.json;
    c = db.session.get(Client, d['id']) if d.get('id') else Client()
    if not d.get('id'): db.session.add(c)
    c.company_name = d['company_name'];
    c.contact_name = d['contact_name'];
    c.location = d['location'];
    c.phone_number = d['phone_number'];
    c.main_contact_email = d['main_contact_email']
    db.session.commit();
    return jsonify({'success': True})


@app.route('/api/project/<int:project_id>')
@login_required
def api_get_project(project_id):
    p = db.session.get(Project, project_id)
    return jsonify({'id': p.id, 'name': p.name, 'client_id': p.client_id,
                    'proposed_start_date': p.proposed_start_date.strftime(
                        '%Y-%m-%d') if p.proposed_start_date else ''}) if p else ({}, 404)


@app.route('/gantt/project/<int:project_id>')
@login_required
def view_gantt(project_id):
    p = db.session.get(Project, project_id)
    return render_template('gantt_view.html', title=f"Project: {p.name}", source_type="project", source_id=p.id)


# --- UPDATED GANTT DATA API ---
@app.route('/api/gantt_data/<source_type>/<int:source_id>')
@login_required
def api_gantt_data(source_type, source_id):
    rows = []
    if source_type == "project":
        p = db.session.get(Project, source_id)
        if p:
            for t in p.tasks:
                start_date = t.start_date
                duration = t.duration_days if t.duration_days > 0 else 1
                end_date = start_date + timedelta(days=duration)
                percent = 100 if t.is_completed else 0

                # Standard Google Gantt Columns:
                # [Task ID, Task Name, Resource, Start, End, Duration, Percent Complete, Dependencies]
                rows.append([
                    str(t.id),
                    str(t.name),
                    t.contractor_type,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    None,  # Duration (we use dates)
                    percent,
                    None  # Dependencies
                ])
    return jsonify({'tasks': rows})


@app.route('/export/gantt/csv/<source_type>/<int:source_id>')
@login_required
def export_gantt_csv(source_type, source_id):
    tasks = []
    if source_type == "project":
        p = db.session.get(Project, source_id)
        if p: tasks = p.tasks
    output = io.StringIO();
    output.write(u'\ufeff');
    writer = csv.writer(output)
    writer.writerow(['Task ID', 'Name', 'Start', 'End', 'Duration', 'Contractor', 'Status', 'Comments'])
    for t in tasks: writer.writerow(
        [t.id, t.name, t.start_date, t.start_date + timedelta(days=t.duration_days), t.duration_days, t.contractor_type,
         "Completed" if t.is_completed else "Active", t.comments])
    return Response(output.getvalue(), mimetype="text/csv",
                    headers={"Content-disposition": f"attachment; filename=gantt_export.csv"})


@app.route('/export/gantt/xml/<source_type>/<int:source_id>')
@login_required
def export_gantt_xml(source_type, source_id):
    tasks = []
    if source_type == "project":
        p = db.session.get(Project, source_id)
        if p: tasks = p.tasks
    xml = '<?xml version="1.0"?><Project>'
    for t in tasks: xml += f'<Task><ID>{t.id}</ID><Name>{t.name}</Name><Start>{t.start_date}</Start><Duration>{t.duration_days}</Duration><Notes>{t.comments or ""}</Notes></Task>'
    xml += '</Project>'
    return Response(xml, mimetype="text/xml", headers={"Content-disposition": "attachment; filename=project.xml"})


def init_db():
    if not os.path.exists(UPLOAD_FOLDER): os.makedirs(UPLOAD_FOLDER)
    db.create_all()

    try:
        with db.engine.connect() as conn:
            result = conn.execute(text("PRAGMA table_info(task)")).fetchall()
            columns = [row[1] for row in result]
            if 'comments' not in columns:
                print(">> Migrating Database: Adding 'comments' column to 'task' table...")
                conn.execute(text("ALTER TABLE task ADD COLUMN comments TEXT"))
                conn.commit()
    except Exception as e:
        print(f">> Migration check failed (ignore if first run): {e}")

    # Update default admin
    if not User.query.filter_by(username='Admin').first():
        db.session.add(User(username='Admin', first_name="System", last_name="Admin", email="admin@hendrycks.reno",
                            password_hash=generate_password_hash('password'), role='admin'))
        db.session.commit()
    if not Group.query.first():
        for g in ["Development", "Sales", "TSC"]: db.session.add(Group(name=g))
        db.session.commit()


if __name__ == '__main__':
    with app.app_context(): init_db()
    app.run(debug=True)
