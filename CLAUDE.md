# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

---

## Commands

```bash
# Activate virtualenv
source .venv/bin/activate

# Run agents locally with mock CloudWatch data (no AWS needed)
python scripts/test_local.py

# Deploy all AWS resources (idempotent — safe to re-run)
python scripts/deploy.py

# Trigger the victim app to fire the CloudWatch alarm
python scripts/trigger_test.py

# Verify all AWS resources exist
python scripts/check_aws.py

# Run FastAPI dashboard locally
uvicorn api.main:app --reload

# Run FastAPI in production (EC2)
uvicorn api.main:app --host 0.0.0.0 --port 8000
```

There is no formal test suite. `scripts/test_local.py` is the primary local validation path.

---

## Environment Variables (`.env`)

```
GROQ_API_KEY=
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
SQS_QUEUE_URL=
SNS_ALERT_TOPIC_ARN=
S3_REPORTS_BUCKET=
DYNAMODB_CHECKPOINT_TABLE=sentinal-ai-checkpoints
DYNAMODB_INCIDENTS_TABLE=sentinal-ai-incidents
API_BASE_URL=http://localhost:8000
USE_MOCK_DATA=true       # set false to hit real CloudWatch
```

All env vars are loaded through `config.py` — never call `os.getenv()` directly in other files.

---

## Architecture

### Data Flow

```
CloudWatch Alarm → EventBridge → SQS → Lambda (lambda_handler.py)
    → LangGraph graph (agents/graph.py)
        → supervisor → log_analyst → metrics_agent → root_cause_agent
          → remediation_agent → human_approval (interrupt/pause)
    → SNS email to engineer
    → FastAPI (api/main.py) receives approve/decline
    → graph resumes → report_agent → S3
```

### LangGraph Supervisor Pattern

`agents/graph.py` is the core file. The graph uses a **rule-based supervisor** — not LLM-driven. Each agent node sets `state["next_agent"]` to the name of the next node before returning, and the supervisor simply reads that field and routes accordingly.

Every sub-agent → supervisor → next sub-agent. The entry point is always `supervisor`, which reads `next_agent` (defaulting to `"log_analyst"`).

**Human approval** uses LangGraph's `interrupt()` built-in. When hit, the graph saves all state to the checkpointer and returns control to the caller. Resume with:
```python
graph.invoke(Command(resume=True), {"configurable": {"thread_id": incident_id}})
```

Currently uses `MemorySaver` as the checkpointer (in-memory, lost on Lambda restart). Week 3 replaces this with a DynamoDB checkpointer so state survives across Lambda invocations.

### State (`state.py`)

`IncidentState` is a `TypedDict` that flows through all nodes. Agents receive the full state and return only the fields they update. `messages` uses `Annotated[list, add_messages]` so LangGraph auto-appends instead of overwrites.

### Tools (`tools/`)

Both `cloudwatch_logs.py` and `cloudwatch_metrics.py` have two modes:
- `USE_MOCK_DATA=true` → hardcoded realistic data (connection pool exhaustion scenario)
- `USE_MOCK_DATA=false` → real boto3 calls to CloudWatch

**Critical:** boto3 clients are created without explicit credentials:
```python
boto3.client("logs", region_name=config.AWS_REGION)
```
This works in Lambda (execution role injects `AWS_SESSION_TOKEN` too) and locally (dotenv sets the vars and boto3 picks them up). Never pass credentials explicitly — it breaks Lambda's STS-based temporary credentials.

### Lambda Layer

Dependencies are packaged in `layer/python/`. The zip structure must be `python/<package>` at the zip root (not `layer/python/<package>`). `scripts/deploy.py` uses `layer_root` as the `arcname` base to get this right.

### LLM

Groq `llama-3.3-70b-versatile` with `temperature=0, max_tokens=1200`. The `_call_llm()` function retries up to 4 times on rate limit errors (30/60/90/120s waits) — Groq free tier is 12,000 TPM, and 5 agents can exceed that. Log events are capped at 25 before sending to the LLM.

---

## Current State vs. Upcoming Work

### Completed (Weeks 1–2)
- Full LangGraph agent graph with mock and real CloudWatch data
- AWS infrastructure: Lambda, SQS, EventBridge, CloudWatch Alarm, SNS, IAM roles, Lambda layer
- `scripts/deploy.py` automates all resource creation idempotently
- End-to-end flow: alarm fires → agents run → SNS email with approval links

### Week 3 (In Progress)
- **FastAPI dashboard on EC2** (`api/main.py` exists but isn't deployed): three endpoints — `GET /incidents/{id}`, `POST /incidents/{id}/approve`, `POST /incidents/{id}/decline`
- **DynamoDB checkpointer**: replace `MemorySaver` in `build_graph()` with `langgraph-checkpoint-dynamodb` (or equivalent) so incident state survives Lambda cold starts. The DynamoDB table name is already in config as `DYNAMODB_CHECKPOINT_TABLE`. `build_graph(checkpointer=...)` accepts a custom checkpointer.
- **S3 report persistence**: after `report_agent_node` completes, write `incident_report` dict to S3 (`S3_REPORTS_BUCKET`/`{incident_id}.json`)
- **DynamoDB incident history**: write a summary record to `DYNAMODB_INCIDENTS_TABLE` after each investigation

### Week 4 (Upcoming)
- **Enhanced victim app** (`victim_app/handler.py`): add configurable failure scenarios beyond random `RuntimeError` — simulate bad SQL queries, memory leaks, timeout cascades, dependency failures — so the LLM analysis produces more varied and realistic output
- **CloudWatch Dashboard**: create a CW dashboard showing incident count, error rate over time, and agent execution duration across all investigations
- **LLM-based supervisor** (optional upgrade): replace the current rule-based `supervisor_node` with an LLM call that can dynamically skip or repeat agents based on findings (e.g., skip `metrics_agent` if logs are already conclusive)

---

## Key Gotchas

- **SQS visibility timeout** must be ≥ Lambda timeout. Lambda is 300s → SQS is 360s.
- **IAM propagation**: after creating an IAM role, wait 12 seconds before creating the Lambda that uses it.
- **EventBridge → SQS** requires an explicit SQS resource policy allowing `events.amazonaws.com` to `sqs:SendMessage`.
- **Lambda layer zip**: packages must be at `python/` relative to zip root, not nested under `layer/python/`.
- **S3 + Lambda must be same region** for layer uploads via S3 reference. Use direct `ZipFile=bytes` upload instead (works up to 50 MB).
- **`config.aws_credentials()`** is only for local scripts that explicitly need to pass credentials. Never call it from tool code or Lambda handlers.
