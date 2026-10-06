"""Tests run against a real PostgreSQL database (see the CI workflow).
Each test creates its own accounts, so tests do not depend on each other."""
from decimal import Decimal

import pytest

from app import app


@pytest.fixture
def client():
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def make_account(client, balance="100.00", owner="test user"):
    response = client.post("/accounts", json={"owner": owner, "balance": balance})
    assert response.status_code == 201
    return response.get_json()["id"]


def balance_of(client, account_id):
    return Decimal(client.get(f"/accounts/{account_id}").get_json()["balance"])


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json()["status"] == "ok"


def test_ready_when_database_is_up(client):
    assert client.get("/ready").status_code == 200


def test_create_and_read_account(client):
    account_id = make_account(client, balance="250.50", owner="Asha")
    body = client.get(f"/accounts/{account_id}").get_json()
    assert body["owner"] == "Asha"
    assert Decimal(body["balance"]) == Decimal("250.50")


def test_account_needs_owner(client):
    assert client.post("/accounts", json={"balance": "10.00"}).status_code == 400


def test_account_rejects_negative_balance(client):
    response = client.post("/accounts", json={"owner": "x", "balance": "-1.00"})
    assert response.status_code == 400


def test_unknown_account_is_404(client):
    assert client.get("/accounts/999999999").status_code == 404


def test_payment_moves_money(client):
    sender = make_account(client, balance="100.00")
    receiver = make_account(client, balance="5.00")

    response = client.post(
        "/payments",
        json={"from_account": sender, "to_account": receiver, "amount": "30.25"},
    )

    assert response.status_code == 201
    assert response.get_json()["status"] == "completed"
    assert balance_of(client, sender) == Decimal("69.75")
    assert balance_of(client, receiver) == Decimal("35.25")


def test_payment_is_recorded(client):
    sender = make_account(client)
    receiver = make_account(client)
    payment_id = client.post(
        "/payments",
        json={"from_account": sender, "to_account": receiver, "amount": "1.00"},
    ).get_json()["id"]

    body = client.get(f"/payments/{payment_id}").get_json()
    assert body["status"] == "completed"
    assert Decimal(body["amount"]) == Decimal("1.00")


def test_insufficient_funds_changes_nothing(client):
    sender = make_account(client, balance="10.00")
    receiver = make_account(client, balance="0.00")

    response = client.post(
        "/payments",
        json={"from_account": sender, "to_account": receiver, "amount": "10.01"},
    )

    assert response.status_code == 422
    assert balance_of(client, sender) == Decimal("10.00")
    assert balance_of(client, receiver) == Decimal("0.00")
    # the failed attempt is still in the audit trail
    payment_id = response.get_json()["id"]
    assert client.get(f"/payments/{payment_id}").get_json()["status"] == "failed"


def test_payment_to_same_account_is_rejected(client):
    account = make_account(client)
    response = client.post(
        "/payments",
        json={"from_account": account, "to_account": account, "amount": "1.00"},
    )
    assert response.status_code == 400


@pytest.mark.parametrize("amount", ["0", "-5.00", "1.999", "abc", None])
def test_payment_rejects_bad_amounts(client, amount):
    sender = make_account(client)
    receiver = make_account(client)
    response = client.post(
        "/payments",
        json={"from_account": sender, "to_account": receiver, "amount": amount},
    )
    assert response.status_code == 400


def test_payment_to_unknown_account_is_404(client):
    sender = make_account(client)
    response = client.post(
        "/payments",
        json={"from_account": sender, "to_account": 999999999, "amount": "1.00"},
    )
    assert response.status_code == 404
    assert balance_of(client, sender) == Decimal("100.00")


def test_unknown_payment_is_404(client):
    assert client.get("/payments/does-not-exist").status_code == 404


def test_metrics_endpoint(client):
    make_account(client)
    response = client.get("/metrics")
    assert response.status_code == 200
    assert b"http_requests_total" in response.data
    assert b"payments_total" in response.data
