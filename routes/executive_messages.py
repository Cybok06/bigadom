from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from flask import Blueprint, jsonify, render_template, request

from db import db
from login import get_current_identity, role_required
from services.payment_messages import (
    PLACEHOLDERS,
    TEMPLATE_TYPES,
    get_payment_message_settings,
    save_payment_message_settings,
)
from services.sms_gateway import gateway_status, normalize_ghana_phone, send_sms_detailed


executive_messages_bp = Blueprint("executive_messages", __name__, url_prefix="/executive/messages")
delivery_logs_col = db["message_delivery_logs"]
customers_col = db["customers"]
users_col = db["users"]
payments_col = db["payments"]

AUDIENCES = {"agents": "Agents", "managers": "Managers", "customers": "Customers"}
CUSTOMER_FILTERS = {
    "all": "All customers",
    "registered_this_month": "Registered this month",
    "registered_last_month": "Registered last month",
    "payment_last_7_days": "Payment in the last 7 days",
    "payment_last_week": "Last payment was last week",
}
_broadcast_token_re = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


@executive_messages_bp.record_once
def _ensure_delivery_log_indexes(_state):
    try:
        delivery_logs_col.create_index([("created_at", -1)])
        delivery_logs_col.create_index([("audience", 1), ("created_at", -1)])
    except Exception:
        pass


def _as_datetime(value):
    if isinstance(value, datetime):
        return value
    raw = str(value or "").strip()[:19]
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def _customer_payment_ids(start: datetime, end: datetime) -> set:
    date_start, date_end = start.strftime("%Y-%m-%d"), end.strftime("%Y-%m-%d")
    query = {
        "payment_type": {"$ne": "WITHDRAWAL"},
        "$or": [
            {"created_at": {"$gte": start, "$lt": end}},
            {"date": {"$gte": date_start, "$lt": date_end}},
        ],
    }
    return set(payments_col.distinct("customer_id", query))


def _resolve_recipients(audience: str, customer_filter: str = "all") -> list[dict]:
    if audience not in AUDIENCES:
        raise ValueError("Select Agents, Managers, or Customers.")

    if audience in {"agents", "managers"}:
        role = audience[:-1]
        docs = users_col.find(
            {"role": role, "status": {"$ne": "disabled"}},
            {"name": 1, "username": 1, "phone": 1, "phone_number": 1},
        )
    else:
        if customer_filter not in CUSTOMER_FILTERS:
            raise ValueError("Select a valid customer group.")
        docs = list(customers_col.find({}, {"name": 1, "phone_number": 1, "date_registered": 1, "created_at": 1}))
        now = datetime.utcnow()
        month_start = datetime(now.year, now.month, 1)
        previous_month_end = month_start
        previous_month_start = datetime(month_start.year - (1 if month_start.month == 1 else 0), 12 if month_start.month == 1 else month_start.month - 1, 1)
        if customer_filter in {"registered_this_month", "registered_last_month"}:
            start, end = (month_start, now + timedelta(seconds=1)) if customer_filter == "registered_this_month" else (previous_month_start, previous_month_end)
            docs = [doc for doc in docs if (registered := _as_datetime(doc.get("date_registered") or doc.get("created_at"))) and start <= registered < end]
        elif customer_filter in {"payment_last_7_days", "payment_last_week"}:
            if customer_filter == "payment_last_7_days":
                start, end = now - timedelta(days=7), now + timedelta(seconds=1)
            else:
                this_week_start = datetime(now.year, now.month, now.day) - timedelta(days=now.weekday())
                start, end = this_week_start - timedelta(days=7), this_week_start
            customer_ids = _customer_payment_ids(start, end)
            if customer_filter == "payment_last_week":
                customer_ids -= _customer_payment_ids(end, now + timedelta(seconds=1))
            docs = [doc for doc in docs if doc.get("_id") in customer_ids]

    recipients = []
    seen_phones = set()
    for doc in docs:
        raw_phone = doc.get("phone_number") or doc.get("phone") or ""
        normalized = normalize_ghana_phone(raw_phone)
        if normalized and normalized in seen_phones:
            continue
        if normalized:
            seen_phones.add(normalized)
        recipients.append({
            "id": str(doc.get("_id") or ""),
            "name": str(doc.get("name") or doc.get("username") or "Customer").strip() or "Customer",
            "phone": str(raw_phone),
            "normalized_phone": normalized,
        })
    return recipients


def _delivery_log_rows(limit: int = 25) -> list[dict]:
    rows = []
    for doc in delivery_logs_col.find({}).sort("created_at", -1).limit(limit):
        rows.append({
            "id": str(doc.get("_id") or ""),
            "audience": doc.get("audience_label") or str(doc.get("audience") or "").title(),
            "filter": doc.get("filter_label") or "All",
            "message": doc.get("message") or "",
            "total": int(doc.get("total") or 0),
            "sent": int(doc.get("sent") or 0),
            "failed": int(doc.get("failed") or 0),
            "invalid": int(doc.get("invalid") or 0),
            "cost": float(doc.get("cost") or 0),
            "currency": doc.get("currency") or "GHS",
            "createdBy": (doc.get("created_by") or {}).get("name") or "Executive",
            "createdAt": doc.get("created_at").strftime("%d %b %Y, %H:%M") if isinstance(doc.get("created_at"), datetime) else "",
        })
    return rows


@executive_messages_bp.route("", methods=["GET"])
@role_required("executive", "admin")
def messages_page():
    return render_template(
        "executive_messages.html",
        placeholders=PLACEHOLDERS,
        template_types=TEMPLATE_TYPES,
        templates=get_payment_message_settings(),
        audiences=AUDIENCES,
        customer_filters=CUSTOMER_FILTERS,
        delivery_logs=_delivery_log_rows(),
        sms_gateway=gateway_status(),
    )


@executive_messages_bp.route("/api", methods=["PUT"])
@role_required("executive", "admin")
def update_messages():
    payload = request.get_json(silent=True) or {}
    identity = get_current_identity()
    try:
        templates = save_payment_message_settings(
            payload.get("templates"),
            updated_by={
                "id": str(identity.get("id") or ""),
                "name": identity.get("name") or identity.get("username") or "Executive",
                "role": identity.get("role") or "executive",
            },
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({"ok": True, "templates": templates})


@executive_messages_bp.route("/audience-count", methods=["GET"])
@role_required("executive", "admin")
def audience_count():
    try:
        recipients = _resolve_recipients(
            str(request.args.get("audience") or ""),
            str(request.args.get("customer_filter") or "all"),
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    return jsonify({
        "ok": True,
        "total": len(recipients),
        "deliverable": sum(1 for row in recipients if row["normalized_phone"]),
        "invalid": sum(1 for row in recipients if not row["normalized_phone"]),
    })


@executive_messages_bp.route("/deliver", methods=["POST"])
@role_required("executive", "admin")
def deliver_message():
    payload = request.get_json(silent=True) or {}
    audience = str(payload.get("audience") or "")
    customer_filter = str(payload.get("customer_filter") or "all")
    message = str(payload.get("message") or "").strip()
    if not message:
        return jsonify({"ok": False, "error": "Enter a message to deliver."}), 400
    if len(message) > 1000:
        return jsonify({"ok": False, "error": "Message must be 1,000 characters or fewer."}), 400
    unknown = sorted(set(_broadcast_token_re.findall(message)) - {"name", "full_name"})
    if unknown:
        return jsonify({"ok": False, "error": f"Unknown broadcast placeholder: {{{unknown[0]}}}."}), 400
    try:
        recipients = _resolve_recipients(audience, customer_filter)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if not recipients:
        return jsonify({"ok": False, "error": "No recipients match this selection."}), 400

    gateway = gateway_status()
    if not gateway["configured"]:
        return jsonify({"ok": False, "error": "VireSender is not configured. Set VIRESENDER_API_KEY before delivering SMS."}), 503

    counts = {"sent": 0, "failed": 0, "invalid": 0}
    valid_recipients = []
    for recipient in recipients:
        if not recipient["normalized_phone"]:
            counts["invalid"] += 1
        else:
            full_name = recipient["name"]
            personalized = message.replace("{full_name}", full_name).replace("{name}", full_name.split()[0])
            valid_recipients.append((recipient["normalized_phone"], personalized))

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [executor.submit(send_sms_detailed, phone, personalized) for phone, personalized in valid_recipients]
        delivery_results = []
        for future in as_completed(futures):
            try:
                delivery = future.result()
            except Exception as exc:
                delivery = {"status": "failed", "error": str(exc), "provider": "VireSender"}
            delivery_results.append(delivery)
            status = delivery.get("status")
            counts["sent" if status == "sent" else "failed"] += 1

    identity = get_current_identity()
    created_at = datetime.utcnow()
    log = {
        "audience": audience,
        "audience_label": AUDIENCES[audience],
        "customer_filter": customer_filter,
        "filter_label": CUSTOMER_FILTERS.get(customer_filter, "All"),
        "message": message,
        "total": len(recipients),
        **counts,
        "provider": "VireSender",
        "sender_id": gateway["sender_id"],
        "sms_units": sum(float(row.get("sms_units") or 0) for row in delivery_results),
        "cost": sum(float(row.get("cost") or 0) for row in delivery_results),
        "currency": next((row.get("currency") for row in delivery_results if row.get("currency")), "GHS"),
        "wallet_balance": next((row.get("wallet_balance") for row in reversed(delivery_results) if row.get("wallet_balance") is not None), None),
        "failed_results": [row for row in delivery_results if row.get("status") != "sent"][:100],
        "created_at": created_at,
        "created_by": {
            "id": str(identity.get("id") or ""),
            "name": identity.get("name") or identity.get("username") or "Executive",
        },
    }
    result = delivery_logs_col.insert_one(log)
    return jsonify({
        "ok": True,
        "log": {
            "id": str(result.inserted_id),
            "audience": log["audience_label"],
            "filter": log["filter_label"],
            "message": message,
            "total": log["total"],
            **counts,
            "cost": log["cost"],
            "currency": log["currency"],
            "createdBy": log["created_by"]["name"],
            "createdAt": created_at.strftime("%d %b %Y, %H:%M"),
        },
    })
