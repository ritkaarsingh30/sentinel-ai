import os
from dotenv import load_dotenv

load_dotenv()

GROQ_API_KEY = os.getenv("GROQ_API_KEY")
MODEL_NAME = "llama-3.3-70b-versatile"

# Lambda sets AWS_DEFAULT_REGION automatically; local dev uses AWS_REGION from .env
AWS_REGION = os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION", "us-east-1")
API_BASE_URL = os.getenv("API_BASE_URL", "http://localhost:8000")
AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

SQS_QUEUE_URL = os.getenv("SQS_QUEUE_URL")
DYNAMODB_CHECKPOINT_TABLE = os.getenv("DYNAMODB_CHECKPOINT_TABLE", "sentinal-ai-checkpoints")
DYNAMODB_CHECKPOINT_WRITES_TABLE = os.getenv("DYNAMODB_CHECKPOINT_WRITES_TABLE", "sentinal-ai-checkpoint-writes")
DYNAMODB_INCIDENTS_TABLE = os.getenv("DYNAMODB_INCIDENTS_TABLE", "sentinal-ai-incidents")
S3_REPORTS_BUCKET = os.getenv("S3_REPORTS_BUCKET")
SNS_ALERT_TOPIC_ARN = os.getenv("SNS_ALERT_TOPIC_ARN")

USE_MOCK_DATA = os.getenv("USE_MOCK_DATA", "true").lower() == "true"


def aws_credentials() -> dict:
    """Return explicit credential kwargs for boto3 only when running locally.
    In Lambda, passing None breaks the execution role — return empty dict instead."""
    if AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY:
        return {
            "aws_access_key_id": AWS_ACCESS_KEY_ID,
            "aws_secret_access_key": AWS_SECRET_ACCESS_KEY,
        }
    return {}
