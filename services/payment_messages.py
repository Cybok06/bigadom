from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from db import db


TEMPLATE_TYPES = ("PRODUCT", "SUSU", "LOAN")
PLACEHOLDERS = {
    "name": "Customer first name",
    "full_name": "Customer full name",
    "payment_amount": "Amount just received",
    "payment_type": "Product, SUSU, or Loan",
    "payment_date": "Payment date",
    "susu_total": "Customer's total SUSU savings",
    "product_name": "Selected product name",
    "product_total": "Selected product price",
    "product_paid": "Total paid toward the product",
    "product_amount_left": "Product balance remaining",
    "loan_number": "Loan reference number",
    "loan_total": "Total loan repayment amount",
    "loan_paid": "Total loan amount paid",
    "loan_amount_left": "Loan balance remaining",
}

DEFAULT_TEMPLATES = {
    "PRODUCT": (
        "Hello {name}, we received GHS {payment_amount} for {product_name}. "
        "Total paid: GHS {product_paid}. Amount left: GHS {product_amount_left}."
    ),
    "SUSU": (
        "Hello {name}, we received GHS {payment_amount} as your SUSU payment. "
        "Your SUSU total is GHS {susu_total}."
    ),
    "LOAN": (
        "Hello {name}, we received GHS {payment_amount} as your loan payment. "
        "Total loan paid: GHS {loan_paid}. Amount left: GHS {loan_amount_left}."
    ),
}

_TOKEN_RE = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_collection = db["payment_message_templates"]


def get_payment_message_settings() -> dict[str, dict[str, Any]]:
    stored = _collection.find_one({"_id": "payment_sms"}) or {}
    stored_templates = stored.get("templates") or {}
    return {
        template_type: {
            "enabled": bool((stored_templates.get(template_type) or {}).get("enabled", True)),
            "message": str((stored_templates.get(template_type) or {}).get("message") or DEFAULT_TEMPLATES[template_type]),
        }
        for template_type in TEMPLATE_TYPES
    }


def validate_payment_message_templates(templates: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(templates, dict):
        raise ValueError("Templates must be provided.")

    cleaned: dict[str, dict[str, Any]] = {}
    for template_type in TEMPLATE_TYPES:
        row = templates.get(template_type)
        if not isinstance(row, dict):
            raise ValueError(f"A {template_type.title()} message is required.")
        message = str(row.get("message") or "").strip()
        if not message:
            raise ValueError(f"The {template_type.title()} message cannot be empty.")
        unknown = sorted(set(_TOKEN_RE.findall(message)) - set(PLACEHOLDERS))
        if unknown:
            raise ValueError(f"Unknown placeholder in {template_type.title()}: {{{unknown[0]}}}.")
        cleaned[template_type] = {"enabled": bool(row.get("enabled", True)), "message": message}
    return cleaned


def save_payment_message_settings(templates: Any, updated_by: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    cleaned = validate_payment_message_templates(templates)
    _collection.update_one(
        {"_id": "payment_sms"},
        {
            "$set": {
                "templates": cleaned,
                "updated_at": datetime.utcnow(),
                "updated_by": updated_by or {},
            }
        },
        upsert=True,
    )
    return cleaned


def render_payment_message(template_type: str, values: dict[str, Any]) -> str | None:
    normalized_type = str(template_type or "").upper()
    if normalized_type not in TEMPLATE_TYPES:
        return None
    settings = get_payment_message_settings()[normalized_type]
    if not settings["enabled"]:
        return None

    normalized_values = {key: str(values.get(key, "")) for key in PLACEHOLDERS}
    return _TOKEN_RE.sub(lambda match: normalized_values.get(match.group(1), ""), settings["message"])
