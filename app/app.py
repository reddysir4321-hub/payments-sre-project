"""Payments API - a small payment transaction service.

Endpoints
  GET  /health          liveness: is the process running?
  GET  /ready           readiness: can we reach the database?
  POST /accounts        create an account with an opening balance
  GET  /accounts/<id>   read an account
  POST /payments        move money between two accounts
  GET  /payments/<id>   read one payment record
  GET  /metrics         Prometheus metrics
"""
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import psycopg2
from flask import Flask, Response, g, jsonify, request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from werkzeug.exceptions import HTTPException

app = Flask(__name__)
APP_VERSION = os.environ.get("APP_VERSION", "dev")
MAX_AMOUNT = Decimal("1000000.00")

# Everything is logged to stdout. In Docker and Kubernetes the platform
# collects stdout, so the app never manages log files itself.
logging.basicConfig(stream=sys.stdout, level=logging.INFO, format="%(message)s")
audit_log = logging.getLogger("audit")

# ---------------------------------------------------------------- metrics
# Counter   = a number that only goes up (requests, payments).
# Histogram = buckets of observed values (how long requests took).
REQUESTS = Counter(
    "http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"]
)
LATENCY = Histogram(
    "http_request_duration_seconds", "Request duration in seconds", ["endpoint"]
)
PAYMENTS = Counter("payments_total", "Payments processed, by result", ["status"])

# Health checks and metric scrapes are not real user traffic, so they are
# left out of the request metrics. Otherwise they would hide a bad error rate.
NOT_USER_TRAFFIC = {"/health", "/ready", "/metrics"}


@app.before_request
def start_timer():
    g.start = time.perf_counter()


@app.after_request
def record_metrics(response):
    endpoint = request.url_rule.rule if request.url_rule else "unmatched"
    if endpoint not in NOT_USER_TRAFFIC:
        REQUESTS.labels(request.method, endpoint, str(response.status_code)).inc()
        LATENCY.labels(endpoint).observe(time.perf_counter() - g.start)
    return response


# --------------------------------------------------------------- database
def get_connection():
    """Open a new database connection. Settings come from environment
    variables, so the same image runs in Compose, CI and Kubernetes."""
    return psycopg2.connect(
        host=os.environ.get("DB_HOST", "localhost"),
        port=os.environ.get("DB_PORT", "5432"),
        dbname=os.environ.get("DB_NAME", "payments"),
        user=os.environ.get("DB_USER", "payments"),
        password=os.environ.get("DB_PASSWORD", ""),
        connect_timeout=3,
    )


SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id         SERIAL PRIMARY KEY,
    owner      TEXT NOT NULL,
    balance    NUMERIC(12, 2) NOT NULL CHECK (balance >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS transactions (
    id           TEXT PRIMARY KEY,
    from_account INTEGER NOT NULL REFERENCES accounts (id),
    to_account   INTEGER NOT NULL REFERENCES accounts (id),
    amount       NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    status       TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""


def init_db(retries=30, delay=2):
    """Create the tables if they do not exist.

    The database container may start after the app, so we retry instead of
    crashing on the first failure. If it never comes up we raise, the
    container exits, and Docker or Kubernetes restarts it.
    """
    for attempt in range(1, retries + 1):
        try:
            conn = get_connection()
            try:
                cur = conn.cursor()
                cur.execute(SCHEMA)
                conn.commit()
            finally:
                conn.close()
            app.logger.info("database ready")
            return
        except psycopg2.Error as err:
            app.logger.warning("database not ready (attempt %s): %s", attempt, err)
            time.sleep(delay)
    raise RuntimeError("could not connect to the database")


# ----------------------------------------------------------------- helpers
def parse_amount(value):
    """Turn user input into a Decimal with at most 2 decimal places.
    Money is never stored as float: 0.1 + 0.2 is not exactly 0.3 in float."""
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        return None
    if not amount.is_finite() or amount != amount.quantize(Decimal("0.01")):
        return None
    return amount


def audit(event, **fields):
    """Write one JSON line per business event. This is the audit trail:
    every payment can be traced by its transaction_id."""
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "event": event,
        **fields,
    }
    audit_log.info(json.dumps(record))


def error(message, status):
    return jsonify(error=message), status


@app.errorhandler(Exception)
def handle_exception(err):
    if isinstance(err, HTTPException):
        return error(err.description, err.code)
    app.logger.exception("unhandled error")
    return error("internal server error", 500)


# ------------------------------------------------------------------ routes
@app.get("/health")
def health():
    return jsonify(status="ok", version=APP_VERSION)


@app.get("/ready")
def ready():
    try:
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
        finally:
            conn.close()
    except psycopg2.Error:
        return jsonify(status="database unavailable"), 503
    return jsonify(status="ready")


@app.get("/metrics")
def metrics():
    return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)


@app.post("/accounts")
def create_account():
    data = request.get_json(silent=True) or {}
    owner = data.get("owner")
    balance = parse_amount(data.get("balance", "0.00"))

    if not isinstance(owner, str) or not owner.strip():
        return error("owner is required", 400)
    if balance is None or balance < 0 or balance > MAX_AMOUNT:
        return error("balance must be between 0 and 1000000, with at most 2 decimals", 400)

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO accounts (owner, balance) VALUES (%s, %s) RETURNING id",
            (owner.strip(), balance),
        )
        account_id = cur.fetchone()[0]
        conn.commit()
    finally:
        conn.close()

    audit("account_created", account_id=account_id, opening_balance=str(balance))
    return jsonify(id=account_id, owner=owner.strip(), balance=str(balance)), 201


@app.get("/accounts/<int:account_id>")
def get_account(account_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, owner, balance, created_at FROM accounts WHERE id = %s",
            (account_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return error("account not found", 404)
    return jsonify(
        id=row[0], owner=row[1], balance=str(row[2]), created_at=row[3].isoformat()
    )


@app.post("/payments")
def create_payment():
    data = request.get_json(silent=True) or {}
    from_id = data.get("from_account")
    to_id = data.get("to_account")
    amount = parse_amount(data.get("amount"))

    if type(from_id) is not int or type(to_id) is not int:
        return error("from_account and to_account must be account ids", 400)
    if from_id == to_id:
        return error("from_account and to_account must be different", 400)
    if amount is None or amount <= 0 or amount > MAX_AMOUNT:
        return error("amount must be greater than 0, with at most 2 decimals", 400)

    payment_id = str(uuid.uuid4())
    insert_sql = (
        "INSERT INTO transactions (id, from_account, to_account, amount, status) "
        "VALUES (%s, %s, %s, %s, %s)"
    )

    conn = get_connection()
    try:
        cur = conn.cursor()

        # One database transaction: either every statement below is saved,
        # or none of them is. Money can never leave one account without
        # arriving in the other.
        #
        # FOR UPDATE locks both rows until we commit, so two payments from
        # the same account cannot both spend the same balance.
        # ORDER BY id means every request locks rows in the same order,
        # which stops two requests from deadlocking each other.
        cur.execute(
            "SELECT id, balance FROM accounts WHERE id IN (%s, %s) "
            "ORDER BY id FOR UPDATE",
            (from_id, to_id),
        )
        balances = dict(cur.fetchall())

        if from_id not in balances or to_id not in balances:
            conn.rollback()
            return error("account not found", 404)

        if balances[from_id] < amount:
            # A rejected payment is still recorded, so the audit trail
            # shows every attempt and not only the successful ones.
            cur.execute(insert_sql, (payment_id, from_id, to_id, amount, "failed"))
            conn.commit()
            PAYMENTS.labels("failed").inc()
            audit("payment", transaction_id=payment_id, from_account=from_id,
                  to_account=to_id, amount=str(amount), status="failed",
                  reason="insufficient funds")
            return jsonify(id=payment_id, status="failed",
                           error="insufficient funds"), 422

        cur.execute(
            "UPDATE accounts SET balance = balance - %s WHERE id = %s",
            (amount, from_id),
        )
        cur.execute(
            "UPDATE accounts SET balance = balance + %s WHERE id = %s",
            (amount, to_id),
        )
        cur.execute(insert_sql, (payment_id, from_id, to_id, amount, "completed"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    PAYMENTS.labels("completed").inc()
    audit("payment", transaction_id=payment_id, from_account=from_id,
          to_account=to_id, amount=str(amount), status="completed")
    return jsonify(id=payment_id, from_account=from_id, to_account=to_id,
                   amount=str(amount), status="completed"), 201


@app.get("/payments/<payment_id>")
def get_payment(payment_id):
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, from_account, to_account, amount, status, created_at "
            "FROM transactions WHERE id = %s",
            (payment_id,),
        )
        row = cur.fetchone()
    finally:
        conn.close()

    if row is None:
        return error("payment not found", 404)
    return jsonify(id=row[0], from_account=row[1], to_account=row[2],
                   amount=str(row[3]), status=row[4], created_at=row[5].isoformat())


# Runs once when the app starts (gunicorn imports this file).
init_db()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
