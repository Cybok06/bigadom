"""Exercise routes with isolated collections; never connect to the live database."""
import ast
from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock

from bson import ObjectId
from flask import Blueprint, Flask, flash, jsonify, redirect, request, url_for
import pytest
from jinja2 import Environment, FileSystemLoader

from services.product_cancellation import is_cancelled_purchase, payment_purchases


def load_functions(filename, names, namespace):
    tree = ast.parse(Path(filename).read_text(encoding="utf-8"))
    nodes = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name in names]
    exec(compile(ast.Module(body=nodes, type_ignores=[]), filename, "exec"), namespace)


@pytest.fixture
def cancellation(monkeypatch):
    app = Flask(__name__)
    app.secret_key = "test"
    bp = Blueprint("view", __name__)
    customers, users = MagicMock(), MagicMock()
    identity = dict(is_authenticated=True, role="executive", user_id=str(ObjectId()))
    monkeypatch.setitem(sys.modules, "login", SimpleNamespace(get_current_identity=lambda: identity))
    customer = {"_id": ObjectId(), "purchases": [
        {"product": {"name": "Fridge", "status": "active", "total": 500}},
        {"product": {"name": "TV", "status": "active", "total": 300}},
    ]}
    customers.find_one.return_value = customer
    customers.update_one.return_value.modified_count = 1
    ns = dict(view_bp=bp, customers_collection=customers, users_collection=users,
              request=request, jsonify=jsonify, flash=flash, redirect=redirect,
              url_for=url_for, ObjectId=ObjectId, datetime=datetime,
              is_cancelled_purchase=is_cancelled_purchase)
    load_functions("view.py", {"cancel_customer_product"}, ns)
    app.register_blueprint(bp)

    def post(index=0, cid=None):
        return app.test_client().post(f"/customer/{cid or customer['_id']}/cancel_product/{index}",
                                     headers={"Accept": "application/json"})
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("role", ["executive", "manager"])
def test_cancel_updates_only_selected_product_with_actor(cancellation, role):
    c = cancellation
    c.identity["role"] = role
    assert c.post(index=1).status_code == 200
    query, update = c.customers.update_one.call_args.args
    assert query["purchases.1"] == c.customer["purchases"][1]
    fields = update["$set"]
    assert fields["purchases.1.product.status"] == "cancelled"
    assert fields["purchases.1.status"] == "cancelled"
    assert fields["purchases.1.cancelled_by_role"] == role
    assert fields["purchases.1.cancelled_by"] == c.identity["user_id"]
    assert all(key.startswith("purchases.1.") for key in fields)


def test_manager_scope_includes_own_agents_only(cancellation):
    c = cancellation
    c.identity["role"] = "manager"
    agent_id = ObjectId()
    c.users.find.return_value = [{"_id": agent_id}]
    assert c.post().status_code == 200
    query = c.customers.find_one.call_args.args[0]
    assert query["$or"] == [
        {"manager_id": {"$in": [ObjectId(c.identity["user_id"]), c.identity["user_id"]]}},
        {"agent_id": {"$in": [agent_id, str(agent_id)]}},
    ]
    assert c.users.find.call_args.args[0]["manager_id"]["$in"] == [ObjectId(c.identity["user_id"]), c.identity["user_id"]]


@pytest.mark.parametrize("role", ["agent", "admin", None])
def test_disallowed_roles_cannot_cancel(cancellation, role):
    cancellation.identity["role"] = role
    assert cancellation.post().status_code == 403
    cancellation.customers.find_one.assert_not_called()
    cancellation.customers.update_one.assert_not_called()


def test_missing_or_outside_scope(cancellation):
    cancellation.customers.find_one.return_value = None
    assert cancellation.post().status_code == 404
    cancellation.customers.update_one.assert_not_called()


def test_invalid_customer_or_product(cancellation):
    assert cancellation.post(cid="invalid").status_code == 400
    assert cancellation.post(index=9).status_code == 404
    cancellation.customers.update_one.assert_not_called()


def test_already_cancelled_is_idempotent(cancellation):
    cancellation.customer["purchases"][0]["product"]["status"] = "cancelled"
    assert cancellation.post().status_code == 200
    cancellation.customers.update_one.assert_not_called()


def test_concurrent_product_edit_is_rejected(cancellation):
    cancellation.customers.update_one.return_value.modified_count = 0
    assert cancellation.post().status_code == 409


@pytest.fixture
def payments():
    app = Flask(__name__)
    app.secret_key = "test"
    bp = Blueprint("payment", __name__)
    customers, records, users, rollups = (MagicMock() for _ in range(4))
    customer = {"_id": ObjectId(), "agent_id": str(ObjectId()), "purchases": [
        {"product": {"name": "Fridge", "status": "cancelled", "total": 500}},
        {"product": {"name": "TV", "status": "active", "total": 300}},
    ]}
    customers.find_one.return_value = customer
    customers.find.return_value = [customer]
    users.find_one.return_value = {"manager_id": ObjectId()}
    database = MagicMock()
    database.loans.find.return_value.sort.return_value = []
    archived = MagicMock()
    archived.find.return_value = []
    render = MagicMock(return_value="rendered")
    ns = dict(payment_bp=bp, login_required=lambda func: func, request=request,
              current_user=SimpleNamespace(id=customer["agent_id"], is_authenticated=True),
              customers_collection=customers, payments_collection=records,
              users_collection=users, sales_close_collection=rollups, db=database,
              archived_customers_collection=archived, render_template=render,
              get_accessible_agent_ids=lambda uid: [uid], ObjectId=ObjectId,
              datetime=datetime, jsonify=jsonify, flash=flash, redirect=redirect, url_for=url_for,
              is_cancelled_purchase=is_cancelled_purchase, payment_purchases=payment_purchases,
              typed_inc=lambda kind, amount: {"total": amount})
    load_functions("payment.py", {"add_payment", "get_product_paid", "_is_ajax", "_existing_payment_info", "_json_safe"}, ns)
    app.register_blueprint(bp)
    client = app.test_client()
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("force", ["no", "yes"])
def test_stale_or_forced_payment_cannot_pay_cancelled_product(payments, force):
    p = payments
    response = p.client.post("/add_payment", data=dict(customer_id=str(p.customer["_id"]),
        product_id="0", amount="50", force=force), headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 409
    assert "cancelled" in response.json["message"]
    p.records.insert_one.assert_not_called()
    p.rollups.update_one.assert_not_called()
    p.customers.update_one.assert_not_called()


def test_remaining_product_uses_original_index_for_payment(payments):
    p = payments
    p.records.find.return_value = []
    response = p.client.post("/add_payment", data=dict(customer_id=str(p.customer["_id"]),
        product_id="1", amount="50"), headers={"X-Requested-With": "XMLHttpRequest"})
    assert response.status_code == 200
    doc = p.records.insert_one.call_args.args[0]
    assert doc["product_index"] == 1
    assert doc["product_name"] == "TV"


def test_cancelled_summary_unavailable(payments):
    p = payments
    response = p.client.get(f"/payment/product_paid?customer_id={p.customer['_id']}&product_index=0")
    assert response.status_code == 409
    p.records.find.assert_not_called()


def test_payment_options_hide_cancelled_and_preserve_indices(payments):
    p = payments
    assert p.client.get("/add_payment").status_code == 200
    purchases = p.render.call_args.kwargs["customers"][0]["purchases"]
    assert len(purchases) == 1
    assert purchases[0]["product_index"] == 1
    assert purchases[0]["product"]["name"] == "TV"


def test_legacy_cancelled_spelling_and_purchase_level_status():
    purchases = [{"status": "canceled", "product": {}},
                 {"product": {"status": " Cancelled "}}, {"product": {"name": "TV"}}]
    assert [p["product_index"] for p in payment_purchases(purchases)] == [2]


@pytest.mark.parametrize("role", ["executive", "manager", "agent"])
def test_profile_cancel_button_permissions_and_cancelled_badge(role):
    env = Environment(loader=FileSystemLoader("templates"))
    template = env.get_template("partials/customer_tabs/products.html")
    purchase = {"product": {"name": "TV", "status": "active", "total": 300},
                "amount_left": 300, "amount_paid": 0, "progress": None}
    args = dict(customer_id="test", session={f"{role}_id": "test"},
                purchases=[purchase], url_for=lambda endpoint, **kw: f"/{endpoint}")
    html = template.render(**args)
    assert ("Cancel product" in html) == (role in {"executive", "manager"})
    purchase["product"]["status"] = "cancelled"
    purchase["amount_left"] = 0
    html = template.render(**args)
    assert "Cancelled</span>" in html
    assert "Cancel product" not in html
    assert "Submit for packaging" not in html


def test_default_payment_product_skips_cancelled():
    ns = {"is_cancelled_purchase": is_cancelled_purchase}
    load_functions("view.py", {"_default_payment_product_index"}, ns)
    choose = ns["_default_payment_product_index"]
    assert choose([{"product": {"status": "cancelled"}}, {"product": {"name": "TV"}}]) == 1
    assert choose([{"product": {"status": "cancelled"}}]) is None
