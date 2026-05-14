"""
Victim app Lambda — simulates a broken production service.

Controlled by two env vars:
  FAIL_RATE     float 0.0–1.0  (default 0.0 = healthy)
  FAILURE_MODE  string         (default "random")

FAILURE_MODE values:
  random            — picks from all error pools (original behaviour)
  sql_error         — bad SQL queries, deadlocks, index corruption
  memory_leak       — OOM kills, heap exhaustion, GC pressure
  timeout_cascade   — cascading timeouts across DB → cache → API chain
  dependency_failure — upstream service unavailable (payment, auth, S3)
"""

import os
import random
import json

FAIL_RATE    = float(os.getenv("FAIL_RATE", "0.0"))
FAILURE_MODE = os.getenv("FAILURE_MODE", "random")

_POOLS = {
    "sql_error": [
        "psycopg2.errors.DeadlockDetected: deadlock detected on table orders — transaction rolled back",
        "sqlalchemy.exc.OperationalError: (psycopg2.OperationalError) SSL connection has been closed unexpectedly",
        "psycopg2.errors.QueryCanceled: ERROR: canceling statement due to statement timeout (30000ms)",
        "sqlalchemy.exc.ProgrammingError: column 'user_id' does not exist in index idx_orders_user",
        "psycopg2.OperationalError: FATAL: connection pool exhausted (max_connections=100, active=100)",
    ],
    "memory_leak": [
        "MemoryError: Python heap space exhausted — unable to allocate 512MB for result set",
        "RuntimeError: Lambda container OOM kill — used 512MB / 512MB limit",
        "ResourceWarning: unclosed file handles (1847 leaked) — file descriptor limit approaching",
        "MemoryError: cannot allocate array — numpy array requires 2.1GB, only 340MB available",
        "gc.collect() called 3200 times in last 60s — severe garbage collection pressure detected",
    ],
    "timeout_cascade": [
        "TimeoutError: DB query timeout after 30s — upstream cache miss forced full table scan",
        "ReadTimeout: Redis cache at redis-prod.cache.us-east-1.amazonaws.com timed out after 5s",
        "requests.exceptions.ConnectTimeout: upstream payment-service /v2/charge timed out (10s)",
        "socket.timeout: downstream analytics pipeline unresponsive for 45s — circuit breaker OPEN",
        "TimeoutError: cascade — DB(30s) → cache(5s) → API gateway(60s) all timed out sequentially",
    ],
    "dependency_failure": [
        "botocore.exceptions.EndpointConnectionError: S3 PutObject failed — endpoint unreachable",
        "ConnectionRefusedError: auth-service at auth.internal:8080 refused connection",
        "requests.exceptions.HTTPError: 503 Service Unavailable from payment-gateway.stripe-relay.internal",
        "RuntimeError: feature-flags service returned 502 — all flags defaulting to OFF",
        "boto3.exceptions.S3UploadFailedError: Failed to upload to s3://prod-uploads — Access Denied",
    ],
    "random": [
        "RuntimeError: Connection pool exhausted for db-prod.cluster.us-east-1.rds.amazonaws.com:5432",
        "psycopg2.OperationalError: FATAL: remaining connection slots are reserved for non-replication superuser connections",
        "TimeoutError: Read timed out after 30s waiting for database response",
        "RuntimeError: Redis cache unavailable — falling back to DB overloaded path",
        "OSError: [Errno 110] Connection timed out connecting to downstream service",
        "psycopg2.errors.DeadlockDetected: deadlock detected on table orders",
        "MemoryError: Lambda container OOM kill — used 512MB / 512MB limit",
        "requests.exceptions.HTTPError: 503 Service Unavailable from payment-gateway",
    ],
}

_active_pool = _POOLS.get(FAILURE_MODE, _POOLS["random"])


def handler(event, context):
    if random.random() < FAIL_RATE:
        raise RuntimeError(random.choice(_active_pool))

    return {
        "statusCode": 200,
        "body": json.dumps({"status": "ok", "requestId": context.aws_request_id}),
    }
