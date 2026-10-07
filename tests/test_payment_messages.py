from unittest.mock import patch

import pytest

from services.payment_messages import TEMPLATE_TYPES, render_payment_message, validate_payment_message_templates


def _templates(message: str):
    return {template_type: {"enabled": True, "message": message} for template_type in TEMPLATE_TYPES}


def test_validates_supported_placeholders():
    result = validate_payment_message_templates(_templates("Hello {name}, GHS {payment_amount}."))
    assert result["SUSU"]["message"] == "Hello {name}, GHS {payment_amount}."


def test_rejects_unknown_placeholders():
    with pytest.raises(ValueError, match="Unknown placeholder"):
        validate_payment_message_templates(_templates("Hello {nickname}."))


def test_renders_saved_template_values():
    saved = {template_type: {"enabled": True, "message": "Hello {name}, balance {loan_amount_left}."} for template_type in TEMPLATE_TYPES}
    with patch("services.payment_messages.get_payment_message_settings", return_value=saved):
        result = render_payment_message("LOAN", {"name": "Ama", "loan_amount_left": "80.00"})
    assert result == "Hello Ama, balance 80.00."


def test_disabled_template_does_not_render():
    saved = {template_type: {"enabled": template_type != "SUSU", "message": "Hello {name}."} for template_type in TEMPLATE_TYPES}
    with patch("services.payment_messages.get_payment_message_settings", return_value=saved):
        assert render_payment_message("SUSU", {"name": "Ama"}) is None
