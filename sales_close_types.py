"""Shared helpers for typed sales-close balances."""

from decimal import Decimal, InvalidOperation


PAYMENT_TYPES = ("SUSU", "LOAN", "PRODUCT")
BALANCE_FIELDS = {
    "SUSU": "susu_amount",
    "LOAN": "loan_amount",
    "PRODUCT": "product_amount",
}


def money(value) -> float:
    try:
        return max(0.0, float(Decimal(str(value or 0))))
    except (InvalidOperation, TypeError, ValueError):
        return 0.0


def requested_breakdown(source) -> dict[str, float]:
    """Read typed amounts from a Flask form/JSON-like mapping."""
    return {
        payment_type: money(source.get(f"{payment_type.lower()}_amount"))
        for payment_type in PAYMENT_TYPES
    }


def typed_inc(payment_type: str, amount: float) -> dict[str, float]:
    field = BALANCE_FIELDS[payment_type.upper()]
    return {"total_amount": amount, field: amount}


def document_breakdown(doc: dict) -> dict[str, float]:
    values = {
        payment_type: money(doc.get(field))
        for payment_type, field in BALANCE_FIELDS.items()
    }
    typed_total = sum(values.values())
    values["LEGACY"] = max(0.0, money(doc.get("total_amount")) - typed_total)
    values["TOTAL"] = typed_total + values["LEGACY"]
    return values


def aggregate_breakdown(collection, match: dict) -> dict[str, float]:
    result = {key: 0.0 for key in (*PAYMENT_TYPES, "LEGACY", "TOTAL")}
    projection = {"total_amount": 1, **{field: 1 for field in BALANCE_FIELDS.values()}}
    for doc in collection.find(match, projection):
        values = document_breakdown(doc)
        for key in result:
            result[key] += values[key]
    return result


def allocate_total(collection, source_id: str, amount: float) -> dict[str, float]:
    """Allocate a legacy single-amount request without losing its source categories."""
    remaining = money(amount)
    available = aggregate_breakdown(collection, {"agent_id": str(source_id)})
    allocated = {}
    for key in (*PAYMENT_TYPES, "LEGACY"):
        allocated[key] = min(available[key], remaining)
        remaining -= allocated[key]
    if remaining > 1e-9:
        raise ValueError(f"Insufficient balance. Available: GHS {available['TOTAL']:,.2f}")
    return allocated


def formatted_breakdown(values: dict) -> dict[str, object]:
    return {
        "susu": f"{values.get('SUSU', 0):,.2f}",
        "loan": f"{values.get('LOAN', 0):,.2f}",
        "product": f"{values.get('PRODUCT', 0):,.2f}",
        "legacy": f"{values.get('LEGACY', 0):,.2f}",
        "total": f"{values.get('TOTAL', 0):,.2f}",
        "susu_num": values.get("SUSU", 0),
        "loan_num": values.get("LOAN", 0),
        "product_num": values.get("PRODUCT", 0),
        "legacy_num": values.get("LEGACY", 0),
        "total_num": values.get("TOTAL", 0),
    }


def transfer_breakdown(collection, source_id: str, target_id: str, requested: dict,
                       today: str, now, withdrawal_meta: dict) -> dict:
    """Move typed balances between ledger owners, newest source documents first."""
    available = aggregate_breakdown(collection, {"agent_id": str(source_id)})
    requested = {key: money(requested.get(key)) for key in (*PAYMENT_TYPES, "LEGACY")}
    for key, amount in requested.items():
        if amount > available.get(key, 0) + 1e-9:
            raise ValueError(f"Insufficient {key.title()} balance. Available: GHS {available.get(key, 0):,.2f}")

    moved = {key: 0.0 for key in (*PAYMENT_TYPES, "LEGACY")}
    docs = list(collection.find({"agent_id": str(source_id)}).sort([
        ("date", -1), ("updated_at", -1)
    ]))
    for payment_type, requested_amount in requested.items():
        remaining = requested_amount
        for doc in docs:
            if remaining <= 1e-9:
                break
            balances = document_breakdown(doc)
            take = min(balances[payment_type], remaining)
            if take <= 0:
                continue
            inc = {"total_amount": -take}
            guard = {"$toDouble": {"$ifNull": ["$total_amount", 0]}}
            if payment_type != "LEGACY":
                field = BALANCE_FIELDS[payment_type]
                inc[field] = -take
                guard = {"$toDouble": {"$ifNull": [f"${field}", 0]}}
            entry = {
                **withdrawal_meta,
                "amount": round(take, 2), "payment_type": payment_type,
                "date": doc.get("date", today), "at": now,
            }
            result = collection.update_one(
                {"_id": doc["_id"], "$expr": {"$gte": [guard, take]}},
                {"$inc": inc, "$set": {"updated_at": now, "last_withdrawal_at": now},
                 "$push": {"withdrawals": entry}},
            )
            if result.modified_count == 1:
                moved[payment_type] += take
                remaining -= take
        if remaining > 1e-9:
            raise RuntimeError("Balance changed while closing. Please retry.")

    total = sum(moved.values())
    if total:
        inc = {"total_amount": total, "count": 1}
        for payment_type in PAYMENT_TYPES:
            if moved[payment_type]:
                inc[BALANCE_FIELDS[payment_type]] = moved[payment_type]
        collection.update_one(
            {"agent_id": str(target_id), "date": today},
            {"$setOnInsert": {"agent_id": str(target_id), "manager_id": str(target_id),
                              "date": today, "created_at": now},
             "$inc": inc, "$set": {"updated_at": now, "last_payment_at": now}},
            upsert=True,
        )
    moved["TOTAL"] = total
    return moved
