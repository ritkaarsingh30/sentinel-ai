"""
Creates all missing SentinelAI AWS resources and patches .env with correct values.
Safe to re-run — skips anything that already exists.
"""

import sys
import os
import re
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3
import config

def mk(service):
    return boto3.client(
        service,
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )

env_patches = {}

print("\nSentinelAI — AWS Resource Setup")
print("=" * 45)

# ── SQS ──────────────────────────────────────────
sqs = mk("sqs")
try:
    r = sqs.get_queue_url(QueueName="sentinal-ai-incidents")
    url = r["QueueUrl"]
    print(f"  [SKIP] SQS queue already exists")
except sqs.exceptions.QueueDoesNotExist:
    r = sqs.create_queue(
        QueueName="sentinal-ai-incidents",
        Attributes={"MessageRetentionPeriod": "86400"},
    )
    url = r["QueueUrl"]
    print(f"  [OK]   SQS queue created")

print(f"         {url}")
env_patches["SQS_QUEUE_URL"] = url

# ── DynamoDB ─────────────────────────────────────
db = mk("dynamodb")

def ensure_table(name, pk, sk=None):
    try:
        db.describe_table(TableName=name)
        print(f"  [SKIP] DynamoDB {name} already exists")
    except db.exceptions.ResourceNotFoundException:
        key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
        attr_defs = [{"AttributeName": pk, "AttributeType": "S"}]
        if sk:
            key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
            attr_defs.append({"AttributeName": sk, "AttributeType": "S"})
        db.create_table(
            TableName=name,
            KeySchema=key_schema,
            AttributeDefinitions=attr_defs,
            BillingMode="PAY_PER_REQUEST",
        )
        # Wait for table to become active
        waiter = db.get_waiter("table_exists")
        waiter.wait(TableName=name, WaiterConfig={"Delay": 2, "MaxAttempts": 15})
        print(f"  [OK]   DynamoDB {name} created")

ensure_table("sentinal-ai-checkpoints", pk="thread_id", sk="checkpoint_id")
ensure_table("sentinal-ai-incidents", pk="incident_id")

# ── SNS ──────────────────────────────────────────
sns = mk("sns")

# Check if topic already exists (list all topics and match by name)
existing_arn = None
paginator = sns.get_paginator("list_topics")
for page in paginator.paginate():
    for t in page["Topics"]:
        if t["TopicArn"].endswith(":sentinal-ai-alerts"):
            existing_arn = t["TopicArn"]
            break

if existing_arn:
    print(f"  [SKIP] SNS topic already exists")
    topic_arn = existing_arn
else:
    r = sns.create_topic(Name="sentinal-ai-alerts")
    topic_arn = r["TopicArn"]
    print(f"  [OK]   SNS topic created")

print(f"         {topic_arn}")
env_patches["SNS_ALERT_TOPIC_ARN"] = topic_arn

# Check if email subscription exists; create if not
subs = sns.list_subscriptions_by_topic(TopicArn=topic_arn)
email_sub = [s for s in subs["Subscriptions"] if s["Protocol"] == "email"]
if email_sub:
    status = email_sub[0].get("SubscriptionArn", "")
    if status == "PendingConfirmation":
        print(f"  [WARN] Email subscription is pending — check your inbox and confirm it")
    else:
        print(f"  [SKIP] Email subscription already confirmed")
else:
    email = "razz.jazz30@gmail.com"
    sns.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
    print(f"  [OK]   Email subscription created for {email}")
    print(f"  [!]    Check your inbox and click 'Confirm subscription'")

# ── Patch .env ───────────────────────────────────
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
with open(env_path) as f:
    env_text = f.read()

for key, value in env_patches.items():
    pattern = rf"^{key}=.*$"
    replacement = f"{key}={value}"
    if re.search(pattern, env_text, re.MULTILINE):
        env_text = re.sub(pattern, replacement, env_text, flags=re.MULTILINE)
    else:
        env_text += f"\n{key}={value}"

with open(env_path, "w") as f:
    f.write(env_text)

print(f"\n  .env updated with correct SQS URL and SNS ARN")
print("=" * 45)
print("  Done. Run scripts/check_aws.py to verify.\n")
