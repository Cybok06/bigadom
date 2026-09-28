from datetime import datetime, timedelta, timezone
from math import ceil

from bson import ObjectId
from flask import Blueprint, abort, render_template, request, url_for

from db import db
from login import get_current_identity, role_required


payment_history_bp = Blueprint('payment_history', __name__)
PAYMENT_TYPES = ('PRODUCT', 'SUSU', 'LOAN')
PAGE_SIZE = 50


def _ids(value):
    text = str(value)
    return [text, ObjectId(text)] if ObjectId.is_valid(text) else [text]


def _names(collection, ids):
    candidates = [candidate for value in ids for candidate in _ids(value)]
    return {
        str(row['_id']): row.get('name') or row.get('username') or 'Unknown'
        for row in collection.find({'_id': {'$in': candidates}}, {'name': 1, 'username': 1})
    }


@payment_history_bp.route('/payment-history')
@role_required('manager', 'executive', 'admin')
def page():
    identity = get_current_identity()
    role = identity.get('role')
    # Keep this explicit: role_required also permits main admins in other roles.
    if role not in {'manager', 'executive', 'admin'}:
        abort(403)
    today = datetime.now(timezone.utc).date().isoformat()  # Ghana uses UTC.
    selected_date = request.args.get('date', today)
    try:
        day = datetime.strptime(selected_date, '%Y-%m-%d')
        if day.strftime('%Y-%m-%d') != selected_date:
            raise ValueError
        next_day = day + timedelta(days=1)
        page_number = int(request.args.get('page', '1'))
        if page_number < 1:
            raise ValueError
    except (ValueError, OverflowError):
        abort(400, description='Select a valid date and page number.')
    selected_type = request.args.get('payment_type', '').upper()
    if selected_type and selected_type not in PAYMENT_TYPES:
        abort(400, description='Select Product, SUSU, Loan, or all payment types.')

    agent_query = {'role': 'agent'}
    if role == 'manager':
        if not identity.get('user_id'):
            abort(403)
        agent_query['manager_id'] = {'$in': _ids(identity['user_id'])}
    agents = list(db.users.find(agent_query, {'name': 1, 'username': 1}).sort('name', 1))
    agent_names = {str(row['_id']): row.get('name') or row.get('username') or 'Unnamed agent' for row in agents}
    selected_agent = request.args.get('agent_id', '')
    if selected_agent and selected_agent not in agent_names:
        abort(403, description='This agent is not available to your account.')

    query = {'$and': [
        {'$or': [
            {'date': selected_date},
            {'date': {'$in': [None, '']}, 'created_at': {'$gte': day, '$lt': next_day}},
        ]},
        {'$or': [
            {'payment_type': {'$in': list(PAYMENT_TYPES)}},
            {'payment_type': {'$in': [None, '']}},  # Older product payments.
        ]},
    ]}
    if role == 'manager' or selected_agent:
        visible_ids = [selected_agent] if selected_agent else list(agent_names)
        query['agent_id'] = {'$in': [candidate for value in visible_ids for candidate in _ids(value)]}
    if selected_type == 'PRODUCT':
        query['$and'].append({'payment_type': {'$in': ['PRODUCT', None, '']}})
    elif selected_type:
        query['payment_type'] = selected_type

    summaries = list(db.payments.aggregate([
        {'$match': query},
        {'$group': {'_id': '$payment_type', 'amount': {'$sum': '$amount'}, 'count': {'$sum': 1}}},
    ]))
    totals = {kind: 0.0 for kind in PAYMENT_TYPES}
    count = 0
    for summary in summaries:
        totals[summary['_id'] or 'PRODUCT'] += float(summary['amount'])
        count += summary['count']
    pages = max(1, ceil(count / PAGE_SIZE))
    page_number = min(page_number, pages)
    payments = list(db.payments.find(query).sort([
        ('date', -1), ('time', -1), ('created_at', -1), ('_id', -1),
    ]).skip((page_number - 1) * PAGE_SIZE).limit(PAGE_SIZE))
    customer_ids = {str(row['customer_id']) for row in payments if row.get('customer_id')}
    customer_names = _names(db.customers, customer_ids)
    missing = customer_ids - customer_names.keys()
    if missing:
        customer_names.update(_names(db['Archived_customers'], missing))
    missing_agents = {str(row.get('agent_id', '')) for row in payments} - agent_names.keys()
    if missing_agents:
        agent_names.update(_names(db.users, missing_agents))
    rows = []
    for payment in payments:
        created = payment.get('created_at')
        kind = payment.get('payment_type') or 'PRODUCT'
        rows.append({
            'reference': str(payment['_id']),
            'agent': agent_names.get(str(payment.get('agent_id')), 'Unknown agent'),
            'customer': customer_names.get(str(payment.get('customer_id')), 'Unknown customer'),
            'type': kind,
            'amount': float(payment.get('amount') or 0),
            'time': payment.get('time') or (created.strftime('%H:%M:%S') if isinstance(created, datetime) else '—'),
            'method': payment.get('method') or '—',
            'detail': payment.get('loan_number') if kind == 'LOAN' else payment.get('product_name') if kind == 'PRODUCT' else 'Savings deposit',
        })
    def page_url(number):
        return url_for('payment_history.page', date=selected_date, agent_id=selected_agent, payment_type=selected_type, page=number)
    return render_template(
        'payment_history.html', role=role, today=today, selected_date=selected_date,
        display_date=day.strftime('%d %b %Y'), selected_agent=selected_agent,
        selected_type=selected_type, agents=agents, payment_types=PAYMENT_TYPES,
        rows=rows, totals=totals, total=sum(totals.values()), count=count,
        page_number=page_number, pages=pages,
        first=(page_number - 1) * PAGE_SIZE + 1 if count else 0,
        last=min(page_number * PAGE_SIZE, count),
        previous_url=page_url(page_number - 1) if page_number > 1 else None,
        next_url=page_url(page_number + 1) if page_number < pages else None,
    )
