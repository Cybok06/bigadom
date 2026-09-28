from datetime import datetime, timedelta
import re
import secrets
from bson import ObjectId
from flask import Blueprint, abort, jsonify, render_template, request, session
from pymongo.errors import PyMongoError
from db import db
from login import get_current_identity
from services.payment_corrections import correct_payment

changes_bp = Blueprint('changes', __name__)
ACTIONS = {'customer.deleted': 'Customer deleted', 'customer.restored': 'Customer restored',
           'payment.changed': 'Payment amount changed', 'customer.details_changed': 'Customer details changed',
           'loan.guarantor_changed': 'Loan guarantor details changed'}


@changes_bp.record_once
def indexes(state):
    try:
        db.change_activities.create_index([('timestamp', -1), ('_id', -1)])
        db.change_activities.create_index([('action', 1), ('timestamp', -1)])
    except PyMongoError:
        pass


@changes_bp.app_context_processor
def payment_edit_context():
    identity = get_current_identity()
    allowed = identity.get('is_authenticated') and identity.get('role') in {'manager', 'executive'}
    if allowed and not session.get('payment_edit_token'):
        session['payment_edit_token'] = secrets.token_urlsafe(32)
    return {'can_edit_payments': bool(allowed), 'payment_edit_token': session.get('payment_edit_token', '')}


@changes_bp.post('/payments/<payment_id>/edit-amount')
def edit_payment_amount(payment_id):
    actor = get_current_identity()
    if not actor.get('is_authenticated') or actor.get('role') not in {'manager', 'executive'}:
        return jsonify(ok=False, message='Only managers and executives may edit payments.'), 403
    token = request.headers.get('X-Payment-Edit-Token', '')
    if not token or not secrets.compare_digest(token, session.get('payment_edit_token', '')):
        return jsonify(ok=False, message='Your session expired. Reload the page and try again.'), 403
    if not ObjectId.is_valid(payment_id):
        return jsonify(ok=False, message='Invalid payment ID.'), 400
    data = request.get_json(silent=True)
    if not isinstance(data, dict) or set(data) != {'amount', 'expected_amount'}:
        return jsonify(ok=False, message='Only the payment amount can be edited.'), 400
    try:
        changed = correct_payment(db, payment_id, actor, data['amount'], data['expected_amount'])
    except PermissionError as exc:
        return jsonify(ok=False, message=str(exc)), 403
    except LookupError as exc:
        return jsonify(ok=False, message=str(exc)), 404
    except ValueError as exc:
        return jsonify(ok=False, message=str(exc)), 409
    except PyMongoError:
        return jsonify(ok=False, message='Unable to save the correction. No changes were committed. Please retry.'), 503
    return jsonify(ok=True, message='Payment amount and unclosed money updated.' if changed else 'Amount unchanged.')


@changes_bp.get('/executive/changes-activities')
def activities():
    actor = get_current_identity()
    if not actor.get('is_authenticated') or actor.get('role') != 'executive':
        abort(403)
    query = {}
    action = request.args.get('action', '')
    search = request.args.get('search', '').strip()
    start, end = request.args.get('start', ''), request.args.get('end', '')
    if action in ACTIONS:
        query['action'] = action
    if search:
        query['$or'] = [{field: {'$regex': re.escape(search), '$options': 'i'}}
                        for field in ('customer_name', 'customer_id', 'actor_name', 'actor_id', 'entity_id')]
    try:
        if start:
            query.setdefault('timestamp', {})['$gte'] = datetime.strptime(start, '%Y-%m-%d')
        if end:
            query.setdefault('timestamp', {})['$lt'] = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
        page = max(1, int(request.args.get('page', 1)))
    except ValueError:
        abort(400)
    total = db.change_activities.count_documents(query)
    pages = max(1, (total + 24) // 25)
    page = min(page, pages)
    rows = list(db.change_activities.find(query).sort([('timestamp', -1), ('_id', -1)]).skip((page - 1) * 25).limit(25))
    return render_template('executive_changes_activities.html', rows=rows, actions=ACTIONS,
                           action=action, search=search, start=start, end=end, total=total, page=page, pages=pages)
