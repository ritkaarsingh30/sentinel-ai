"""
Week 4 deployment — runs once, idempotent (safe to re-run).

Creates in order:
  1. IAM execution role for both Lambdas
  2. DynamoDB tables (checkpoints, checkpoint-writes, incidents)
  3. Lambda layer (all Python deps from requirements-lambda.txt)
  4. SentinelAI Lambda function  (lambda_handler.handler)
  5. SQS → Lambda event source mapping
  6. Victim app Lambda           (victim_app/handler.handler)
  7. CloudWatch Alarm            (Errors > 5 in 1 min on victim-app-prod)
  8. EventBridge rule            (Alarm ALARM → SQS sentinal-ai-incidents)
  9. CloudWatch Dashboard        (sentinal-ai-dashboard)
"""

import json
import os
import subprocess
import sys
import time
import zipfile
import io
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boto3
import config

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUNCTION_NAME   = "sentinal-ai"
VICTIM_NAME     = "victim-app-prod"
ALARM_NAME      = "HighErrorRate-victim-app-prod"
RULE_NAME       = "sentinal-ai-alarm-rule"
LAYER_NAME      = "sentinal-ai-deps"
ROLE_NAME       = "sentinal-ai-lambda-role"


def mk(service):
    return boto3.client(
        service,
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def step(msg):
    print(f"\n{'─'*50}\n  {msg}\n{'─'*50}")


# ── 1. IAM execution role ─────────────────────────────────────────────────────

def create_execution_role(account_id: str) -> str:
    iam = mk("iam")

    trust = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "lambda.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }],
    })

    inline_policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["sqs:ReceiveMessage", "sqs:DeleteMessage", "sqs:GetQueueAttributes"],
                "Resource": f"arn:aws:sqs:{config.AWS_REGION}:{account_id}:sentinal-ai-incidents",
            },
            {
                "Effect": "Allow",
                "Action": ["dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:UpdateItem",
                           "dynamodb:DeleteItem", "dynamodb:Query", "dynamodb:Scan",
                           "dynamodb:BatchWriteItem"],
                "Resource": [
                    f"arn:aws:dynamodb:{config.AWS_REGION}:{account_id}:table/{config.DYNAMODB_CHECKPOINT_TABLE}",
                    f"arn:aws:dynamodb:{config.AWS_REGION}:{account_id}:table/{config.DYNAMODB_CHECKPOINT_WRITES_TABLE}",
                    f"arn:aws:dynamodb:{config.AWS_REGION}:{account_id}:table/{config.DYNAMODB_INCIDENTS_TABLE}",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["s3:PutObject", "s3:GetObject"],
                "Resource": f"arn:aws:s3:::{config.S3_REPORTS_BUCKET}/*",
            },
            {
                "Effect": "Allow",
                "Action": ["sns:Publish"],
                "Resource": config.SNS_ALERT_TOPIC_ARN,
            },
            {
                "Effect": "Allow",
                "Action": [
                    "cloudwatch:GetMetricStatistics", "cloudwatch:ListMetrics",
                    "logs:FilterLogEvents", "logs:GetLogEvents",
                    "logs:DescribeLogGroups", "logs:DescribeLogStreams",
                ],
                "Resource": "*",
            },
        ],
    })

    step("IAM execution role")
    try:
        r = iam.get_role(RoleName=ROLE_NAME)
        role_arn = r["Role"]["Arn"]
        print(f"  [SKIP] Role already exists: {role_arn}")
    except iam.exceptions.NoSuchEntityException:
        r = iam.create_role(
            RoleName=ROLE_NAME,
            AssumeRolePolicyDocument=trust,
            Description="SentinelAI Lambda execution role",
        )
        role_arn = r["Role"]["Arn"]
        print(f"  [OK]   Role created: {role_arn}")

    iam.put_role_policy(
        RoleName=ROLE_NAME,
        PolicyName="sentinal-ai-inline",
        PolicyDocument=inline_policy,
    )
    iam.attach_role_policy(
        RoleName=ROLE_NAME,
        PolicyArn="arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
    )
    print("  [OK]   Policies attached")

    # IAM propagation delay
    print("  [WAIT] Waiting 12s for IAM propagation...")
    time.sleep(12)
    return role_arn


# ── 2. DynamoDB tables ───────────────────────────────────────────────────────

def _ensure_dynamodb_table(client, table_name: str, pk: str, sk: str | None = None):
    try:
        client.describe_table(TableName=table_name)
        print(f"  [SKIP] Table already exists: {table_name}")
        return
    except client.exceptions.ResourceNotFoundException:
        pass

    key_schema = [{"AttributeName": pk, "KeyType": "HASH"}]
    attr_defs = [{"AttributeName": pk, "AttributeType": "S"}]
    if sk:
        key_schema.append({"AttributeName": sk, "KeyType": "RANGE"})
        attr_defs.append({"AttributeName": sk, "AttributeType": "S"})

    client.create_table(
        TableName=table_name,
        KeySchema=key_schema,
        AttributeDefinitions=attr_defs,
        BillingMode="PAY_PER_REQUEST",
    )
    waiter = client.get_waiter("table_exists")
    waiter.wait(TableName=table_name)
    print(f"  [OK]   Table created: {table_name}")


def create_dynamodb_tables():
    step("DynamoDB tables")
    ddb = mk("dynamodb")

    # LangGraph DynamoDBSaver requires two tables
    _ensure_dynamodb_table(ddb, config.DYNAMODB_CHECKPOINT_TABLE, "thread_id", "checkpoint_id")
    _ensure_dynamodb_table(ddb, config.DYNAMODB_CHECKPOINT_WRITES_TABLE,
                           "thread_id_checkpoint_id_checkpoint_ns", "task_id_idx")
    # Incident history table (partition key only — one row per incident)
    _ensure_dynamodb_table(ddb, config.DYNAMODB_INCIDENTS_TABLE, "incident_id")


# ── 3. Lambda layer ───────────────────────────────────────────────────────────

def build_and_publish_layer() -> str:
    step("Lambda layer (building deps — this takes ~2 min)")

    layer_dir = os.path.join(ROOT, "layer", "python")
    os.makedirs(layer_dir, exist_ok=True)

    req_file = os.path.join(ROOT, "requirements-lambda.txt")
    print(f"  pip install -r requirements-lambda.txt -t layer/python/")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "-r", req_file, "-t", layer_dir, "-q", "--upgrade"],
        check=True,
    )
    print("  [OK]   Packages installed into layer/python/")

    # Zip the layer
    layer_zip_path = os.path.join(ROOT, "sentinal-ai-layer.zip")
    print(f"  Zipping layer...")
    layer_root = os.path.join(ROOT, "layer")
    with zipfile.ZipFile(layer_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, _, filenames in os.walk(layer_root):
            for fn in filenames:
                full = os.path.join(dirpath, fn)
                arcname = os.path.relpath(full, layer_root)  # python/<pkg>/... not layer/python/<pkg>/...
                zf.write(full, arcname)

    size_mb = os.path.getsize(layer_zip_path) / 1_000_000
    print(f"  [OK]   layer zip: {size_mb:.1f} MB — uploading directly to Lambda")

    with open(layer_zip_path, "rb") as f:
        zip_bytes = f.read()

    lam = mk("lambda")
    r = lam.publish_layer_version(
        LayerName=LAYER_NAME,
        Content={"ZipFile": zip_bytes},
        CompatibleRuntimes=["python3.12"],
        Description="SentinelAI Python dependencies",
    )
    layer_arn = r["LayerVersionArn"]
    print(f"  [OK]   Layer published: {layer_arn}")

    os.remove(layer_zip_path)
    return layer_arn


# ── 3. SentinelAI Lambda function ────────────────────────────────────────────

def _zip_function_code() -> bytes:
    """Zip only the source files needed by the Lambda (no .venv, no scripts)."""
    include = [
        "lambda_handler.py",
        "config.py",
        "state.py",
        "agents/__init__.py",
        "agents/graph.py",
        "agents/checkpointer.py",
        "tools/__init__.py",
        "tools/cloudwatch_logs.py",
        "tools/cloudwatch_metrics.py",
        "tools/persistence.py",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in include:
            zf.write(os.path.join(ROOT, rel), rel)
    return buf.getvalue()


def _lambda_env_vars() -> dict:
    """Env vars for the SentinelAI Lambda — no AWS credentials (execution role handles that)."""
    return {
        "GROQ_API_KEY":                config.GROQ_API_KEY or "",
        "SQS_QUEUE_URL":               config.SQS_QUEUE_URL or "",
        "DYNAMODB_CHECKPOINT_TABLE":   config.DYNAMODB_CHECKPOINT_TABLE,
        "DYNAMODB_CHECKPOINT_WRITES_TABLE": config.DYNAMODB_CHECKPOINT_WRITES_TABLE,
        "DYNAMODB_INCIDENTS_TABLE":    config.DYNAMODB_INCIDENTS_TABLE,
        "S3_REPORTS_BUCKET":           config.S3_REPORTS_BUCKET or "",
        "SNS_ALERT_TOPIC_ARN":         config.SNS_ALERT_TOPIC_ARN or "",
        "USE_MOCK_DATA":               "false",
        "LANGCHAIN_TRACING_V2":        os.getenv("LANGCHAIN_TRACING_V2", "false"),
        "LANGCHAIN_API_KEY":           os.getenv("LANGCHAIN_API_KEY", ""),
        "LANGCHAIN_PROJECT":           os.getenv("LANGCHAIN_PROJECT", "sentinal-ai"),
    }


def deploy_sentinal_function(role_arn: str, layer_arn: str) -> str:
    step("SentinelAI Lambda function")
    lam = mk("lambda")
    code_zip = _zip_function_code()
    env = {"Variables": _lambda_env_vars()}

    try:
        r = lam.get_function(FunctionName=FUNCTION_NAME)
        fn_arn = r["Configuration"]["FunctionArn"]
        print(f"  [UPDATE] Updating existing function...")
        lam.update_function_code(FunctionName=FUNCTION_NAME, ZipFile=code_zip)
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
        lam.update_function_configuration(
            FunctionName=FUNCTION_NAME,
            Timeout=300,
            MemorySize=512,
            Layers=[layer_arn],
            Environment=env,
        )
        lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
        print(f"  [OK]   Updated: {fn_arn}")
    except lam.exceptions.ResourceNotFoundException:
        r = lam.create_function(
            FunctionName=FUNCTION_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="lambda_handler.handler",
            Code={"ZipFile": code_zip},
            Timeout=300,
            MemorySize=512,
            Layers=[layer_arn],
            Environment=env,
            Description="SentinelAI autonomous incident response agent",
        )
        fn_arn = r["FunctionArn"]
        print(f"  [OK]   Created: {fn_arn}")
        # Wait for function to become active
        waiter = lam.get_waiter("function_active_v2")
        waiter.wait(FunctionName=FUNCTION_NAME)

    return fn_arn


# ── 4. SQS event source mapping ───────────────────────────────────────────────

def add_sqs_trigger(fn_arn: str, account_id: str):
    step("SQS → Lambda trigger")
    lam = mk("lambda")
    sqs = mk("sqs")
    queue_arn = f"arn:aws:sqs:{config.AWS_REGION}:{account_id}:sentinal-ai-incidents"

    # SQS visibility timeout must be >= Lambda timeout (300s). Use 360s.
    sqs.set_queue_attributes(
        QueueUrl=config.SQS_QUEUE_URL,
        Attributes={"VisibilityTimeout": "360"},
    )
    print("  [OK]   SQS visibility timeout set to 360s")

    mappings = lam.list_event_source_mappings(
        FunctionName=FUNCTION_NAME,
        EventSourceArn=queue_arn,
    )["EventSourceMappings"]

    if mappings:
        print(f"  [SKIP] Mapping already exists (state: {mappings[0]['State']})")
        return

    lam.create_event_source_mapping(
        FunctionName=FUNCTION_NAME,
        EventSourceArn=queue_arn,
        BatchSize=1,
        FunctionResponseTypes=["ReportBatchItemFailures"],
    )
    print(f"  [OK]   SQS trigger added (batch size 1)")


# ── 5. Victim app Lambda ──────────────────────────────────────────────────────

def deploy_victim_app(role_arn: str) -> str:
    step("Victim app Lambda")
    lam = mk("lambda")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(os.path.join(ROOT, "victim_app", "handler.py"), "handler.py")
    code_zip = buf.getvalue()

    victim_env = {"FAIL_RATE": "0.0", "FAILURE_MODE": "random"}

    try:
        r = lam.get_function(FunctionName=VICTIM_NAME)
        fn_arn = r["Configuration"]["FunctionArn"]
        lam.update_function_code(FunctionName=VICTIM_NAME, ZipFile=code_zip)
        lam.get_waiter("function_updated_v2").wait(FunctionName=VICTIM_NAME)
        lam.update_function_configuration(
            FunctionName=VICTIM_NAME,
            Environment={"Variables": victim_env},
        )
        print(f"  [UPDATE] {fn_arn}")
    except lam.exceptions.ResourceNotFoundException:
        r = lam.create_function(
            FunctionName=VICTIM_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="handler.handler",
            Code={"ZipFile": code_zip},
            Timeout=30,
            MemorySize=128,
            Environment={"Variables": victim_env},
            Description="SentinelAI test target — set FAIL_RATE/FAILURE_MODE env vars to trigger errors",
        )
        fn_arn = r["FunctionArn"]
        waiter = lam.get_waiter("function_active_v2")
        waiter.wait(FunctionName=VICTIM_NAME)
        print(f"  [OK]   Created: {fn_arn}")

    return fn_arn


# ── 6. CloudWatch Alarm ───────────────────────────────────────────────────────

def create_cloudwatch_alarm():
    step("CloudWatch Alarm")
    cw = mk("cloudwatch")

    cw.put_metric_alarm(
        AlarmName=ALARM_NAME,
        AlarmDescription="SentinelAI: victim-app-prod error rate spike",
        Namespace="AWS/Lambda",
        MetricName="Errors",
        Dimensions=[{"Name": "FunctionName", "Value": VICTIM_NAME}],
        Statistic="Sum",
        Period=60,
        EvaluationPeriods=1,
        Threshold=5,
        ComparisonOperator="GreaterThanThreshold",
        TreatMissingData="notBreaching",
    )
    print(f"  [OK]   Alarm '{ALARM_NAME}': Errors > 5 in 1 min → ALARM")


# ── 7. EventBridge rule → SQS ─────────────────────────────────────────────────

def create_eventbridge_rule(account_id: str):
    step("EventBridge rule → SQS")
    eb   = mk("events")
    sqs  = mk("sqs")

    event_pattern = json.dumps({
        "source": ["aws.cloudwatch"],
        "detail-type": ["CloudWatch Alarm State Change"],
        "detail": {
            "alarmName": [ALARM_NAME],
            "state": {"value": ["ALARM"]},
        },
    })

    r = eb.put_rule(
        Name=RULE_NAME,
        EventPattern=event_pattern,
        State="ENABLED",
        Description="SentinelAI: route CloudWatch alarm to SQS",
    )
    rule_arn = r["RuleArn"]
    print(f"  [OK]   Rule created: {rule_arn}")

    queue_url = config.SQS_QUEUE_URL
    queue_arn = f"arn:aws:sqs:{config.AWS_REGION}:{account_id}:sentinal-ai-incidents"

    # Allow EventBridge to send to SQS
    policy = json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "AllowEventBridge",
            "Effect": "Allow",
            "Principal": {"Service": "events.amazonaws.com"},
            "Action": "sqs:SendMessage",
            "Resource": queue_arn,
            "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
        }],
    })
    sqs.set_queue_attributes(QueueUrl=queue_url, Attributes={"Policy": policy})
    print(f"  [OK]   SQS policy updated — EventBridge can send messages")

    eb.put_targets(
        Rule=RULE_NAME,
        Targets=[{"Id": "sqs-target", "Arn": queue_arn}],
    )
    print(f"  [OK]   Target set → {queue_arn}")


# ── 8. CloudWatch Dashboard ──────────────────────────────────────────────────

DASHBOARD_NAME = "sentinal-ai-dashboard"


def create_cloudwatch_dashboard():
    step("CloudWatch Dashboard")
    cw = mk("cloudwatch")

    widgets = [
        # Row 1: Victim app signals
        {
            "type": "metric", "x": 0, "y": 0, "width": 12, "height": 6,
            "properties": {
                "title": "Victim App — Errors",
                "metrics": [["AWS/Lambda", "Errors", "FunctionName", VICTIM_NAME, {"stat": "Sum", "period": 60}]],
                "view": "timeSeries", "stacked": False, "region": config.AWS_REGION,
                "period": 60, "liveData": True,
            },
        },
        {
            "type": "metric", "x": 12, "y": 0, "width": 12, "height": 6,
            "properties": {
                "title": "Victim App — Invocations",
                "metrics": [["AWS/Lambda", "Invocations", "FunctionName", VICTIM_NAME, {"stat": "Sum", "period": 60}]],
                "view": "timeSeries", "stacked": False, "region": config.AWS_REGION,
                "period": 60, "liveData": True,
            },
        },
        # Row 2: SentinelAI Lambda signals
        {
            "type": "metric", "x": 0, "y": 6, "width": 12, "height": 6,
            "properties": {
                "title": "SentinelAI — Invocations",
                "metrics": [["AWS/Lambda", "Invocations", "FunctionName", FUNCTION_NAME, {"stat": "Sum", "period": 60}]],
                "view": "timeSeries", "stacked": False, "region": config.AWS_REGION,
                "period": 60, "liveData": True,
            },
        },
        {
            "type": "metric", "x": 12, "y": 6, "width": 12, "height": 6,
            "properties": {
                "title": "SentinelAI — Duration P99 (ms)",
                "metrics": [["AWS/Lambda", "Duration", "FunctionName", FUNCTION_NAME, {"stat": "p99", "period": 60}]],
                "view": "timeSeries", "stacked": False, "region": config.AWS_REGION,
                "period": 60, "liveData": True,
            },
        },
        # Row 3: SQS queue depth
        {
            "type": "metric", "x": 0, "y": 12, "width": 12, "height": 6,
            "properties": {
                "title": "SQS — Messages Sent (sentinal-ai-incidents)",
                "metrics": [["AWS/SQS", "NumberOfMessagesSent", "QueueName", "sentinal-ai-incidents", {"stat": "Sum", "period": 60}]],
                "view": "timeSeries", "stacked": False, "region": config.AWS_REGION,
                "period": 60, "liveData": True,
            },
        },
    ]

    dashboard_body = json.dumps({"widgets": widgets})

    try:
        cw.put_dashboard(DashboardName=DASHBOARD_NAME, DashboardBody=dashboard_body)
        print(f"  [OK]   Dashboard '{DASHBOARD_NAME}' created/updated")
        print(f"         https://console.aws.amazon.com/cloudwatch/home?region={config.AWS_REGION}#dashboards:name={DASHBOARD_NAME}")
    except cw.exceptions.DashboardInvalidInputError as exc:
        print(f"  [FAIL] Dashboard body invalid: {exc}")
    except Exception as exc:
        err = str(exc)
        if "AccessDenied" in err or "not authorized" in err:
            print(f"  [WARN] Skipping dashboard — IAM user needs cloudwatch:PutDashboard")
            print(f"         Add it to the sentinal-ai-dev IAM user policy to enable this step.")
        else:
            print(f"  [WARN] Dashboard creation failed: {exc}")


# ── 9. API Lambda + Function URL ─────────────────────────────────────────────

API_NAME = "sentinal-ai-api"


def _zip_api_code() -> bytes:
    """Zip the files needed by the FastAPI Lambda."""
    include = [
        "api/__init__.py",
        "api/main.py",
        "config.py",
        "state.py",
        "agents/__init__.py",
        "agents/graph.py",
        "agents/checkpointer.py",
        "tools/__init__.py",
        "tools/cloudwatch_logs.py",
        "tools/cloudwatch_metrics.py",
        "tools/persistence.py",
    ]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for rel in include:
            zf.write(os.path.join(ROOT, rel), rel)
    return buf.getvalue()


def _api_env_vars() -> dict:
    return {
        "GROQ_API_KEY":                    config.GROQ_API_KEY or "",
        "DYNAMODB_CHECKPOINT_TABLE":       config.DYNAMODB_CHECKPOINT_TABLE,
        "DYNAMODB_CHECKPOINT_WRITES_TABLE": config.DYNAMODB_CHECKPOINT_WRITES_TABLE,
        "DYNAMODB_INCIDENTS_TABLE":        config.DYNAMODB_INCIDENTS_TABLE,
        "S3_REPORTS_BUCKET":               config.S3_REPORTS_BUCKET or "",
        "SNS_ALERT_TOPIC_ARN":             config.SNS_ALERT_TOPIC_ARN or "",
        "USE_MOCK_DATA":                   "false",
        "LANGCHAIN_TRACING_V2":            os.getenv("LANGCHAIN_TRACING_V2", "false"),
        "LANGCHAIN_API_KEY":               os.getenv("LANGCHAIN_API_KEY", ""),
        "LANGCHAIN_PROJECT":               os.getenv("LANGCHAIN_PROJECT", "sentinal-ai"),
    }


def deploy_api_function(role_arn: str, layer_arn: str) -> str:
    step("API Lambda (sentinal-ai-api)")
    lam = mk("lambda")
    code_zip = _zip_api_code()
    env = {"Variables": _api_env_vars()}

    try:
        r = lam.get_function(FunctionName=API_NAME)
        fn_arn = r["Configuration"]["FunctionArn"]
        print(f"  [UPDATE] Updating existing function...")
        lam.update_function_code(FunctionName=API_NAME, ZipFile=code_zip)
        lam.get_waiter("function_updated_v2").wait(FunctionName=API_NAME)
        lam.update_function_configuration(
            FunctionName=API_NAME,
            Timeout=300,
            MemorySize=512,
            Layers=[layer_arn],
            Environment=env,
        )
        lam.get_waiter("function_updated_v2").wait(FunctionName=API_NAME)
        print(f"  [OK]   Updated: {fn_arn}")
    except lam.exceptions.ResourceNotFoundException:
        r = lam.create_function(
            FunctionName=API_NAME,
            Runtime="python3.12",
            Role=role_arn,
            Handler="api.main.handler",
            Code={"ZipFile": code_zip},
            Timeout=300,
            MemorySize=512,
            Layers=[layer_arn],
            Environment=env,
            Description="SentinelAI FastAPI approval dashboard",
        )
        fn_arn = r["FunctionArn"]
        lam.get_waiter("function_active_v2").wait(FunctionName=API_NAME)
        print(f"  [OK]   Created: {fn_arn}")

    return fn_arn


def create_api_gateway(account_id: str) -> str:
    """Create an API Gateway HTTP API in front of the API Lambda. Returns the base URL."""
    apigw = mk("apigatewayv2")
    lam   = mk("lambda")
    api_name = API_NAME

    # Check if an HTTP API with this name already exists
    existing = [a for a in apigw.get_apis()["Items"] if a["Name"] == api_name]
    if existing:
        api_url = existing[0]["ApiEndpoint"]
        print(f"  [SKIP] API Gateway already exists: {api_url}")
        return api_url

    fn_arn = lam.get_function_configuration(FunctionName=API_NAME)["FunctionArn"]

    r = apigw.create_api(
        Name=api_name,
        ProtocolType="HTTP",
        Target=fn_arn,
        CorsConfiguration={
            "AllowOrigins": ["*"],
            "AllowMethods": ["GET", "POST"],
            "AllowHeaders": ["content-type"],
        },
    )
    api_id  = r["ApiId"]
    api_url = r["ApiEndpoint"]

    # Grant API Gateway permission to invoke the Lambda
    source_arn = f"arn:aws:execute-api:{config.AWS_REGION}:{account_id}:{api_id}/*/*"
    try:
        lam.add_permission(
            FunctionName=API_NAME,
            StatementId="apigw-invoke",
            Action="lambda:InvokeFunction",
            Principal="apigateway.amazonaws.com",
            SourceArn=source_arn,
        )
    except lam.exceptions.ResourceConflictException:
        pass

    print(f"  [OK]   API Gateway: {api_url}")
    return api_url


def update_sentinal_api_url(api_url: str):
    """Patch the main sentinal-ai Lambda env so approval email links are real URLs."""
    step("Wiring API_BASE_URL into sentinal-ai Lambda")
    lam = mk("lambda")
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    env = _lambda_env_vars()
    env["API_BASE_URL"] = api_url
    lam.update_function_configuration(
        FunctionName=FUNCTION_NAME,
        Environment={"Variables": env},
    )
    lam.get_waiter("function_updated_v2").wait(FunctionName=FUNCTION_NAME)
    print(f"  [OK]   sentinal-ai Lambda now points approval emails to: {api_url}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("\n" + "=" * 50)
    print("  SentinelAI — Week 5 (Final) Deployment")
    print("=" * 50)

    sts = mk("sts")
    account_id = sts.get_caller_identity()["Account"]
    print(f"\n  Account : {account_id}")
    print(f"  Region  : {config.AWS_REGION}")

    role_arn  = create_execution_role(account_id)
    create_dynamodb_tables()
    layer_arn = build_and_publish_layer()
    fn_arn    = deploy_sentinal_function(role_arn, layer_arn)
    add_sqs_trigger(fn_arn, account_id)
    deploy_victim_app(role_arn)
    create_cloudwatch_alarm()
    create_eventbridge_rule(account_id)
    create_cloudwatch_dashboard()

    step("API Lambda + API Gateway")
    deploy_api_function(role_arn, layer_arn)
    api_url = create_api_gateway(account_id)
    update_sentinal_api_url(api_url)

    print("\n" + "=" * 50)
    print("  Deployment complete — SentinelAI is fully operational!")
    print(f"  SentinelAI Lambda : {fn_arn}")
    print(f"  Victim app        : arn:aws:lambda:{config.AWS_REGION}:{account_id}:function:{VICTIM_NAME}")
    print(f"  Alarm             : {ALARM_NAME}")
    print(f"  Dashboard         : https://console.aws.amazon.com/cloudwatch/home?region={config.AWS_REGION}#dashboards:name={DASHBOARD_NAME}")
    print(f"  API (approve/decline): {api_url}")
    print()
    print("  Run  python scripts/trigger_test.py  to fire an end-to-end test")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    main()
