import pytest

from view import _clean_customer_details


def test_customer_edit_accepts_exactly_ten_digit_phone():
    assert _clean_customer_details({
        "name": "Ama Mensah", "phone_number": "0530393625",
        "location": "Accra", "occupation": "Trader",
    }) == {
        "name": "Ama Mensah", "phone_number": "0530393625",
        "location": "Accra", "occupation": "Trader",
    }


@pytest.mark.parametrize("phone", ["530393625", "05303936251", "+233530393625", "053 039 3625", "abcdefghij"])
def test_customer_edit_rejects_non_ten_digit_phone(phone):
    with pytest.raises(ValueError, match="exactly 10 digits"):
        _clean_customer_details({"name": "Ama Mensah", "phone_number": phone})


def test_customer_edit_requires_name():
    with pytest.raises(ValueError, match="name is required"):
        _clean_customer_details({"name": " ", "phone_number": "0530393625"})


def test_customer_edit_accepts_cloudflare_image():
    result = _clean_customer_details({
        "name": "Ama", "phone_number": "0530393625",
        "image_url": "https://imagedelivery.net/example/image/public", "cf_image_id": "image",
    })
    assert result["image_url"] == "https://imagedelivery.net/example/image/public"
    assert result["cf_image_id"] == "image"


def test_customer_edit_rejects_untrusted_image_url():
    with pytest.raises(ValueError, match="Cloudflare Images"):
        _clean_customer_details({
            "name": "Ama", "phone_number": "0530393625", "image_url": "https://example.com/photo.jpg",
        })
