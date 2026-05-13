from typing import Annotated, TypedDict
from langgraph.graph.message import add_messages
from langchain_core.messages import BaseMessage


class IncidentState(TypedDict):
    # Incident metadata (set at entry point)
    incident_id: str
    alarm_name: str
    alarm_description: str
    log_group: str
    metric_name: str
    region: str
    start_time: str   # ISO 8601
    end_time: str     # ISO 8601

    # Agent outputs (filled progressively)
    log_findings: str
    metrics_findings: str
    root_cause: str
    remediation_steps: list[str]
    incident_report: dict

    # Supervisor routing — which node runs next
    next_agent: str

    # Flow flags
    human_approved: bool

    # LLM message history
    messages: Annotated[list[BaseMessage], add_messages]
