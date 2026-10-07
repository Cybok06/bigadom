from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from bson import ObjectId
from flask import Blueprint, flash, redirect, render_template, request, url_for

from db import db
from login import get_current_identity, role_required


executive_cash_variances_bp = Blueprint(
    "executive_cash_variances", __name__, url_prefix="/executive/shortage-and-surplus"
)
users_col = db["users"]
cash_variances_col = db["agent_cash_variances"]
PER_PAGE = 20

try:
    cash_variances_col.create_index([("created_at", -1), ("_id", -1)])
    cash_variances_col.create_index([("agent_id", 1), ("created_at", -1), ("_id", -1)])
except Exception:
    pass


def _clean_entry(form):
    agent_id = str(form.get("agent_id") or "").strip()
    if not ObjectId.is_valid(agent_id):
        raise ValueError("Select a valid agent.")
    entry_type = str(form.get("entry_type") or "").strip().lower()
    if entry_type not in {"shortage", "surplus"}:
        raise ValueError("Select Shortage or Surplus.")
    try:
        amount = Decimal(str(form.get("amount") or ""))
    except InvalidOperation:
        raise ValueError("Enter a valid cash amount.") from None
    if not amount.is_finite() or amount <= 0 or amount > Decimal("999999999.99"):
        raise ValueError("Amount must be between GHS 0.01 and GHS 999,999,999.99.")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("Amount must have no more than two decimal places.")
    date_raw = str(form.get("entry_date") or "").strip()
    try:
        entry_date = datetime.strptime(date_raw, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("Select a valid entry date.") from None
    note = str(form.get("note") or "").strip()
    if len(note) > 1000:
        raise ValueError("Notes must be 1,000 characters or fewer.")
    return dict(agent_id=agent_id, entry_type=entry_type, amount=float(amount),
                entry_date=entry_date, note=note)


@executive_cash_variances_bp.route("/", methods=["GET", "POST"])
@role_required("executive")
def page():
    identity = get_current_identity()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    form_values = dict(agent_id="", entry_type="", amount="", entry_date=today, note="")
    error = None
    if request.method == "POST":
        form_values.update({key: request.form.get(key, "") for key in form_values})
        try:
            entry = _clean_entry(request.form)
            agent = users_col.find_one({"_id": ObjectId(entry["agent_id"]), "role": "agent"})
            if not agent:
                raise ValueError("Selected agent was not found. Select an existing agent.")
        except ValueError as exc:
            error = str(exc)
        else:
            cash_variances_col.insert_one({
                **entry,
                "agent_name": agent.get("name") or agent.get("username") or "Unnamed agent",
                "recorded_by": str(identity["user_id"]),
                "recorded_by_name": identity.get("name") or "Executive",
                "created_at": datetime.now(timezone.utc),
            })
            flash(f"{entry['entry_type'].title()} cash entry recorded successfully.", "success")
            return redirect(url_for("executive_cash_variances.page", agent_id=entry["agent_id"]))

    agents = list(users_col.find({"role": "agent"}, {"name": 1, "username": 1, "branch": 1}).sort("name", 1))
    for agent in agents:
        agent["id_str"] = str(agent["_id"])
    agent_filter = (request.args.get("agent_id") or "").strip()
    query = {"agent_id": agent_filter} if agent_filter else {}
    total_records = cash_variances_col.count_documents(query)
    total_pages = max(1, (total_records + PER_PAGE - 1) // PER_PAGE)
    try:
        current_page = int(request.args.get("page", "1"))
    except (ValueError, TypeError):
        current_page = 1
    current_page = max(1, min(current_page, total_pages))
    records = list(cash_variances_col.find(query).sort([("created_at", -1), ("_id", -1)])
                   .skip((current_page - 1) * PER_PAGE).limit(PER_PAGE))
    # Preserve access to historical entries if an agent has since been removed.
    filter_agent_name = next((agent.get("name") or agent.get("username") for agent in agents
                              if agent["id_str"] == agent_filter), agent_filter)
    return render_template(
        "executive/shortage_and_surplus.html", agents=agents, records=records,
        agent_filter=agent_filter, filter_agent_name=filter_agent_name,
        form_values=form_values, error=error, total_records=total_records,
        current_page=current_page, total_pages=total_pages,
        page_numbers=range(max(1, current_page - 2), min(total_pages, current_page + 2) + 1),
    ), 400 if error else 200
