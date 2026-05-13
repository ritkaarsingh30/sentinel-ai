"""Reads CloudWatch log events for a given log group and time window.

Returns real data when USE_MOCK_DATA=false (requires AWS creds).
Returns realistic mock data when USE_MOCK_DATA=true (Week 1 local dev).
"""

import json
from datetime import datetime, timezone
import boto3
import config


# ---------------------------------------------------------------------------
# Mock data
# ---------------------------------------------------------------------------

_MOCK_LOG_EVENTS = [
    {
        "timestamp": "2026-05-07T02:13:44Z",
        "message": "[ERROR] RuntimeError: Connection pool exhausted for db-prod-1.cluster-abc.us-east-1.rds.amazonaws.com:5432",
    },
    {
        "timestamp": "2026-05-07T02:13:45Z",
        "message": "[ERROR] RuntimeError: Connection pool exhausted for db-prod-1.cluster-abc.us-east-1.rds.amazonaws.com:5432",
    },
    {
        "timestamp": "2026-05-07T02:13:46Z",
        "message": "[WARN] Retry attempt 1/3 for database connection",
    },
    {
        "timestamp": "2026-05-07T02:13:47Z",
        "message": "[ERROR] RuntimeError: Connection pool exhausted for db-prod-1.cluster-abc.us-east-1.rds.amazonaws.com:5432",
    },
    {
        "timestamp": "2026-05-07T02:13:48Z",
        "message": "[ERROR] HTTPException: 500 Internal Server Error — /api/v1/orders",
    },
    {
        "timestamp": "2026-05-07T02:13:48Z",
        "message": "[ERROR] HTTPException: 500 Internal Server Error — /api/v1/users",
    },
    {
        "timestamp": "2026-05-07T02:13:49Z",
        "message": "[ERROR] HTTPException: 500 Internal Server Error — /api/v1/products",
    },
    {
        "timestamp": "2026-05-07T02:13:52Z",
        "message": "[INFO] Lambda cold start completed in 1240ms",
    },
    {
        "timestamp": "2026-05-07T02:14:01Z",
        "message": "[ERROR] psycopg2.OperationalError: FATAL: remaining connection slots are reserved for non-replication superuser connections",
    },
    {
        "timestamp": "2026-05-07T02:14:02Z",
        "message": "[ERROR] Unhandled exception in request handler",
        "stack_trace": "Traceback (most recent call last):\n  File 'handler.py', line 88\npsycopg2.OperationalError: FATAL: too many connections",
    },
    {
        "timestamp": "2026-05-07T02:14:10Z",
        "message": "[ERROR] HTTPException: 500 Internal Server Error — /api/v1/checkout",
    },
    {
        "timestamp": "2026-05-07T02:14:15Z",
        "message": "[WARN] Max retries exceeded. Failing fast.",
    },
]


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_log_events(log_group: str, start_time: str, end_time: str) -> list[dict]:
    """Return log events from CloudWatch or mock data."""
    if config.USE_MOCK_DATA:
        return _MOCK_LOG_EVENTS

    client = boto3.client("logs", region_name=config.AWS_REGION)

    start_ms = int(datetime.fromisoformat(start_time.replace("Z", "+00:00")).timestamp() * 1000)
    end_ms = int(datetime.fromisoformat(end_time.replace("Z", "+00:00")).timestamp() * 1000)

    events = []
    kwargs = {
        "logGroupName": log_group,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": 200,
    }
    while True:
        response = client.filter_log_events(**kwargs)
        for e in response.get("events", []):
            ts = datetime.fromtimestamp(e["timestamp"] / 1000, tz=timezone.utc).isoformat()
            events.append({"timestamp": ts, "message": e["message"].strip()})
        next_token = response.get("nextToken")
        if not next_token or len(events) >= 200:
            break
        kwargs["nextToken"] = next_token

    return events


def format_logs_for_llm(events: list[dict]) -> str:
    """Serialize log events to a compact string for LLM context."""
    lines = [f"[{e['timestamp']}] {e['message']}" for e in events]
    return "\n".join(lines)
