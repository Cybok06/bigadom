from datetime import date, datetime
from decimal import Decimal

from services.loans import balance, due_penalty_cycles, repayment_dates, status_for, terms


def test_standard_loan_terms():
    cases = [(500, "35.00", "465.00", "10.00", "650.00"),
             (1000, "70.00", "930.00", "20.00", "1300.00"),
             (1500, "105.00", "1395.00", "30.00", "1950.00")]
    for amount, fee, net, daily, total in cases:
        result = terms(amount)
        assert result["processing_fee"] == Decimal(fee)
        assert result["amount_disbursed"] == Decimal(net)
        assert result["daily_repayment"] == Decimal(daily)
        assert result["expected_total_repayment"] == Decimal(total)


def test_schedule_starts_next_day_and_counts_65_non_sundays():
    result = repayment_dates(date(2026, 8, 31))
    assert result["repayment_start_date"] == date(2026, 9, 1)
    cursor = result["repayment_start_date"]
    counted = 0
    while cursor <= result["expected_completion_date"]:
        if cursor.weekday() != 6:
            counted += 1
        cursor = cursor.fromordinal(cursor.toordinal() + 1)
    assert counted == 65
    assert result["expected_completion_date"].weekday() != 6
    assert (result["grace_period_end_date"] - result["expected_completion_date"]).days == 14


def test_sunday_start_is_preserved_but_not_counted():
    result = repayment_dates(date(2026, 9, 5))
    assert result["repayment_start_date"].weekday() == 6


def test_penalty_cycles_are_calendar_cycles():
    loan = {"next_penalty_date": date(2026, 10, 1)}
    assert due_penalty_cycles(loan, date(2026, 9, 30)) == 0
    assert due_penalty_cycles(loan, date(2026, 10, 1)) == 1
    assert due_penalty_cycles(loan, date(2026, 10, 15)) == 2


def test_balance_and_status():
    loan = {"expected_total_repayment": 650, "total_penalties": 35, "amount_paid": 100,
            "current_balance": 585, "status": "active", "expected_completion_date": date(2026, 9, 1),
            "grace_period_end_date": date(2026, 9, 15)}
    assert balance(loan) == Decimal("585.00")
    assert status_for(loan, date(2026, 9, 10)) == "grace_period"
    assert status_for(loan, date(2026, 9, 16)) == "overdue"
    loan["current_balance"] = 0
    assert status_for(loan, date(2026, 9, 16)) == "settled"


def test_schedule_dates_can_be_converted_for_bson_storage():
    calculated = repayment_dates(date(2026, 8, 31))
    stored = {key: datetime.combine(value, datetime.min.time()) for key, value in calculated.items()}
    assert all(isinstance(value, datetime) for value in stored.values())
    assert stored["repayment_start_date"] == datetime(2026, 9, 1)
