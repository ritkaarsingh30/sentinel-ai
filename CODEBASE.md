# SentinelAI — Codebase Walkthrough

This document explains every file, every decision, and every bug we hit and fixed.
Read it top to bottom once and you'll understand the whole project.

---

## Table of Contents

1. [What SentinelAI Does](#what-sentinelai-does)
2. [Project Structure](#project-structure)
3. [Week 1 — Local Agents](#week-1--local-agents)
4. [Week 2 — AWS Deployment](#week-2--aws-deployment)
5. [File-by-File Reference](#file-by-file-reference)
6. [The Full Flow (End to End)](#the-full-flow-end-to-end)
7. [Bugs Fixed Along the Way](#bugs-fixed-along-the-way)
8. [Key Concepts Explained](#key-concepts-explained)

---

## What SentinelAI Does

When your app breaks on AWS (high error rate, crashes, slow responses), normally an engineer gets paged, manually digs through logs for 30–60 minutes, then decides on a fix.

SentinelAI automates the investigation. The moment a CloudWatch alarm fires:

1. 5 AI agents run automatically — reading logs, analyzing metrics, finding root cause, proposing a fix
2. The investigation pauses and emails you a full diagnosis
3. You click Approve or Decline
4. If approved, the fix plan is saved to a report in S3

**Total time from alarm to email: ~10 seconds (Lambda cold start + 5 LLM calls)**

---

## Project Structure

```
senital-ai/
│
├── agents/
│   ├── __init__.py          # empty, makes it a Python package
│   └── graph.py             # THE BRAIN — all 5 agents + graph wiring
│
├── tools/
│   ├── __init__.py          # empty
│   ├── cloudwatch_logs.py   # reads CloudWatch log events (real or mock)
│   └── cloudwatch_metrics.py# reads CloudWatch metric datapoints (real or mock)
│
├── api/
│   ├── __init__.py          # empty
│   └── main.py              # FastAPI approval dashboard (Week 3+)
│
├── victim_app/
│   ├── __init__.py          # empty
│   └── handler.py           # fake broken app — throws errors on demand
│
├── scripts/
│   ├── test_local.py        # Week 1 test runner (CLI, uses mock data)
│   ├── check_aws.py         # verifies all AWS resources exist
│   ├── setup_aws.py         # creates missing AWS resources
│   ├── deploy.py            # Week 2 — deploys everything to AWS
│   └── trigger_test.py      # fires the victim app to trip the alarm
│
├── config.py                # all env vars in one place
├── state.py                 # defines the data structure that flows through agents
├── lambda_handler.py        # AWS Lambda entry point
├── requirements.txt         # Python deps for local dev
├── requirements-lambda.txt  # Python deps for Lambda layer (no boto3, no fastapi)
├── .env                     # your secrets (never committed to git)
├── .env.example             # template showing what goes in .env
├── AWS_SETUP.md             # step-by-step AWS console guide
└── CODEBASE.md              # this file
```

---

## Week 1 — Local Agents

**Goal:** Build the AI agent brain with fake data. No AWS needed yet.

### What we built

- `state.py` — the shared data bag all agents read from and write to
- `tools/cloudwatch_logs.py` — can return real logs OR 12 hardcoded fake log events
- `tools/cloudwatch_metrics.py` — can return real metrics OR 20 hardcoded fake datapoints
- `agents/graph.py` — the full 5-agent system
- `scripts/test_local.py` — a CLI script to run the whole thing interactively

### How it worked

Running `python scripts/test_local.py` would:
1. Create a fake alarm event (connection pool exhaustion scenario)
2. Feed it through all 5 agents using Groq (real LLM calls, fake data)
3. Pause and ask "Approve remediation? [y/N]"
4. If approved, generate a structured incident report

Everything used `USE_MOCK_DATA=true` in `.env` so no AWS credentials were needed.

---

## Week 2 — AWS Deployment

**Goal:** Wire everything to real AWS. Real alarm → real agents → real email.

### What we built

- `victim_app/handler.py` — a Lambda that throws errors on demand (the "app" being monitored)
- Updated `lambda_handler.py` — parses real CloudWatch alarm events from EventBridge
- `scripts/deploy.py` — automated script that creates every AWS resource
- `scripts/trigger_test.py` — invokes the victim 25 times to trip the alarm

### What `scripts/deploy.py` creates (in order)

| Step | What it creates | Why |
|---|---|---|
| 1 | IAM execution role | Gives the Lambda permission to call CloudWatch, DynamoDB, S3, SNS |
| 2 | Lambda layer | Packages all Python deps (langgraph, langchain, groq, etc.) as a reusable layer |
| 3 | SentinelAI Lambda | The main function — runs the agent graph |
| 4 | SQS trigger | Connects SQS → Lambda so every message triggers an investigation |
| 5 | Victim app Lambda | The fake broken app |
| 6 | CloudWatch Alarm | Watches victim app errors — fires if >5 errors in 1 minute |
| 7 | EventBridge rule | Routes the alarm signal → SQS queue |

---

## File-by-File Reference

### `config.py`

Loads all environment variables. Every other file imports this instead of calling `os.getenv()` directly.

```python
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
```

The `or` chain here matters: locally you set `AWS_REGION` in `.env`. In Lambda, AWS automatically sets `AWS_DEFAULT_REGION`. This line handles both cases.

```python
def aws_credentials() -> dict:
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        return {"aws_access_key_id": ..., "aws_secret_access_key": ...}
    return {}
```

This helper is used by scripts that run locally. In Lambda we never call this — see the "Bugs Fixed" section for why.

---

### `state.py`

Defines `IncidentState` — a TypedDict (typed dictionary) that LangGraph uses as the shared state flowing between agents.

```python
class IncidentState(TypedDict):
    incident_id: str       # e.g. "INC-32EBE5D5"
    alarm_name: str        # "HighErrorRate-victim-app-prod"
    log_group: str         # "/aws/lambda/victim-app-prod"
    start_time: str        # ISO 8601 timestamp
    end_time: str

    log_findings: str      # filled in by log_analyst_node
    metrics_findings: str  # filled in by metrics_agent_node
    root_cause: str        # filled in by root_cause_agent_node
    remediation_steps: list[str]  # filled in by remediation_agent_node
    incident_report: dict  # filled in by report_agent_node

    next_agent: str        # supervisor uses this to decide where to go next
    human_approved: bool   # set to True when engineer approves

    messages: list         # LangGraph message history (auto-appended)
```

Every node (agent) receives the full state and returns a dict of fields to update. LangGraph merges the update back in automatically.

---

### `tools/cloudwatch_logs.py`

Two modes controlled by `config.USE_MOCK_DATA`:

**Mock mode (`USE_MOCK_DATA=true`):** Returns 12 hardcoded log events showing a database connection pool exhaustion — realistic enough for the LLM to produce good analysis.

**Real mode (`USE_MOCK_DATA=false`):** Calls `boto3.client("logs").filter_log_events()` to pull real log events from CloudWatch. Fetches up to 200 events, handles pagination.

```python
client = boto3.client("logs", region_name=config.AWS_REGION)
```

Note: no credentials passed explicitly. boto3 finds them automatically from the environment — this works both locally (dotenv sets env vars) and in Lambda (execution role injects `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, and `AWS_SESSION_TOKEN` automatically).

---

### `tools/cloudwatch_metrics.py`

Same pattern as logs. Mock mode returns 20 datapoints showing:
- 13 minutes of healthy baseline (~0.3–0.9% error rate, ~120ms latency)
- Then a spike: error rate jumps to 42% → peaks at 83%, latency spikes to 6500ms

Real mode calls `boto3.client("cloudwatch").get_metric_statistics()`.

---

### `agents/graph.py`

**This is the most important file.** It contains:

#### 1. The LLM

```python
_llm = ChatGroq(model="llama-3.3-70b-versatile", api_key=..., temperature=0, max_tokens=1200)
```

`temperature=0` means deterministic — the LLM always picks the most confident answer, no randomness.
`max_tokens=1200` caps the response length. Important for staying under Groq's free tier rate limits.

#### 2. `_call_llm()` — with retry logic

```python
def _call_llm(system_prompt, user_content):
    for attempt in range(4):
        try:
            return _llm.invoke(messages).content.strip()
        except Exception as e:
            if "rate_limit" in str(e) or "413" in str(e) or "429" in str(e):
                wait = 30 * (attempt + 1)  # 30s, 60s, 90s, 120s
                time.sleep(wait)
            else:
                raise
```

Groq free tier has a 12,000 tokens-per-minute limit. With 5 agents making LLM calls, we sometimes hit it. This retries automatically with increasing waits.

#### 3. The Supervisor (routing logic)

```python
def supervisor_node(state):
    current = state.get("next_agent", "log_analyst")
    return {"next_agent": current}

def route_from_supervisor(state):
    return state.get("next_agent", "__end__")
```

The supervisor is **rule-based, not LLM-based**. It just reads `state["next_agent"]` and routes there. Each sub-agent sets `next_agent` to the next step before returning.

This is intentional for Week 1/2. A real LLM-based supervisor could dynamically decide to skip agents or re-run them — that's a Week 4+ improvement.

#### 4. The 5 Agent Nodes

Each agent follows the same pattern:
1. Pull data from state (and/or from CloudWatch tools)
2. Format a prompt
3. Call `_call_llm(system_prompt, user_content)`
4. Return updated state fields + set `next_agent`

| Agent | What it does | Sets |
|---|---|---|
| `log_analyst_node` | Reads logs, finds errors, patterns, timeline | `log_findings` |
| `metrics_agent_node` | Reads metrics, quantifies severity, correlates with logs | `metrics_findings` |
| `root_cause_agent_node` | Synthesizes both findings into a root cause | `root_cause` |
| `remediation_agent_node` | Proposes concrete fix steps | `remediation_steps` |
| `human_approval_node` | **PAUSES** — waits for engineer decision | `human_approved` |
| `report_agent_node` | Writes structured JSON incident report | `incident_report` |

#### 5. The Human Approval Interrupt

```python
def human_approval_node(state):
    approved = interrupt({
        "incident_id": state["incident_id"],
        "root_cause": state["root_cause"],
        "remediation_steps": state["remediation_steps"],
    })
    return {"human_approved": bool(approved), "next_agent": "report_agent" if approved else "__end__"}
```

`interrupt()` is a LangGraph built-in. When the graph hits this, it:
1. Saves the entire state to the checkpointer (in memory locally, DynamoDB in production)
2. Returns control to the caller (Lambda or test script)
3. The graph stays "paused" — it can be resumed later

To resume, you call:
```python
graph.invoke(Command(resume=True), thread_config)   # approve
graph.invoke(Command(resume=False), thread_config)  # decline
```

#### 6. The Graph Assembly

```python
g = StateGraph(IncidentState)
g.add_node("supervisor", supervisor_node)
g.add_node("log_analyst", log_analyst_node)
# ... etc

g.set_entry_point("supervisor")
g.add_conditional_edges("supervisor", route_from_supervisor, {...})

# Every sub-agent returns to supervisor after finishing
for node in ["log_analyst", ...]:
    g.add_edge(node, "supervisor")
```

The flow is: `supervisor → log_analyst → supervisor → metrics_agent → supervisor → ...`

The supervisor always runs between agents. It reads `next_agent` from state and routes to the right node.

---

### `lambda_handler.py`

The AWS Lambda entry point. Lambda calls `handler(event, context)` whenever a message arrives on SQS.

#### Parsing two event formats

```python
def _parse_body(body: dict) -> dict:
    if body.get("source") == "aws.cloudwatch":
        # Real EventBridge event — extract alarm details
        ...
    return body  # simplified test format
```

When a real CloudWatch alarm fires, EventBridge sends a complex nested JSON. We extract the `alarmName`, `FunctionName` (to build the log group path), and compute a 30-minute time window ending at the alarm time.

When testing manually (via `trigger_test.py` → SQS directly), we send a simpler flat JSON.

#### What the handler does

```python
def handler(event, context):
    body = json.loads(records[0]["body"])
    parsed = _parse_body(body)
    
    initial_state = make_initial_state(**parsed)
    compiled_graph = build_graph()
    result = compiled_graph.invoke(initial_state, thread_config)
    
    _send_approval_email(incident_id, root_cause, remediation_steps)
```

1. Parses the SQS message
2. Creates the initial state
3. Runs the graph (which pauses at `human_approval`)
4. Sends the approval email via SNS

---

### `victim_app/handler.py`

A simple Lambda designed to fail on purpose.

```python
FAIL_RATE = float(os.getenv("FAIL_RATE", "0.0"))

def handler(event, context):
    if random.random() < FAIL_RATE:
        raise RuntimeError(random.choice(_ERRORS))
    return {"statusCode": 200, ...}
```

`deploy.py` creates this with `FAIL_RATE=0.0` (healthy by default).
`trigger_test.py` temporarily sets `FAIL_RATE=0.9` (90% failure), invokes it 25 times to generate ~22 errors, then resets it back to `0.0`.

When Lambda throws an unhandled exception, AWS records it as an `Errors` count in CloudWatch metrics. Our alarm fires when this count exceeds 5 in one minute.

---

### `scripts/deploy.py`

Fully automated deployment. Safe to re-run — every step checks if the resource already exists before creating it.

Key things it does that are easy to get wrong:

**Lambda layer zip structure:**
The zip must have packages at `python/<package>`, NOT `layer/python/<package>`.
```python
arcname = os.path.relpath(full, layer_root)  # layer_root = /path/to/layer/
```
We initially got this wrong and had to fix it (see Bugs section).

**SQS visibility timeout:**
Lambda requires the SQS visibility timeout ≥ the Lambda function timeout.
Our Lambda has a 300s timeout, so we set SQS to 360s.
```python
sqs.set_queue_attributes(QueueUrl=..., Attributes={"VisibilityTimeout": "360"})
```

**EventBridge → SQS permission:**
EventBridge can't just send to any SQS queue. You have to update the SQS resource policy to explicitly allow it.
```python
sqs.set_queue_attributes(QueueUrl=..., Attributes={"Policy": json.dumps({...})})
```

**IAM propagation delay:**
After creating an IAM role, AWS takes ~10 seconds to propagate it globally. If you immediately create a Lambda using that role, it fails. The script waits 12 seconds.
```python
time.sleep(12)
```

---

### `api/main.py`

FastAPI approval dashboard — used in Week 3+ when running on EC2.

Three endpoints:
- `GET /incidents/{id}` — shows the full investigation (root cause, findings, remediation steps)
- `POST /incidents/{id}/approve` — resumes the paused LangGraph with `Command(resume=True)`
- `POST /incidents/{id}/decline` — resumes with `Command(resume=False)`

Not deployed yet. For now, approvals happen via the `test_local.py` CLI prompt.

---

## The Full Flow (End to End)

### What happens when you run `python scripts/trigger_test.py`

```
1. trigger_test.py sets FAIL_RATE=0.9 on victim-app-prod Lambda
2. Invokes victim-app-prod 25 times in parallel
3. ~22 of those throw RuntimeError (Lambda records them as Errors metric)
4. CloudWatch sees Errors > 5 in 1 minute → alarm state: OK → ALARM
5. EventBridge receives the state change automatically (no extra config needed)
6. EventBridge matches our rule → sends message to SQS sentinal-ai-incidents
7. SQS has a message → triggers sentinal-ai Lambda
8. Lambda parses the EventBridge message → extracts alarm name, log group, time window
9. Lambda creates initial IncidentState and starts the LangGraph
10. Graph runs: supervisor → log_analyst → supervisor → metrics_agent → ...
11. log_analyst: reads real CloudWatch logs from /aws/lambda/victim-app-prod
12. metrics_agent: reads real CloudWatch metrics for victim-app-prod
13. root_cause, remediation agents: synthesize findings using Groq LLM
14. Graph hits human_approval → interrupt() → graph pauses
15. Lambda continues past the interrupt, calls _send_approval_email()
16. SNS sends email to razz.jazz30@gmail.com
17. Email contains incident ID, root cause, proposed fix
18. (Week 3) Engineer clicks approve link in email
19. FastAPI endpoint calls graph.invoke(Command(resume=True), ...)
20. Report agent generates structured JSON incident report
21. (Week 3) Report saved to S3
```

---

## Bugs Fixed Along the Way

These are the actual problems we hit in order. Understanding them helps you debug similar issues.

---

### Bug 1: `groq` version conflict
**What happened:** `langchain-groq 1.1.2` pins `groq<1.0.0`, but we upgraded `groq` to `1.2.0`.
**Why it was fine anyway:** The groq 1.x SDK is backwards-compatible. We tested it and it worked.
**Fix:** Removed `groq>=1.2.0` from `requirements-lambda.txt`. Let `langchain-groq` resolve the groq version itself. Added explicit versions to `requirements.txt` reflecting what's actually installed.

---

### Bug 2: IAM user missing permissions
**What happened:** `sentinal-ai-dev` could not create IAM roles (`AccessDenied`).
**Why:** We only gave it the 5 service-specific policies initially (SQS, DynamoDB, S3, SNS, CloudWatch). Creating Lambda roles requires IAM permissions.
**Fix:** Added `IAMFullAccess` policy. Then also added an inline `lambda:*` policy because `AWSLambda_FullAccess` doesn't actually include all Lambda actions (it's a curated console-helper policy, not a true full-access policy).

---

### Bug 3: Lambda layer zip — wrong internal structure
**What happened:** Lambda couldn't find `dotenv` (or any package). Error: `No module named 'dotenv'`.
**Root cause:** The zip was structured as `layer/python/dotenv/...` but Lambda expects `python/dotenv/...` (relative to zip root).
**The bad code:**
```python
arcname = os.path.relpath(full, ROOT)  # ROOT = project root
# produced: layer/python/dotenv/__init__.py  ← WRONG
```
**The fix:**
```python
arcname = os.path.relpath(full, layer_root)  # layer_root = project/layer/
# produces: python/dotenv/__init__.py  ← CORRECT
```

---

### Bug 4: SQS visibility timeout too short
**What happened:** `InvalidParameterValueException: Queue visibility timeout: 30 seconds is less than Function timeout: 300 seconds`.
**Why:** AWS requires the SQS visibility timeout ≥ Lambda timeout. If Lambda takes longer than the visibility timeout, SQS thinks the message was abandoned and retries it — causing duplicate invocations.
**Fix:** Set SQS visibility timeout to 360 seconds before creating the event source mapping.

---

### Bug 5: Lambda passing `None` credentials breaks execution role
**What happened:** Lambda ran, reached CloudWatch API, but got `UnrecognizedClientException: security token is invalid`.
**Root cause:** In Lambda, AWS injects three env vars: `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, AND `AWS_SESSION_TOKEN`. These are *temporary* STS credentials. Our tool code was doing:
```python
boto3.client("logs",
    aws_access_key_id=config.AWS_ACCESS_KEY_ID,   # got the temp key
    aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,  # got the temp secret
    # but NEVER passed aws_session_token ← INVALID without it
)
```
**Fix:** Don't pass credentials explicitly in Lambda. Let boto3 find all three automatically from the environment:
```python
boto3.client("logs", region_name=config.AWS_REGION)  # boto3 finds everything itself
```
This works locally too — `load_dotenv()` sets `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in `os.environ`, and boto3 picks them up.

---

### Bug 6: Groq rate limit (413 / rate_limit_exceeded)
**What happened:** Log analyst agent sent 14,076 tokens in one request. Groq free tier limit is 12,000 TPM.
**Fix (two-part):**
1. Cap log events at 25 before sending to LLM: `events[:25]`
2. Set `max_tokens=1200` on the LLM to cap response length
3. Added retry logic in `_call_llm()` — if rate limited, wait 30/60/90s and retry

---

### Bug 7: S3 bucket in wrong region for Lambda layer upload
**What happened:** When trying to publish a Lambda layer using an S3 source, got `PermanentRedirect: The bucket is in region eu-north-1`.
**Why:** Lambda and S3 must be in the same region for layer uploads via S3 reference. Our S3 bucket was created in eu-north-1, but Lambda is in us-east-1.
**Fix:** Upload the layer zip directly to Lambda (`ZipFile=bytes`) instead of via S3. At 17.8 MB it's well under Lambda's 50 MB direct upload limit.

---

## Key Concepts Explained

### Why LangGraph instead of a simple loop?

A simple loop calling 5 functions in sequence would work — but LangGraph gives you:
1. **State management** — all agent data lives in one typed structure, no global variables
2. **Human-in-the-loop** — `interrupt()` pauses the graph mid-execution and saves state so it can resume days later from the exact same point
3. **Checkpointing** — if Lambda crashes halfway through, the state is saved and can be recovered
4. **Observability** — LangSmith traces every node, input, output, and duration

### Why SQS between EventBridge and Lambda?

You could route EventBridge directly to Lambda — but SQS gives you:
- **Retry on failure** — if Lambda crashes, the message stays in SQS and tries again (up to 3 times)
- **Buffering** — if 10 alarms fire at once, SQS queues them instead of invoking 10 Lambdas simultaneously

### Why a Lambda Layer?

Lambda functions have a 250 MB unzipped size limit. All our Python packages (langgraph, langchain, groq, pydantic, etc.) take ~80 MB. Without a layer, you'd have to include those packages in every deployment zip. With a layer:
- The layer is uploaded once and reused across deployments
- Function code updates deploy in seconds (small zip, just your files)
- The layer is cached on Lambda's warm containers

### Why `USE_MOCK_DATA`?

Running the full pipeline against real CloudWatch requires:
- Actual errors in a real log group
- Waiting for metrics to populate (1-minute resolution)
- AWS credentials on every machine

`USE_MOCK_DATA=true` lets you run the full agent pipeline instantly on any machine with just a Groq API key. The mock data is realistic enough that the LLM produces high-quality analysis.

### What is an IAM Execution Role?

When Lambda runs your code and your code calls CloudWatch/DynamoDB/S3, AWS needs to know if you're allowed to do that. The execution role is an IAM role that Lambda *assumes* when running — it's like a temporary identity. The role has policies attached that say "this Lambda can read CloudWatch logs" etc. 

This is why we never put `AWS_ACCESS_KEY_ID` in the Lambda's environment variables — the role handles authentication automatically, and AWS injects temporary credentials that rotate every ~15 minutes.

---

## What's Left (Week 3–5)

| Week | What to build |
|---|---|
| 3 | FastAPI dashboard on EC2 — approve/decline from a web UI instead of email links |
| 3 | DynamoDB checkpointer — replace `MemorySaver` so state survives Lambda restarts |
| 4 | Real victim app — actual app with configurable bugs (bad SQL, memory leak, etc.) |
| 4 | CloudWatch Dashboard — visualize incidents over time |
| 5 | LangSmith tracing — full observability into agent reasoning |
| 5 | README + demo video for GitHub |
