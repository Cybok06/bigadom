from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal
import secrets

from bson import ObjectId
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session, url_for
from flask_login import current_user, login_required
from pymongo import ReturnDocument

from db import db
from services.activation_groups import get_accessible_agent_ids
from services.loans import (GRACE_DAYS, as_float, balance, display,
                            due_penalty_cycles, money, mongo_money, repayment_dates,
                            status_for, terms)

loans_bp = Blueprint("loans", __name__, url_prefix="/loans")
loans_col, payments_col, penalties_col = db.loans, db.payments, db.loan_penalties
customers_col, users_col = db.customers, db.users


@loans_bp.record_once
def _ensure_indexes(_state):
    try:
        loans_col.create_index("loan_number", unique=True)
        loans_col.create_index([("manager_id", 1), ("status", 1), ("created_at", -1)])
        loans_col.create_index([("agent_id", 1), ("status", 1), ("created_at", -1)])
        loans_col.create_index([("customer_id", 1), ("created_at", -1)])
        payments_col.create_index([("loan_id", 1), ("created_at", -1)])
        penalties_col.create_index("idempotency_key", unique=True)
    except Exception:
        # Startup must remain available if the database is temporarily offline.
        pass


def _role():
    if session.get("executive_id"):
        return "executive"
    if session.get("manager_id"):
        return "manager"
    if current_user.is_authenticated and current_user.role == "agent":
        return "agent"
    return None


def _manager_id():
    return str(session.get("manager_id") or "")


def _owned_customer(customer_id):
    try:
        oid = ObjectId(customer_id)
    except Exception:
        return None
    if _role() == "agent":
        return customers_col.find_one({"_id": oid, "agent_id": {"$in": get_accessible_agent_ids(current_user.id)}})
    if _role() == "manager":
        mid = _manager_id()
        return customers_col.find_one({"_id": oid, "manager_id": {"$in": [mid, ObjectId(mid)]}})
    return None


def _owned_loan(loan_id):
    try:
        query = {"_id": ObjectId(loan_id)}
    except Exception:
        return None
    if _role() == "agent":
        query["agent_id"] = {"$in": get_accessible_agent_ids(current_user.id)}
    elif _role() == "manager":
        mid = _manager_id()
        query["manager_id"] = {"$in": [mid, ObjectId(mid)]}
    elif _role() == "executive":
        pass
    else:
        return None
    return loans_col.find_one(query)


def _detail_rows(prefix):
    names, values, rows = request.form.getlist(f"{prefix}_name[]"), request.form.getlist(f"{prefix}_value[]"), []
    for name, value in zip(names, values):
        name, value = name.strip(), value.strip()
        if bool(name) != bool(value):
            raise ValueError("Every added detail requires both a name and a value.")
        if name:
            rows.append({"name": name[:100], "value": value[:500]})
    return rows


def sync_loan(loan):
    if loan.get("status") not in {"active", "grace_period", "overdue", "approved"}:
        return loan
    cycles = due_penalty_cycles(loan)
    per_cycle = money(loan.get("processing_fee"))
    for offset in range(cycles):
        cycle = int(loan.get("penalty_cycles_applied", 0)) + 1
        key = f"{loan['_id']}:{cycle}"
        if penalties_col.find_one({"idempotency_key": key}):
            continue
        before = balance(loan)
        penalty = {"loan_id": loan["_id"], "customer_id": loan["customer_id"], "cycle_number": cycle,
                   "idempotency_key": key, "amount": mongo_money(per_cycle), "applied_at": datetime.utcnow(),
                   "reason": "Outstanding balance after 14-day overdue cycle",
                   "balance_before": mongo_money(before), "balance_after": mongo_money(before + per_cycle)}
        penalties_col.insert_one(penalty)
        loan["penalty_cycles_applied"] = cycle
        loan["total_penalties"] = mongo_money(money(loan.get("total_penalties")) + per_cycle)
        loan["current_balance"] = mongo_money(before + per_cycle)
        loan["next_penalty_date"] = loan["next_penalty_date"] + timedelta(days=GRACE_DAYS)
    loan["status"] = status_for(loan)
    loan["updated_at"] = datetime.utcnow()
    loans_col.update_one({"_id": loan["_id"]}, {"$set": {k: loan[k] for k in ("penalty_cycles_applied", "total_penalties", "current_balance", "next_penalty_date", "status", "updated_at")}})
    return loan


@loans_bp.get("")
def index():
    role = _role()
    if not role:
        return redirect(url_for("login.login"))
    if role == "executive":
        return redirect(url_for("loans.executive_index"))
    query = {}
    if role == "agent":
        query["agent_id"] = {"$in": get_accessible_agent_ids(current_user.id)}
    else:
        mid = _manager_id(); query["manager_id"] = {"$in": [mid, ObjectId(mid)]}
    status = request.args.get("status", "").strip()
    if status:
        query["status"] = status
    docs = []
    for loan in loans_col.find(query).sort("created_at", -1):
        loan = sync_loan(loan)
        customer = customers_col.find_one({"_id": loan["customer_id"]}, {"name": 1, "phone_number": 1, "image_url": 1}) or {}
        agent = users_col.find_one({"_id": ObjectId(str(loan["agent_id"]))}, {"name": 1}) or {}
        row = display(loan); row["customer"] = customer; row["agent_name"] = agent.get("name", "Unknown")
        text = request.args.get("q", "").lower().strip()
        if not text or text in f"{customer.get('name','')} {customer.get('phone_number','')} {row.get('loan_number','')}".lower():
            docs.append(row)
    counts = {s: loans_col.count_documents({**query, "status": s}) for s in ("pending", "active", "grace_period", "overdue", "settled")}
    return render_template("loans/index.html", loans=docs, counts=counts, role=role)


@loans_bp.get("/pending-count")
def pending_count():
    if _role() != "manager":
        return jsonify({"ok": False, "error": "Forbidden"}), 403

    manager_id = _manager_id()
    if not manager_id:
        return jsonify({"ok": True, "pending": 0})
    manager_ids = [manager_id]
    if ObjectId.is_valid(manager_id):
        manager_ids.append(ObjectId(manager_id))
    count = loans_col.count_documents({"manager_id": {"$in": manager_ids}, "status": "pending"})
    return jsonify({"ok": True, "pending": count})


@loans_bp.get("/executive")
def executive_index():
    if _role() != "executive":
        abort(403)
    manager_id = request.args.get("manager_id", "").strip()
    agent_id = request.args.get("agent_id", "").strip()
    branch = request.args.get("branch", "").strip()
    status = request.args.get("status", "").strip()
    search = request.args.get("q", "").strip().lower()

    managers = list(users_col.find({"role": "manager"}, {"name": 1, "branch": 1}).sort("name", 1))
    agent_query = {"role": "agent"}
    if manager_id:
        try: agent_query["manager_id"] = {"$in": [manager_id, ObjectId(manager_id)]}
        except Exception: agent_query["manager_id"] = manager_id
    if branch:
        agent_query["branch"] = branch
    agents = list(users_col.find(agent_query, {"name": 1, "branch": 1, "manager_id": 1}).sort("name", 1))
    allowed_agents = [str(a["_id"]) for a in agents]
    query = {}
    if manager_id:
        try: query["manager_id"] = {"$in": [manager_id, ObjectId(manager_id)]}
        except Exception: query["manager_id"] = manager_id
    if agent_id:
        query["agent_id"] = agent_id
    elif branch:
        query["agent_id"] = {"$in": allowed_agents}
    if status:
        query["status"] = status

    manager_map = {str(m["_id"]): m.get("name", "Unknown") for m in managers}
    all_agents = list(users_col.find({"role": "agent"}, {"name": 1, "branch": 1, "manager_id": 1}))
    agent_map = {str(a["_id"]): a for a in all_agents}
    rows = []
    for loan in loans_col.find(query).sort("created_at", -1):
        loan = sync_loan(loan)
        customer = customers_col.find_one({"_id": loan["customer_id"]}, {"name": 1, "phone_number": 1, "image_url": 1}) or {}
        agent = agent_map.get(str(loan.get("agent_id")), {})
        haystack = f"{customer.get('name','')} {customer.get('phone_number','')} {loan.get('loan_number','')} {agent.get('name','')}".lower()
        if search and search not in haystack:
            continue
        row = display(loan); row["customer"] = customer; row["agent_name"] = agent.get("name", "Unknown")
        row["branch"] = agent.get("branch", "—"); row["manager_name"] = manager_map.get(str(loan.get("manager_id")), "Unknown")
        rows.append(row)
    totals = {"count": len(rows), "principal": sum(r["original_amount"] for r in rows),
              "paid": sum(r["amount_paid"] for r in rows), "balance": sum(r["current_balance"] for r in rows)}
    branches = sorted({str(a.get("branch")) for a in all_agents if a.get("branch")})
    return render_template("loans/executive.html", loans=rows, totals=totals, managers=managers,
                           agents=agents, branches=branches)


@loans_bp.route("/apply", methods=["GET", "POST"])
@login_required
def apply():
    if _role() != "agent": abort(403)
    ids = get_accessible_agent_ids(current_user.id)
    customers = list(customers_col.find({"agent_id": {"$in": ids}}, {"name": 1, "phone_number": 1}).sort("name", 1))
    if request.method == "POST":
        customer = _owned_customer(request.form.get("customer_id"))
        if not customer: abort(403)
        try:
            calc = terms(request.form.get("amount")); guarantor = _detail_rows("guarantor"); extra = _detail_rows("customer")
        except (ValueError, ArithmeticError) as exc:
            flash(str(exc), "danger"); return redirect(url_for("loans.apply"))
        agent = users_col.find_one({"_id": ObjectId(current_user.id)}) or {}
        manager = customer.get("manager_id") or agent.get("manager_id")
        if not manager:
            flash("Customer agent is not linked to a manager.", "danger"); return redirect(url_for("loans.apply"))
        now = datetime.utcnow()
        doc = {"loan_number": f"LNA-{now:%Y%m%d}-{secrets.token_hex(2).upper()}", "loan_name": f"Loan Application – {now:%d %b %Y}",
               "customer_id": customer["_id"], "agent_id": str(customer.get("agent_id") or current_user.id), "manager_id": manager,
               **{k: mongo_money(v) for k, v in calc.items() if k != "penalty_per_cycle"}, "processing_fee_rate": mongo_money("0.07"),
               "repayment_days": 65, "amount_paid": mongo_money(0), "total_penalties": mongo_money(0),
               "current_balance": mongo_money(calc["expected_total_repayment"]), "penalty_cycles_applied": 0, "status": "pending",
               "customer_extra_details": extra, "guarantor_details": guarantor, "application_date": now,
               "created_at": now, "updated_at": now, "created_by": ObjectId(current_user.id)}
        loans_col.insert_one(doc)
        flash("Loan application submitted for manager approval.", "success"); return redirect(url_for("loans.index"))
    return render_template("loans/apply.html", customers=customers)


@loans_bp.post("/<loan_id>/decision")
def decision(loan_id):
    if _role() != "manager": abort(403)
    loan = _owned_loan(loan_id)
    if not loan or loan.get("status") != "pending": abort(404)
    action = request.form.get("action")
    now = datetime.utcnow()
    if action == "reject":
        reason = request.form.get("reason", "").strip()
        if not reason: flash("A rejection reason is required.", "danger"); return redirect(url_for("loans.detail", loan_id=loan_id))
        loans_col.update_one({"_id": loan["_id"], "status": "pending"}, {"$set": {"status": "rejected", "rejection_reason": reason, "rejected_at": now, "rejected_by": ObjectId(_manager_id()), "updated_at": now}})
    elif action == "approve":
        approval_date = date.today()
        calculated_dates = repayment_dates(approval_date)
        # BSON supports datetime but not Python's date type. Store all loan
        # milestones at midnight so they remain sortable and queryable.
        mongo_dates = {
            key: datetime.combine(value, datetime.min.time())
            for key, value in calculated_dates.items()
        }
        loans_col.update_one(
            {"_id": loan["_id"], "status": "pending"},
            {"$set": {
                "status": "active",
                "loan_name": f"Loan - {approval_date:%d %b %Y}",
                "approved_at": now,
                "approved_by": ObjectId(_manager_id()),
                "disbursement_date": datetime.combine(approval_date, datetime.min.time()),
                **mongo_dates,
                "updated_at": now,
            }},
        )
    else: abort(400)
    flash("Loan approved successfully." if action == "approve" else "Loan rejected successfully.", "success")
    return redirect(url_for("loans.detail", loan_id=loan_id))


@loans_bp.get("/<loan_id>")
def detail(loan_id):
    loan = _owned_loan(loan_id)
    if not loan: abort(404)
    loan = sync_loan(loan)
    customer = customers_col.find_one({"_id": loan["customer_id"]}) or {}
    payments = list(payments_col.find({"loan_id": loan["_id"], "payment_type": "LOAN"}).sort("created_at", -1))
    penalties = list(penalties_col.find({"loan_id": loan["_id"]}).sort("cycle_number", 1))
    payment_days = {}
    for payment in payments:
        raw_date = payment.get("date")
        key = raw_date.strftime("%Y-%m-%d") if isinstance(raw_date, datetime) else str(raw_date or "")[:10]
        if key:
            payment_days[key] = round(payment_days.get(key, 0) + float(payment.get("amount") or 0), 2)
    schedule = []
    start, end = loan.get("repayment_start_date"), loan.get("expected_completion_date")
    if isinstance(start, datetime): start = start.date()
    if isinstance(end, datetime): end = end.date()
    if start and end:
        cursor = start
        while cursor <= end:
            if cursor.weekday() != 6:
                schedule.append(cursor.isoformat())
            cursor += timedelta(days=1)
    return render_template("loans/detail.html", loan=display(loan), customer=customer, payments=payments,
                           penalties=[display({**p, "expected_total_repayment": 0, "amount_paid": 0, "total_penalties": p.get("amount"), "current_balance": p.get("balance_after")}) for p in penalties],
                           role=_role(), repayment_calendar={"schedule": schedule, "payments": payment_days,
                           "daily": as_float(loan.get("daily_repayment")), "today": date.today().isoformat()})


def active_loan_for_customer(customer_id, loan_id=None):
    query = {"customer_id": customer_id, "status": {"$in": ["active", "approved", "grace_period", "overdue"]}}
    if loan_id is not None:
        try:
            query["_id"] = ObjectId(str(loan_id))
        except Exception:
            return None
    loan = loans_col.find_one(query, sort=[("created_at", -1)])
    return sync_loan(loan) if loan else None
