"""
AWS Lambda entry point.

Triggered by SQS. Handles two message formats:
  1. Real EventBridge CloudWatch Alarm State Change event (production)
  2. Simplified JSON (manual testing / scripts/trigger_test.py)

Runs the LangGraph agent graph until the human_approval interrupt,
then emails the engineer via SNS.
"""

import json
import boto3
from datetime import datetime, timedelta, timezone

import config
from agents.graph import build_graph, make_initial_state
from agents.checkpointer import get_checkpointer


def _parse_body(body: dict) -> dict:
    """Normalise either event format into the fields make_initial_state expects."""

    # Real EventBridge CloudWatch Alarm State Change
    if body.get("source") == "aws.cloudwatch":
        detail = body["detail"]
        alarm_name = detail["alarmName"]
        alarm_desc = detail.get("configuration", {}).get("description", "")
        region = body.get("region", config.AWS_REGION)

        log_group = "/aws/lambda/unknown"
        metric_name = "Errors"
        for m in detail.get("configuration", {}).get("metrics", []):
            ms = m.get("metricStat", {})
            dims = ms.get("metric", {}).get("dimensions", {})
            fn = dims.get("FunctionName", "")
            if fn:
                log_group = f"/aws/lambda/{fn}"
            metric_name = ms.get("metric", {}).get("name", metric_name)

        # 30-minute window ending when the alarm fired
        ts = detail["state"].get("timestamp", "")
        try:
            alarm_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except Exception:
            alarm_time = datetime.now(timezone.utc)

        return {
            "alarm_name": alarm_name,
            "alarm_description": alarm_desc,
            "log_group": log_group,
            "metric_name": metric_name,
            "region": region,
            "start_time": (alarm_time - timedelta(minutes=30)).isoformat(),
            "end_time": alarm_time.isoformat(),
        }

    # Simplified direct format used by trigger_test.py
    return body


def _send_approval_email(incident_id: str, root_cause: str, remediation_steps: list[str]) -> None:
    if not config.SNS_ALERT_TOPIC_ARN:
        print("[SNS] SNS_ALERT_TOPIC_ARN not set — skipping email")
        return

    steps_text = "\n".join(f"  {s}" for s in remediation_steps)
    message = f"""SentinelAI — Incident Requires Your Approval
{"=" * 50}

Incident ID: {incident_id}

ROOT CAUSE:
{root_cause}

PROPOSED REMEDIATION:
{steps_text}

{"=" * 50}
APPROVE:  {config.API_BASE_URL}/incidents/{incident_id}/approve
DECLINE:  {config.API_BASE_URL}/incidents/{incident_id}/decline

Review full details: {config.API_BASE_URL}/incidents/{incident_id}
"""

    # In Lambda, credentials come from the execution role — no explicit keys needed
    sns = boto3.client("sns", region_name=config.AWS_REGION)
    sns.publish(
        TopicArn=config.SNS_ALERT_TOPIC_ARN,
        Subject=f"[SentinelAI] Action Required — {incident_id}",
        Message=message,
    )
    print(f"[SNS] Approval email sent for {incident_id}")


def handler(event: dict, context) -> dict:
    """Lambda entry point — processes one SQS record per invocation."""
    records = event.get("Records", [])
    if not records:
        return {"statusCode": 200, "body": "no records"}

    body = json.loads(records[0]["body"])
    parsed = _parse_body(body)
    print(f"[Lambda] Alarm: {parsed.get('alarm_name')} | Log group: {parsed.get('log_group')}")

    initial_state = make_initial_state(
        alarm_name=parsed["alarm_name"],
        alarm_description=parsed.get("alarm_description", ""),
        log_group=parsed["log_group"],
        metric_name=parsed.get("metric_name", "Errors"),
        region=parsed.get("region", config.AWS_REGION),
        start_time=parsed["start_time"],
        end_time=parsed["end_time"],
    )

    incident_id = initial_state["incident_id"]
    compiled_graph = build_graph(checkpointer=get_checkpointer())

    result = compiled_graph.invoke(
        initial_state,
        {"configurable": {"thread_id": incident_id}},
    )

    print(f"[Lambda] Investigation complete — awaiting approval for {incident_id}")

    _send_approval_email(
        incident_id=incident_id,
        root_cause=result.get("root_cause", ""),
        remediation_steps=result.get("remediation_steps", []),
    )

    return {
        "statusCode": 200,
        "body": json.dumps({"incident_id": incident_id, "status": "awaiting_approval"}),
    }
