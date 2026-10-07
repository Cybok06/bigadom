"""Isolated database tests, including rollback; no live database imports."""
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch
import pytest
import mongomock
from bson import ObjectId
from services.payment_corrections import correct_payment
from services.change_activities import audited_details_update


class Collection:
    def __init__(self, collection): self.raw = collection
    def __getattr__(self, name):
        method = getattr(self.raw, name)
        def call(*args, **kwargs):
            kwargs.pop('session', None)
            return method(*args, **kwargs)
        return call


class Database:
    def __init__(self):
        self.raw = mongomock.MongoClient().test
        self.client = SimpleNamespace(start_session=lambda: self)
    def __getattr__(self, name): return Collection(self.raw[name])
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def with_transaction(self, callback):
        snapshot = {name: list(self.raw[name].find()) for name in self.raw.list_collection_names()}
        try: return callback(self)
        except Exception:
            for name in self.raw.list_collection_names(): self.raw.drop_collection(name)
            for name, docs in snapshot.items():
                if docs: self.raw[name].insert_many(deepcopy(docs))
            raise


@pytest.fixture
def records():
    db = Database()
    manager, agent, cid, pid = [ObjectId() for _ in range(4)]
    actor = {'is_authenticated': True, 'role': 'manager', 'user_id': str(manager), 'name': 'Manager Ama'}
    customer = {'_id': cid, 'name': 'Customer A', 'manager_id': manager, 'agent_id': str(agent)}
    payment = {'_id': pid, 'customer_id': cid, 'agent_id': str(agent), 'manager_id': manager,
               'payment_type': 'PRODUCT', 'amount': 200.0, 'date': '2026-09-10', 'method': 'Cash', 'product_index': 0}
    db.customers.insert_one(customer); db.payments.insert_one(payment)
    db.sales_close.insert_one({'agent_id': str(agent), 'date': payment['date'], 'total_amount': 200.0, 'product_amount': 200.0, 'count': 1})
    return SimpleNamespace(**locals())


@pytest.mark.parametrize('role', ['manager', 'executive'])
def test_200_corrected_to_100_changes_payment_ledger_and_audit(records, role):
    r = records; r.actor['role'] = role
    assert correct_payment(r.db, r.pid, r.actor, '100', '200')
    payment = r.db.payments.find_one({'_id': r.pid})
    assert payment['amount'] == 100
    for key in ('date', 'agent_id', 'customer_id', 'method', 'product_index'):
        assert payment[key] == r.payment[key]
    ledger = r.db.sales_close.find_one({})
    assert ledger['total_amount'] == ledger['product_amount'] == 100
    assert ledger['count'] == 1
    log = r.db.change_activities.find_one({})
    assert log['changes']['amount'] == {'from': 200.0, 'to': 100.0}
    assert log['actor_name'] == 'Manager Ama'
    assert log['extra']['unclosed_adjustment'] == -100


def test_increase_only_adds_difference(records):
    r=records; correct_payment(r.db,r.pid,r.actor,'250.25','200')
    assert r.db.sales_close.find_one({})['total_amount'] == 250.25


def test_legacy_ledger_does_not_create_negative_typed_balance(records):
    r=records; r.db.sales_close.update_one({}, {'$unset': {'product_amount': ''}})
    correct_payment(r.db,r.pid,r.actor,100,200)
    row=r.db.sales_close.find_one({})
    assert row['total_amount']==100 and 'product_amount' not in row


def test_susu_does_not_debit_product_balance(records):
    r=records; r.db.payments.update_one({}, {'$set': {'payment_type':'SUSU'}})
    with pytest.raises(ValueError,match='Insufficient unclosed'):
        correct_payment(r.db,r.pid,r.actor,100,200)
    assert r.db.sales_close.find_one({})['total_amount']==200
    assert r.db.payments.find_one({})['amount']==200


def test_insufficient_unclosed_rolls_back_partial_debit(records):
    r=records; r.db.sales_close.update_one({}, {'$set': {'total_amount':50,'product_amount':50}})
    with pytest.raises(ValueError,match='Insufficient unclosed'):
        correct_payment(r.db,r.pid,r.actor,100,200)
    assert r.db.sales_close.find_one({})['total_amount']==50
    assert r.db.payments.find_one({})['amount']==200
    assert r.db.change_activities.count_documents({})==0


def test_audit_failure_rolls_back_payment_and_ledger(records):
    r=records
    with patch('services.payment_corrections.record_change',side_effect=RuntimeError('audit unavailable')):
        with pytest.raises(RuntimeError): correct_payment(r.db,r.pid,r.actor,100,200)
    assert r.db.sales_close.find_one({})['total_amount']==200
    assert r.db.payments.find_one({})['amount']==200


def test_stale_edit_and_repeat_do_not_double_debit(records):
    r=records; correct_payment(r.db,r.pid,r.actor,100,200)
    with pytest.raises(ValueError,match='Payment changed'):
        correct_payment(r.db,r.pid,r.actor,100,200)
    assert r.db.sales_close.find_one({})['total_amount']==100
    assert r.db.change_activities.count_documents({})==1


@pytest.mark.parametrize('value',['NaN','Infinity','0','-1','1.001','nope',None])
def test_invalid_amount_does_not_write(records,value):
    r=records
    with pytest.raises(ValueError): correct_payment(r.db,r.pid,r.actor,value,200)
    assert r.db.payments.find_one({})['amount']==200


@pytest.mark.parametrize('role',['agent','admin',None])
def test_roles_cannot_edit(records,role):
    r=records; r.actor['role']=role
    with pytest.raises(PermissionError): correct_payment(r.db,r.pid,r.actor,100,200)


def test_manager_cannot_edit_other_managers_customer(records):
    r=records; r.actor['user_id']=str(ObjectId())
    with pytest.raises(LookupError): correct_payment(r.db,r.pid,r.actor,100,200)


def test_loan_correction_reopens_settled_loan(records):
    r=records; loan_id=ObjectId()
    r.db.payments.update_one({}, {'$set': {'payment_type':'LOAN','loan_id':loan_id}})
    r.db.sales_close.update_one({}, {'$set': {'loan_amount':200,'product_amount':0}})
    r.db.loans.insert_one({'_id':loan_id,'customer_id':r.cid,'status':'settled','amount_paid':200,
                          'current_balance':0,'expected_total_repayment':200,'settled_at':'old'})
    correct_payment(r.db,r.pid,r.actor,100,200)
    loan=r.db.loans.find_one({})
    assert str(loan['amount_paid'])=='100.00' and str(loan['current_balance'])=='100.00'
    assert loan['status']=='active' and 'settled_at' not in loan
    assert r.db.sales_close.find_one({})['loan_amount']==100


def test_customer_detail_log_and_update_are_atomic(records):
    r=records
    audited_details_update(r.db,r.db.customers,r.customer,{'name':'New name'},r.actor,'customer.details_changed',r.customer)
    assert r.db.customers.find_one({})['name']=='New name'
    assert r.db.change_activities.find_one({})['changes']['name']=={'from':'Customer A','to':'New name'}
    with pytest.raises(ValueError,match='record changed'):
        audited_details_update(r.db,r.db.customers,r.customer,{'name':'Stale name'},r.actor,'customer.details_changed',r.customer)
    assert r.db.change_activities.count_documents({})==1


def test_guarantor_audit_failure_prevents_detail_change(records):
    r=records; loan={'_id':ObjectId(),'guarantor_details':[{'name':'Name','value':'Before'}]}
    r.db.loans.insert_one(loan)
    with patch('services.change_activities.record_change',side_effect=RuntimeError('failed')):
        with pytest.raises(RuntimeError):
            audited_details_update(r.db,r.db.loans,loan,{'guarantor_details':[{'name':'Name','value':'After'}]},r.actor,'loan.guarantor_changed',r.customer)
    assert r.db.loans.find_one({})['guarantor_details']==loan['guarantor_details']
