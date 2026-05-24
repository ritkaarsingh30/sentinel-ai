"""
Verifies all AWS resources exist and credentials work.
Run this once after filling in .env to confirm setup is correct.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import boto3
from botocore.exceptions import ClientError, NoCredentialsError
import config

OK = "  [OK]"
FAIL = "  [FAIL]"

errors = []

def check(label, fn):
    try:
        fn()
        print(f"{OK}  {label}")
    except Exception as e:
        print(f"{FAIL} {label}")
        print(f"         {e}")
        errors.append(label)

def mk(service):
    return boto3.client(
        service,
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )

print("\nSentinelAI — AWS Connectivity Check")
print("=" * 45)

# Credentials
def creds():
    sts = mk("sts")
    identity = sts.get_caller_identity()
    print(f"\n  Account : {identity['Account']}")
    print(f"  User    : {identity['Arn']}")

check("AWS credentials", creds)

# SQS
def sqs():
    assert config.SQS_QUEUE_URL, "SQS_QUEUE_URL not set in .env"
    sqs = mk("sqs")
    sqs.get_queue_attributes(QueueUrl=config.SQS_QUEUE_URL, AttributeNames=["QueueArn"])

check(f"SQS queue ({config.SQS_QUEUE_URL.split('/')[-1] if config.SQS_QUEUE_URL else 'not set'})", sqs)

# DynamoDB checkpoints table
def dynamo_checkpoints():
    db = mk("dynamodb")
    db.describe_table(TableName=config.DYNAMODB_CHECKPOINT_TABLE)

check(f"DynamoDB table ({config.DYNAMODB_CHECKPOINT_TABLE})", dynamo_checkpoints)

# DynamoDB incidents table
def dynamo_incidents():
    db = mk("dynamodb")
    db.describe_table(TableName=config.DYNAMODB_INCIDENTS_TABLE)

check(f"DynamoDB table ({config.DYNAMODB_INCIDENTS_TABLE})", dynamo_incidents)

# S3
def s3():
    assert config.S3_REPORTS_BUCKET, "S3_REPORTS_BUCKET not set in .env"
    s3 = mk("s3")
    s3.head_bucket(Bucket=config.S3_REPORTS_BUCKET)

check(f"S3 bucket ({config.S3_REPORTS_BUCKET})", s3)

# SNS
def sns():
    assert config.SNS_ALERT_TOPIC_ARN, "SNS_ALERT_TOPIC_ARN not set in .env"
    sns = mk("sns")
    sns.get_topic_attributes(TopicArn=config.SNS_ALERT_TOPIC_ARN)

check(f"SNS topic ({config.SNS_ALERT_TOPIC_ARN.split(':')[-1] if config.SNS_ALERT_TOPIC_ARN else 'not set'})", sns)

# CloudWatch (read-only)
def cloudwatch():
    cw = mk("cloudwatch")
    cw.list_metrics(Namespace="AWS/Lambda", MetricName="Errors")

check("CloudWatch read access", cloudwatch)

# Groq key present
def groq_key():
    assert config.GROQ_API_KEY and config.GROQ_API_KEY.startswith("gsk_"), \
        "GROQ_API_KEY missing or doesn't start with 'gsk_'"

check("Groq API key (format)", groq_key)

# SentinelAI Lambda
def sentinel_lambda():
    lam = mk("lambda")
    r = lam.get_function(FunctionName="sentinal-ai")
    state = r["Configuration"]["State"]
    assert state == "Active", f"Lambda state is {state}, expected Active"

check("SentinelAI Lambda (sentinal-ai)", sentinel_lambda)

# API Lambda + Function URL
def api_lambda():
    lam = mk("lambda")
    lam.get_function(FunctionName="sentinal-ai-api")
    r = lam.get_function_url_config(FunctionName="sentinal-ai-api")
    url = r["FunctionUrl"]
    env = lam.get_function_configuration(FunctionName="sentinal-ai")["Environment"]["Variables"]
    api_base = env.get("API_BASE_URL", "")
    assert api_base and "localhost" not in api_base, \
        f"API_BASE_URL in sentinal-ai Lambda is '{api_base}' — run deploy.py to wire the real URL"
    print(f"\n  API URL : {url}")

check("API Lambda + Function URL (sentinal-ai-api)", api_lambda)

# Victim app Lambda
def victim_lambda():
    lam = mk("lambda")
    lam.get_function(FunctionName="victim-app-prod")

check("Victim app Lambda (victim-app-prod)", victim_lambda)

# CloudWatch Alarm
def cw_alarm():
    cw = mk("cloudwatch")
    alarms = cw.describe_alarms(AlarmNames=["HighErrorRate-victim-app-prod"])["MetricAlarms"]
    assert alarms, "Alarm 'HighErrorRate-victim-app-prod' not found"

check("CloudWatch Alarm (HighErrorRate-victim-app-prod)", cw_alarm)

# Summary
print("\n" + "=" * 45)
if errors:
    print(f"  {len(errors)} check(s) failed: {', '.join(errors)}")
    print("  Fix the issues above and re-run.\n")
    sys.exit(1)
else:
    print("  All checks passed. SentinelAI is fully operational.\n")
