"""Exercise the history route and real role decorator without a live database."""
import ast
import importlib.util
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import mongomock
import pytest
from bson import ObjectId
from flask import Flask, jsonify, redirect, request, url_for
from jinja2 import ChoiceLoader, DictLoader


@pytest.fixture
def history(monkeypatch):
    database = mongomock.MongoClient().history
    manager, other_manager, own, other, customer = [ObjectId() for _ in range(5)]
    actor = {'is_authenticated': True, 'role': 'manager', 'user_id': str(manager)}
    # Load the production decorator alone, avoiding login/database startup effects.
    tree = ast.parse(Path('login.py').read_text(encoding='utf-8'))
    decorator = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'role_required')
    namespace = dict(get_current_identity=lambda: actor, request=request, jsonify=jsonify, redirect=redirect, url_for=url_for)
    exec(compile(ast.Module(body=[decorator], type_ignores=[]), 'login.py', 'exec'), namespace)
    monkeypatch.setitem(sys.modules, 'db', SimpleNamespace(db=database))
    monkeypatch.setitem(sys.modules, 'login', SimpleNamespace(get_current_identity=lambda: actor, role_required=namespace['role_required']))
    spec = importlib.util.spec_from_file_location('isolated_payment_history', 'routes/payment_history.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = Flask(__name__, template_folder=str(Path('templates').resolve()))
    app.config['TESTING'] = True
    app.jinja_loader = ChoiceLoader([DictLoader({f'{role}_sidebar.html': f'{role} sidebar' for role in ('manager', 'admin', 'executive')}), app.jinja_loader])
    app.add_url_rule('/login', endpoint='login.login', view_func=lambda: 'Login')
    app.register_blueprint(module.payment_history_bp)
    database.users.insert_many([
        {'_id': own, 'role': 'agent', 'manager_id': manager, 'name': 'Own Agent'},
        {'_id': other, 'role': 'agent', 'manager_id': str(other_manager), 'name': 'Other Agent'},
    ])
    database.customers.insert_one({'_id': customer, 'name': 'Ama Mensah'})
    today = datetime.now(timezone.utc).date().isoformat()
    for agent, amount, kind in [(own, 20, 'SUSU'), (own, 30, 'LOAN'), (own, 40, 'PRODUCT'), (other, 999, 'SUSU')]:
        database.payments.insert_one({'agent_id': str(agent), 'customer_id': customer, 'amount': amount,
                                     'date': today, 'time': '12:30:00', 'payment_type': kind, 'method': 'Cash'})
    database.payments.insert_many([
        {'agent_id': str(own), 'customer_id': customer, 'amount': 500, 'date': today, 'payment_type': 'WITHDRAWAL'},
        {'agent_id': str(own), 'customer_id': customer, 'amount': 700, 'date': '2020-01-01', 'payment_type': 'PRODUCT'},
    ])
    return SimpleNamespace(client=app.test_client(), app=app, db=database, actor=actor, own=own, other=other,
                           customer=customer, manager=manager, today=today, module=module)


def test_manager_sees_only_own_agents_today(history):
    response = history.client.get('/payment-history')
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    for text in ('Own Agent', 'Ama Mensah', '90.00', '3 payments', 'Today’s payments'):
        assert text in html
    for text in ('Other Agent', '999.00', '500.00', '700.00'):
        assert text not in html


@pytest.mark.parametrize('role', ['admin', 'executive'])
def test_global_roles_see_and_filter_all_agents(history, role):
    history.actor['role'] = role
    html = history.client.get('/payment-history').get_data(as_text=True)
    assert 'Other Agent' in html and '1,089.00' in html
    html = history.client.get(f'/payment-history?agent_id={history.other}&payment_type=SUSU').get_data(as_text=True)
    assert '999.00' in html and '1 payments' in html


def test_manager_cannot_request_another_agent(history):
    assert history.client.get(f'/payment-history?agent_id={history.other}').status_code == 403


@pytest.mark.parametrize('kind,amount', [('SUSU', '20.00'), ('LOAN', '30.00'), ('PRODUCT', '40.00')])
def test_payment_type_filter(history, kind, amount):
    html = history.client.get(f'/payment-history?payment_type={kind}&agent_id={history.own}').get_data(as_text=True)
    assert amount in html and '1 payments' in html


def test_date_filter(history):
    html = history.client.get('/payment-history?date=2020-01-01').get_data(as_text=True)
    assert '700.00' in html and '01 Jan 2020' in html and '1 payments' in html


@pytest.mark.parametrize('query', ['date=bad', 'date=2026-02-30', 'date=9999-12-31', 'payment_type=WITHDRAWAL', 'page=abc', 'page=0'])
def test_invalid_filters(history, query):
    assert history.client.get('/payment-history?' + query).status_code == 400


@pytest.mark.parametrize('role', ['agent', 'inventory', 'customer_support'])
def test_other_roles_denied_even_with_main_admin_flag(history, role):
    history.actor.update(role=role, is_main_admin=True)
    assert history.client.get('/payment-history').status_code == 403


def test_unauthenticated_redirects_to_login(history):
    history.actor['is_authenticated'] = False
    assert history.client.get('/payment-history').status_code == 302


def test_manager_without_agents_gets_empty_results(history):
    history.actor['user_id'] = str(ObjectId())
    html = history.client.get('/payment-history').get_data(as_text=True)
    assert 'No agents are currently assigned to you' in html
    assert '999.00' not in html and 'Own Agent' not in html


def test_legacy_ids_archived_customer_and_missing_type(history):
    history.db.users.update_one({'_id': history.own}, {'$set': {'manager_id': str(history.manager)}})
    customer = history.db.customers.find_one_and_delete({'_id': history.customer})
    history.db.Archived_customers.insert_one(customer)
    history.db.payments.insert_one({'agent_id': history.own, 'customer_id': str(history.customer),
                                   'amount': 12.5, 'created_at': datetime.fromisoformat(history.today)})
    html = history.client.get('/payment-history?payment_type=PRODUCT').get_data(as_text=True)
    assert '52.50' in html and '12.50' in html and 'Ama Mensah' in html


def test_pagination_keeps_filters_and_totals(history):
    for i in range(55):
        history.db.payments.insert_one({'agent_id': str(history.own), 'amount': 1, 'payment_type': 'SUSU',
                                       'date': history.today, 'time': f'11:{i:02}:00'})
    html = history.client.get(f'/payment-history?agent_id={history.own}&payment_type=SUSU&page=2').get_data(as_text=True)
    assert 'Showing 51–56 of 56 payments' in html and '75.00' in html
    assert f'agent_id={history.own}' in html and 'payment_type=SUSU' in html and 'Previous' in html


def test_customer_names_are_escaped(history):
    history.db.customers.update_one({'_id': history.customer}, {'$set': {'name': '<script>alert(1)</script>'}})
    html = history.client.get('/payment-history').get_data(as_text=True)
    assert '<script>alert(1)</script>' not in html
    assert '&lt;script&gt;' in html
