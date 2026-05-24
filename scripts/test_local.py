"""
Local test runner.

Simulates a CloudWatch alarm firing. Runs the full agent graph with mock data,
pauses at human_approval, then resumes with a CLI prompt.

Usage:
  python scripts/test_local.py
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from datetime import datetime, timezone
from langgraph.types import Command

from agents.graph import build_graph, make_initial_state


MOCK_ALARM = {
    "alarm_name": "HighErrorRate-orders-api-prod",
    "alarm_description": "Lambda error rate exceeded 5% for 2 consecutive minutes",
    "log_group": "/aws/lambda/orders-api-prod",
    "metric_name": "Errors",
    "region": "us-east-1",
    "start_time": "2026-05-07T02:00:00Z",
    "end_time": "2026-05-07T02:20:00Z",
}


def separator(title: str = "") -> None:
    line = "─" * 60
    if title:
        print(f"\n{line}\n  {title}\n{line}")
    else:
        print(line)


def main():
    print("\n" + "=" * 60)
    print("  SentinelAI — Local Test Run")
    print("=" * 60)

    initial_state = make_initial_state(**MOCK_ALARM)
    incident_id = initial_state["incident_id"]
    thread_config = {"configurable": {"thread_id": incident_id}}

    graph = build_graph()

    print(f"\nIncident ID : {incident_id}")
    print(f"Alarm       : {MOCK_ALARM['alarm_name']}")
    print(f"Log group   : {MOCK_ALARM['log_group']}")
    print("\nStarting investigation...\n")

    # Run until human_approval interrupt
    result = graph.invoke(initial_state, thread_config)

    separator("LOG ANALYST FINDINGS")
    print(result.get("log_findings", "(none)"))

    separator("METRICS ANALYST FINDINGS")
    print(result.get("metrics_findings", "(none)"))

    separator("ROOT CAUSE")
    print(result.get("root_cause", "(none)"))

    separator("PROPOSED REMEDIATION")
    for step in result.get("remediation_steps", []):
        print(f"  {step}")

    separator()
    decision = input("\nApprove remediation? [y/N] ").strip().lower()
    approved = decision == "y"

    print(f"\nDecision: {'APPROVED' if approved else 'DECLINED'}")
    print("Resuming graph...\n")

    final = graph.invoke(Command(resume=approved), thread_config)

    if approved:
        separator("INCIDENT REPORT")
        import json
        report = final.get("incident_report", {})
        print(json.dumps(report, indent=2))
    else:
        print("Remediation declined. Incident closed without action.")

    separator()
    print(f"Done. Incident {incident_id} complete.\n")


if __name__ == "__main__":
    main()
