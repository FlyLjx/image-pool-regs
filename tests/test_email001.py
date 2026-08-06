from __future__ import annotations

from app.registration.email001 import Email001AutoPurchaseClient, extract_mailboxes


def test_extract_mailboxes_accepts_nested_delivery_text():
    payload = {
        "status_code": 0,
        "data": {
            "order_no": "ORDER-1",
            "delivery": "one@outlook.com----pw----client-1----refresh-1\n"
            "two@outlook.com----pw----client-2----refresh-2",
        },
    }

    items = extract_mailboxes(payload)

    assert [item["email"] for item in items] == ["one@outlook.com", "two@outlook.com"]
    assert items[0]["client_id"] == "client-1"


def test_auto_purchase_orders_once_and_imports_returned_mailboxes():
    client = Email001AutoPurchaseClient({
        "email001_auto_purchase": True,
        "email001_api_key": "api-key",
        "email001_sku_id": 14,
        "email001_quantity": 100,
    })
    calls = []
    available = {"slots": 0}

    class Response:
        status_code = 200
        text = "payload"

        @staticmethod
        def json():
            return {
                "status_code": 0,
                "msg": "success",
                "data": {
                    "content": "one@outlook.com----pw----client-1----refresh-1",
                },
            }

    class Session:
        @staticmethod
        def request(method, url, **kwargs):
            calls.append((method, url, kwargs))
            return Response()

        @staticmethod
        def close():
            pass

    client.session = Session()

    def import_payload(items):
        assert len(items) == 1
        available["slots"] = 6
        return {"added": 1}

    try:
        result = client.purchase_and_import(
            available_slots=lambda: available["slots"],
            import_payload=import_payload,
        )
    finally:
        client.close()

    assert result["purchased"] is True
    assert result["available_slots"] == 6
    assert len(calls) == 1
    assert calls[0][2]["json"] == {"api_key": "api-key", "sku_id": 14, "quantity": 100}
