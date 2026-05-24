"""
FastAPI approval dashboard — deployed as a Lambda Function URL.

Endpoints:
  GET  /health
  GET  /incidents/{incident_id}               — view full incident state (JSON)
  GET  /incidents/{incident_id}/approve       — confirmation page (browser-friendly)
  POST /incidents/{incident_id}/approve       — actually resume graph with approval
  GET  /incidents/{incident_id}/decline       — confirmation page (browser-friendly)
  POST /incidents/{incident_id}/decline       — actually resume graph with decline

Email links point to the GET endpoints so clicking them shows a confirmation
page rather than immediately taking an action. The confirmation page POSTs.
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from langgraph.types import Command

from agents.graph import build_graph
from agents.checkpointer import get_checkpointer

app = FastAPI(title="SentinelAI", version="1.0.0")

_graph = build_graph(checkpointer=get_checkpointer())


# ── Health ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


# ── Incident detail ───────────────────────────────────────────────────────────

@app.get("/incidents/{incident_id}")
def get_incident(incident_id: str):
    cfg = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(cfg)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")
    state = snapshot.values
    return {
        "incident_id":      state.get("incident_id"),
        "alarm_name":       state.get("alarm_name"),
        "status":           _resolve_status(snapshot),
        "root_cause":       state.get("root_cause"),
        "remediation_steps": state.get("remediation_steps", []),
        "log_findings":     state.get("log_findings"),
        "metrics_findings": state.get("metrics_findings"),
        "incident_report":  state.get("incident_report"),
        "human_approved":   state.get("human_approved"),
    }


# ── Approve ───────────────────────────────────────────────────────────────────

@app.get("/incidents/{incident_id}/approve", response_class=HTMLResponse)
def approve_confirm(incident_id: str):
    """Browser lands here from the email link — shows a confirmation page."""
    return _confirm_page(
        incident_id=incident_id,
        action="approve",
        heading="Approve Remediation",
        body_color="#2d6a2d",
        button_label="✓ Yes, Approve & Generate Report",
    )


@app.post("/incidents/{incident_id}/approve", response_class=HTMLResponse)
def approve_incident(incident_id: str):
    cfg = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(cfg)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")

    _graph.invoke(Command(resume=True), cfg)
    return _result_page(
        heading="✓ Approved",
        message=f"Incident <b>{incident_id}</b> approved. The report agent is generating the final report and saving it to S3.",
        color="#2d6a2d",
    )


# ── Decline ───────────────────────────────────────────────────────────────────

@app.get("/incidents/{incident_id}/decline", response_class=HTMLResponse)
def decline_confirm(incident_id: str):
    return _confirm_page(
        incident_id=incident_id,
        action="decline",
        heading="Decline Remediation",
        body_color="#8b2020",
        button_label="✗ Yes, Decline",
    )


@app.post("/incidents/{incident_id}/decline", response_class=HTMLResponse)
def decline_incident(incident_id: str):
    cfg = {"configurable": {"thread_id": incident_id}}
    snapshot = _graph.get_state(cfg)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail="Incident not found")

    _graph.invoke(Command(resume=False), cfg)
    return _result_page(
        heading="✗ Declined",
        message=f"Incident <b>{incident_id}</b> declined. No remediation will be applied.",
        color="#8b2020",
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

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


def _confirm_page(incident_id: str, action: str, heading: str,
                  body_color: str, button_label: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>SentinelAI — {heading}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 60px auto; padding: 0 20px; color: #222; }}
    h1   {{ color: {body_color}; }}
    .id  {{ font-family: monospace; background: #f4f4f4; padding: 4px 8px; border-radius: 4px; }}
    form {{ margin-top: 30px; }}
    button {{
      font-size: 16px; padding: 12px 24px; background: {body_color};
      color: white; border: none; border-radius: 6px; cursor: pointer;
    }}
    button:hover {{ opacity: 0.85; }}
    .back {{ margin-top: 20px; display: block; color: #555; font-size: 14px; }}
  </style>
</head>
<body>
  <h1>SentinelAI — {heading}</h1>
  <p>Incident: <span class="id">{incident_id}</span></p>
  <p>Are you sure you want to <strong>{action}</strong> this remediation plan?</p>
  <form method="post" action="/incidents/{incident_id}/{action}">
    <button type="submit">{button_label}</button>
  </form>
  <a class="back" href="/incidents/{incident_id}">← View full incident details (JSON)</a>
</body>
</html>"""


def _result_page(heading: str, message: str, color: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>SentinelAI — {heading}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; max-width: 600px; margin: 60px auto; padding: 0 20px; color: #222; }}
    h1   {{ color: {color}; }}
  </style>
</head>
<body>
  <h1>{heading}</h1>
  <p>{message}</p>
</body>
</html>"""


# ── Lambda entry point (Mangum wraps the ASGI app) ────────────────────────────

from mangum import Mangum
handler = Mangum(app, lifespan="off")
