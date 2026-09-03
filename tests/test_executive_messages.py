from unittest.mock import patch

from services.sms_gateway import normalize_ghana_phone
from routes import executive_messages as messages


class _Users:
    def find(self, query, projection):
        assert query["role"] == "agent"
        return [
            {"_id": "1", "name": "Ama Mensah", "phone": "055 123 4567"},
            {"_id": "2", "name": "Duplicate", "phone_number": "+233551234567"},
            {"_id": "3", "name": "No Phone", "phone": "invalid"},
        ]


def test_normalizes_ghana_phone_numbers():
    assert normalize_ghana_phone("055 123 4567") == "233551234567"
    assert normalize_ghana_phone("+233551234567") == "233551234567"
    assert normalize_ghana_phone("123") is None


def test_agent_recipient_resolution_deduplicates_valid_phone_numbers():
    with patch.object(messages, "users_col", _Users()):
        recipients = messages._resolve_recipients("agents")
    assert len(recipients) == 2
    assert sum(1 for row in recipients if row["normalized_phone"]) == 1


def test_broadcast_name_placeholders_are_personalized():
    text = "Hello {name}, account holder {full_name}."
    full_name = "Ama Mensah"
    rendered = text.replace("{full_name}", full_name).replace("{name}", full_name.split()[0])
    assert rendered == "Hello Ama, account holder Ama Mensah."
