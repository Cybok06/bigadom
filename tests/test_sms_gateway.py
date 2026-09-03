from unittest.mock import Mock, patch

from services import sms_gateway


def test_viresender_send_contract_and_response_metadata():
    response = Mock(status_code=200)
    response.json.return_value = {
        "success": True, "sms_id": "SMS-12345", "status": "sent",
        "recipient_count": 1, "sms_units": 1, "cost": 0.04,
        "currency": "GHS", "wallet_balance": 199.96,
    }
    with patch.object(sms_gateway, "VIRESENDER_API_KEY", "secret"), patch.object(
        sms_gateway.requests, "post", return_value=response
    ) as post:
        result = sms_gateway.send_sms_detailed("0551234567", "Hello from VireSender")

    assert result["status"] == "sent"
    assert result["sms_id"] == "SMS-12345"
    assert result["cost"] == 0.04
    call = post.call_args
    assert call.args[0] == "https://viresender.com/v1/sms/send"
    assert call.kwargs["headers"]["Authorization"] == "Bearer secret"
    assert call.kwargs["json"] == {
        "to": "233551234567", "message": "Hello from VireSender", "sender_id": "SMARTLIVING"
    }


def test_missing_key_is_reported_without_network_call():
    with patch.object(sms_gateway, "VIRESENDER_API_KEY", ""), patch.object(sms_gateway.requests, "post") as post:
        result = sms_gateway.send_sms_detailed("0551234567", "Hello")
    assert result["status"] == "not_configured"
    post.assert_not_called()


def test_provider_rejection_is_not_reported_as_sent():
    response = Mock(status_code=401)
    response.json.return_value = {"success": False, "message": "Invalid API key"}
    with patch.object(sms_gateway, "VIRESENDER_API_KEY", "bad"), patch.object(
        sms_gateway.requests, "post", return_value=response
    ):
        result = sms_gateway.send_sms_detailed("233551234567", "Hello")
    assert result["status"] == "failed"
    assert result["http_status"] == 401
    assert result["error"] == "Invalid API key"
