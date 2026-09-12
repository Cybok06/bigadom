from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, session, jsonify, flash
from bson import ObjectId
from datetime import datetime, date, time, timezone, timedelta
from typing import Any

from db import db

executive_target_bp = Blueprint("executive_target", __name__)

executive_targets_col = db["executive_targets"]
users_col = db["users"]
payments_col = db["payments"]
customers_col = db["customers"]


def _ensure_indexes():
    try:
        executive_targets_col.create_index([("manager_id", 1), ("period", 1), ("is_active", 1)])
        executive_targets_col.create_index([("created_at", -1)])
        payments_col.create_index([("agent_id", 1), ("date", 1), ("payment_type", 1)])
        customers_col.create_index([("agent_id", 1), ("date_registered", 1)])
        customers_col.create_index([("purchases.purchase_date", 1)])
        customers_col.create_index([("purchases.agent_id", 1)])
    except Exception:
        pass


_ensure_indexes()


def _require_executive() -> bool:
    role = (session.get("role") or "").lower()
    return bool(session.get("executive_id") or role == "executive")


def _utc_today_date() -> date:
    return datetime.now(timezone.utc).date()


def _period_window(period: str) -> dict[str, Any]:
    period = (period or "").lower()
    today = _utc_today_date()

    if period == "monthly":
        start = today.replace(day=1)
        end = (start.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)
    elif period == "yearly":
        start = today.replace(month=1, day=1)
        end = today.replace(month=12, day=31)
    else:
        start = today
        end = today

    start_str = start.isoformat()
    end_str = end.isoformat()
    start_dt = datetime.combine(start, time.min)
    end_dt = datetime.combine(end, time.max)

    return {
        "period": period or "daily",
        "start_date": start,
        "end_date": end,
        "start_str": start_str,
        "end_str": end_str,
        "start_dt": start_dt,
        "end_dt": end_dt,
    }


def _agent_id_sets(agent_ids: list[str]) -> tuple[list[str], list[ObjectId]]:
    oid_list: list[ObjectId] = []
    for aid in agent_ids:
        try:
            oid_list.append(ObjectId(aid))
        except Exception:
            continue
    return agent_ids, oid_list


def _agent_match(agent_ids: list[str], agent_oids: list[ObjectId]) -> dict[str, Any]:
    ors = []
    if agent_ids:
        ors.append({"agent_id": {"$in": agent_ids}})
    if agent_oids:
        ors.append({"agent_id": {"$in": agent_oids}})
    return {"$or": ors} if ors else {"agent_id": {"$in": []}}


def _split_equal_int(total: int, count: int) -> list[int]:
    if count <= 0:
        return []
    base = total // count
    rem = total % count
    return [base + (1 if i < rem else 0) for i in range(count)]


def _split_equal_cash(total: float, count: int) -> list[float]:
    if count <= 0:
        return []
    base = round(total / count, 2)
    parts = [base for _ in range(count)]
    diff = round(total - sum(parts), 2)
    if parts:
        parts[0] = round(parts[0] + diff, 2)
    return parts


def _get_manager_agents(manager_oid: ObjectId) -> list[dict[str, str]]:
    cursor = users_col.find(
        {"role": "agent", "manager_id": manager_oid},
        {"_id": 1, "name": 1},
    )
    agents: list[dict[str, str]] = []
    for doc in cursor:
        agents.append({"id": str(doc.get("_id")), "name": doc.get("name") or "Agent"})
    return agents


def _build_allocations(
    agents: list[dict[str, str]],
    product_target: int,
    cash_target: float,
    customer_target: int,
) -> list[dict[str, Any]]:
    count = len(agents)
    prod_parts = _split_equal_int(product_target, count)
    cash_parts = _split_equal_cash(cash_target, count)
    cust_parts = _split_equal_int(customer_target, count)

    allocations: list[dict[str, Any]] = []
    for idx, agent in enumerate(agents):
        allocations.append({
            "agent_id": agent["id"],
            "agent_name": agent["name"],
            "product_quota": prod_parts[idx] if idx < len(prod_parts) else 0,
            "cash_quota": cash_parts[idx] if idx < len(cash_parts) else 0.0,
            "customer_quota": cust_parts[idx] if idx < len(cust_parts) else 0,
        })
    return allocations


def _aggregate_payments_by_agent(agent_ids: list[str], agent_oids: list[ObjectId], start_str: str, end_str: str):
    match = {
        "payment_type": {"$ne": "WITHDRAWAL"},
        "date": {"$gte": start_str, "$lte": end_str},
    }
    match.update(_agent_match(agent_ids, agent_oids))

    pipe = [
        {"$match": match},
        {"$group": {
            "_id": {"$toString": "$agent_id"},
            "total": {"$sum": {"$toDouble": {"$ifNull": ["$amount", 0]}}},
        }},
    ]
    rows = payments_col.aggregate(pipe)
    return {str(r["_id"]): float(r.get("total") or 0.0) for r in rows}


def _aggregate_products_by_agent(agent_ids: list[str], agent_oids: list[ObjectId], start_str: str, end_str: str):
    match = _agent_match(agent_ids, agent_oids)
    match["purchases.purchase_date"] = {"$gte": start_str, "$lte": end_str}

    pipe = [
        {"$match": match},
        {"$unwind": "$purchases"},
        {"$match": {"purchases.purchase_date": {"$gte": start_str, "$lte": end_str}}},
        {"$group": {
            "_id": {"$toString": "$agent_id"},
            "units": {"$sum": {"$ifNull": ["$purchases.product.quantity", 1]}},
        }},
    ]
    rows = customers_col.aggregate(pipe)
    return {str(r["_id"]): int(r.get("units") or 0) for r in rows}


def _aggregate_customers_by_agent(agent_ids: list[str], agent_oids: list[ObjectId], start_dt: datetime, end_dt: datetime):
    match = _agent_match(agent_ids, agent_oids)
    match["date_registered"] = {"$gte": start_dt, "$lte": end_dt}

    pipe = [
        {"$match": match},
        {"$group": {"_id": {"$toString": "$agent_id"}, "count": {"$sum": 1}}},
    ]
    rows = customers_col.aggregate(pipe)
    return {str(r["_id"]): int(r.get("count") or 0) for r in rows}


def _percent(part: float, whole: float) -> float:
    if not whole:
        return 0.0
    return round((part / whole) * 100, 2)


def _overall_pct(items: list[tuple[float, float]]) -> float:
    parts = 0
    total = 0.0
    for achieved, target in items:
        if target:
            parts += 1
            total += _percent(achieved, target)
    return round((total / parts) if parts else 0.0, 2)


def _get_active_target(manager_oid: ObjectId, period: str) -> dict[str, Any] | None:
    return executive_targets_col.find_one({
        "manager_id": manager_oid,
        "period": period,
        "is_active": True,
    })


def compute_manager_target_progress(manager_oid: ObjectId, period: str) -> dict[str, Any] | None:
    target = _get_active_target(manager_oid, period)
    if not target:
        return None

    agents = _get_manager_agents(manager_oid)
    agent_ids = [a["id"] for a in agents]
    agent_ids_str, agent_ids_oid = _agent_id_sets(agent_ids)

    win = _period_window(period)

    payments_by_agent = _aggregate_payments_by_agent(agent_ids_str, agent_ids_oid, win["start_str"], win["end_str"])
    products_by_agent = _aggregate_products_by_agent(agent_ids_str, agent_ids_oid, win["start_str"], win["end_str"])
    customers_by_agent = _aggregate_customers_by_agent(agent_ids_str, agent_ids_oid, win["start_dt"], win["end_dt"])

    allocations = target.get("agent_allocations") or []
    per_agent = []
    for alloc in allocations:
        aid = str(alloc.get("agent_id") or "")
        product_quota = int(alloc.get("product_quota") or 0)
        cash_quota = float(alloc.get("cash_quota") or 0.0)
        customer_quota = int(alloc.get("customer_quota") or 0)
        product_achieved = int(products_by_agent.get(aid, 0))
        cash_achieved = round(float(payments_by_agent.get(aid, 0.0)), 2)
        customer_achieved = int(customers_by_agent.get(aid, 0))
        per_agent.append({
            "agent_id": aid,
            "agent_name": alloc.get("agent_name") or "Agent",
            "product_quota": product_quota,
            "cash_quota": cash_quota,
            "customer_quota": customer_quota,
            "product_achieved": product_achieved,
            "cash_achieved": cash_achieved,
            "customer_achieved": customer_achieved,
            "product_pct": _percent(product_achieved, product_quota),
            "cash_pct": _percent(cash_achieved, cash_quota),
            "customer_pct": _percent(customer_achieved, customer_quota),
            "overall_pct": _overall_pct([
                (product_achieved, product_quota),
                (cash_achieved, cash_quota),
                (customer_achieved, customer_quota),
            ]),
        })

    product_target = int(target.get("product_target") or 0)
    cash_target = float(target.get("cash_target") or 0.0)
    customer_target = int(target.get("customer_target") or 0)

    product_achieved = sum(a["product_achieved"] for a in per_agent)
    cash_achieved = round(sum(a["cash_achieved"] for a in per_agent), 2)
    customer_achieved = sum(a["customer_achieved"] for a in per_agent)

    summary = {
        "period": period,
        "start_date": win["start_str"],
        "end_date": win["end_str"],
        "targets": {
            "product_target": product_target,
            "cash_target": cash_target,
            "customer_target": customer_target,
        },
        "achieved": {
            "product_achieved": product_achieved,
            "cash_achieved": cash_achieved,
            "customer_achieved": customer_achieved,
        },
        "percents": {
            "product_pct": _percent(product_achieved, product_target),
            "cash_pct": _percent(cash_achieved, cash_target),
            "customer_pct": _percent(customer_achieved, customer_target),
            "overall_pct": _overall_pct([
                (product_achieved, product_target),
                (cash_achieved, cash_target),
                (customer_achieved, customer_target),
            ]),
        },
        "agents": per_agent,
        "target_id": str(target.get("_id")),
        "is_active": bool(target.get("is_active")),
    }
    return summary


def compute_agent_target_progress(agent_id: str, manager_oid: ObjectId, period: str) -> dict[str, Any] | None:
    target = _get_active_target(manager_oid, period)
    if not target:
        return None

    win = _period_window(period)
    agent_ids_str, agent_ids_oid = _agent_id_sets([agent_id])

    payments_by_agent = _aggregate_payments_by_agent(agent_ids_str, agent_ids_oid, win["start_str"], win["end_str"])
    products_by_agent = _aggregate_products_by_agent(agent_ids_str, agent_ids_oid, win["start_str"], win["end_str"])
    customers_by_agent = _aggregate_customers_by_agent(agent_ids_str, agent_ids_oid, win["start_dt"], win["end_dt"])

    alloc = None
    for row in (target.get("agent_allocations") or []):
        if str(row.get("agent_id")) == str(agent_id):
            alloc = row
            break

    if not alloc:
        return None

    product_quota = int(alloc.get("product_quota") or 0)
    cash_quota = float(alloc.get("cash_quota") or 0.0)
    customer_quota = int(alloc.get("customer_quota") or 0)

    product_achieved = int(products_by_agent.get(agent_id, 0))
    cash_achieved = round(float(payments_by_agent.get(agent_id, 0.0)), 2)
    customer_achieved = int(customers_by_agent.get(agent_id, 0))

    return {
        "period": period,
        "start_date": win["start_str"],
        "end_date": win["end_str"],
        "quota": {
            "product_quota": product_quota,
            "cash_quota": cash_quota,
            "customer_quota": customer_quota,
        },
        "achieved": {
            "product_achieved": product_achieved,
            "cash_achieved": cash_achieved,
            "customer_achieved": customer_achieved,
        },
        "percents": {
            "product_pct": _percent(product_achieved, product_quota),
            "cash_pct": _percent(cash_achieved, cash_quota),
            "customer_pct": _percent(customer_achieved, customer_quota),
            "overall_pct": _overall_pct([
                (product_achieved, product_quota),
                (cash_achieved, cash_quota),
                (customer_achieved, customer_quota),
            ]),
        },
        "agent_name": alloc.get("agent_name") or "Agent",
        "target_id": str(target.get("_id")),
        "is_active": bool(target.get("is_active")),
    }


@executive_target_bp.get("/executive/targets")
def executive_targets_page():
    if not _require_executive():
        flash("Unauthorized", "danger")
        return redirect(url_for("login.login"))

    managers = list(users_col.find({"role": "manager"}, {"_id": 1, "name": 1}).sort("name", 1))
    manager_map = {str(m["_id"]): (m.get("name") or "Manager") for m in managers}

    targets = list(executive_targets_col.find({}).sort("created_at", -1).limit(200))
    for t in targets:
        t["id"] = str(t.get("_id"))
        mid = str(t.get("manager_id") or "")
        t["manager_name"] = manager_map.get(mid, "Manager")
        t["agent_count"] = len(t.get("agent_allocations") or [])
        created_at = t.get("created_at")
        if isinstance(created_at, datetime):
            t["created_at_str"] = created_at.strftime("%Y-%m-%d %H:%M")
        else:
            t["created_at_str"] = ""

    return render_template(
        "executive/targets.html",
        managers=managers,
        targets=targets,
    )


@executive_target_bp.post("/executive/targets/create")
def executive_targets_create():
    if not _require_executive():
        return jsonify(ok=False, message="Unauthorized"), 401

    manager_id = (request.form.get("manager_id") or "").strip()
    period = (request.form.get("period") or "daily").strip().lower()
    try:
        manager_oid = ObjectId(manager_id)
    except Exception:
        return jsonify(ok=False, message="Invalid manager id"), 400

    manager = users_col.find_one({"_id": manager_oid, "role": "manager"})
    if not manager:
        return jsonify(ok=False, message="Manager not found"), 404

    def _to_int(v):
        try:
            return int(float(v))
        except Exception:
            return 0

    def _to_float(v):
        try:
            return float(v)
        except Exception:
            return 0.0

    product_target = _to_int(request.form.get("product_target"))
    cash_target = _to_float(request.form.get("cash_target"))
    customer_target = _to_int(request.form.get("customer_target"))

    agents = _get_manager_agents(manager_oid)
    if not agents:
        return jsonify(ok=False, message="No agents found for this manager."), 400

    allocations = _build_allocations(agents, product_target, cash_target, customer_target)
    now = datetime.utcnow()

    executive_id = str(session.get("executive_id") or session.get("admin_id") or "")

    executive_targets_col.update_many(
        {"manager_id": manager_oid, "period": period, "is_active": True},
        {"$set": {"is_active": False, "updated_at": now}},
    )

    doc = {
        "manager_id": manager_oid,
        "period": period,
        "product_target": product_target,
        "cash_target": cash_target,
        "customer_target": customer_target,
        "distribution_mode": "equal",
        "agent_allocations": allocations,
        "created_by": executive_id,
        "created_at": now,
        "updated_at": now,
        "is_active": True,
    }

    executive_targets_col.insert_one(doc)
    return redirect(url_for("executive_target.executive_targets_page"))


@executive_target_bp.post("/executive/targets/rebuild/<target_id>")
def executive_targets_rebuild(target_id: str):
    if not _require_executive():
        return jsonify(ok=False, message="Unauthorized"), 401

    try:
        target_oid = ObjectId(target_id)
    except Exception:
        return jsonify(ok=False, message="Invalid target id"), 400

    target = executive_targets_col.find_one({"_id": target_oid})
    if not target:
        return jsonify(ok=False, message="Target not found"), 404

    manager_oid = target.get("manager_id")
    if not isinstance(manager_oid, ObjectId):
        return jsonify(ok=False, message="Invalid manager id in target"), 400

    agents = _get_manager_agents(manager_oid)
    allocations = _build_allocations(
        agents,
        int(target.get("product_target") or 0),
        float(target.get("cash_target") or 0.0),
        int(target.get("customer_target") or 0),
    )

    executive_targets_col.update_one(
        {"_id": target_oid},
        {"$set": {"agent_allocations": allocations, "updated_at": datetime.utcnow()}},
    )
    return redirect(url_for("executive_target.executive_targets_page"))


@executive_target_bp.post("/executive/targets/toggle/<target_id>")
def executive_targets_toggle(target_id: str):
    if not _require_executive():
        return jsonify(ok=False, message="Unauthorized"), 401

    try:
        target_oid = ObjectId(target_id)
    except Exception:
        return jsonify(ok=False, message="Invalid target id"), 400

    target = executive_targets_col.find_one({"_id": target_oid})
    if not target:
        return jsonify(ok=False, message="Target not found"), 404

    is_active = bool(target.get("is_active"))
    manager_oid = target.get("manager_id")
    period = target.get("period")

    if not is_active:
        executive_targets_col.update_many(
            {"manager_id": manager_oid, "period": period, "is_active": True},
            {"$set": {"is_active": False, "updated_at": datetime.utcnow()}},
        )

    executive_targets_col.update_one(
        {"_id": target_oid},
        {"$set": {"is_active": (not is_active), "updated_at": datetime.utcnow()}},
    )
    return redirect(url_for("executive_target.executive_targets_page"))


@executive_target_bp.get("/executive/targets/progress")
def executive_targets_progress():
    if not _require_executive():
        return jsonify(ok=False, message="Unauthorized"), 401

    manager_id = (request.args.get("manager_id") or "").strip()
    period = (request.args.get("period") or "daily").strip().lower()

    try:
        manager_oid = ObjectId(manager_id)
    except Exception:
        return jsonify(ok=False, message="Invalid manager id"), 400

    summary = compute_manager_target_progress(manager_oid, period)
    if not summary:
        return jsonify(ok=False, message="No active target found.")

    return jsonify(ok=True, summary=summary)
