import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
import sys
import pytest
from flask import Flask
from bson import ObjectId


@pytest.fixture
def routes(monkeypatch):
    database=MagicMock()
    actor={'is_authenticated':True,'role':'manager','user_id':str(ObjectId()),'name':'Ama'}
    monkeypatch.setitem(sys.modules,'db',SimpleNamespace(db=database))
    monkeypatch.setitem(sys.modules,'login',SimpleNamespace(get_current_identity=lambda:actor))
    spec=importlib.util.spec_from_file_location('isolated_changes_routes','routes/change_activities.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    app=Flask(__name__);app.secret_key='test';app.register_blueprint(module.changes_bp)
    module.correct_payment=MagicMock(return_value=True)
    client=app.test_client()
    with client.session_transaction() as session:session['payment_edit_token']='test-token'
    return SimpleNamespace(**locals())


def edit(r,payload=None,token='test-token'):
    return r.client.post('/payments/'+str(ObjectId())+'/edit-amount',json=payload or {'amount':'100','expected_amount':'200'},headers={'X-Payment-Edit-Token':token})


@pytest.mark.parametrize('role',['manager','executive'])
def test_manager_executive_amount_edit_route(routes,role):
    routes.actor['role']=role
    assert edit(routes).status_code==200
    routes.module.correct_payment.assert_called_once()


def test_date_edit_is_rejected(routes):
    assert edit(routes,{'amount':'100','expected_amount':'200','date':'2025-01-01'}).status_code==400
    routes.module.correct_payment.assert_not_called()


def test_csrf_required(routes):
    assert edit(routes,token='').status_code==403
    routes.module.correct_payment.assert_not_called()


@pytest.mark.parametrize('role',['agent','admin',None])
def test_payment_route_denies_other_roles(routes,role):
    routes.actor['role']=role
    assert edit(routes).status_code==403
    routes.module.correct_payment.assert_not_called()


@pytest.mark.parametrize('role',['manager','agent','admin'])
def test_changes_page_executive_only(routes,role):
    routes.actor['role']=role
    assert routes.client.get('/executive/changes-activities').status_code==403
    routes.database.change_activities.find.assert_not_called()


def test_changes_page_search_filters(routes):
    r=routes;r.actor['role']='executive'
    r.database.change_activities.count_documents.return_value=0
    r.database.change_activities.find.return_value.sort.return_value.skip.return_value.limit.return_value=[]
    r.module.render_template=MagicMock(return_value='Changes Activities')
    response=r.client.get('/executive/changes-activities?search=A.*&action=payment.changed&start=2026-09-01&end=2026-09-10')
    assert response.status_code==200
    query=r.database.change_activities.find.call_args.args[0]
    assert query['action']=='payment.changed'
    assert query['$or'][0]['customer_name']['$regex']==r'A\.\*'
    assert query['timestamp']['$lt'].day==11


def test_payment_edit_controls_hidden_from_agents():
    from jinja2 import Environment,FileSystemLoader
    env=Environment(loader=FileSystemLoader('templates'))
    template=env.get_template('partials/payment_edit_button.html')
    payment={'_id':str(ObjectId()),'amount':200,'date':'2026-09-10'}
    assert 'data-edit-payment' not in template.render(can_edit_payments=False,payment=payment)
    assert 'data-edit-payment' in template.render(can_edit_payments=True,payment=payment)


def test_changes_page_renders_before_after_and_actor():
    from datetime import datetime
    from jinja2 import Environment,FileSystemLoader,ChoiceLoader,DictLoader
    env=Environment(loader=ChoiceLoader([DictLoader({'executive_sidebar.html':''}),FileSystemLoader('templates')]))
    row={'action':'payment.changed','timestamp':datetime.now(),'actor_name':'Manager Ama','actor_id':'actor',
         'actor_role':'manager','customer_name':'Customer A','customer_id':'customer','entity_id':'payment',
         'changes':{'amount':{'from':200,'to':100}},'extra':{}}
    html=env.get_template('executive_changes_activities.html').render(rows=[row],actions={'payment.changed':'Payment amount changed'},
          action='',search='',start='',end='',total=1,page=1,pages=1)
    for text in ('Manager Ama','Customer A','200','100','Payment amount changed'):assert text in html
