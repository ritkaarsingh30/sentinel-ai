"""Reads CloudWatch metric datapoints for a given metric and time window.

Returns real data when USE_MOCK_DATA=false (requires AWS creds).
Returns realistic mock data when USE_MOCK_DATA=true (Week 1 local dev).
"""

from datetime import datetime, timedelta, timezone
import boto3
import config


# ---------------------------------------------------------------------------
# Mock data — shows a clear error-rate spike starting at 02:13
# ---------------------------------------------------------------------------

def _build_mock_datapoints() -> list[dict]:
    base = datetime(2026, 5, 7, 2, 0, 0, tzinfo=timezone.utc)
    points = []
    # Healthy baseline: error rate ~0.5%, p99 latency ~120ms
    for i in range(13):
        points.append({
            "timestamp": (base + timedelta(minutes=i)).isoformat(),
            "error_rate_percent": round(0.3 + (i * 0.05), 2),
            "p99_latency_ms": 110 + (i * 2),
            "invocation_count": 450 + (i * 10),
        })
    # Spike starts at minute 13 (02:13)
    spike_errors = [42.1, 78.4, 81.2, 83.0, 80.5, 79.1, 77.8]
    spike_latency = [2100, 4800, 6200, 6500, 6300, 6100, 5900]
    spike_invocations = [460, 120, 80, 75, 80, 90, 95]
    for i, (err, lat, inv) in enumerate(zip(spike_errors, spike_latency, spike_invocations)):
        points.append({
            "timestamp": (base + timedelta(minutes=13 + i)).isoformat(),
            "error_rate_percent": err,
            "p99_latency_ms": lat,
            "invocation_count": inv,
        })
    return points


_MOCK_DATAPOINTS = _build_mock_datapoints()


# ---------------------------------------------------------------------------
# Public interface
# ---------------------------------------------------------------------------

def get_metric_datapoints(
    metric_name: str,
    namespace: str,
    log_group: str,
    start_time: str,
    end_time: str,
) -> list[dict]:
    """Return metric datapoints from CloudWatch or mock data."""
    if config.USE_MOCK_DATA:
        return _MOCK_DATAPOINTS

    client = boto3.client("cloudwatch", region_name=config.AWS_REGION)

    start = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
    end = datetime.fromisoformat(end_time.replace("Z", "+00:00"))

    response = client.get_metric_statistics(
        Namespace=namespace,
        MetricName=metric_name,
        Dimensions=[{"Name": "FunctionName", "Value": log_group.lstrip("/")}],
        StartTime=start,
        EndTime=end,
        Period=60,
        Statistics=["Average", "Maximum", "SampleCount"],
    )

    points = []
    for dp in sorted(response["Datapoints"], key=lambda x: x["Timestamp"]):
        points.append({
            "timestamp": dp["Timestamp"].isoformat(),
            "average": round(dp.get("Average", 0), 2),
            "maximum": round(dp.get("Maximum", 0), 2),
            "sample_count": int(dp.get("SampleCount", 0)),
        })
    return points


def format_metrics_for_llm(datapoints: list[dict]) -> str:
    """Serialize metric datapoints to a compact string for LLM context."""
    lines = []
    for dp in datapoints:
        parts = [f"[{dp['timestamp']}]"]
        for k, v in dp.items():
            if k != "timestamp":
                parts.append(f"{k}={v}")
        lines.append("  ".join(parts))
    return "\n".join(lines)
