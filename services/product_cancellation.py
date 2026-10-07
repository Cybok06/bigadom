"""Product cancellation rules shared by customer profiles and payments."""


def is_cancelled_purchase(purchase):
    statuses = (purchase.get("status"), (purchase.get("product") or {}).get("status"))
    return any(str(status or "").strip().lower() in {"cancelled", "canceled"} for status in statuses)


def payment_purchases(purchases):
    """Keep original indices: existing payments refer to positions in purchases."""
    return [dict(purchase, product_index=index) for index, purchase in enumerate(purchases or [])
            if not is_cancelled_purchase(purchase)]
