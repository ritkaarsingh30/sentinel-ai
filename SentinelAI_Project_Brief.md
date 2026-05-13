# SentinelAI — Project Brief
### Autonomous Cloud Incident Response Agent

---

## What Problem Does It Solve?

When a production application running on AWS breaks — high error rates, slow responses, crashes — someone has to investigate. Today that means:

- An engineer gets paged at 3am
- They manually dig through logs, check metrics, SSH into servers
- After 30–60 minutes they figure out the root cause
- They decide on a fix and execute it

**This is slow, manual, and expensive.**

SentinelAI replaces steps 2 and 3 with AI agents. The moment an alarm fires, a team of specialized agents automatically investigates the incident — reading logs, analyzing metrics, identifying the root cause — and delivers a ready-to-approve diagnosis to the engineer in under 2 minutes. The engineer reviews, clicks approve, and the fix executes. Their total involvement: 30 seconds.

---

## How It Works — The Flow

```
Your App throws errors
        ↓
CloudWatch detects anomaly (metric breaches threshold)
        ↓
CloudWatch Alarm fires → STATE changes: OK → ALARM
        ↓
EventBridge receives the alarm signal
        ↓
EventBridge routes it → SQS Queue (a small JSON message)
        ↓
Lambda picks up the message from SQS
        ↓
LangGraph Agent Graph starts
        ↓
Agents investigate (logs + metrics) via CloudWatch APIs
        ↓
Root cause + remediation plan produced
        ↓
⏸ PAUSE — Human Approval Required (state saved to DynamoDB)
        ↓
Engineer receives email via SNS with full diagnosis
        ↓
Engineer approves via FastAPI dashboard
        ↓
Remediation executes via AWS APIs
        ↓
Incident report saved to S3
```

---

## The Agent Architecture (LangGraph)

SentinelAI uses a **Supervisor Pattern** — one master agent that delegates to specialized sub-agents.

```
Supervisor Agent
├── Log Analyst Agent     → Reads CloudWatch Logs, finds error patterns
├── Metrics Agent         → Reads CloudWatch Metrics, correlates timeline
├── Root Cause Agent      → Synthesizes both findings into a diagnosis
├── Remediation Agent     → Proposes concrete fix steps
└── Report Agent          → Generates structured incident report
```

The Supervisor collects all results, decides when enough information is gathered, and assembles the final output before pausing for human approval.

**Why Human-in-the-Loop?**
Automated remediation without oversight is dangerous. The agent might misdiagnose. It might not know a batch job is running that looks like high CPU but is intentional. Human approval is the safety valve. This is also how real production AIOps tools work.

---

## Tech Stack

| Component | Technology | Why |
|---|---|---|
| Agent Orchestration | LangGraph | Multi-agent graph, HITL checkpointing |
| LLM | Groq (Llama 3.3 70B) | Free tier, fast inference, no token cost during dev |
| API Layer | FastAPI | Approval dashboard, incident endpoints |
| Event Trigger | CloudWatch Alarm | Detects anomalies automatically |
| Event Router | EventBridge | Routes alarm signal to SQS |
| Message Queue | SQS | Buffers events, ensures reliable delivery to Lambda |
| Entry Point | AWS Lambda | Receives SQS message, starts agent graph |
| Compute (API) | EC2 t2.micro | Hosts FastAPI app, free tier eligible |
| State Storage | DynamoDB | LangGraph checkpoints + incident history |
| Report Storage | S3 | Structured incident reports |
| Notifications | SNS | Emails engineer when approval is needed |
| LLM Observability | LangSmith | Traces agent reasoning for debugging/demo |
| Monitoring | CloudWatch | Source of all logs and metrics |
| Security | IAM Roles | No hardcoded credentials, role-based access |

---

## AWS Services — What Each One Does

**CloudWatch** — The nervous system. Every AWS service automatically sends logs and metrics here. SentinelAI reads from it but never touches the monitored app directly.

**CloudWatch Alarm** — A rule that watches a metric. When the metric crosses a threshold (e.g. error rate > 5% for 2 minutes), the alarm "fires" — its state changes from OK to ALARM, triggering the rest of the pipeline.

**EventBridge** — A smart traffic router. Receives the alarm signal and routes it to the right destination based on rules you define. Makes the system extensible — tomorrow you can add Slack notifications by adding one rule, without touching anything else.

**SQS (Simple Queue Service)** — A message queue. Holds small JSON packets ("incident messages") and delivers them to Lambda one by one. Acts as a buffer — if Lambda fails, the message stays in the queue and gets retried.

**Lambda** — The entry point. Picks up the SQS message, extracts the incident context, and kicks off the LangGraph agent graph. It's not the brain — it's the door.

**DynamoDB** — Stores two things: LangGraph's agent state (so it can pause and resume across the human approval step) and the full incident history.

**S3** — Stores the final structured incident report after resolution.

**SNS** — Sends an email to the engineer when SentinelAI needs approval.

**EC2 t2.micro** — Hosts the FastAPI approval dashboard. Free tier: 750 hours/month for 12 months.

---

## Why This Project Stands Out

Most AI portfolio projects are RAG chatbots or document Q&A tools. SentinelAI is different because it demonstrates:

1. **Multi-agent orchestration** — Supervisor pattern with specialized sub-agents, not a single chain
2. **Event-driven architecture** — Real AWS pipeline, not a manual API call
3. **Human-in-the-loop design** — Stateful agent checkpointing with explicit approval gates
4. **Production thinking** — IAM roles, SQS reliability, DynamoDB persistence, not just happy-path code
5. **Real problem domain** — AIOps is a category companies pay thousands/month for (Datadog, PagerDuty, AWS DevOps Guru)

---

## What You Need to Build It

- AWS Account (free tier is enough)
- Groq API Key (free tier)
- LangSmith Account (free tier)
- Python 3.11+
- Basic AWS console familiarity

---

## Free Tier Compatibility

| Service | Free Tier | Your Usage |
|---|---|---|
| Lambda | 1M requests/month, always free | ~50 invocations/month |
| DynamoDB | 25GB always free | Kilobytes |
| SQS | 1M messages/month, always free | ~50 messages/month |
| S3 | 5GB for 12 months | Megabytes |
| SNS | 1M publishes/month, always free | ~50 emails/month |
| EventBridge | 1M events/month, always free | ~50 events/month |
| EC2 t2.micro | 750 hours/month for 12 months | 730 hours (24/7) |
| CloudWatch | 10 alarms, 10 metrics, always free | Fine for demo |

**Total estimated cost: $0** for the first 12 months if you avoid NAT Gateways and unattached Elastic IPs.

---

## Open Source Strategy

SentinelAI will be open source. Users deploy their own instance into their own AWS account — you never touch their credentials. They:

1. Clone the repo
2. Add their own `.env` file (Groq API key, AWS region, log group name)
3. Run the setup script which creates all AWS resources in their account automatically
4. Point SentinelAI at their app's CloudWatch log group

This is how all major open source cloud tools work (Grafana, n8n, LangFlow).

**Future scope:** Cross-account monitoring via IAM role delegation — allowing SentinelAI to monitor apps in external AWS accounts without credential sharing.

---

## Build Order (5 Weeks)

| Week | Focus | AWS Involved? |
|---|---|---|
| 1 | Build LangGraph agents locally with mocked CloudWatch data | No — pure Python |
| 2 | Set up AWS infrastructure: SQS, DynamoDB, S3, Lambda, EC2 | Yes — console setup |
| 3 | Wire Lambda → LangGraph, test by manually pushing SQS messages | Yes |
| 4 | Set up CloudWatch alarm on victim app, run full end-to-end | Yes |
| 5 | FastAPI approval dashboard, LangSmith tracing, README, demo video | Yes |

---

## Resume Line

> **SentinelAI — Autonomous Cloud Incident Response Agent**
> *Python · LangGraph · FastAPI · Groq · AWS (Lambda, SQS, EventBridge, CloudWatch, DynamoDB, S3, SNS, EC2)*
>
> Built a multi-agent system that autonomously investigates AWS cloud incidents the moment a CloudWatch alarm fires, using a LangGraph supervisor pattern with specialized sub-agents for log analysis, metrics correlation, and root cause synthesis. Implemented human-in-the-loop approval workflow with DynamoDB-persisted agent state checkpointing before any remediation executes. Reduces incident triage time from ~45 minutes of manual investigation to under 2 minutes of AI-assisted diagnosis.

---

*Brief prepared: April 2026*
