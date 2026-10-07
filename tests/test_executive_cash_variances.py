"""Isolated cash-log route tests with real permissions and template rendering."""
import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from bson import ObjectId
from flask import Flask, jsonify, redirect, render_template, request, url_for
from jinja2 import ChoiceLoader, DictLoader, FileSystemLoader
import pytest


@pytest.fixture
def cash_log():
    app = Flask(__name__)
    app.secret_key = "test"
    app.jinja_loader = ChoiceLoader([
        DictLoader({"executive_sidebar.html": "<aside>Executive navigation</aside>"}),
        FileSystemLoader("templates"),
    ])
    app.add_url_rule("/login", endpoint="login.login", view_func=lambda: "Login")
    identity = dict(is_authenticated=True, role="executive", user_id=str(ObjectId()), name="Executive Ama")
    # Use the application's actual permission decorator without importing the live DB.
    login_tree = ast.parse(Path("login.py").read_text(encoding="utf-8"))
    role_node = next(node for node in login_tree.body if isinstance(node, ast.FunctionDef) and node.name == "role_required")
    auth_ns = dict(get_current_identity=lambda: identity, request=request, jsonify=jsonify,
                   redirect=redirect, url_for=url_for)
    exec(compile(ast.Module(body=[role_node], type_ignores=[]), "login.py", "exec"), auth_ns)
    agents = [{"_id": ObjectId(), "name": "Agent Kofi", "branch": "Accra"},
              {"_id": ObjectId(), "name": "Agent Akua", "branch": "Kumasi"}]
    users, entries = MagicMock(), MagicMock()
    users.find.return_value.sort.return_value = agents
    users.find_one.return_value = agents[0]
    entries.count_documents.return_value = 0
    cursor = entries.find.return_value.sort.return_value
    cursor.skip.return_value.limit.return_value = []
    database = {"users": users, "agent_cash_variances": entries}
    tree = ast.parse(Path("routes/executive_cash_variances.py").read_text(encoding="utf-8"))
    tree.body = [node for node in tree.body if not (
        isinstance(node, ast.ImportFrom) and node.module in {"db", "login"})]
    ns = dict(__name__=__name__, db=database, get_current_identity=lambda: identity,
              role_required=auth_ns["role_required"])
    exec(compile(tree, "routes/executive_cash_variances.py", "exec"), ns)
    render = MagicMock(wraps=render_template)
    ns["render_template"] = render
    app.register_blueprint(ns["executive_cash_variances_bp"])
    client = app.test_client()
    valid = dict(agent_id=str(agents[0]["_id"]), entry_type="shortage", amount="23.45",
                 entry_date="2026-10-07", note="Cash reconciliation")
    path = "/executive/shortage-and-surplus/"
    return SimpleNamespace(**locals())


@pytest.mark.parametrize("entry_type", ["shortage", "surplus"])
def test_record_cash_entry_and_redirect_without_repeat_save(cash_log, entry_type):
    c = cash_log
    response = c.client.post(c.path, data={**c.valid, "entry_type": entry_type})
    assert response.status_code == 302
    assert "agent_id=" + c.valid["agent_id"] in response.location
    doc = c.entries.insert_one.call_args.args[0]
    assert doc["agent_id"] == c.valid["agent_id"]
    assert doc["agent_name"] == "Agent Kofi"
    assert doc["entry_type"] == entry_type
    assert doc["amount"] == 23.45
    assert doc["entry_date"] == "2026-10-07"
    assert doc["recorded_by"] == c.identity["user_id"]
    assert doc["recorded_by_name"] == "Executive Ama"
    assert doc["created_at"].utcoffset().total_seconds() == 0
    assert c.client.get(response.location).status_code == 200
    c.entries.insert_one.assert_called_once()


@pytest.mark.parametrize("field,value", [
    ("agent_id", ""), ("agent_id", "invalid"), ("entry_type", "invalid"),
    ("amount", ""), ("amount", "invalid"), ("amount", "0"), ("amount", "-1"),
    ("amount", "NaN"), ("amount", "Infinity"), ("amount", "-Infinity"),
    ("amount", "1.001"), ("amount", "1000000000"),
    ("entry_date", ""), ("entry_date", "2026-02-30"), ("note", "a" * 1001),
])
def test_invalid_input_rejected_without_inserting(cash_log, field, value):
    c = cash_log
    response = c.client.post(c.path, data={**c.valid, field: value})
    assert response.status_code == 400
    c.entries.insert_one.assert_not_called()
    assert c.render.call_args.kwargs["form_values"][field] == value


def test_selected_user_must_be_an_existing_agent(cash_log):
    c = cash_log
    c.users.find_one.return_value = None
    assert c.client.post(c.path, data=c.valid).status_code == 400
    c.entries.insert_one.assert_not_called()
    assert c.users.find_one.call_args.args[0] == {"_id": c.agents[0]["_id"], "role": "agent"}


@pytest.mark.parametrize("role", ["agent", "manager", "admin", "accounting"])
@pytest.mark.parametrize("method", ["get", "post"])
def test_non_executives_cannot_view_or_record(cash_log, role, method):
    c = cash_log
    c.identity["role"] = role
    assert getattr(c.client, method)(c.path, data=c.valid).status_code == 403
    c.entries.insert_one.assert_not_called()
    c.entries.find.assert_not_called()
    c.users.find.assert_not_called()


def test_anonymous_user_redirects_to_login(cash_log):
    c = cash_log
    c.identity["is_authenticated"] = False
    response = c.client.get(c.path)
    assert response.status_code == 302
    assert "/login" in response.location
    c.entries.find.assert_not_called()


def test_agent_filter_paginates_in_database_and_links_preserve_filter(cash_log):
    c = cash_log
    c.entries.count_documents.return_value = 61
    response = c.client.get(c.path, query_string=dict(agent_id=c.valid["agent_id"], page="2"))
    assert response.status_code == 200
    query = {"agent_id": c.valid["agent_id"]}
    c.entries.count_documents.assert_called_once_with(query)
    c.entries.find.assert_called_once_with(query)
    c.entries.find.return_value.sort.assert_called_once_with([("created_at", -1), ("_id", -1)])
    c.cursor.skip.assert_called_once_with(20)
    c.cursor.skip.return_value.limit.assert_called_once_with(20)
    html = response.get_data(as_text=True)
    assert f"agent_id={c.valid['agent_id']}&amp;page=3" in html
    assert "Page 2 of 4" in html


@pytest.mark.parametrize("requested,expected", [("bad", 1), ("0", 1), ("-5", 1), ("999", 3)])
def test_pagination_clamps_invalid_or_out_of_range_pages(cash_log, requested, expected):
    c = cash_log
    c.entries.count_documents.return_value = 41
    assert c.client.get(c.path, query_string={"page": requested}).status_code == 200
    assert c.render.call_args.kwargs["current_page"] == expected
    c.cursor.skip.assert_called_once_with((expected - 1) * 20)


def test_records_render_cash_amounts_and_escape_notes(cash_log):
    c = cash_log
    c.entries.count_documents.return_value = 1
    c.cursor.skip.return_value.limit.return_value = [dict(
        c.valid, agent_name="Agent Kofi", amount=23.45, recorded_by_name="Executive Ama",
        note="<script>alert('test')</script>")]
    html = c.client.get(c.path).get_data(as_text=True)
    assert "Shortage</span>" in html
    assert "23.45" in html
    assert "Agent Kofi" in html
    assert "&lt;script&gt;" in html
    assert "<script>alert" not in html


def test_no_records_and_no_agents_render_empty_state(cash_log):
    c = cash_log
    c.users.find.return_value.sort.return_value = []
    html = c.client.get(c.path).get_data(as_text=True)
    assert "No cash records" in html
    assert "No agents are available" in html
    assert "Page 1 of 1" in html


def test_history_can_still_be_filtered_for_deleted_agent(cash_log):
    c = cash_log
    removed_agent = str(ObjectId())
    html = c.client.get(c.path, query_string={"agent_id": removed_agent}).get_data(as_text=True)
    assert removed_agent + " (historical agent)" in html
    c.entries.find.assert_called_once_with({"agent_id": removed_agent})
