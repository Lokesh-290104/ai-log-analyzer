"""Deterministic demo logs: two hours of a small shop's services with one incident.

The incident: at 10:42 UTC the payments database pool starts timing out, payments errors
spike for ~12 minutes, orders see upstream failures, and a Python traceback is logged.
Mixed formats (plain text, JSON lines, logfmt) exercise the parser. Seeded, so every
call returns the same text and tests can assert exact numbers.
"""

import json
import random
from datetime import UTC, datetime, timedelta

START = datetime(2026, 9, 28, 9, 30, tzinfo=UTC)
DURATION_MIN = 120
INCIDENT_START = START + timedelta(minutes=72)  # 10:42
INCIDENT_END = INCIDENT_START + timedelta(minutes=12)

_NORMAL = {
    "api-gateway": [
        ("INFO", "GET /api/products 200 in {ms}ms"),
        ("INFO", "POST /api/cart 201 in {ms}ms"),
        ("WARN", "Slow request GET /api/search took {slow}ms"),
    ],
    "auth": [
        ("INFO", "User {user} logged in"),
        ("INFO", "Token refreshed for user {user}"),
        ("WARN", "Failed login attempt for user {user}"),
    ],
    "payments": [
        ("INFO", "Charge {order} captured amount={amount}"),
        ("ERROR", "Card declined for order {order}"),
    ],
    "orders": [
        ("INFO", "Order {order} created"),
        ("INFO", "Order {order} shipped"),
    ],
    "inventory": [
        ("INFO", "Stock reserved for order {order}"),
        ("WARN", "Low stock for SKU-{sku}"),
    ],
    "notifications": [
        ("INFO", "Email sent to user {user}"),
        ("ERROR", "SMTP connection refused by mail-{node}.internal"),
    ],
}
_WEIGHTS = {"api-gateway": 5, "auth": 2, "payments": 2, "orders": 2, "inventory": 1, "notifications": 1}
_RARE_ERRORS = {"Card declined for order {order}": 0.12, "SMTP connection refused by mail-{node}.internal": 0.05}

_TRACEBACK = """Traceback (most recent call last):
  File "/srv/payments/db.py", line 88, in acquire
    conn = await pool.acquire(timeout=5)
TimeoutError: pool exhausted (max=20)"""


def _fill(template: str, rng: random.Random) -> str:
    return template.format(
        ms=rng.randint(8, 240), slow=rng.randint(1500, 4000), user=rng.randint(1000, 9999),
        order=f"ORD-{rng.randint(10000, 99999)}", amount=f"{rng.randint(5, 400)}.{rng.randint(0, 99):02d}",
        sku=rng.randint(100, 999), node=rng.randint(1, 3),
    )


def _format(ts: datetime, level: str, service: str, message: str, style: int) -> str:
    iso = ts.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts.microsecond // 1000:03d}Z"
    if style == 0:
        return f"{iso} {level} [{service}] {message}"
    if style == 1:
        return json.dumps({"timestamp": iso, "level": level.lower(), "service": service, "message": message})
    return f'ts={iso} level={level.lower()} service={service} msg="{message}"'


def generate_sample_logs(seed: int = 42) -> str:
    rng = random.Random(seed)
    services = list(_WEIGHTS)
    weights = [_WEIGHTS[s] for s in services]
    lines: list[str] = []
    ts = START
    end = START + timedelta(minutes=DURATION_MIN)
    traceback_logged = False
    while ts < end:
        ts += timedelta(milliseconds=rng.randint(800, 4200))
        in_incident = INCIDENT_START <= ts < INCIDENT_END
        # Services log in their own style: gateway/auth plain, payments/orders JSON, the rest logfmt.
        if in_incident and rng.random() < 0.55:
            if rng.random() < 0.7:
                service, level = "payments", "ERROR"
                message = f"Database timeout after 5000ms acquiring connection (pool=payments-db, order ORD-{rng.randint(10000, 99999)})"
            else:
                service, level = "orders", "ERROR"
                message = "Upstream payments returned 503 for checkout"
        else:
            service = rng.choices(services, weights)[0]
            options = _NORMAL[service]
            level, template = rng.choice(options)
            # Keep normal-time errors and warnings rare.
            if level == "ERROR" and rng.random() > _RARE_ERRORS.get(template, 0.1):
                level, template = options[0]
            elif level == "WARN" and rng.random() > 0.3:
                level, template = options[0]
            message = _fill(template, rng)
        style = {"api-gateway": 0, "auth": 0, "payments": 1, "orders": 1}.get(service, 2)
        lines.append(_format(ts, level, service, message, style))
        if in_incident and service == "payments" and not traceback_logged:
            lines[-1] = _format(ts, "FATAL", "payments", "Unhandled error in charge worker", 0)
            lines.append(_TRACEBACK)
            traceback_logged = True
    lines.append("--- log rotated ---")
    return "\n".join(lines) + "\n"
