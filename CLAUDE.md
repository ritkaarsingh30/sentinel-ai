# CLAUDE.md

Guidance for Claude Code when working in this repository.

---

## Commands

```bash
# First-time setup (creates all AWS resources + writes .env)
python scripts/setup.py

# Run agents locally with mock CloudWatch data (no AWS needed)
python scripts/test_local.py

# Deploy / re-deploy all AWS resources (idempotent)
python scripts/deploy.py

# Trigger an end-to-end test (fires the CloudWatch alarm)
python scripts/trigger_test.py

# Verify all AWS resources exist and are configured correctly
python scripts/check_aws.py
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
DYNAMODB_CHECKPOINT_WRITES_TABLE=sentinal-ai-checkpoint-writes
DYNAMODB_INCIDENTS_TABLE=sentinal-ai-incidents
API_BASE_URL=https://<api-gateway-id>.execute-api.us-east-1.amazonaws.com
USE_MOCK_DATA=true       # set false to hit real CloudWatch
```

All env vars are loaded through `config.py` — never call `os.getenv()` directly in other files.

---

## Architecture

### Data Flow

```
CloudWatch Alarm → EventBridge → SQS → Lambda (lambda_handler.py)
    → LangGraph graph (agents/graph.py)
        → supervisor (LLM-driven) → log_analyst → [metrics_agent] → root_cause_agent
          → remediation_agent → human_approval (interrupt/pause)
    → SNS email to engineer
    → engineer clicks approve/decline link
    → API Gateway → sentinal-ai-api Lambda (api/main.py)
    → graph resumes → report_agent → S3
    → incident row written to DynamoDB incidents table
```

### LangGraph Supervisor Pattern

`agents/graph.py` is the core file. The graph uses an **LLM-driven supervisor** that makes one intelligent routing decision: after `log_analyst` runs, it decides whether to call `metrics_agent` (more data) or skip it when the log findings already identify the root cause.

All other routing is enforced programmatically — the LLM cannot skip `remediation_agent`, `human_approval`, or `report_agent`.

Every sub-agent → supervisor → next sub-agent. The entry point is always `supervisor`.

**Human approval** uses LangGraph's `interrupt()` built-in. When hit, the graph saves all state to DynamoDB and returns control to the caller. The API Lambda resumes it with:
```python
graph.invoke(Command(resume=True), {"configurable": {"thread_id": incident_id}})
```

### Checkpointer (`agents/checkpointer.py`)

`get_checkpointer()` returns `DynamoDBSaver` in production so graph state survives Lambda restarts and can be resumed by the API Lambda. Falls back to `MemorySaver` for local runs (`USE_MOCK_DATA=true`).

### State (`state.py`)

`IncidentState` is a `TypedDict` that flows through all nodes. Agents receive the full state and return only the fields they update. `messages` uses `Annotated[list, add_messages]` so LangGraph auto-appends instead of overwrites.

### Tools (`tools/`)

`cloudwatch_logs.py` and `cloudwatch_metrics.py` have two modes:
- `USE_MOCK_DATA=true` → hardcoded realistic data (connection pool exhaustion scenario)
- `USE_MOCK_DATA=false` → real boto3 calls to CloudWatch

**Critical:** boto3 clients are created without explicit credentials:
```python
boto3.client("logs", region_name=config.AWS_REGION)
```
This works in Lambda (execution role) and locally (dotenv sets env vars, boto3 picks them up). Never pass credentials explicitly in tool code — it breaks Lambda's STS-based auth.

### Lambda Layer

Dependencies are packaged in `layer/python/`. The zip structure must be `python/<package>` at the zip root. `scripts/deploy.py` uses `layer_root` as the `arcname` base to get this right.

### LLM

Groq `llama-3.3-70b-versatile` with `temperature=0, max_tokens=1200`. The `_call_llm()` function retries up to 4 times on rate limit errors (30/60/90/120s waits) — Groq free tier is 12,000 TPM, and 5+ agents can exceed that. Log events are capped at 25 before sending to the LLM.

### Victim App (`victim_app/handler.py`)

A fake Lambda that throws realistic errors when `FAIL_RATE > 0`. Set `FAILURE_MODE` to `sql_error`, `memory_leak`, `timeout_cascade`, or `dependency_failure` to produce different error patterns for the LLM to analyze.

---

## Key Gotchas

- **SQS visibility timeout** must be ≥ Lambda timeout. Lambda is 300s → SQS is 360s.
- **IAM propagation**: after creating an IAM role, wait 12 seconds before creating the Lambda that uses it.
- **EventBridge → SQS** requires an explicit SQS resource policy allowing `events.amazonaws.com` to `sqs:SendMessage`.
- **Lambda layer zip**: packages must be at `python/` relative to zip root, not nested under `layer/python/`.
- **Lambda Function URLs with `AuthType=NONE`** return 403 on some AWS accounts due to account-level public access restrictions. Use API Gateway instead (already done — `sentinal-ai-api` sits behind API Gateway).
- **`config.aws_credentials()`** is only for local scripts that need explicit credentials. Never call from Lambda handlers or tool code.
