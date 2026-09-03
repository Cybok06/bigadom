from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from bson import ObjectId
from flask import Blueprint, jsonify, render_template, request

from db import db
from login import get_current_identity, role_required


attendance_bp = Blueprint("attendance", __name__)
attendance_col = db["attendance_workdays"]
users_col = db["users"]

ATTENDANCE_ACTIONS = {
    "report_to_work": "Report to Work",
    "lunch_start": "Lunch Start",
    "lunch_return": "Lunch Return",
    "leave_office": "Leave Office",
}

ALLOWED_ROLES = {"agent", "manager", "inventory", "hr", "admin", "executive"}


def _ensure_indexes() -> None:
    try:
        attendance_col.create_index([("user_id", 1), ("attendance_date", 1)], unique=True)
        attendance_col.create_index([("attendance_date", -1), ("role", 1)])
        attendance_col.create_index([("updated_at", -1)])
    except Exception:
        pass


_ensure_indexes()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return _utc_now().date().isoformat()


def _safe_object_id(value: Any) -> Optional[ObjectId]:
    try:
        return ObjectId(str(value))
    except Exception:
        return None


def _fetch_user_doc(user_id: str) -> Dict[str, Any]:
    oid = _safe_object_id(user_id)
    if oid is not None:
        doc = users_col.find_one({"_id": oid}) or {}
        if doc:
            return doc
    return users_col.find_one({"_id": user_id}) or {}


def _sidebar_template_for_role(role: str) -> str:
    return {
        "agent": "side_bar.html",
        "manager": "manager_sidebar.html",
        "inventory": "inventory_sidebar.html",
        "admin": "admin_sidebar.html",
        "executive": "executive_sidebar.html",
        "hr": "hr_pages/hr_shell.html",
    }.get(role, "side_bar.html")


def _compute_status(doc: Dict[str, Any]) -> Dict[str, Any]:
    report = doc.get("report_to_work_at")
    lunch_start = doc.get("lunch_start_at")
    lunch_return = doc.get("lunch_return_at")
    leave = doc.get("leave_office_at")

    if leave:
        label, tone = "Closed for Day", "success"
    elif lunch_start and not lunch_return:
        label, tone = "At Lunch", "warning"
    elif report:
        label, tone = "At Work", "primary"
    else:
        label, tone = "Not Reported", "muted"

    next_action = None
    if not report:
        next_action = "report_to_work"
    elif lunch_start and not lunch_return:
        next_action = "lunch_return"
    elif not lunch_start:
        next_action = "lunch_start"
    elif not leave:
        next_action = "leave_office"

    return {
        "label": label,
        "tone": tone,
        "next_action": next_action,
        "is_closed": bool(leave),
    }


def _serialize_doc(doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not doc:
        fallback = {
            "attendance_date": _today_str(),
            "report_to_work_at": "",
            "lunch_start_at": "",
            "lunch_return_at": "",
            "leave_office_at": "",
            "events": [],
        }
        fallback["status"] = _compute_status(fallback)
        return fallback

    data = {
        "id": str(doc.get("_id") or ""),
        "attendance_date": doc.get("attendance_date") or "",
        "report_to_work_at": doc.get("report_to_work_at") or "",
        "lunch_start_at": doc.get("lunch_start_at") or "",
        "lunch_return_at": doc.get("lunch_return_at") or "",
        "leave_office_at": doc.get("leave_office_at") or "",
        "events": doc.get("events") or [],
        "role": doc.get("role") or "",
        "branch": doc.get("branch") or "",
    }
    data["status"] = _compute_status(data)
    return data


def _validate_transition(doc: Dict[str, Any], action: str) -> Optional[str]:
    if action == "report_to_work":
        if doc.get("report_to_work_at"):
            return "You have already reported to work today."
        return None

    if action == "lunch_start":
        if not doc.get("report_to_work_at"):
            return "Report to work first."
        if doc.get("leave_office_at"):
            return "Your workday is already closed."
        if doc.get("lunch_start_at") and not doc.get("lunch_return_at"):
            return "Lunch has already started."
        if doc.get("lunch_return_at"):
            return "Lunch has already been completed for today."
        return None

    if action == "lunch_return":
        if not doc.get("lunch_start_at"):
            return "Lunch has not started yet."
        if doc.get("lunch_return_at"):
            return "Lunch return has already been logged."
        if doc.get("leave_office_at"):
            return "Your workday is already closed."
        return None

    if action == "leave_office":
        if not doc.get("report_to_work_at"):
            return "Report to work first."
        if doc.get("leave_office_at"):
            return "Leave office has already been logged today."
        return None

    return "Unsupported attendance action."


def _base_today_doc(ident: Dict[str, Any], user_doc: Dict[str, Any]) -> Dict[str, Any]:
    now = _utc_now()
    return {
        "user_id": str(ident.get("user_id") or ""),
        "name": user_doc.get("name") or ident.get("name") or ident.get("username") or "User",
        "username": user_doc.get("username") or ident.get("username") or "",
        "role": str(ident.get("role") or "").strip().lower(),
        "branch": user_doc.get("branch") or user_doc.get("store_name") or user_doc.get("store") or "",
        "attendance_date": _today_str(),
        "report_to_work_at": "",
        "lunch_start_at": "",
        "lunch_return_at": "",
        "leave_office_at": "",
        "events": [],
        "created_at": now,
        "updated_at": now,
    }


@attendance_bp.route("/attendance", methods=["GET"])
@role_required("agent", "manager", "inventory", "hr", "admin", "executive")
def attendance_page():
    ident = get_current_identity()
    role = str(ident.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        return "Forbidden", 403

    user_doc = _fetch_user_doc(str(ident.get("user_id") or ""))
    return render_template(
        "attendance_page.html",
        attendance_role=role,
        sidebar_template=_sidebar_template_for_role(role),
        page_title="Attendance",
        active_page="attendance",
        user_name=user_doc.get("name") or ident.get("name") or ident.get("username") or "User",
        user_branch=user_doc.get("branch") or user_doc.get("store_name") or user_doc.get("store") or "",
        user_position=user_doc.get("position") or user_doc.get("department") or "",
        today_date=_today_str(),
        attendance_actions=ATTENDANCE_ACTIONS,
    )


@attendance_bp.route("/attendance/api/today", methods=["GET"])
@role_required("agent", "manager", "inventory", "hr", "admin", "executive")
def attendance_today():
    ident = get_current_identity()
    doc = attendance_col.find_one(
        {"user_id": str(ident.get("user_id") or ""), "attendance_date": _today_str()}
    )
    return jsonify(ok=True, attendance=_serialize_doc(doc))


@attendance_bp.route("/attendance/api/history", methods=["GET"])
@role_required("agent", "manager", "inventory", "hr", "admin", "executive")
def attendance_history():
    ident = get_current_identity()
    try:
        limit = max(1, min(31, int(request.args.get("limit", "10"))))
    except Exception:
        limit = 10

    cursor = attendance_col.find(
        {"user_id": str(ident.get("user_id") or "")},
        {
            "attendance_date": 1,
            "report_to_work_at": 1,
            "lunch_start_at": 1,
            "lunch_return_at": 1,
            "leave_office_at": 1,
            "events": 1,
            "role": 1,
            "branch": 1,
        },
    ).sort("attendance_date", -1).limit(limit)
    return jsonify(ok=True, history=[_serialize_doc(row) for row in cursor])


@attendance_bp.route("/attendance/api/action", methods=["POST"])
@role_required("agent", "manager", "inventory", "hr", "admin", "executive")
def attendance_action():
    ident = get_current_identity()
    role = str(ident.get("role") or "").strip().lower()
    if role not in ALLOWED_ROLES:
        return jsonify(ok=False, message="Forbidden"), 403

    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action") or "").strip().lower()
    if action not in ATTENDANCE_ACTIONS:
        return jsonify(ok=False, message="Invalid attendance action."), 400

    user_id = str(ident.get("user_id") or "")
    user_doc = _fetch_user_doc(user_id)
    today = _today_str()
    now = _utc_now()
    stamp = now.isoformat().replace("+00:00", "Z")

    existing = attendance_col.find_one({"user_id": user_id, "attendance_date": today})
    working = dict(existing or _base_today_doc(ident, user_doc))

    error = _validate_transition(working, action)
    if error:
        return jsonify(ok=False, message=error, attendance=_serialize_doc(working)), 409

    working[f"{action}_at"] = stamp
    working["updated_at"] = now
    working["role"] = role
    working["branch"] = user_doc.get("branch") or user_doc.get("store_name") or user_doc.get("store") or ""
    working["events"] = list(working.get("events") or [])
    working["events"].insert(
        0,
        {"action": action, "label": ATTENDANCE_ACTIONS[action], "at": stamp},
    )

    if existing:
        attendance_col.update_one(
            {"_id": existing["_id"]},
            {
                "$set": {
                    f"{action}_at": stamp,
                    "updated_at": now,
                    "events": working["events"],
                    "role": role,
                    "branch": working["branch"],
                }
            },
        )
    else:
        attendance_col.insert_one(working)

    saved = attendance_col.find_one({"user_id": user_id, "attendance_date": today})
    return jsonify(
        ok=True,
        message=f"{ATTENDANCE_ACTIONS[action]} logged successfully.",
        attendance=_serialize_doc(saved),
    )
