"""
Persistence helpers.

save_report_to_s3  — uploads the final incident report JSON to S3.
record_incident    — writes a summary row to the DynamoDB incidents table.

Both are no-ops when USE_MOCK_DATA is true so local test runs stay offline.
"""

import json
import boto3
from datetime import datetime, timezone

import config


def save_report_to_s3(incident_id: str, report: dict) -> None:
    if config.USE_MOCK_DATA:
        print(f"[S3] Mock mode — skipping S3 upload for {incident_id}")
        return
    if not config.S3_REPORTS_BUCKET:
        print("[S3] S3_REPORTS_BUCKET not set — skipping upload")
        return

    s3 = boto3.client("s3", region_name=config.AWS_REGION)
    key = f"{incident_id}.json"
    s3.put_object(
        Bucket=config.S3_REPORTS_BUCKET,
        Key=key,
        Body=json.dumps(report, indent=2),
        ContentType="application/json",
    )
    print(f"[S3] Report saved: s3://{config.S3_REPORTS_BUCKET}/{key}")


def record_incident(state: dict, status: str) -> None:
    """Write a summary row to the incidents table. status: 'resolved' | 'declined'."""
    if config.USE_MOCK_DATA:
        print(f"[DynamoDB] Mock mode — skipping incident record for {state.get('incident_id')}")
        return

    dynamodb = boto3.resource("dynamodb", region_name=config.AWS_REGION)
    table = dynamodb.Table(config.DYNAMODB_INCIDENTS_TABLE)

    item = {
        "incident_id": state["incident_id"],
        "alarm_name": state["alarm_name"],
        "status": status,
        "root_cause_summary": state.get("root_cause", "")[:500],
        "start_time": state["start_time"],
        "end_time": state["end_time"],
        "region": state["region"],
        "log_group": state["log_group"],
        "human_approved": state.get("human_approved", False),
        "recorded_at": datetime.now(timezone.utc).isoformat(),
    }
    table.put_item(Item=item)
    print(f"[DynamoDB] Incident {state['incident_id']} recorded with status={status}")
