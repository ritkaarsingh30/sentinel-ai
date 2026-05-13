"""
Victim app Lambda — simulates a broken production service.

Controlled by the FAIL_RATE env var (default 0.0 = healthy).
Set FAIL_RATE=0.9 to make 90% of invocations throw an error,
which drives up the AWS/Lambda Errors metric and trips the CloudWatch alarm.
"""

import os
import random
import json

FAIL_RATE = float(os.getenv("FAIL_RATE", "0.0"))

_ERRORS = [
    "RuntimeError: Connection pool exhausted for db-prod.cluster.us-east-1.rds.amazonaws.com:5432",
    "psycopg2.OperationalError: FATAL: remaining connection slots are reserved for non-replication superuser connections",
    "TimeoutError: Read timed out after 30s waiting for database response",
    "RuntimeError: Redis cache unavailable — falling back to DB overloaded path",
    "OSError: [Errno 110] Connection timed out connecting to downstream service",
]


def handler(event, context):
    if random.random() < FAIL_RATE:
        # Raise a real exception — Lambda records this as an Error metric in CloudWatch
        raise RuntimeError(random.choice(_ERRORS))

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "ok", "requestId": context.aws_request_id}),
    }
