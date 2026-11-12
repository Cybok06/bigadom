"""Regression tests with an isolated database; never connect to live MongoDB."""
import ast
from copy import deepcopy
from datetime import datetime
import importlib.util
from pathlib import Path
from types import SimpleNamespace, ModuleType
import sys

from bson import ObjectId
from flask import Flask, request, jsonify
import mongomock
import pytest

from services.customer_transfers import transfer_customers
from services.loans import money


class Collection:
    def __init__(self, raw): self.raw = raw
    def __getattr__(self, name):
        def call(*args, **kwargs):
            kwargs.pop("session", None)
            if name == "bulk_write":
                matched = modified = 0
                for operation in args[0]:
                    result = self.raw.update_one(operation._filter, operation._doc)
                    matched += result.matched_count; modified += result.modified_count
                return SimpleNamespace(matched_count=matched, modified_count=modified)
            return getattr(self.raw, name)(*args, **kwargs)
        return call


class Database:
    def __init__(self):
        self.raw = mongomock.MongoClient().test
        self.client = SimpleNamespace(start_session=lambda: self)
    def __getattr__(self, name): return Collection(self.raw[name])
    def __getitem__(self, name): return Collection(self.raw[name])
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def with_transaction(self, callback, **kwargs):
        snapshot = {n: list(self.raw[n].find()) for n in self.raw.list_collection_names()}
        try: return callback(self)
        except Exception:
            for n in self.raw.list_collection_names(): self.raw.drop_collection(n)
            for n, docs in snapshot.items():
                if docs: self.raw[n].insert_many(deepcopy(docs))
            raise


@pytest.fixture
def setup(monkeypatch):
    db = Database()
    manager, old, new, other, cid, untouched = [ObjectId() for _ in range(6)]
    for aid, mid in [(old, manager), (new, manager), (other, ObjectId())]:
        db.users.insert_one({"_id": aid, "role": "agent", "name": str(aid), "manager_id": mid})
    for customer in [cid, untouched]:
        db.customers.insert_one({"_id": customer, "name": str(customer), "agent_id": str(old), "manager_id": manager})
    loans = []
    for status in ["pending", "approved", "active", "grace_period", "overdue", "settled", "cancelled", "rejected"]:
        loans.append({"customer_id": cid, "agent_id": str(old), "manager_id": manager,
                      "status": status, "current_balance": 100, "amount_paid": 20})
    db.loans.insert_many(loans)
    db.payments.insert_one({"customer_id": cid, "agent_id": str(old), "amount": 20, "payment_type": "LOAN"})
    fake_db = ModuleType("db"); fake_db.db = db
    fake_audit = ModuleType("services.activity_audit")
    fake_audit.audit_action = lambda *a, **k: lambda f: f
    monkeypatch.setitem(sys.modules, "db", fake_db)
    monkeypatch.setitem(sys.modules, "services.activity_audit", fake_audit)
    spec = importlib.util.spec_from_file_location("test_transfer_route", "routes/transfer_customer.py")
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    app = Flask(__name__); app.secret_key = "test"
    app.register_blueprint(module.transfer_customer_bp)
    client = app.test_client()
    with client.session_transaction() as session: session["manager_id"] = str(manager)
    return SimpleNamespace(db=db, manager=manager, old=old, new=new, other=other,
                           cid=cid, untouched=untouched, client=client, app=app)


def payload(s, **changes):
    return {"from_agent_id": str(s.old), "to_agent_id": str(s.new), "customer_ids": [str(s.cid)], **changes}


def test_transfer_moves_open_loans_and_preserves_finances_and_history(setup):
    s = setup
    before = list(s.db.loans.find())
    payments = list(s.db.payments.find())
    response = s.client.post("/transfer-customers/run", json=payload(s))
    assert response.status_code == 200
    assert response.json["updated_customers"] == 1
    assert response.json["updated_loans"] == 5
    for prior in before:
        after = s.db.loans.find_one({"_id": prior["_id"]})
        expected = dict(prior)
        if prior["status"] in {"pending", "approved", "active", "grace_period", "overdue"}:
            expected["agent_id"] = str(s.new)
        assert after == expected
    assert list(s.db.payments.find()) == payments
    assert s.db.customers.find_one({"_id": s.untouched})["agent_id"] == str(s.old)
    assert s.db.customer_transfer_logs.find_one()["updated_loans"] == 5


@pytest.mark.parametrize("change,status", [({"dry_run": True}, 200), ({"customer_ids": ["bad"]}, 400), ({"customer_ids": "bad"}, 400)])
def test_preview_and_invalid_selection_do_not_write(setup, change, status):
    s = setup
    before = list(s.db.loans.find())
    response = s.client.post("/transfer-customers/run", json=payload(s, **change))
    assert response.status_code == status
    assert list(s.db.loans.find()) == before
    assert s.db.customer_transfer_logs.count_documents({}) == 0
    assert s.db.customers.find_one({"_id": s.cid})["agent_id"] == str(s.old)


def test_manager_cannot_transfer_outside_supervision(setup):
    s = setup
    response = s.client.post("/transfer-customers/run", json=payload(s, to_agent_id=str(s.other)))
    assert response.status_code == 403
    assert s.db.customers.find_one({"_id": s.cid})["agent_id"] == str(s.old)


def test_failure_rolls_back_customers_and_loans(setup, monkeypatch):
    s = setup
    original = Collection.__getattr__
    def patched(self, name):
        if self.raw.name == "customer_transfer_logs" and name == "insert_one":
            def fail(*a, **k): raise RuntimeError("audit write failed")
            return fail
        return original(self, name)
    monkeypatch.setattr(Collection, "__getattr__", patched)
    response = s.client.post("/transfer-customers/run", json=payload(s))
    assert response.status_code == 503
    assert s.db.customers.find_one({"_id": s.cid})["agent_id"] == str(s.old)
    assert all(l["agent_id"] == str(s.old) for l in s.db.loans.find())


def test_all_customers_and_string_loan_ids_cross_manager_transfer(setup):
    s = setup
    s.db.loans.insert_one({"customer_id": str(s.untouched), "status": "active", "agent_id": str(s.old)})
    dest_manager = s.db.users.find_one({"_id": s.other})["manager_id"]
    with s.client.session_transaction() as session:
        session.clear(); session["admin_id"] = str(ObjectId())
    response = s.client.post("/transfer-customers/run", json=payload(s, to_agent_id=str(s.other), customer_ids=[]))
    assert response.status_code == 200
    assert response.json["updated_customers"] == 2
    assert response.json["updated_loans"] == 6
    assert all(c["manager_id"] == dest_manager for c in s.db.customers.find())
    assert all(l["manager_id"] == dest_manager for l in s.db.loans.find({"status": "active"}))


def test_real_payment_handler_accepts_new_agent_and_rejects_previous_agent(setup, monkeypatch):
    s = setup
    # Extract the real handler, avoiding application startup and external services.
    tree = ast.parse(Path("payment.py").read_text(encoding="utf-8"))
    handler = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "add_payment")
    handler.decorator_list = []
    actor = SimpleNamespace(id=str(s.old))
    namespace = dict(datetime=datetime, request=request, jsonify=jsonify, ObjectId=ObjectId,
                     current_user=actor, get_accessible_agent_ids=lambda uid: [str(uid)],
                     customers_collection=s.db.customers, users_collection=s.db.users, money=money,
                     _is_ajax=lambda req: True, _restore_archived_customer=lambda *a: (None, None))
    exec(compile(ast.Module(body=[handler], type_ignores=[]), "payment.py", "exec"), namespace)
    loan_module = ModuleType("routes.loans")
    tree = ast.parse(Path("routes/loans.py").read_text(encoding="utf-8"))
    lookup = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "active_loan_for_customer")
    lookup_ns = dict(ObjectId=ObjectId, loans_col=s.db.loans, sync_loan=lambda loan: loan)
    exec(compile(ast.Module(body=[lookup], type_ignores=[]), "routes/loans.py", "exec"), lookup_ns)
    loan_module.active_loan_for_customer = lookup_ns["active_loan_for_customer"]
    loan_module.sync_loan = lambda loan: loan
    monkeypatch.setitem(sys.modules, "routes.loans", loan_module)
    s.app.add_url_rule("/test-payment", view_func=namespace["add_payment"], methods=["POST"])
    lid = s.db.loans.find_one({"status": "active"})["_id"]
    # An overpayment stops after access validation, before any financial writes.
    data = dict(customer_id=str(s.cid), loan_id=str(lid), amount="101", payment_type="LOAN")
    s.db.customers.update_one({"_id": s.cid}, {"$set": {"agent_id": str(s.new)}})
    actor.id = str(s.new)
    assert s.client.post("/test-payment", data=data).status_code == 404
    s.db.customers.update_one({"_id": s.cid}, {"$set": {"agent_id": str(s.old)}})
    assert s.client.post("/transfer-customers/run", json=payload(s)).status_code == 200
    response = s.client.post("/test-payment", data=data)
    assert response.status_code == 400
    assert "exceeds the loan balance" in response.json["message"]
    actor.id = str(s.old)
    assert s.client.post("/test-payment", data=data).status_code == 403


def test_database_repair_preserves_other_fields_and_is_idempotent(setup):
    from scripts.repair_transferred_loans import apply_plan
    s = setup
    loan = s.db.loans.find_one({"status": "active"})
    s.db.customers.update_one({"_id": s.cid}, {"$set": {"agent_id": str(s.new)}})
    plan = [{"loan_before": loan, "customer_id": s.cid, "expected_customer_agent_id": str(s.new),
             "expected_customer_manager_id": s.manager, "transfer_log_id": ObjectId(),
             "new_ownership": {"agent_id": str(s.new), "manager_id": s.manager}}]
    result = apply_plan(s.db, plan, "test-repair")
    assert result["financial_fields_unchanged"]
    assert s.db.loans.find_one({"_id": loan["_id"]}) == {**loan, **plan[0]["new_ownership"]}
    assert apply_plan(s.db, plan, "test-repair")["loan_count"] == 1
    assert s.db.customer_transfer_loan_repairs.count_documents({}) == 1


@pytest.mark.parametrize("changed", ["customer", "loan"])
def test_database_repair_rejects_stale_backup(setup, changed):
    from scripts.repair_transferred_loans import apply_plan
    s = setup
    loan = s.db.loans.find_one({"status": "active"})
    plan = [{"loan_before": loan, "customer_id": s.cid, "expected_customer_agent_id": str(s.old),
             "expected_customer_manager_id": s.manager, "transfer_log_id": ObjectId(),
             "new_ownership": {"agent_id": str(s.new), "manager_id": s.manager}}]
    if changed == "customer":
        s.db.customers.update_one({"_id": s.cid}, {"$set": {"agent_id": str(s.other)}})
    else:
        s.db.loans.update_one({"_id": loan["_id"]}, {"$set": {"current_balance": 90}})
    with pytest.raises(RuntimeError, match="changed since"):
        apply_plan(s.db, plan, "test-stale")
    assert s.db.loans.find_one({"_id": loan["_id"]})["agent_id"] == str(s.old)
    assert s.db.customer_transfer_loan_repairs.count_documents({}) == 0
