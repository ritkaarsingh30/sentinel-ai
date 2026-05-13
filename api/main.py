"""
FastAPI approval dashboard.

Endpoints:
  GET  /incidents/{incident_id}          — view full incident state
  POST /incidents/{incident_id}/approve  — resume graph with approval
  POST /incidents/{incident_id}/decline  — resume graph with decline
"""

from fastapi import FastAPI, HTTPException
from langgraph.types import Command

from agents.graph import build_graph
from agents.checkpointer import get_checkpointer

app = FastAPI(title="SentinelAI", version="0.1.0")

# Shared compiled graph — DynamoDB checkpointer in prod so the API can resume
# graphs that were interrupted inside Lambda.
_graph = build_graph(checkpointer=get_checkpointer())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    config = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")
    state = snapshot.values
    return {
        "incident_id": state.get("incident_id"),
        "alarm_name": state.get("alarm_name"),
        "status": _resolve_status(snapshot),
        "root_cause": state.get("root_cause"),
        "remediation_steps": state.get("remediation_steps", []),
        "log_findings": state.get("log_findings"),
        "metrics_findings": state.get("metrics_findings"),
        "incident_report": state.get("incident_report"),
        "human_approved": state.get("human_approved"),
    }


@app.post("/incidents/{incident_id}/approve")
def approve_incident(incident_id: str):
    thread_config = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(thread_config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")

    _graph.invoke(Command(resume=True), thread_config)
    return {"incident_id": incident_id, "status": "approved_and_resumed"}


@app.post("/incidents/{incident_id}/decline")
def decline_incident(incident_id: str):
    thread_config = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(thread_config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")

    _graph.invoke(Command(resume=False), thread_config)
    return {"incident_id": incident_id, "status": "declined"}


def _resolve_status(snapshot) -> str:
    if not snapshot:
        return "unknown"
    if snapshot.next:
        return "awaiting_approval"
    state = snapshot.values
    if state.get("incident_report"):
        return "resolved"
    if state.get("human_approved") is False and state.get("remediation_steps"):
        return "declined"
    return "in_progress"
