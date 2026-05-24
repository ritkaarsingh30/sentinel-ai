# Understanding SentinelAI — A Guide to What Was Built and Why

This document explains the entire project from first principles. Read it once straight through. By the end you should be able to look at any file and know exactly why it exists.

---

## The One-Sentence Version

SentinelAI watches your AWS Lambda function for errors. When it detects a spike, it automatically investigates the incident using a chain of AI agents, proposes a fix, and emails you for approval before doing anything.

---

## The Problem It Solves

Imagine your production Lambda starts throwing errors at 2am. Normally you'd have to:
1. Notice the alarm
2. Log into CloudWatch and dig through logs
3. Pull up the metrics to see when it started and how bad it is
4. Figure out the root cause yourself
5. Decide what to do about it
6. Document what happened

SentinelAI automates steps 2–6. You still make the final call (step 6), but the system does the investigation and hands you a complete picture with a proposed fix.

---

## The Data Flow — What Actually Happens

```
1. victim-app-prod Lambda throws errors
        ↓
2. CloudWatch sees >5 errors in 1 minute → alarm fires
        ↓
3. EventBridge detects the alarm state change
        ↓
4. EventBridge sends a message to SQS queue (sentinal-ai-incidents)
        ↓
5. SQS triggers the SentinelAI Lambda (lambda_handler.py)
        ↓
6. Lambda runs the LangGraph agent graph (agents/graph.py)
        ↓  [5 AI agents run in sequence]
        ↓
7. Graph pauses at human_approval — saves state to DynamoDB
        ↓
8. Lambda emails you via SNS with root cause + proposed fix + approve/decline links
        ↓
9. You click Approve or Decline
        ↓
10. FastAPI (api/main.py) receives your click → resumes the graph from DynamoDB
        ↓
11. If approved: report_agent writes a JSON report to S3
        If declined: investigation ends
        ↓
12. DynamoDB incidents table gets a summary row either way
```

Every step exists because of a specific design requirement. Let me explain each one.

---

## The AWS Pieces

### victim-app-prod Lambda (`victim_app/handler.py`)

This is a fake "broken" service. It's a Lambda that deliberately throws errors when you set `FAIL_RATE=0.9` (90% failure rate). It's not part of the investigation system — it's just the thing being monitored, so you have something to trigger alarms with.

The `FAILURE_MODE` env var (added in Week 4) lets you pick what kind of errors to throw: SQL errors, memory leaks, timeout cascades, or dependency failures. This matters because the AI agents analyze the error text, so different modes produce different-looking investigations.

### CloudWatch Alarm

Configured to fire when `AWS/Lambda Errors` for `victim-app-prod` exceeds 5 in a 1-minute window. This is a standard CloudWatch metric alarm. The key thing to understand: CloudWatch itself doesn't call your code when an alarm fires — it just changes the alarm's state from OK to ALARM. Something else has to notice that state change.

### EventBridge Rule

This is the "something else." EventBridge watches for the CloudWatch alarm state change event and forwards it to SQS. Why SQS instead of directly triggering Lambda?

- SQS adds a buffer. If the Lambda is slow or fails, the message stays in the queue and retries.
- SQS has a visibility timeout (360s), which is set longer than the Lambda timeout (300s). This prevents the same alarm from triggering two simultaneous investigations.

### SQS Queue (`sentinal-ai-incidents`)

Holds one message per alarm firing. The SentinelAI Lambda is wired to poll this queue (this wiring is called an "event source mapping"). Batch size is 1 — each alarm gets its own Lambda invocation.

### SentinelAI Lambda (`lambda_handler.py`)

The entry point for investigation. When SQS delivers a message, this runs. It:
1. Parses the alarm event to extract which Lambda had errors, and when
2. Creates an initial state object (the "incident")
3. Runs the LangGraph agent graph until the graph pauses (at human_approval)
4. Emails you with the findings

Timeout is 300 seconds because the 5 LLM calls (one per agent) can take 30-60 seconds each, plus rate limit retries.

### SNS Topic (`sentinal-ai-alerts`)

Sends the approval email. SNS is AWS's notification service. The email contains the root cause, the proposed remediation steps, and two clickable URLs — one to approve, one to decline. These URLs point to the FastAPI server.

---

## The Python Files

### `config.py`

All env vars live here. Every other file imports from `config` instead of calling `os.getenv()` directly. This means if you ever need to see what env vars the system uses, you look in exactly one place.

The `USE_MOCK_DATA` flag switches the entire system between local-dev mode (hardcoded fake data, no AWS calls) and production mode (real CloudWatch data, real DynamoDB, real S3).

### `state.py`

Defines `IncidentState` — a Python TypedDict (basically a typed dictionary) that flows through every agent. Think of it as a shared whiteboard that every agent can read from and write to.

```python
class IncidentState(TypedDict):
    incident_id: str         # e.g. "INC-A3B7C2D1"
    alarm_name: str          # "HighErrorRate-victim-app-prod"
    log_group: str           # "/aws/lambda/victim-app-prod"
    # ... metadata ...

    log_findings: str        # filled by log_analyst_node
    metrics_findings: str    # filled by metrics_agent_node
    root_cause: str          # filled by root_cause_agent_node
    remediation_steps: list  # filled by remediation_agent_node
    incident_report: dict    # filled by report_agent_node

    next_agent: str          # routing signal — which node runs next
    human_approved: bool     # set by human_approval_node
    messages: list           # full LLM conversation history
```

Agents receive the full state but return only the fields they changed. LangGraph merges those partial updates back into the state. The `messages` field is special — LangGraph automatically appends to it instead of replacing it (this is what `Annotated[list, add_messages]` does).

### `agents/graph.py`

The core file. This is where all 6 agents are defined and wired together.

**How LangGraph works:**
LangGraph is a library for building "graphs" where nodes are Python functions and edges are the connections between them. Each node receives the current state, does something, and returns a partial update to the state. LangGraph handles merging and saving.

**The agent sequence:**

```
supervisor → log_analyst → supervisor → metrics_agent (or skip) →
supervisor → root_cause_agent → supervisor → remediation_agent →
supervisor → human_approval (PAUSE) → supervisor → report_agent → END
```

Every agent goes back through the supervisor. The supervisor decides what runs next.

**The supervisor (Week 4 upgrade):**
Originally the supervisor was pure if/else logic — it just read `next_agent` from state and forwarded it unchanged. In Week 4 it was upgraded to use an LLM for one decision: after `log_analyst` finishes, should it call `metrics_agent` (more data) or skip straight to `root_cause_agent` (if the logs already make the cause obvious)?

All other routing is still hardcoded — the LLM cannot skip the remediation agent, human approval, or report generation. Those are mandatory.

**The human_approval node:**
This uses LangGraph's `interrupt()` function. When the graph hits this node, it:
1. Saves the entire state to the checkpointer (DynamoDB in production)
2. Returns control to the caller (the Lambda function)
3. The graph is now "frozen" — it will not run again until something resumes it

The Lambda then emails you. When you click Approve, the FastAPI server calls `graph.invoke(Command(resume=True), ...)` which unfreezes the graph and continues from exactly where it stopped.

**The `_call_llm()` function:**
All LLM calls go through this wrapper. It uses Groq's `llama-3.3-70b-versatile` model. The wrapper adds retry logic for rate limit errors — Groq's free tier allows 12,000 tokens per minute, and 5 agents can exceed that. It waits 30s, then 60s, then 90s, then 120s between retries.

### `agents/checkpointer.py`

A small factory function. Returns a `DynamoDBSaver` in production (so graph state persists to DynamoDB across Lambda restarts) or an in-memory `MemorySaver` for local testing.

**Why does this matter?**
Lambda functions are stateless — each invocation gets a fresh Python process. If the graph ran entirely within one Lambda invocation, that would be fine. But the human_approval pause means the graph has to stop mid-run, wait for your input (possibly hours), and then continue in a *different* Lambda invocation triggered by the FastAPI server. Without a persistent checkpointer, the frozen state would be lost the moment the first Lambda invocation ended.

DynamoDB holds the frozen graph state so any Lambda invocation — or the FastAPI server — can pick it up and resume.

### `tools/cloudwatch_logs.py` and `tools/cloudwatch_metrics.py`

These fetch the actual data the agents analyze. When `USE_MOCK_DATA=true`, they return hardcoded realistic data (a connection pool exhaustion scenario). When `USE_MOCK_DATA=false`, they make real boto3 calls to CloudWatch.

The `log_analyst` agent calls `get_log_events()` to get the raw log lines from the 30-minute window around the alarm. The `metrics_agent` calls `get_metric_datapoints()` to get the time-series error rate, latency, and invocation counts.

Log events are capped at 25 before being sent to the LLM to avoid hitting token limits.

### `tools/persistence.py`

Two functions:
- `save_report_to_s3()` — uploads the final JSON report to S3 after the investigation resolves
- `record_incident()` — writes a one-line summary to the DynamoDB incidents table

Both are no-ops when `USE_MOCK_DATA=true`.

### `api/main.py`

A FastAPI server with three endpoints:

```
GET  /incidents/{id}         → view the full investigation state
POST /incidents/{id}/approve → resume the frozen graph with approval
POST /incidents/{id}/decline → resume the frozen graph with decline
```

The approve/decline endpoints are what the links in your email point to. The server uses the same DynamoDB checkpointer as the Lambda, so it can load and resume any frozen graph by incident ID.

This server is not deployed yet — it's meant to run on EC2. For now the links in the email lead nowhere (the approve/decline will work once you run `uvicorn api.main:app` on a server with a public IP).

### `scripts/deploy.py`

Creates all AWS resources in the right order. It's idempotent — every resource creation is wrapped in a try/except that skips if the resource already exists. Safe to run multiple times.

Order matters because of dependencies:
- IAM role must exist before Lambda (Lambda needs the role)
- Lambda layer must exist before Lambda (Lambda needs the layer)
- Lambda must exist before SQS mapping (mapping references the function)
- The 12-second wait after IAM role creation is because AWS IAM changes take time to propagate globally

### `scripts/trigger_test.py`

A helper script that manually triggers the end-to-end flow. It:
1. Sets `FAIL_RATE=0.9` on the victim Lambda (via API call)
2. Invokes it 25 times in parallel (to generate errors fast)
3. Polls the CloudWatch alarm until it fires
4. Resets `FAIL_RATE=0.0`

### `scripts/test_local.py`

Runs the entire agent graph locally using mock CloudWatch data. No AWS calls, no real LLM rate limits (well, it does call Groq — just no CloudWatch/DynamoDB). This is the fastest way to test agent behavior during development.

---

## Key Concepts Explained

### Why LangGraph?

You could build the same thing with a simple Python loop: call log_analyst, call metrics_agent, etc. LangGraph adds two things that would be painful to build yourself:

1. **State management** — automatically merges partial updates, tracks message history
2. **Human-in-the-loop via `interrupt()`** — pauses the graph mid-run, saves all state, resumes later. Without this, implementing the "pause and wait for email approval" flow would require you to manually serialize state to DynamoDB, build resume logic, etc.

### Why a Supervisor Pattern?

Each agent is a separate node that only knows about its own job. The supervisor is the router between them. This means:
- Adding a new agent = add a new node + update the supervisor routing
- Changing the order = change the supervisor, not any agent
- The agents don't know about each other

Alternative: a linear chain where each agent directly calls the next. The supervisor pattern is more flexible.

### Why DynamoDB for Checkpointing?

The LangGraph graph pauses at `human_approval`. Between the pause and the resume, there might be minutes or hours. Lambda has a max timeout of 15 minutes — so the graph cannot just "wait" inside Lambda. It must freeze and be revived by a different process (the FastAPI server). DynamoDB is the freezer.

Two tables are needed:
- `sentinal-ai-checkpoints` — stores each checkpoint snapshot (one row per graph state snapshot)
- `sentinal-ai-checkpoint-writes` — stores in-progress writes (LangGraph's internal mechanism for crash recovery)

### Why SNS for Email?

SNS is the simplest way to send email from Lambda without setting up a mail server. You create a topic, subscribe your email to it (confirm the subscription once), and then `sns.publish()` sends an email. The approval links in the email point to the FastAPI server.

### Why SQS between EventBridge and Lambda?

Direct EventBridge → Lambda would also work. The SQS buffer adds:
- **Retry on Lambda failure** — if the Lambda crashes, SQS re-delivers after the visibility timeout
- **Rate control** — prevents multiple simultaneous investigations if the alarm bounces

---

## The Three DynamoDB Tables

| Table | What It Stores | Who Writes |
|---|---|---|
| `sentinal-ai-checkpoints` | Frozen LangGraph graph state (one row per checkpoint) | `DynamoDBSaver` (LangGraph library) |
| `sentinal-ai-checkpoint-writes` | LangGraph in-flight write tracking (crash safety) | `DynamoDBSaver` (LangGraph library) |
| `sentinal-ai-incidents` | One summary row per completed incident | `tools/persistence.py` |

The first two are managed entirely by the `langgraph-checkpoint-dynamodb` library — you never read or write them directly. The incidents table is your own, for querying incident history.

---

## The Two Modes

| | Local (`USE_MOCK_DATA=true`) | Production (`USE_MOCK_DATA=false`) |
|---|---|---|
| CloudWatch data | Hardcoded fake events | Real boto3 API calls |
| Checkpointer | MemorySaver (in-memory, gone on exit) | DynamoDBSaver (persistent) |
| S3 reports | Skipped, prints message | Real upload to S3 bucket |
| DynamoDB incidents | Skipped, prints message | Real write |
| Lambda env var | Set in `.env` | Set in Lambda config by `deploy.py` |

---

## Reading the Code for the First Time

Start here, in this order:

1. **`state.py`** (30 lines) — understand the shared whiteboard before anything else
2. **`victim_app/handler.py`** (70 lines) — understand what's being monitored
3. **`lambda_handler.py`** (136 lines) — understand the entry point and the overall flow
4. **`agents/graph.py`** (one section at a time):
   - The `_call_llm()` function — how all LLM calls work
   - `supervisor_node()` — the routing logic
   - One agent node (e.g., `log_analyst_node`) — they all have the same shape
   - `build_graph()` — how the nodes get wired together
5. **`agents/checkpointer.py`** (23 lines) — why graph state survives Lambda restarts
6. **`api/main.py`** (81 lines) — how approve/decline works

---

## Experiments to Run Yourself

**See the LLM supervisor make a routing decision:**
```bash
.venv/bin/python3 scripts/test_local.py
```
Watch for lines like `[Supervisor] LLM chose: root_cause_agent (hint was: metrics_agent)`. That's the LLM deciding to skip the metrics agent because the logs were conclusive.

**See what the mock CloudWatch data looks like:**
```bash
.venv/bin/python3 -c "
from tools.cloudwatch_logs import get_log_events, format_logs_for_llm
events = get_log_events('/aws/lambda/orders-api-prod', '2026-01-01', '2026-01-02')
print(format_logs_for_llm(events[:5]))
"
```

**See the full incident state after a run:**
Add `import json; print(json.dumps(result, indent=2, default=str))` right after `compiled_graph.invoke(...)` in `lambda_handler.py` and run locally.

**Trigger different failure modes:**
Change `FAILURE_MODE` in `.env` to `sql_error`, `memory_leak`, `timeout_cascade`, or `dependency_failure`, then run `scripts/test_local.py` again. The agents will see different error patterns and produce different RCAs.

---

## What Still Isn't Done

- **FastAPI is not deployed** — the approve/decline links in emails lead nowhere until you run `api/main.py` on a server with a public IP. Run it locally with `uvicorn api.main:app --reload` and you can test the endpoints manually.
- **The victim app is fake** — in a real system, you'd point the CloudWatch alarm at an actual service you own.
- **Real CloudWatch data** — set `USE_MOCK_DATA=false` in `.env` and the agents will analyze your actual Lambda logs and metrics instead of the hardcoded scenario.
