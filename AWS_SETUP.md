# AWS Setup Guide — SentinelAI

This guide walks you through every AWS resource SentinelAI needs.
No prior AWS experience required. Every step includes what to click, what to type, and why it matters.

**Time to complete:** ~30 minutes  
**Cost:** $0 (all free tier)

---

## Before You Start

You need an AWS account. If you don't have one:
1. Go to [aws.amazon.com](https://aws.amazon.com) → click **Create an AWS Account**
2. Enter your email, create a root password
3. Choose **Free** (Basic Support)
4. You'll need a credit card — AWS won't charge you unless you exceed free tier limits
5. Verify your phone number

> **Important:** After creating your account, do NOT use the root account for daily work.
> We'll create a separate user (IAM user) with limited permissions. This is standard security practice.

---

## Step 1 — Create an IAM User

**What is IAM?** IAM (Identity and Access Management) controls who can do what in your AWS account.
Instead of using your main account credentials in code, you create a dedicated user with only the permissions SentinelAI needs.

### 1.1 Open IAM

- In the AWS console top search bar, type `IAM` → click **IAM**
- In the left sidebar, click **Users**
- Click the orange **Create user** button

### 1.2 User details

- **User name:** `sentinal-ai-dev`
- Click **Next**

### 1.3 Set permissions

- Select **Attach policies directly**
- In the search box, search for and check each of these 5 policies:

| Policy name | What it allows |
|---|---|
| `AmazonSQSFullAccess` | Read/write the incident queue |
| `AmazonDynamoDBFullAccess` | Read/write agent state and incident history |
| `AmazonS3FullAccess` | Save incident reports |
| `AmazonSNSFullAccess` | Send approval emails |
| `CloudWatchReadOnlyAccess` | Read logs and metrics (read-only is enough) |

> To find each one: type the name in the search box, check the checkbox, then search for the next one.
> You should have 5 checked when done.

- Click **Next** → **Create user**

### 1.4 Create access keys

Access keys are like a username + password for code (not humans).

- Click on the user you just created (`sentinal-ai-dev`)
- Click the **Security credentials** tab
- Scroll down to **Access keys** → click **Create access key**
- Select **Application running outside AWS** → click **Next** → **Create access key**
- You will see:
  - **Access key ID** — starts with `AKIA...`
  - **Secret access key** — a long random string

> **Copy both values now.** The secret key is shown ONCE and cannot be retrieved again.
> Paste them into your `.env` file:
> ```
> AWS_ACCESS_KEY_ID=AKIA...
> AWS_SECRET_ACCESS_KEY=...
> ```

- Click **Done**

---

## Step 2 — Create the SQS Queue

**What is SQS?** A message queue. When a CloudWatch alarm fires, it drops a small JSON message here.
Lambda picks it up and starts the investigation. If Lambda crashes, the message stays in the queue and retries automatically.

### 2.1 Open SQS

- Search bar → type `SQS` → click **Simple Queue Service**
- Click **Create queue**

### 2.2 Configure the queue

- **Type:** Standard (not FIFO)
- **Name:** `sentinal-ai-incidents`
- Scroll down — leave all other settings as defaults
- Click **Create queue**

### 2.3 Copy the Queue URL

After creation you'll see a details page. Copy the **URL** — it looks like:
```
https://sqs.us-east-1.amazonaws.com/123456789012/sentinal-ai-incidents
```

Paste it into `.env`:
```
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/...
```

---

## Step 3 — Create DynamoDB Tables

**What is DynamoDB?** A fast NoSQL database. SentinelAI uses it for two things:
1. **Checkpoints table** — LangGraph saves the agent's state here when it pauses for your approval. This lets the graph survive across Lambda invocations.
2. **Incidents table** — stores a record of every incident for history/auditing.

### 3.1 Open DynamoDB

- Search bar → `DynamoDB` → click **DynamoDB**
- Click **Create table**

### 3.2 Create the checkpoints table

- **Table name:** `sentinal-ai-checkpoints`
- **Partition key:** `thread_id` — type: **String**
- **Sort key:** `checkpoint_id` — type: **String**
- **Table settings:** select **Customize settings**
- Under **Read/write capacity settings** → select **On-demand**
  - *(On-demand = you pay per request, not per hour. Free tier covers thousands of requests.)*
- Click **Create table**

### 3.3 Create the incidents table

- Click **Create table** again
- **Table name:** `sentinal-ai-incidents`
- **Partition key:** `incident_id` — type: **String**
- No sort key needed
- **Table settings:** On-demand (same as above)
- Click **Create table**

> You now have two tables. No data in them yet — that's fine.

---

## Step 4 — Create an S3 Bucket

**What is S3?** File storage. After an incident is resolved, SentinelAI saves the structured incident report here as a JSON file. Think of it like Google Drive for AWS.

### 4.1 Open S3

- Search bar → `S3` → click **S3**
- Click **Create bucket**

### 4.2 Configure the bucket

- **Bucket name:** `sentinal-ai-reports-` followed by a unique suffix
  - S3 bucket names are globally unique across ALL AWS accounts
  - Use something like your initials + 4 random digits: `sentinal-ai-reports-rj7291`
  - Write this name down — you'll need it in `.env`
- **AWS Region:** `us-east-1`
  - Keep everything in the same region to avoid data transfer costs
- **Block all public access:** leave all 4 checkboxes **checked** (this is the default and correct)
- Leave everything else as defaults
- Click **Create bucket**

Paste the bucket name into `.env`:
```
S3_REPORTS_BUCKET=sentinal-ai-reports-rj7291
```

---

## Step 5 — Create an SNS Topic and Email Subscription

**What is SNS?** A notification service. When SentinelAI finishes its investigation and needs your approval, it sends you an email through SNS with the diagnosis and an approve/decline link.

### 5.1 Open SNS

- Search bar → `SNS` → click **Simple Notification Service**
- In the left sidebar, click **Topics**
- Click **Create topic**

### 5.2 Configure the topic

- **Type:** Standard
- **Name:** `sentinal-ai-alerts`
- Leave everything else as defaults
- Click **Create topic**

### 5.3 Copy the Topic ARN

On the topic details page, copy the **ARN** — it looks like:
```
arn:aws:sns:us-east-1:123456789012:sentinal-ai-alerts
```

Paste into `.env`:
```
SNS_ALERT_TOPIC_ARN=arn:aws:sns:us-east-1:...
```

### 5.4 Subscribe your email

- On the same topic details page, click **Create subscription**
- **Protocol:** Email
- **Endpoint:** your email address
- Click **Create subscription**

> **Check your inbox now.** AWS sends a confirmation email with a link.
> You MUST click "Confirm subscription" in that email or SNS will never deliver to you.

---

## Step 6 — Get Your Groq API Key

**What is Groq?** The AI inference provider. SentinelAI uses Groq to run Llama 3.3 70B — it's free during development.

1. Go to [console.groq.com](https://console.groq.com)
2. Sign up / log in
3. Click **API Keys** in the left sidebar
4. Click **Create API Key**
5. Name it `sentinal-ai`, copy the key

Paste into `.env`:
```
GROQ_API_KEY=gsk_...
```

---

## Step 7 — Get Your LangSmith API Key (Optional but Recommended)

**What is LangSmith?** A dashboard that shows you every step of the agent's reasoning — which agent ran, what the LLM said, how long it took. Very useful for debugging.

1. Go to [smith.langchain.com](https://smith.langchain.com)
2. Sign up → go to **Settings** → **API Keys**
3. Click **Create API Key**, copy it

Paste into `.env`:
```
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=lsv2_...
LANGCHAIN_PROJECT=sentinal-ai
```

---

## Step 8 — Verify Your .env File

Your completed `.env` file (copied from `.env.example`) should now look like this:

```env
# LLM
GROQ_API_KEY=gsk_your_actual_key

# AWS credentials
AWS_REGION=us-east-1
AWS_ACCESS_KEY_ID=AKIA_your_actual_key
AWS_SECRET_ACCESS_KEY=your_actual_secret

# SQS
SQS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/YOUR_ACCOUNT/sentinal-ai-incidents

# DynamoDB
DYNAMODB_CHECKPOINT_TABLE=sentinal-ai-checkpoints
DYNAMODB_INCIDENTS_TABLE=sentinal-ai-incidents

# S3
S3_REPORTS_BUCKET=sentinal-ai-reports-YOUR_SUFFIX

# SNS
SNS_ALERT_TOPIC_ARN=arn:aws:sns:us-east-1:YOUR_ACCOUNT:sentinal-ai-alerts

# LangSmith
LANGCHAIN_TRACING_V2=true
LANGCHAIN_API_KEY=lsv2_your_actual_key
LANGCHAIN_PROJECT=sentinal-ai

# Week 1: keep this true. Week 2+: set to false
USE_MOCK_DATA=true
```

---

## Step 9 — Run the Local Test

With your `.env` filled in (only `GROQ_API_KEY` is required for Week 1):

```bash
cd /path/to/senital-ai

# Install dependencies
pip install -r requirements.txt

# Run the local test (uses mock AWS data)
python scripts/test_local.py
```

You should see the 5 agents run in sequence, then a prompt asking you to approve/decline.
If it completes and prints an incident report, everything is working.

---

## What Comes in Week 2 (AWS Wiring)

These are NOT needed yet. You'll set them up after the agents are working locally:

| Resource | Purpose | When |
|---|---|---|
| Lambda function | Entry point — triggered by SQS | Week 2 |
| EventBridge rule | Routes CloudWatch alarm → SQS | Week 2 |
| CloudWatch Alarm | Detects your app's error spike | Week 4 |
| EC2 t2.micro | Hosts the FastAPI approval dashboard | Week 3 |

---

## Costs Checklist

Before you leave the AWS console, verify you haven't accidentally created anything expensive:

- [ ] No NAT Gateways created (~$32/month each)
- [ ] No Elastic IPs that are unattached (~$3.60/month each)
- [ ] EC2 not launched yet (when you do, use t2.micro only)
- [ ] DynamoDB tables set to **On-demand**, not provisioned with high capacity

If you're ever unsure what's running, go to **AWS Cost Explorer** or **Billing Dashboard** and check.

---

## Troubleshooting

**"Access Denied" error when running code**
→ The IAM user is missing a permission. Go to IAM → Users → `sentinal-ai-dev` → Permissions and check all 5 policies are attached.

**No confirmation email from SNS**
→ Check spam. If still nothing, go to SNS → Topics → `sentinal-ai-alerts` → Subscriptions and verify the status. Delete and recreate the subscription if needed.

**"Bucket name already exists" when creating S3**
→ S3 bucket names are globally unique. Add more characters to your suffix.

**LangGraph import errors**
→ Run `pip install -r requirements.txt` again. Make sure you're in the right virtual environment.
