from __future__ import annotations

from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from bson.decimal128 import Decimal128

RATE = Decimal("0.07")
REPAYMENT_DAYS = 65
GRACE_DAYS = 14
OPEN_STATUSES = {"pending", "approved", "active", "grace_period", "overdue"}


def money(value) -> Decimal:
    if isinstance(value, Decimal128):
        value = value.to_decimal()
    return Decimal(str(value or 0)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def mongo_money(value) -> Decimal128:
    return Decimal128(money(value))


def as_float(value) -> float:
    return float(money(value))


def terms(amount) -> dict:
    principal = money(amount)
    if principal <= 0:
        raise ValueError("Loan amount must be greater than zero.")
    fee = money(principal * RATE)
    daily = money(principal / Decimal("50"))
    total = money(daily * REPAYMENT_DAYS)
    return {
        "original_amount": principal,
        "processing_fee": fee,
        "amount_disbursed": money(principal - fee),
        "daily_repayment": daily,
        "expected_total_repayment": total,
        "penalty_per_cycle": fee,
    }


def repayment_dates(disbursement: date) -> dict:
    current = disbursement + timedelta(days=1)
    start = current
    count = 0
    completion = current
    while count < REPAYMENT_DAYS:
        if current.weekday() != 6:
            count += 1
            completion = current
        current += timedelta(days=1)
    grace_start = completion + timedelta(days=1)
    grace_end = completion + timedelta(days=GRACE_DAYS)
    return {
        "repayment_start_date": start,
        "expected_completion_date": completion,
        "grace_period_start_date": grace_start,
        "grace_period_end_date": grace_end,
        "next_penalty_date": grace_end + timedelta(days=1),
    }


def due_penalty_cycles(loan: dict, today: date | None = None) -> int:
    today = today or date.today()
    raw = loan.get("next_penalty_date")
    if not raw:
        return 0
    if isinstance(raw, datetime):
        raw = raw.date()
    if isinstance(raw, str):
        raw = date.fromisoformat(raw)
    if today < raw:
        return 0
    return ((today - raw).days // GRACE_DAYS) + 1


def balance(loan: dict, paid=None, penalties=None) -> Decimal:
    paid = money(loan.get("amount_paid") if paid is None else paid)
    penalties = money(loan.get("total_penalties") if penalties is None else penalties)
    return max(Decimal("0.00"), money(loan.get("expected_total_repayment")) + penalties - paid)


def status_for(loan: dict, today: date | None = None) -> str:
    if money(loan.get("current_balance")) <= 0 and loan.get("status") not in {"pending", "rejected", "cancelled"}:
        return "settled"
    if loan.get("status") in {"pending", "rejected", "cancelled"}:
        return loan["status"]
    today = today or date.today()
    completion = loan.get("expected_completion_date")
    grace_end = loan.get("grace_period_end_date")
    if isinstance(completion, datetime): completion = completion.date()
    elif isinstance(completion, str): completion = date.fromisoformat(completion)
    if isinstance(grace_end, datetime): grace_end = grace_end.date()
    elif isinstance(grace_end, str): grace_end = date.fromisoformat(grace_end)
    if grace_end and today > grace_end:
        return "overdue"
    if completion and today > completion:
        return "grace_period"
    return "active"


def display(loan: dict) -> dict:
    result = dict(loan)
    result["id"] = str(result.get("_id", ""))
    for field in ("original_amount", "processing_fee", "amount_disbursed", "daily_repayment",
                  "expected_total_repayment", "amount_paid", "total_penalties", "current_balance"):
        result[field] = as_float(result.get(field))
    due = result["expected_total_repayment"] + result["total_penalties"]
    result["progress"] = min(100, round((result["amount_paid"] / due * 100) if due else 0))
    return result
