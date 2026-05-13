"""
Triggers the full end-to-end pipeline by:
  1. Enabling FAIL_RATE=0.9 on victim-app-prod
  2. Invoking it 25 times (≈22 errors) to trip the CloudWatch alarm
  3. Polling until the alarm state becomes ALARM
  4. Re-disabling the victim app (FAIL_RATE=0.0)

The alarm → EventBridge → SQS → SentinelAI Lambda chain fires automatically.
Watch the SentinelAI Lambda logs in CloudWatch to see the agents running.
"""

import sys
import os
import time
import json
import concurrent.futures

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import boto3
import config

VICTIM_NAME = "victim-app-prod"
ALARM_NAME  = "HighErrorRate-victim-app-prod"
INVOCATIONS = 25


def mk(service):
    return boto3.client(
        service,
        region_name=config.AWS_REGION,
        aws_access_key_id=config.AWS_ACCESS_KEY_ID,
        aws_secret_access_key=config.AWS_SECRET_ACCESS_KEY,
    )


def set_fail_rate(rate: float):
    lam = mk("lambda")
    lam.update_function_configuration(
        FunctionName=VICTIM_NAME,
        Environment={"Variables": {"FAIL_RATE": str(rate)}},
    )
    # Brief wait for config update to propagate
    time.sleep(3)
    print(f"  Victim FAIL_RATE set to {rate}")


def invoke_once(_):
    lam = mk("lambda")
    r = lam.invoke(FunctionName=VICTIM_NAME, InvocationType="RequestResponse")
    status = r["StatusCode"]
    if "FunctionError" in r:
        return "error"
    return "ok"


def fire_victim():
    print(f"\n  Invoking {VICTIM_NAME} {INVOCATIONS} times in parallel...")
    errors = oks = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as pool:
        for result in pool.map(invoke_once, range(INVOCATIONS)):
            if result == "error":
                errors += 1
            else:
                oks += 1
    print(f"  Results: {errors} errors, {oks} ok  (need >5 errors to trip alarm)")
    return errors


def wait_for_alarm():
    cw = mk("cloudwatch")
    print(f"\n  Polling alarm state (checks every 15s, up to 5 min)...")
    for i in range(20):
        r = cw.describe_alarms(AlarmNames=[ALARM_NAME])
        alarms = r["MetricAlarms"]
        if not alarms:
            print("  [!] Alarm not found — did deploy.py run successfully?")
            return False
        state = alarms[0]["StateValue"]
        print(f"  [{i+1:02d}] Alarm state: {state}")
        if state == "ALARM":
            print("  [OK] Alarm fired! SentinelAI Lambda should be starting...")
            return True
        time.sleep(15)
    print("  [TIMEOUT] Alarm did not fire within 5 minutes.")
    return False


def main():
    print("\n" + "=" * 50)
    print("  SentinelAI — Trigger End-to-End Test")
    print("=" * 50)

    try:
        set_fail_rate(0.9)
        errors = fire_victim()

        if errors <= 5:
            print(f"\n  [WARN] Only {errors} errors generated — may not be enough to trip alarm (threshold: 5)")
            print("  Try running this script again immediately.")

        fired = wait_for_alarm()

        if fired:
            print("\n  Pipeline triggered! Check CloudWatch Logs for SentinelAI Lambda:")
            print(f"  https://console.aws.amazon.com/cloudwatch/home?region={config.AWS_REGION}#logsV2:log-groups/log-group/$252Faws$252Flambda$252Fsentinal-ai")
            print("\n  The investigation will take 1-2 minutes.")
            print("  You'll receive an approval email when it's done.")
        else:
            print("\n  Alarm didn't fire. Check that deploy.py ran successfully.")

    finally:
        print("\n  Resetting victim app to healthy (FAIL_RATE=0.0)...")
        set_fail_rate(0.0)
        print("  Done.\n")


if __name__ == "__main__":
    main()
