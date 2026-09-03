#!/usr/bin/env python3
"""
Copies an ECR image from the Source Account to the Destination Account,
then runs the DataSync -> CodePipeline -> CodeBuild pipeline that follows.

Flow:
  1. Verify the image exists in the Source Account's ECR and show
     vulnerability scan findings.
  2. Ask the user whether to continue.
  3. Invoke a Lambda in the Destination Account to copy the image over.
  4. Verify the image now exists in the Destination Account's ECR.
  5. Run the App DataSync Task, then (if it succeeds) the Infra DataSync Task.
  6. The Infra DataSync Task triggers a CodePipeline execution that runs a
     CodeBuild project; tail its logs and report the final build status.
"""

import argparse
import json
import sys
import time
from datetime import datetime, timezone

import boto3
from botocore.exceptions import ClientError

DEFAULT_REGION = "us-east-1"
DEFAULT_PROFILE_A = "cms-dev"
DEFAULT_PROFILE_B = "cms-test"
DEFAULT_LAMBDA_NAME = "ecr-image-sync"
DEFAULT_APP_DATASYNC_TASK = "hp-cms-poc-app-artifacts-cms-non-prod-datasync"
DEFAULT_INFRA_DATASYNC_TASK = "hp-cms-poc-infra-artifacts-cms-non-prod-datasync"
DEFAULT_PIPELINE_NAME = "hp-cms-test-application-deploy-pipeline"

SEVERITY_ORDER = ["CRITICAL", "HIGH", "MEDIUM", "LOW", "INFORMATIONAL", "UNDEFINED"]


def prompt(text, default=None):
    suffix = f" [{default}]" if default else ""
    value = input(f"{text}{suffix}: ").strip()
    return value or default


def parse_args():
    p = argparse.ArgumentParser(description="Copy an ECR image from the Source Account to the Destination Account and run the sync pipeline.")
    p.add_argument("--profile-a", help=f"AWS CLI profile for the Source Account (default: {DEFAULT_PROFILE_A})")
    p.add_argument("--profile-b", help=f"AWS CLI profile for the Destination Account (default: {DEFAULT_PROFILE_B})")
    p.add_argument("--region", help=f"AWS region for all calls (default: {DEFAULT_REGION})")
    p.add_argument("--repo", help="Full repo:tag, e.g. cms/iplus/iplus-payment835-extract-service:0.0.28-RELEASE")
    p.add_argument("--lambda-name", help=f"Lambda function name in the Destination Account (default: {DEFAULT_LAMBDA_NAME})")
    p.add_argument("--app-datasync-task", help=f"App DataSync Task name (or ARN) (Destination Account) (default: {DEFAULT_APP_DATASYNC_TASK})")
    p.add_argument("--infra-datasync-task", help=f"Infra DataSync Task name (or ARN) (Destination Account) (default: {DEFAULT_INFRA_DATASYNC_TASK})")
    p.add_argument("--pipeline-name", help=f"CodePipeline name triggered by the Infra DataSync Task (Destination Account) (default: {DEFAULT_PIPELINE_NAME})")
    p.add_argument(
        "--no-dry-run",
        dest="dry_run",
        action="store_false",
        default=True,
        help="Actually copy the image and run DataSync/CodePipeline (default is dry-run: show what would be done without making changes)",
    )
    return p.parse_args()


def resolve_config(args):
    cfg = {}
    cfg["profile_a"] = args.profile_a or prompt("AWS profile for the Source Account", DEFAULT_PROFILE_A)
    cfg["profile_b"] = args.profile_b or prompt("AWS profile for the Destination Account", DEFAULT_PROFILE_B)
    cfg["region"] = args.region or DEFAULT_REGION

    repo_full = args.repo
    if repo_full:
        if ":" not in repo_full:
            sys.exit(f"Invalid --repo value: {repo_full!r} (expected <repo-path>:<tag>)")
    else:
        while True:
            repo_full = prompt(
                "Repo and tag (e.g. cms/iplus/iplus-payment835-extract-service:0.0.28-RELEASE)"
            )
            if repo_full and ":" in repo_full:
                break
            print(f"  Invalid value: {repo_full!r} (expected <repo-path>:<tag>)")
    cfg["repo"], cfg["tag"] = repo_full.rsplit(":", 1)

    cfg["lambda_name"] = args.lambda_name or DEFAULT_LAMBDA_NAME
    cfg["app_datasync_task"] = args.app_datasync_task or prompt("App DataSync Task name (or ARN)", DEFAULT_APP_DATASYNC_TASK)
    cfg["infra_datasync_task"] = args.infra_datasync_task or prompt("Infra DataSync Task name (or ARN)", DEFAULT_INFRA_DATASYNC_TASK)
    cfg["pipeline_name"] = args.pipeline_name or prompt("CodePipeline name", DEFAULT_PIPELINE_NAME)
    cfg["dry_run"] = args.dry_run
    return cfg


def confirm_config(cfg):
    print("\n=== Configuration ===")
    print(f"  Source Account profile      : {cfg['profile_a']}")
    print(f"  Destination Account profile : {cfg['profile_b']}")
    print(f"  Region              : {cfg['region']}")
    print(f"  Repository          : {cfg['repo']}")
    print(f"  Tag                 : {cfg['tag']}")
    print(f"  Lambda function     : {cfg['lambda_name']}")
    print(f"  App DataSync Task   : {cfg['app_datasync_task']} (name or ARN)")
    print(f"  Infra DataSync Task : {cfg['infra_datasync_task']} (name or ARN)")
    print(f"  CodePipeline name   : {cfg['pipeline_name']}")
    if cfg["dry_run"]:
        print(f"  Mode                : DRY RUN (no changes will be made)")
    print()
    if prompt("Proceed with these values? [y/N]", "N").lower() != "y":
        sys.exit("Aborted by user.")


def get_account_id(session, label):
    sts = session.client("sts")
    try:
        identity = sts.get_caller_identity()
    except ClientError as e:
        sys.exit(f"Could not verify credentials for {label}: {e}")
    print(f"  {label}: verified as {identity['Arn']} (account {identity['Account']})")
    return identity["Account"]


def verify_image_exists(ecr_client, repo, tag, label):
    try:
        resp = ecr_client.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
    except ClientError as e:
        sys.exit(f"Image {repo}:{tag} not found in {label}: {e}")
    detail = resp["imageDetails"][0]
    print(f"  Found {repo}:{tag} in {label} (pushed {detail.get('imagePushedAt')})")
    return detail


def start_and_wait_for_scan(ecr_client, repo, tag, poll_interval=5, timeout=300):
    print("  Triggering new vulnerability scan...")
    try:
        ecr_client.start_image_scan(repositoryName=repo, imageId={"imageTag": tag})
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code")
        if code == "LimitExceededException":
            print("  A scan is already in progress; waiting for it to complete...")
        else:
            print(f"  Could not start new scan ({e}); showing latest available findings if any.")

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = ecr_client.describe_image_scan_findings(repositoryName=repo, imageId={"imageTag": tag})
        except ClientError as e:
            print(f"  Could not retrieve scan findings: {e}")
            return None
        status = resp.get("imageScanStatus", {}).get("status")
        if status == "COMPLETE":
            return resp
        if status == "FAILED":
            print(f"  Scan failed: {resp.get('imageScanStatus', {}).get('description')}")
            return resp
        time.sleep(poll_interval)

    print("  Timed out waiting for scan to complete.")
    try:
        return ecr_client.describe_image_scan_findings(repositoryName=repo, imageId={"imageTag": tag})
    except ClientError:
        return None


def show_vulnerabilities(ecr_client, repo, tag):
    resp = start_and_wait_for_scan(ecr_client, repo, tag)
    if resp is None:
        return

    status = resp.get("imageScanStatus", {}).get("status")
    if status != "COMPLETE":
        print(f"  No completed scan available (status: {status}).")
        return

    findings = resp["imageScanFindings"]
    counts = findings.get("findingSeverityCounts", {})
    print("  Vulnerability summary:")
    for severity in SEVERITY_ORDER:
        if severity in counts:
            print(f"    {severity:<14}: {counts[severity]}")

    severity_rank = {severity: i for i, severity in enumerate(SEVERITY_ORDER)}
    sorted_findings = sorted(
        findings.get("findings", []),
        key=lambda f: severity_rank.get(f.get("severity"), len(SEVERITY_ORDER)),
    )
    print_paged(
        [f"    [{f.get('severity')}] {f.get('name')} - {f.get('description', '')[:100]}" for f in sorted_findings]
    )


def print_paged(lines, page_size=20):
    for i in range(0, len(lines), page_size):
        for line in lines[i : i + page_size]:
            print(line)
        remaining = len(lines) - (i + page_size)
        if remaining > 0:
            if prompt(f"  -- {remaining} more finding(s); press Enter to continue, or 'q' to stop --", "") == "q":
                return


def invoke_copy_lambda(lambda_client, function_name, repo, tag):
    payload = json.dumps({"repo": repo, "tags": tag}).encode()
    try:
        resp = lambda_client.invoke(
            FunctionName=function_name,
            InvocationType="RequestResponse",
            Payload=payload,
        )
    except ClientError as e:
        sys.exit(f"Failed to invoke Lambda {function_name}: {e}")

    body = resp["Payload"].read().decode()
    if resp.get("FunctionError"):
        sys.exit(f"Lambda {function_name} returned an error ({resp['FunctionError']}): {body}")
    if resp.get("StatusCode") != 200:
        sys.exit(f"Lambda {function_name} returned unexpected status {resp.get('StatusCode')}: {body}")

    print(f"  Lambda invocation succeeded (StatusCode 200). Response: {body}")


def wait_for_image_in_account_b(ecr_client, repo, tag, timeout=300, interval=15):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            ecr_client.describe_images(repositoryName=repo, imageIds=[{"imageTag": tag}])
            print(f"  Confirmed {repo}:{tag} now exists in the Destination Account.")
            return
        except ClientError:
            print(f"  Image not yet visible in the Destination Account, retrying in {interval}s...")
            time.sleep(interval)
    sys.exit(f"Timed out waiting for {repo}:{tag} to appear in the Destination Account.")


def resolve_datasync_arn(datasync_client, task_name_or_arn):
    if task_name_or_arn.startswith("arn:"):
        return task_name_or_arn

    matches = []
    paginator = datasync_client.get_paginator("list_tasks")
    for page in paginator.paginate():
        for task in page.get("Tasks", []):
            if task.get("Name") == task_name_or_arn:
                matches.append(task["TaskArn"])

    if not matches:
        sys.exit(f"No DataSync task named {task_name_or_arn!r} was found in the Destination Account.")
    if len(matches) > 1:
        listed = "\n".join(f"    - {arn}" for arn in matches)
        sys.exit(
            f"Multiple DataSync tasks are named {task_name_or_arn!r} in the Destination Account:\n{listed}\n"
            "Re-run and pass one of these ARNs directly instead of the name."
        )
    return matches[0]


def run_datasync_task(datasync_client, task_arn, label, poll_interval=10):
    print(f"  Starting DataSync {label} ({task_arn})...")
    resp = datasync_client.start_task_execution(TaskArn=task_arn)
    execution_arn = resp["TaskExecutionArn"]

    last_status = None
    while True:
        resp = datasync_client.describe_task_execution(TaskExecutionArn=execution_arn)
        status = resp["Status"]
        if status != last_status:
            print(f"    {label} status: {status}")
            last_status = status
        if status == "SUCCESS":
            return True
        if status == "ERROR":
            print(f"    {label} failed: {resp.get('Result', {})}")
            return False
        time.sleep(poll_interval)


def find_new_pipeline_execution(codepipeline_client, pipeline_name, after_time, timeout=180, poll_interval=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = codepipeline_client.list_pipeline_executions(pipelineName=pipeline_name)
        for execution in resp.get("pipelineExecutionSummaries", []):
            start_time = execution.get("startTime")
            if start_time and start_time > after_time:
                return execution["pipelineExecutionId"]
        time.sleep(poll_interval)
    sys.exit(f"Timed out waiting for a new execution of pipeline {pipeline_name}.")


def get_codebuild_build_id(codepipeline_client, pipeline_name, execution_id, timeout=180, poll_interval=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        resp = codepipeline_client.list_action_executions(
            pipelineName=pipeline_name,
            filter={"pipelineExecutionId": execution_id},
        )
        for detail in resp.get("actionExecutionDetails", []):
            build_id = detail.get("output", {}).get("executionResult", {}).get("externalExecutionId")
            if build_id:
                print(f"  Found CodeBuild build: {build_id}")
                return build_id
        time.sleep(poll_interval)
    sys.exit(f"Timed out waiting for the CodeBuild action to start for execution {execution_id}.")


def tail_codebuild_logs(codebuild_client, logs_client, build_id, poll_interval=3):
    log_group = log_stream = None
    next_token = None
    build_status = "IN_PROGRESS"

    while True:
        builds = codebuild_client.batch_get_builds(ids=[build_id])["builds"]
        if not builds:
            sys.exit(f"CodeBuild build {build_id} not found.")
        build = builds[0]
        build_status = build["buildStatus"]

        if log_group is None:
            logs_info = build.get("logs", {})
            if logs_info.get("groupName") and logs_info.get("streamName"):
                log_group = logs_info["groupName"]
                log_stream = logs_info["streamName"]

        if log_group:
            try:
                kwargs = {"logGroupName": log_group, "logStreamName": log_stream, "startFromHead": True}
                if next_token:
                    kwargs["nextToken"] = next_token
                events = logs_client.get_log_events(**kwargs)
                for event in events["events"]:
                    print(event["message"].rstrip())
                new_token = events["nextForwardToken"]
                next_token = new_token
            except logs_client.exceptions.ResourceNotFoundException:
                pass

        if build_status != "IN_PROGRESS":
            break
        time.sleep(poll_interval)

    print(f"\n=== CodeBuild finished with status: {build_status} ===")
    return build_status


def main():
    args = parse_args()
    cfg = resolve_config(args)
    confirm_config(cfg)

    session_a = boto3.Session(profile_name=cfg["profile_a"], region_name=cfg["region"])
    session_b = boto3.Session(profile_name=cfg["profile_b"], region_name=cfg["region"])

    print("\n=== Verifying credentials ===")
    get_account_id(session_a, "Source Account")
    get_account_id(session_b, "Destination Account")

    ecr_a = session_a.client("ecr")
    ecr_b = session_b.client("ecr")

    print("\n=== Verifying image in the Source Account ===")
    verify_image_exists(ecr_a, cfg["repo"], cfg["tag"], "the Source Account")

    print("\n=== Vulnerability findings ===")
    show_vulnerabilities(ecr_a, cfg["repo"], cfg["tag"])

    if prompt("\nContinue with copying this image to the Destination Account? [y/N]", "N").lower() != "y":
        sys.exit("Aborted by user after reviewing vulnerabilities.")

    if cfg["dry_run"]:
        print("\n=== Dry run: no changes will be made ===")
        print(f"  Would invoke Lambda '{cfg['lambda_name']}' in the Destination Account to copy {cfg['repo']}:{cfg['tag']} from the Source Account.")
        print(f"  Would wait for {cfg['repo']}:{cfg['tag']} to appear in the Destination Account's ECR.")
        print(f"  Would start the App DataSync Task ({cfg['app_datasync_task']}) and wait for SUCCESS.")
        print(f"  Would start the Infra DataSync Task ({cfg['infra_datasync_task']}) and wait for SUCCESS.")
        print(f"  Would wait for a new execution of CodePipeline '{cfg['pipeline_name']}' and tail its CodeBuild logs.")
        sys.exit(0)

    datasync_b = session_b.client("datasync")

    print("\n=== Copying image to the Destination Account ===")
    lambda_b = session_b.client("lambda")
    invoke_copy_lambda(lambda_b, cfg["lambda_name"], cfg["repo"], cfg["tag"])
    wait_for_image_in_account_b(ecr_b, cfg["repo"], cfg["tag"])

    if prompt("\nContinue with running the DataSync tasks? [y/N]", "N").lower() != "y":
        sys.exit("Aborted by user after copying the image.")

    print("\n=== Running DataSync tasks ===")

    app_task_arn = resolve_datasync_arn(datasync_b, cfg["app_datasync_task"])
    if not run_datasync_task(datasync_b, app_task_arn, "App DataSync Task"):
        sys.exit("App DataSync Task failed; aborting.")

    if prompt("\nContinue with running the Infra DataSync Task? [y/N]", "N").lower() != "y":
        sys.exit("Aborted by user after the App DataSync Task.")

    infra_task_arn = resolve_datasync_arn(datasync_b, cfg["infra_datasync_task"])
    trigger_time = datetime.now(timezone.utc)
    if not run_datasync_task(datasync_b, infra_task_arn, "Infra DataSync Task"):
        sys.exit("Infra DataSync Task failed; aborting.")

    if prompt("\nContinue with waiting for CodePipeline / CodeBuild? [y/N]", "N").lower() != "y":
        sys.exit("Aborted by user after the Infra DataSync Task.")

    print("\n=== Waiting for CodePipeline / CodeBuild ===")
    codepipeline_b = session_b.client("codepipeline")
    codebuild_b = session_b.client("codebuild")
    logs_b = session_b.client("logs")

    execution_id = find_new_pipeline_execution(codepipeline_b, cfg["pipeline_name"], trigger_time)
    build_id = get_codebuild_build_id(codepipeline_b, cfg["pipeline_name"], execution_id)

    print(f"\n=== Tailing CodeBuild logs for {build_id} ===\n")
    final_status = tail_codebuild_logs(codebuild_b, logs_b, build_id)

    sys.exit(0 if final_status == "SUCCEEDED" else 1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit("\nInterrupted by user.")
