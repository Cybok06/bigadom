from __future__ import annotations

import os
from typing import Any

import requests


# The documented www host redirects to the canonical host. Requests may drop
# Authorization across that redirect, so call the canonical API directly.
VIRESENDER_BASE_URL = os.getenv("VIRESENDER_BASE_URL", "https://viresender.com").rstrip("/")
VIRESENDER_API_KEY = os.getenv("VIRESENDER_API_KEY", "").strip()
VIRESENDER_SENDER_ID = os.getenv("VIRESENDER_SENDER_ID", "SMARTLIVING").strip()
VIRESENDER_TIMEOUT = float(os.getenv("VIRESENDER_TIMEOUT", "15"))


def normalize_ghana_phone(raw: str) -> str | None:
    phone = "".join(character for character in str(raw or "") if character.isdigit())
    if phone.startswith("0") and len(phone) == 10:
        phone = "233" + phone[1:]
    return phone if phone.startswith("233") and len(phone) == 12 else None


def gateway_status() -> dict[str, Any]:
    return {"configured": bool(VIRESENDER_API_KEY and VIRESENDER_SENDER_ID), "provider": "VireSender",
            "sender_id": VIRESENDER_SENDER_ID, "base_url": VIRESENDER_BASE_URL}


def send_sms_detailed(phone: str, message: str) -> dict[str, Any]:
    normalized = normalize_ghana_phone(phone)
    text = str(message or "").strip()
    result = {"status": "failed", "provider": "VireSender", "to": normalized or str(phone or ""), "sender_id": VIRESENDER_SENDER_ID}
    if normalized is None:
        return {**result, "status": "invalid_phone", "error": "Invalid Ghana phone number."}
    if not text:
        return {**result, "error": "SMS message is empty."}
    if not VIRESENDER_API_KEY:
        return {**result, "status": "not_configured", "error": "VIRESENDER_API_KEY is not configured."}
    try:
        response = requests.post(f"{VIRESENDER_BASE_URL}/v1/sms/send",
            headers={"Authorization": f"Bearer {VIRESENDER_API_KEY}", "Content-Type": "application/json", "Accept": "application/json"},
            json={"to": normalized, "message": text, "sender_id": VIRESENDER_SENDER_ID}, timeout=VIRESENDER_TIMEOUT)
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        success = response.status_code == 200 and payload.get("success") is True
        return {**result, "status": "sent" if success else "failed", "http_status": response.status_code,
                "sms_id": payload.get("sms_id"), "provider_status": payload.get("status"),
                "recipient_count": payload.get("recipient_count"), "sms_units": payload.get("sms_units"),
                "cost": payload.get("cost"), "currency": payload.get("currency"), "wallet_balance": payload.get("wallet_balance"),
                "error": None if success else str(payload.get("message") or payload.get("error") or "VireSender rejected the SMS.")}
    except requests.RequestException as exc:
        return {**result, "status": "failed", "error": str(exc)}


def send_sms(phone: str, message: str) -> str:
    return str(send_sms_detailed(phone, message)["status"])
