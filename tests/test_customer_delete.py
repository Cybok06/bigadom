import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys
import re

import pytest
from bson import ObjectId
from flask import Flask, Blueprint, session, request, jsonify, flash, redirect, url_for
from pymongo.errors import DuplicateKeyError


@pytest.fixture
def deletion(monkeypatch):
    # Load only the route under test: never import the live database connection.
    tree = ast.parse(Path('customers.py').read_text(encoding='utf-8'))
    routes = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in {'delete_customer', '_deleted_customer_scope', 'deleted_customers_list', 'restore_deleted_customer', '_safe_int'}]
    app = Flask(__name__)
    app.secret_key = 'test'
    bp = Blueprint('customers', __name__)
    active, deleted, database = MagicMock(), MagicMock(), MagicMock()
    transaction = database.client.start_session.return_value.__enter__.return_value
    transaction.with_transaction.side_effect = lambda callback: callback(transaction)
    identity = {'is_authenticated': True, 'role': 'manager', 'user_id': str(ObjectId())}
    monkeypatch.setitem(sys.modules, 'login', SimpleNamespace(get_current_identity=lambda: identity))
    ns = dict(customers_bp=bp, customers_col=active, deleted_col=deleted, db=database,
              Any=object, re=re, ObjectId=ObjectId, session=session, request=request, jsonify=jsonify,
              flash=flash, redirect=redirect, url_for=url_for)
    exec(compile(ast.Module(body=routes, type_ignores=[]), 'customers.py', 'exec'), ns)
    app.register_blueprint(bp)
    customer = {'_id': ObjectId(), 'manager_id': ObjectId(identity['user_id']),
                'name': 'Ama', 'purchases': [{'product': {'total': 200}}]}
    active.find_one.return_value = customer
    active.delete_one.return_value.deleted_count = 1
    def post(cid=None):
        return app.test_client().post('/customer/' + str(cid or customer['_id']) + '/delete',
                                     headers={'Accept': 'application/json'})
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('role', ['manager', 'executive'])
def test_move_preserves_entire_document_and_payment_links(deletion, role):
    d = deletion
    d.identity['role'] = role
    assert d.post().status_code == 200
    d.deleted.insert_one.assert_called_once_with(d.customer, session=d.transaction)
    d.active.delete_one.assert_called_once_with({'_id': d.customer['_id']}, session=d.transaction)
    d.transaction.with_transaction.assert_called_once()
    query = d.active.find_one.call_args.args[0]
    if role == 'manager':
        assert query['manager_id']['$in'] == [d.identity['user_id'], ObjectId(d.identity['user_id'])]
    else:
        assert 'manager_id' not in query
    assert not d.database.payments.mock_calls


def test_outside_scope_or_missing_customer_is_not_deleted(deletion):
    deletion.active.find_one.return_value = None
    assert deletion.post().status_code == 404
    deletion.deleted.insert_one.assert_not_called()
    deletion.active.delete_one.assert_not_called()


@pytest.mark.parametrize('role', ['agent', 'admin', None])
def test_other_roles_cannot_delete(deletion, role):
    deletion.identity['role'] = role
    assert deletion.post().status_code == 403
    deletion.database.client.start_session.assert_not_called()


def test_invalid_id_does_not_touch_database(deletion):
    assert deletion.post('invalid').status_code == 400
    deletion.database.client.start_session.assert_not_called()


def test_destination_conflict_keeps_active_customer(deletion):
    deletion.deleted.insert_one.side_effect = DuplicateKeyError('existing deleted customer')
    assert deletion.post().status_code == 503
    deletion.active.delete_one.assert_not_called()


def test_failed_removal_aborts_transaction_callback(deletion):
    deletion.active.delete_one.return_value.deleted_count = 0
    assert deletion.post().status_code == 503


def test_delete_rejects_get(deletion):
    assert deletion.app.test_client().get('/customer/' + str(deletion.customer['_id']) + '/delete').status_code == 405


@pytest.mark.parametrize('role', ['manager', 'executive'])
def test_restore_preserves_document(deletion, role):
    d = deletion
    d.identity['role'] = role
    d.deleted.find_one.return_value = d.customer
    d.deleted.delete_one.return_value.deleted_count = 1
    response = d.app.test_client().post('/customer/' + str(d.customer['_id']) + '/restore')
    assert response.status_code == 200
    d.active.insert_one.assert_called_once_with(d.customer, session=d.transaction)
    d.deleted.delete_one.assert_called_once_with({'_id': d.customer['_id']}, session=d.transaction)
    query = d.deleted.find_one.call_args.args[0]
    assert ('manager_id' in query) == (role == 'manager')
    assert not d.database.payments.mock_calls


def test_restore_conflict_preserves_deleted_copy(deletion):
    d = deletion
    d.deleted.find_one.return_value = d.customer
    d.active.insert_one.side_effect = DuplicateKeyError('duplicate')
    assert d.app.test_client().post('/customer/' + str(d.customer['_id']) + '/restore').status_code == 409
    d.deleted.delete_one.assert_not_called()


def test_restore_missing_or_outside_scope(deletion):
    d = deletion
    d.deleted.find_one.return_value = None
    assert d.app.test_client().post('/customer/' + str(d.customer['_id']) + '/restore').status_code == 404
    d.active.insert_one.assert_not_called()


@pytest.mark.parametrize('endpoint', ['/customers/deleted', '/customer/123/restore'])
def test_deleted_endpoints_reject_agents(deletion, endpoint):
    deletion.identity['role'] = 'agent'
    client = deletion.app.test_client()
    response = client.get(endpoint) if endpoint.endswith('deleted') else client.post(endpoint)
    assert response.status_code == 403
    deletion.deleted.find.assert_not_called()
    deletion.active.insert_one.assert_not_called()


@pytest.mark.parametrize('role', ['manager', 'executive'])
def test_deleted_list_scopes_search_and_pagination(deletion, role):
    d = deletion
    d.identity['role'] = role
    d.deleted.count_documents.return_value = 21
    d.deleted.find.return_value.sort.return_value.skip.return_value.limit.return_value = [d.customer]
    response = d.app.test_client().get('/customers/deleted?search=A.*&page=2')
    assert response.status_code == 200
    assert response.json['page'] == 2
    assert response.json['pages'] == 2
    assert response.json['customers'][0]['id'] == str(d.customer['_id'])
    query = d.deleted.find.call_args.args[0]
    assert ('manager_id' in query) == (role == 'manager')
    assert query['$or'][0]['name']['$regex'] == re.escape('A.*')
    d.deleted.find.return_value.sort.return_value.skip.assert_called_once_with(20)


def test_restore_failed_removal_reports_failure(deletion):
    d = deletion
    d.deleted.find_one.return_value = d.customer
    d.deleted.delete_one.return_value.deleted_count = 0
    assert d.app.test_client().post('/customer/' + str(d.customer['_id']) + '/restore').status_code == 503
