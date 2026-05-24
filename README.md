# SentinelAI

An autonomous AI-powered incident response system. When a CloudWatch alarm fires, SentinelAI automatically investigates, diagnoses the root cause, proposes a remediation plan, and emails an engineer for one-click approval — all without manual triage.

---

## How it works

```
CloudWatch Alarm
      │
      ▼
  EventBridge
      │
      ▼
    SQS Queue
      │
      ▼
 Lambda (sentinal-ai)
      │
      ▼
 LangGraph Agent Graph
  ┌───────────────────────────────────────────────────┐
  │  supervisor → log_analyst → [metrics_agent]       │
  │           → root_cause_agent → remediation_agent  │
  │           → human_approval (interrupt/pause)       │
  │           → report_agent                           │
  └───────────────────────────────────────────────────┘
      │                           │
      ▼                           ▼
 SNS Email                 API Gateway
 (approve/decline links)   (sentinal-ai-api Lambda)
                                  │
                          ┌───────┴───────┐
                          ▼               ▼
                      DynamoDB          S3
                    (state + history)  (reports)
```

The **supervisor** is LLM-driven: it enforces mandatory pipeline steps (logs → root cause → remediation → approval → report) but uses an LLM call to decide whether to skip `metrics_agent` when log findings are already conclusive.

**Human approval** uses LangGraph's `interrupt()` — graph state is checkpointed to DynamoDB and resumed when the engineer clicks Approve or Decline in the email.

---

## Quick start

### Prerequisites

- Python 3.11+
- AWS account with programmatic access
- Groq API key (free at [console.groq.com](https://console.groq.com))

### One-command setup

```bash
git clone <repo-url> && cd sentinel-ai
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/setup.py
```

`setup.py` will prompt for your credentials, create all AWS resources, write `.env`, and deploy everything automatically.

### Run locally (no AWS needed)

```bash
source .venv/bin/activate
python scripts/test_local.py
```

Uses hardcoded mock CloudWatch data so you can develop and test the full agent graph offline.

---

## Scripts

| Script | Purpose |
|---|---|
| `scripts/setup.py` | One-command setup: creates S3, SQS, SNS, writes `.env`, deploys |
| `scripts/deploy.py` | Deploy / update all AWS resources (idempotent) |
| `scripts/test_local.py` | Run the full agent graph locally with mock data |
| `scripts/trigger_test.py` | Invoke the victim Lambda to fire a CloudWatch alarm |
| `scripts/check_aws.py` | Verify all AWS resources exist and are configured correctly |

---

## Configuration (`.env`)

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
API_BASE_URL=                # set automatically by deploy.py
USE_MOCK_DATA=true           # set false to hit real CloudWatch
```

All values flow through `config.py` — never call `os.getenv()` directly.

---

## Project structure

```
sentinel-ai/
├── agents/
│   ├── graph.py          # LangGraph graph: all nodes + supervisor
│   ├── checkpointer.py   # DynamoDB checkpointer setup
│   └── state.py          # IncidentState TypedDict
├── api/
│   └── main.py           # FastAPI approval API (deployed via Lambda + API Gateway)
├── tools/
│   ├── cloudwatch_logs.py    # Fetch / mock CloudWatch logs
│   ├── cloudwatch_metrics.py # Fetch / mock CloudWatch metrics
│   └── persistence.py        # S3 report upload + DynamoDB incident record
├── victim_app/
│   └── handler.py        # Dummy Lambda that fails at a configurable rate
├── scripts/
│   ├── setup.py          # One-command setup wizard
│   ├── deploy.py         # Deploy all AWS resources
│   ├── test_local.py     # Local dev runner
│   ├── trigger_test.py   # Fire the alarm
│   └── check_aws.py      # Verify AWS resources
├── lambda_handler.py     # Entry point for the main SentinelAI Lambda
├── config.py             # Centralised env var loading
├── requirements.txt      # Local dev dependencies
└── requirements-lambda.txt # Lambda layer dependencies
```

---

## Victim app failure modes

The victim Lambda (`victim_app/handler.py`) accepts a `FAILURE_MODE` env var to simulate different incident types:

| Mode | Simulates |
|---|---|
| `random` | Mixed errors (default) |
| `sql_error` | DB deadlocks, connection exhaustion, index corruption |
| `memory_leak` | OOM kills, heap exhaustion, GC pressure |
| `timeout_cascade` | Cascading timeouts across DB → cache → API |
| `dependency_failure` | Upstream service unavailable (S3, auth, payment) |

Set `FAIL_RATE` (0.0–1.0) to control how often invocations fail.

---

## LLM

Uses Groq `llama-3.3-70b-versatile` (`temperature=0`, `max_tokens=1200`). The `_call_llm()` helper retries up to 4 times on rate-limit errors (Groq free tier: 12,000 TPM).

---

## License

MIT — see [LICENSE](LICENSE).
