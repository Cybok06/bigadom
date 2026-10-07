from sales_close_types import document_breakdown, formatted_breakdown, requested_breakdown, typed_inc


def test_document_breakdown_preserves_legacy_balance():
    result = document_breakdown({
        "total_amount": 100,
        "susu_amount": 30,
        "loan_amount": 20,
        "product_amount": 10,
    })
    assert result == {"SUSU": 30, "LOAN": 20, "PRODUCT": 10, "LEGACY": 40, "TOTAL": 100}


def test_requested_breakdown_rejects_negative_and_bad_values():
    result = requested_breakdown({
        "susu_amount": "25.50", "loan_amount": "bad", "product_amount": "-5"
    })
    assert result == {"SUSU": 25.5, "LOAN": 0, "PRODUCT": 0}


def test_typed_increment_keeps_combined_total():
    assert typed_inc("LOAN", 17.25) == {"total_amount": 17.25, "loan_amount": 17.25}


def test_formatted_breakdown_exposes_display_and_numeric_values():
    result = formatted_breakdown({"SUSU": 1, "LOAN": 2, "PRODUCT": 3, "LEGACY": 4, "TOTAL": 10})
    assert result["loan"] == "2.00"
    assert result["legacy_num"] == 4
    assert result["total_num"] == 10
