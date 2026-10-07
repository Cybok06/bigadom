import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bson import ObjectId
from flask import Flask
from werkzeug.datastructures import MultiDict


@pytest.fixture
def editor(monkeypatch):
    # Load the routes with isolated database doubles; never connect to MongoDB.
    spec = importlib.util.spec_from_file_location('loan_guarantor_routes_test', Path(__file__).parents[1] / 'routes/loans.py')
    module = importlib.util.module_from_spec(spec)
    with patch.dict('sys.modules', {
        'db': SimpleNamespace(db=MagicMock()),
        'services.activation_groups': SimpleNamespace(get_accessible_agent_ids=lambda _: ['agent-1']),
    }):
        spec.loader.exec_module(module)
    module.current_user = SimpleNamespace(id='agent-1', role='agent', is_authenticated=True)
    monkeypatch.setitem(__import__('sys').modules, 'login', SimpleNamespace(get_current_identity=lambda: {
        'is_authenticated': True, 'user_id': 'agent-1', 'role': 'agent', 'name': 'Agent Ama'}))
    transaction = module.db.client.start_session.return_value.__enter__.return_value
    transaction.with_transaction.side_effect = lambda callback: callback(transaction)
    module.loans_col.update_one.return_value.matched_count = 1

    loan = {'_id': ObjectId(), 'guarantor_details': [{'name': 'Name', 'value': 'Ama'}]}
    module.loans_col.find_one.return_value = loan
    app = Flask(__name__)
    app.secret_key = 'test-only'
    app.register_blueprint(module.loans_bp)
    client = app.test_client()
    with client.session_transaction() as session:
        session['guarantor_edit_token'] = 'test-token'
    return module, client, loan


def submit(editor, names, values, token='test-token'):
    module, client, loan = editor
    data = MultiDict([('guarantor_token', token)] + [('guarantor_name[]', name) for name in names] + [('guarantor_value[]', value) for value in values])
    return client.post(f"/loans/{loan['_id']}/guarantor", data=data)


def test_edit_and_add_guarantor_details(editor):
    module, _, loan = editor
    response = submit(editor, [' Name ', 'Phone'], [' Ama Mensah ', '0241234567'])
    assert response.status_code == 200
    assert response.json['details'] == [{'name': 'Name', 'value': 'Ama Mensah'}, {'name': 'Phone', 'value': '0241234567'}]
    query = module.loans_col.find_one.call_args.args[0]
    assert query['agent_id'] == {'$in': ['agent-1']}
    update = module.loans_col.update_one.call_args.args[1]['$set']
    assert set(update) == {'guarantor_details', 'guarantor_updated_at', 'guarantor_updated_by', 'updated_at'}
    assert update['guarantor_updated_by'] == 'agent-1'


@pytest.mark.parametrize('names,values', [(['Name'], ['']), (['Name'], []), ([], []), (['x' * 101], ['Ama']), (['Name'], ['x' * 501]), (['Name'] * 51, ['Ama'] * 51)])
def test_invalid_details_do_not_write(editor, names, values):
    assert submit(editor, names, values).status_code == 400
    editor[0].loans_col.update_one.assert_not_called()


def test_other_agents_loan_cannot_be_updated(editor):
    editor[0].loans_col.find_one.return_value = None
    assert submit(editor, ['Name'], ['Ama']).status_code == 404
    editor[0].loans_col.update_one.assert_not_called()


def test_missing_token_cannot_write(editor):
    assert submit(editor, ['Name'], ['Ama'], token='').status_code == 403
    editor[0].loans_col.update_one.assert_not_called()


def test_non_agent_cannot_write(editor):
    editor[0].current_user.role = 'customer'
    assert submit(editor, ['Name'], ['Ama']).status_code == 403
    editor[0].loans_col.update_one.assert_not_called()


@pytest.mark.parametrize('role', ['manager', 'executive'])
@pytest.mark.parametrize('payment_count', [0, 1, 2, 3, 4])
def test_loan_cancellation_payment_limit(editor, role, payment_count):
    module, client, loan = editor
    loan.update(status='active', amount_paid=20 * payment_count)
    module.payments_col.count_documents.return_value = payment_count
    module.loans_col.update_one.return_value.modified_count = 1
    with client.session_transaction() as session:
        session[role + '_id'] = str(ObjectId())
        session['loan_cancel_token'] = 'cancel-token'
    response = client.post(f"/loans/{loan['_id']}/cancel", data={'cancel_token': 'cancel-token', 'reason': 'Application entered twice'})
    assert response.status_code == 302
    if payment_count >= 3:
        module.loans_col.update_one.assert_not_called()
    else:
        query, update = module.loans_col.update_one.call_args.args
        assert query['amount_paid'] == loan['amount_paid']
        assert query['$or'][0] == {'loan_payment_count': {'$lt': 3}}
        assert update['$set']['status'] == 'cancelled'
        assert update['$set']['cancelled_by_role'] == role
        assert 'amount_paid' not in update['$set']


@pytest.mark.parametrize('status', ['pending', 'rejected', 'settled', 'cancelled'])
def test_cancellation_rejects_non_ongoing_loans(editor, status):
    module, client, loan = editor
    loan['status'] = status
    with client.session_transaction() as session:
        session['manager_id'] = str(ObjectId())
        session['loan_cancel_token'] = 'cancel-token'
    assert client.post(f"/loans/{loan['_id']}/cancel", data={'cancel_token': 'cancel-token', 'reason': 'Duplicate'}).status_code == 302
    module.loans_col.update_one.assert_not_called()


def test_agent_cannot_cancel(editor):
    module, client, loan = editor
    assert client.post(f"/loans/{loan['_id']}/cancel", data={'reason': 'Duplicate'}).status_code == 403
    module.loans_col.update_one.assert_not_called()


def test_cancellation_blocks_payment_still_being_recorded(editor):
    module, client, loan = editor
    loan.update(status='active', loan_payment_count=3)
    module.payments_col.count_documents.return_value = 2
    with client.session_transaction() as session:
        session['executive_id'] = str(ObjectId())
        session['loan_cancel_token'] = 'cancel-token'
    client.post(f"/loans/{loan['_id']}/cancel", data={'cancel_token': 'cancel-token', 'reason': 'Duplicate'})
    module.loans_col.update_one.assert_not_called()


def test_cancelled_loan_does_not_sync_or_accrue_penalties(editor):
    module, _, loan = editor
    loan['status'] = 'cancelled'
    assert module.sync_loan(loan) is loan
    module.loans_col.update_one.assert_not_called()
    module.penalties_col.insert_one.assert_not_called()


@pytest.mark.parametrize('count', [2, 3, 4])
def test_cancel_button_and_modal_follow_payment_limit(count):
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(str(Path(__file__).parents[1] / 'templates')))
    html = env.get_template('loans/cancellation.html').render(
        loan={'id': 'loan', 'status': 'active', 'loan_number': 'LN-1'}, role='manager',
        cancellation_payment_count=count, session={}, url_for=lambda *args, **kwargs: '/cancel',
    )
    assert ('disabled aria-describedby="cancelLoanDisabledReason"' in html) == (count >= 3)
    assert ('id="cancelLoanModal"' in html) == (count < 3)
