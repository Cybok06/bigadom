from flask import Blueprint, request, render_template, redirect, url_for, session, jsonify, flash, current_app
from flask_bcrypt import Bcrypt
from bson.objectid import ObjectId
from datetime import datetime
from db import db
from werkzeug.utils import secure_filename
import uuid
import os
import re
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

register_bp = Blueprint('register', __name__)
bcrypt = Bcrypt()

# MongoDB collection
users_collection = db.users

# Upload config
UPLOAD_FOLDER = '/uploads'
ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg'}
INVITE_MAX_AGE_SECONDS = 60 * 60 * 24 * 30

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


def _invite_serializer():
    return URLSafeTimedSerializer(current_app.secret_key, salt='manager-agent-registration')


def _invite_manager(token):
    try:
        manager_id = _invite_serializer().loads(token, max_age=INVITE_MAX_AGE_SECONDS)
        manager_oid = ObjectId(str(manager_id))
    except (BadSignature, SignatureExpired, ValueError, TypeError):
        return None
    return users_collection.find_one(
        {'_id': manager_oid, 'role': 'manager', 'status': {'$nin': ['Inactive', 'inactive', 'disabled']}},
        {'name': 1, 'branch': 1},
    )


def _create_agent(data, manager_oid, forced_branch=None):
    required = ('username', 'password', 'name')
    if any(not (data.get(field) or '').strip() for field in required):
        raise ValueError('Username, password, and full name are required.')
    if len(data.get('password', '')) < 6:
        raise ValueError('Password must contain at least 6 characters.')

    username = data['username'].strip()
    if users_collection.find_one({'username': {'$regex': f'^{re.escape(username)}$', '$options': 'i'}}):
        raise ValueError('That username is already in use. Choose another username.')

    now = datetime.utcnow()
    user = {
        'username': username,
        'password': bcrypt.generate_password_hash(data['password']).decode('utf-8'),
        'role': 'agent',
        'name': data['name'].strip(),
        'phone': data.get('phone', '').strip(),
        'email': data.get('email', '').strip().lower(),
        'gender': data.get('gender', '').strip(),
        'branch': str(forced_branch if forced_branch is not None else data.get('branch', '')).strip(),
        'position': data.get('position', '').strip(),
        'location': data.get('location', '').strip(),
        'start_date': data.get('start_date', '').strip(),
        'image_url': data.get('image_url', '').strip(),
        'image_id': data.get('image_id', '').strip(),
        'status': 'Active' if forced_branch is not None else data.get('status', 'Active'),
        'assets': [item.strip() for item in data.get('assets', '').split(',') if item.strip()],
        'date_registered': now,
        'manager_id': manager_oid,
    }
    users_collection.insert_one(user)
    return user

@register_bp.record_once
def on_load(state):
    bcrypt.init_app(state.app)

@register_bp.route('/register', methods=['GET', 'POST'])
def register():
    if 'manager_id' not in session:
        return redirect(url_for('login.login'))

    try:
        manager_oid = ObjectId(session['manager_id'])
    except Exception:
        return redirect(url_for('login.logout'))
    invite_token = _invite_serializer().dumps(str(manager_oid))
    share_url = url_for('register.register_from_invite', token=invite_token, _external=True)

    if request.method == 'POST':
        try:
            user = _create_agent(request.form, manager_oid)
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template('register.html', form_data=request.form, share_url=share_url, upload_url=url_for('register.upload_agent_image'))
        flash(f"Agent {user['name']} was registered successfully.", 'success')
        # Use the stable path so post-registration navigation cannot fail if
        # endpoint names change while the development server is reloading.
        return redirect('/agents')

    return render_template('register.html', share_url=share_url, upload_url=url_for('register.upload_agent_image'))


@register_bp.route('/register/agent/<token>', methods=['GET', 'POST'])
def register_from_invite(token):
    manager = _invite_manager(token)
    if not manager:
        return render_template('agent_invite_invalid.html'), 410

    upload_url = url_for('register.upload_agent_image', token=token)
    if request.method == 'POST':
        try:
            _create_agent(request.form, manager['_id'], forced_branch=manager.get('branch') or '')
        except ValueError as exc:
            flash(str(exc), 'danger')
            return render_template(
                'register.html', form_data=request.form, is_invite=True, manager=manager,
                invite_token=token, upload_url=upload_url,
            )
        return render_template(
            'register.html', is_invite=True, manager=manager, registration_success=True,
            login_url=url_for('login.login'), upload_url=upload_url,
        )
    return render_template(
        'register.html', is_invite=True, manager=manager, invite_token=token, upload_url=upload_url,
    )

@register_bp.route('/register/upload_image', methods=['POST'])
def upload_agent_image():
    """Compatibility endpoint for cached copies of the old register page.

    New and old clients now use the same Cloudflare Images implementation.
    """
    token = request.args.get('token') or ''
    if 'manager_id' not in session and not _invite_manager(token):
        return jsonify({'success': False, 'error': 'Authentication required'}), 401
    from add_product import upload_image as upload_to_cloudflare
    return upload_to_cloudflare()
