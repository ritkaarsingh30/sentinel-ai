"""
SentinelAI — One-Command Setup

Run this ONCE on a fresh AWS account to create every resource and configure
your environment. Safe to re-run — skips anything that already exists.

Requirements:
  - Python 3.12+ with pip
  - AWS account (IAM user with AdministratorAccess or the permissions listed below)
  - Groq API key  (free at https://console.groq.com)

Usage:
  python scripts/setup.py
"""

import sys
import os
import re
import subprocess
import json
import time
import getpass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")
sys.path.insert(0, ROOT)


# ── Helpers ───────────────────────────────────────────────────────────────────

def step(msg):
    print(f"\n{'─'*50}\n  {msg}\n{'─'*50}")

def ok(msg):   print(f"  [OK]   {msg}")
def skip(msg): print(f"  [SKIP] {msg}")
def warn(msg): print(f"  [WARN] {msg}")
def fail(msg): print(f"  [FAIL] {msg}"); sys.exit(1)

def prompt(label, default=None, secret=False):
    """Prompt the user for input, showing a default if one exists."""
    shown_default = ("*" * 8) if (secret and default) else default
    suffix = f" [{shown_default}]" if default else ""
    full_label = f"  {label}{suffix}: "
    if secret:
        value = getpass.getpass(full_label).strip()
    else:
        value = input(full_label).strip()
    return value or default or ""

def read_env() -> dict:
    """Parse the current .env file into a dict. Returns {} if missing."""
    if not os.path.exists(ENV_PATH):
        return {}
    result = {}
    with open(ENV_PATH) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                result[k.strip()] = v.strip()
    return result

def write_env(values: dict):
    """Write or update .env with the given key=value pairs."""
    if os.path.exists(ENV_PATH):
        with open(ENV_PATH) as f:
            text = f.read()
    else:
        text = ""

    for key, value in values.items():
        if re.search(rf"^{re.escape(key)}=", text, re.MULTILINE):
            text = re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, flags=re.MULTILINE)
        else:
            text += f"\n{key}={value}"

    with open(ENV_PATH, "w") as f:
        f.write(text.lstrip("\n"))

def mk(service, key_id, secret, region):
    import boto3
    return boto3.client(service, region_name=region,
                        aws_access_key_id=key_id,
                        aws_secret_access_key=secret)


# ── Step 0: Collect credentials ───────────────────────────────────────────────

def collect_credentials() -> dict:
    existing = read_env()

    print("\n" + "=" * 50)
    print("  SentinelAI — First-Time Setup")
    print("=" * 50)
    print("""
  This wizard will create all AWS resources and write
  your .env file automatically. You need:

    • AWS access key + secret (IAM user with full access)
    • Alert email address (where approval emails go)
    • Groq API key  →  https://console.groq.com
""")

    key_id   = prompt("AWS Access Key ID",     existing.get("AWS_ACCESS_KEY_ID"), secret=True)
    secret   = prompt("AWS Secret Access Key", existing.get("AWS_SECRET_ACCESS_KEY"), secret=True)
    region   = prompt("AWS Region",            existing.get("AWS_REGION") or "us-east-1")
    email    = prompt("Alert email address",   existing.get("_SETUP_EMAIL"))
    groq_key = prompt("Groq API key",          existing.get("GROQ_API_KEY"), secret=True)

    if not all([key_id, secret, region, email, groq_key]):
        fail("All fields are required. Re-run and fill in every prompt.")

    return {"key_id": key_id, "secret": secret, "region": region,
            "email": email, "groq_key": groq_key}


# ── Step 1: Verify credentials ────────────────────────────────────────────────

def verify_credentials(c: dict) -> str:
    step("Verifying AWS credentials")
    import boto3
    try:
        sts = mk("sts", c["key_id"], c["secret"], c["region"])
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        ok(f"Account: {account_id}  |  User: {identity['Arn'].split('/')[-1]}")
        return account_id
    except Exception as e:
        fail(f"Credentials invalid: {e}")


# ── Step 2: Install Python dependencies ───────────────────────────────────────

def install_deps():
    step("Installing Python dependencies")
    req = os.path.join(ROOT, "requirements.txt")
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req, "-q", "--upgrade"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        warn(f"pip had errors:\n{result.stderr[-500:]}")
    else:
        ok("All packages installed")


# ── Step 3: Create S3 bucket ──────────────────────────────────────────────────

def ensure_s3_bucket(c: dict, account_id: str) -> str:
    step("S3 reports bucket")
    s3 = mk("s3", c["key_id"], c["secret"], c["region"])
    suffix = account_id[-6:]
    bucket = f"sentinal-ai-reports-{suffix}"

    try:
        s3.head_bucket(Bucket=bucket)
        skip(f"{bucket} already exists")
        return bucket
    except Exception:
        pass

    try:
        if c["region"] == "us-east-1":
            s3.create_bucket(Bucket=bucket)
        else:
            s3.create_bucket(
                Bucket=bucket,
                CreateBucketConfiguration={"LocationConstraint": c["region"]},
            )
        ok(f"Created: {bucket}")
    except Exception as e:
        fail(f"Could not create S3 bucket: {e}")

    return bucket


# ── Step 4: Create SQS queue ──────────────────────────────────────────────────

def ensure_sqs_queue(c: dict) -> str:
    step("SQS queue")
    sqs = mk("sqs", c["key_id"], c["secret"], c["region"])
    try:
        url = sqs.get_queue_url(QueueName="sentinal-ai-incidents")["QueueUrl"]
        skip(f"Queue already exists: {url}")
        return url
    except sqs.exceptions.QueueDoesNotExist:
        pass

    url = sqs.create_queue(
        QueueName="sentinal-ai-incidents",
        Attributes={"MessageRetentionPeriod": "86400", "VisibilityTimeout": "360"},
    )["QueueUrl"]
    ok(f"Created: {url}")
    return url


# ── Step 5: Create SNS topic + email subscription ─────────────────────────────

def ensure_sns_topic(c: dict, email: str) -> str:
    step("SNS alert topic")
    sns = mk("sns", c["key_id"], c["secret"], c["region"])

    # Find or create topic
    topic_arn = None
    for page in sns.get_paginator("list_topics").paginate():
        for t in page["Topics"]:
            if t["TopicArn"].endswith(":sentinal-ai-alerts"):
                topic_arn = t["TopicArn"]
                break

    if topic_arn:
        skip(f"Topic already exists: {topic_arn}")
    else:
        topic_arn = sns.create_topic(Name="sentinal-ai-alerts")["TopicArn"]
        ok(f"Created: {topic_arn}")

    # Check email subscription
    subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)["Subscriptions"]
    email_sub = [s for s in subs if s["Protocol"] == "email"]

    if email_sub:
        arn = email_sub[0].get("SubscriptionArn", "PendingConfirmation")
        if arn == "PendingConfirmation":
            warn(f"Email to {email_sub[0]['Endpoint']} is still pending — check your inbox")
        else:
            skip(f"Email subscription already confirmed for {email_sub[0]['Endpoint']}")
    else:
        sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        ok(f"Subscribed {email} — check your inbox and click 'Confirm subscription'")
        print("\n  !! IMPORTANT: You must confirm the subscription email before")
        print("     approval emails will be delivered. Check your inbox now.")
        print("     (You can continue setup; confirm while deploy runs)\n")

    return topic_arn


# ── Step 6: Write .env ────────────────────────────────────────────────────────

def write_dotenv(c: dict, account_id: str, bucket: str, queue_url: str, sns_arn: str):
    step("Writing .env")
    values = {
        "GROQ_API_KEY":                      c["groq_key"],
        "AWS_REGION":                        c["region"],
        "AWS_ACCESS_KEY_ID":                 c["key_id"],
        "AWS_SECRET_ACCESS_KEY":             c["secret"],
        "SQS_QUEUE_URL":                     queue_url,
        "DYNAMODB_CHECKPOINT_TABLE":         "sentinal-ai-checkpoints",
        "DYNAMODB_CHECKPOINT_WRITES_TABLE":  "sentinal-ai-checkpoint-writes",
        "DYNAMODB_INCIDENTS_TABLE":          "sentinal-ai-incidents",
        "S3_REPORTS_BUCKET":                 bucket,
        "SNS_ALERT_TOPIC_ARN":               sns_arn,
        "LANGCHAIN_TRACING_V2":              "false",
        "LANGCHAIN_API_KEY":                 "",
        "LANGCHAIN_PROJECT":                 "sentinal-ai",
        "USE_MOCK_DATA":                     "true",
        "_SETUP_EMAIL":                      c["email"],  # remembered for re-runs
    }
    write_env(values)
    ok(f".env written at {ENV_PATH}")


# ── Step 7: Run deploy.py ─────────────────────────────────────────────────────

def run_deploy():
    step("Deploying all AWS resources (this takes ~3 minutes)")
    deploy_script = os.path.join(ROOT, "scripts", "deploy.py")
    result = subprocess.run([sys.executable, deploy_script])
    if result.returncode != 0:
        fail("deploy.py failed — see output above for details")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    creds      = collect_credentials()
    account_id = verify_credentials(creds)
    install_deps()
    bucket     = ensure_s3_bucket(creds, account_id)
    queue_url  = ensure_sqs_queue(creds)
    sns_arn    = ensure_sns_topic(creds, creds["email"])
    write_dotenv(creds, account_id, bucket, queue_url, sns_arn)
    run_deploy()

    # Read the API URL from the updated env
    env = read_env()
    api_url = env.get("API_BASE_URL", "(see deploy output above)")

    print("\n" + "=" * 50)
    print("  SentinelAI is ready!")
    print("=" * 50)
    print(f"""
  Resources created in AWS account {account_id}:
    SQS queue    : sentinal-ai-incidents
    SNS topic    : sentinal-ai-alerts → {creds['email']}
    S3 bucket    : {bucket}
    DynamoDB     : 3 tables (checkpoints, writes, incidents)
    Lambda       : sentinal-ai  +  victim-app-prod  +  sentinal-ai-api
    API Gateway  : {api_url}
    CW Dashboard : sentinal-ai-dashboard
    CW Alarm     : HighErrorRate-victim-app-prod

  Next steps:
    1. Confirm the SNS subscription email in your inbox (if not done)
    2. Fire a test:   python scripts/trigger_test.py
    3. Check health:  python scripts/check_aws.py
    4. Understand it: read UNDERSTANDING.md
""")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
