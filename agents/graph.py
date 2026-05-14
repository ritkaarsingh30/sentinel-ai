"""
SentinelAI LangGraph agent graph.

Supervisor Pattern:
  supervisor → log_analyst → supervisor → metrics_agent → supervisor
  → root_cause_agent → supervisor → remediation_agent → supervisor
  → human_approval (interrupt) → supervisor → report_agent → END
"""

import json
import time
import uuid
from datetime import datetime, timezone

from langchain_core.messages import HumanMessage, AIMessage, SystemMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, END
from langgraph.types import interrupt

import config
from state import IncidentState
from tools.cloudwatch_logs import get_log_events, format_logs_for_llm
from tools.cloudwatch_metrics import get_metric_datapoints, format_metrics_for_llm
from tools.persistence import save_report_to_s3, record_incident


# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

_llm = ChatGroq(
    model=config.MODEL_NAME,
    api_key=config.GROQ_API_KEY,
    temperature=0,
    max_tokens=1200,
)


def _call_llm(system_prompt: str, user_content: str) -> str:
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_content),
    ]
    for attempt in range(4):
        try:
            return _llm.invoke(messages).content.strip()
        except Exception as e:
            err = str(e)
            if "rate_limit" in err or "413" in err or "429" in err:
                wait = 30 * (attempt + 1)
                print(f"[Rate limit] Waiting {wait}s (attempt {attempt + 1}/4)")
                time.sleep(wait)
            else:
                raise
    raise RuntimeError("LLM call failed after 4 rate-limit retries")


# ---------------------------------------------------------------------------
# Supervisor node — LLM-driven routing
# ---------------------------------------------------------------------------

_METRICS_SKIP_SYSTEM = """You are an incident response coordinator deciding whether to run the metrics_agent.

The log_analyst has already run and produced findings. Your job: decide if running the metrics_agent
(which reads CloudWatch error rate, latency, invocation count) would add new information, or if the
log findings alone already make the root cause clear enough to skip straight to root_cause_agent.

Skip metrics_agent when: the log findings explicitly name a specific root cause (e.g. DB connection
pool exhausted, OOM kill, a specific dependency failure) with a clear failure chain.

Run metrics_agent when: the logs show errors but the cause is ambiguous, or quantifying severity
(spike timing, traffic volume) would materially change the RCA.

Respond with ONLY one of: metrics_agent  OR  root_cause_agent"""


def supervisor_node(state: IncidentState) -> dict:
    hint = state.get("next_agent", "log_analyst")

    # Always respect explicit end signals (decline / done)
    if hint == "__end__":
        return {"next_agent": "__end__"}

    has_logs        = bool(state.get("log_findings"))
    has_root_cause  = bool(state.get("root_cause"))
    has_remediation = bool(state.get("remediation_steps"))
    has_report      = bool(state.get("incident_report"))
    human_approved  = state.get("human_approved", False)

    # Enforce mandatory steps in order — the LLM cannot skip these
    if has_root_cause and not has_remediation:
        print("[Supervisor] Enforcing: remediation_agent (mandatory after root cause)")
        return {"next_agent": "remediation_agent"}
    if has_remediation and not has_report and not human_approved:
        print("[Supervisor] Enforcing: human_approval (mandatory after remediation)")
        return {"next_agent": "human_approval"}
    if human_approved and not has_report:
        print("[Supervisor] Enforcing: report_agent (mandatory after approval)")
        return {"next_agent": "report_agent"}
    if has_report:
        return {"next_agent": "__end__"}

    # The one smart decision: after log_analyst, should we run metrics_agent or skip it?
    if has_logs and hint == "metrics_agent":
        user_prompt = f"""Log findings from the current incident:

{state.get('log_findings', '')[:600]}

Should we run metrics_agent for quantitative data, or do these logs already identify the root cause clearly enough?"""
        try:
            response = _call_llm(_METRICS_SKIP_SYSTEM, user_prompt)
            chosen = response.strip().split()[0].rstrip(".,:")
            if chosen in {"metrics_agent", "root_cause_agent"}:
                print(f"[Supervisor] LLM chose: {chosen} (hint was: metrics_agent)")
                return {"next_agent": chosen}
            print(f"[Supervisor] LLM returned unexpected '{chosen}', following hint: metrics_agent")
        except Exception as exc:
            print(f"[Supervisor] LLM error ({exc}), following hint: metrics_agent")

    # Default: follow sub-agent's hint
    print(f"[Supervisor] Routing: {hint}")
    return {"next_agent": hint}


def route_from_supervisor(state: IncidentState) -> str:
    return state.get("next_agent", "__end__")


# ---------------------------------------------------------------------------
# Log Analyst Agent
# ---------------------------------------------------------------------------

_LOG_ANALYST_SYSTEM = """You are a senior SRE specializing in log analysis.
You will be given raw CloudWatch log events from a production application that is experiencing an incident.

Your job:
1. Identify the primary error type and its frequency
2. Find the first occurrence (incident start time)
3. Identify which endpoints or components are affected
4. Extract any stack traces or error messages that reveal root cause clues
5. Note any patterns (cascading failures, retry storms, etc.)

Be specific and concise. Output a structured analysis, not a narrative essay.
Format: use short bullet points under clear headings."""


def log_analyst_node(state: IncidentState) -> dict:
    events = get_log_events(state["log_group"], state["start_time"], state["end_time"])
    log_text = format_logs_for_llm(events[:25])  # cap at 25 to stay under TPM limit

    prompt = f"""Incident: {state['alarm_name']}
Alarm description: {state['alarm_description']}
Log group: {state['log_group']}
Time window: {state['start_time']} → {state['end_time']}

--- LOG EVENTS ---
{log_text}
"""

    findings = _call_llm(_LOG_ANALYST_SYSTEM, prompt)
    return {
        "log_findings": findings,
        "next_agent": "metrics_agent",
        "messages": [AIMessage(content=f"[Log Analyst]\n{findings}")],
    }


# ---------------------------------------------------------------------------
# Metrics Agent
# ---------------------------------------------------------------------------

_METRICS_SYSTEM = """You are a senior SRE specializing in metrics analysis and performance engineering.
You will be given CloudWatch metric datapoints covering the period before and during an incident.

Your job:
1. Identify the exact time the anomaly began
2. Quantify the severity (e.g., error rate jumped from X% to Y%)
3. Correlate metrics — does latency spike before errors, or simultaneously?
4. Identify whether invocation count dropped (Lambda throttling?) or held steady
5. Assess whether this looks like a traffic surge, a dependency failure, or a resource exhaustion

Be specific with numbers. Use the datapoints directly."""


def metrics_agent_node(state: IncidentState) -> dict:
    datapoints = get_metric_datapoints(
        state["metric_name"],
        "AWS/Lambda",
        state["log_group"],
        state["start_time"],
        state["end_time"],
    )
    metrics_text = format_metrics_for_llm(datapoints)

    prompt = f"""Incident: {state['alarm_name']}
Metric being monitored: {state['metric_name']}
Time window: {state['start_time']} → {state['end_time']}

--- METRIC DATAPOINTS ---
{metrics_text}
"""

    findings = _call_llm(_METRICS_SYSTEM, prompt)
    return {
        "metrics_findings": findings,
        "next_agent": "root_cause_agent",
        "messages": [AIMessage(content=f"[Metrics Agent]\n{findings}")],
    }


# ---------------------------------------------------------------------------
# Root Cause Agent
# ---------------------------------------------------------------------------

_ROOT_CAUSE_SYSTEM = """You are a principal engineer conducting an RCA (Root Cause Analysis).
You will be given findings from a log analyst and a metrics analyst about a production incident.

Your job:
1. Synthesize both analyses into a single root cause statement
2. Explain the failure chain (what triggered what)
3. Assess confidence level (High / Medium / Low) and why
4. Rule out alternative explanations
5. Identify any contributing factors (e.g., recent deploy, traffic spike, scheduled job)

Output format:
**Root Cause:** one sentence
**Failure Chain:** step-by-step
**Confidence:** High/Medium/Low — reason
**Contributing Factors:** bullet list (or "None identified")"""


def root_cause_agent_node(state: IncidentState) -> dict:
    prompt = f"""Incident: {state['alarm_name']}
Time window: {state['start_time']} → {state['end_time']}

--- LOG ANALYST FINDINGS ---
{state['log_findings']}

--- METRICS ANALYST FINDINGS ---
{state['metrics_findings']}
"""

    root_cause = _call_llm(_ROOT_CAUSE_SYSTEM, prompt)
    return {
        "root_cause": root_cause,
        "next_agent": "remediation_agent",
        "messages": [AIMessage(content=f"[Root Cause Agent]\n{root_cause}")],
    }


# ---------------------------------------------------------------------------
# Remediation Agent
# ---------------------------------------------------------------------------

_REMEDIATION_SYSTEM = """You are a senior DevOps engineer responsible for remediating production incidents on AWS.
You will be given a root cause analysis for an active incident.

Your job:
1. Propose concrete, ordered remediation steps
2. Each step must be a specific AWS action (not vague advice)
3. Include immediate fixes AND follow-up prevention steps
4. Flag any step that carries risk and needs extra caution
5. Include estimated time to apply each step

Output format: numbered list of steps.
Each step: [IMMEDIATE/FOLLOW-UP] [~Xmin] Action — rationale"""


def remediation_agent_node(state: IncidentState) -> dict:
    prompt = f"""Incident: {state['alarm_name']}
Region: {state['region']}
Log Group: {state['log_group']}

--- ROOT CAUSE ---
{state['root_cause']}
"""

    plan = _call_llm(_REMEDIATION_SYSTEM, prompt)
    steps = [line.strip() for line in plan.split("\n") if line.strip()]
    return {
        "remediation_steps": steps,
        "next_agent": "human_approval",
        "messages": [AIMessage(content=f"[Remediation Agent]\n{plan}")],
    }


# ---------------------------------------------------------------------------
# Human Approval Node (LangGraph interrupt)
# ---------------------------------------------------------------------------

def human_approval_node(state: IncidentState) -> dict:
    """Pause here. The graph state is saved to the checkpointer.
    The FastAPI endpoint resumes this graph with Command(resume=True/False).
    """
    approved = interrupt({
        "incident_id": state["incident_id"],
        "alarm_name": state["alarm_name"],
        "root_cause": state["root_cause"],
        "remediation_steps": state["remediation_steps"],
        "instructions": "Call POST /incidents/{incident_id}/approve or /decline to resume.",
    })

    if not approved:
        record_incident({**state, "human_approved": False}, "declined")

    return {
        "human_approved": bool(approved),
        "next_agent": "report_agent" if approved else "__end__",
    }


# ---------------------------------------------------------------------------
# Report Agent
# ---------------------------------------------------------------------------

_REPORT_SYSTEM = """You are a technical writer creating a post-incident report.
Given a complete incident investigation, produce a structured JSON report.

Output ONLY valid JSON, no markdown fences, no commentary.

Schema:
{
  "incident_id": "...",
  "title": "...",
  "severity": "P1/P2/P3",
  "start_time": "...",
  "end_time": "...",
  "duration_minutes": 0,
  "root_cause_summary": "one sentence",
  "impact": "what was affected and for how long",
  "timeline": [{"time": "...", "event": "..."}],
  "remediation_applied": ["step 1", "step 2"],
  "prevention": ["action 1", "action 2"],
  "resolved": true
}"""


def report_agent_node(state: IncidentState) -> dict:
    prompt = f"""Incident ID: {state['incident_id']}
Alarm: {state['alarm_name']}
Start: {state['start_time']}
End: {state['end_time']}
Approved: {state['human_approved']}

Root Cause:
{state['root_cause']}

Remediation Steps:
{chr(10).join(state['remediation_steps'])}

Log Findings:
{state['log_findings']}

Metrics Findings:
{state['metrics_findings']}
"""

    report_json_str = _call_llm(_REPORT_SYSTEM, prompt)
    try:
        report = json.loads(report_json_str)
    except json.JSONDecodeError:
        report = {"raw": report_json_str, "parse_error": True}

    report.setdefault("incident_id", state["incident_id"])

    save_report_to_s3(state["incident_id"], report)
    record_incident({**state, "human_approved": True}, "resolved")

    return {
        "incident_report": report,
        "next_agent": "__end__",
        "messages": [AIMessage(content=f"[Report Agent] Report generated for {state['incident_id']}")],
    }


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None):
    g = StateGraph(IncidentState)

    g.add_node("supervisor", supervisor_node)
    g.add_node("log_analyst", log_analyst_node)
    g.add_node("metrics_agent", metrics_agent_node)
    g.add_node("root_cause_agent", root_cause_agent_node)
    g.add_node("remediation_agent", remediation_agent_node)
    g.add_node("human_approval", human_approval_node)
    g.add_node("report_agent", report_agent_node)

    g.set_entry_point("supervisor")

    g.add_conditional_edges(
        "supervisor",
        route_from_supervisor,
        {
            "log_analyst": "log_analyst",
            "metrics_agent": "metrics_agent",
            "root_cause_agent": "root_cause_agent",
            "remediation_agent": "remediation_agent",
            "human_approval": "human_approval",
            "report_agent": "report_agent",
            "__end__": END,
        },
    )

    # Every sub-agent returns to the supervisor
    for node in ["log_analyst", "metrics_agent", "root_cause_agent",
                 "remediation_agent", "human_approval", "report_agent"]:
        g.add_edge(node, "supervisor")

    if checkpointer is None:
        from agents.checkpointer import get_checkpointer
        checkpointer = get_checkpointer()
    return g.compile(checkpointer=checkpointer)


# ---------------------------------------------------------------------------
# Helper: create initial state from a CloudWatch alarm event
# ---------------------------------------------------------------------------

def make_initial_state(
    alarm_name: str,
    alarm_description: str,
    log_group: str,
    metric_name: str,
    region: str,
    start_time: str,
    end_time: str,
) -> IncidentState:
    return IncidentState(
        incident_id=f"INC-{uuid.uuid4().hex[:8].upper()}",
        alarm_name=alarm_name,
        alarm_description=alarm_description,
        log_group=log_group,
        metric_name=metric_name,
        region=region,
        start_time=start_time,
        end_time=end_time,
        log_findings="",
        metrics_findings="",
        root_cause="",
        remediation_steps=[],
        incident_report={},
        next_agent="log_analyst",
        human_approved=False,
        messages=[],
    )
