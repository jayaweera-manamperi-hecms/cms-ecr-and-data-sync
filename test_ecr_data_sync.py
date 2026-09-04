"""
Tests for ecr_data_sync.py using two complementary boto3 testing tools,
neither of which touches real AWS:

- moto (`@mock_aws`): a real in-process fake of AWS services with actual
  state (repositories, images, DataSync tasks...). Used wherever moto has
  solid support (ECR, STS, DataSync).
- botocore.stub.Stubber: hand-fed canned responses on a real client. Used
  for Lambda invoke (moto's invoke requires Docker to actually run code),
  and for CodePipeline/CodeBuild/CloudWatch Logs (moto doesn't implement
  the specific actions this script calls: list_pipeline_executions,
  list_action_executions, batch_get_builds/get_log_events sequencing).

Run with: venv/bin/pytest test_ecr_data_sync.py -v
"""

import io
import json
from datetime import datetime, timezone

import boto3
import pytest
from botocore.response import StreamingBody
from botocore.stub import Stubber
from moto import mock_aws

import ecr_data_sync as eds


# --------------------------------------------------------------------------
# Shared fixtures / helpers
# --------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Every function under test polls with time.sleep(); never actually wait."""
    monkeypatch.setattr(eds.time, "sleep", lambda *_a, **_k: None)


class FakeClock:
    """Deterministic stand-in for time.time(): each call returns 0, 1, 2, ...

    Lets timeout-loop tests (`while time.time() < deadline`) run an exact,
    predictable number of iterations instead of racing the real clock.
    """

    def __init__(self):
        self.value = 0.0

    def __call__(self):
        current = self.value
        self.value += 1.0
        return current


@pytest.fixture
def fake_clock(monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(eds.time, "time", clock)
    return clock


def make_ecr_image(client, repo_name, tag):
    client.create_repository(repositoryName=repo_name)
    manifest = json.dumps(
        {
            "schemaVersion": 2,
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "config": {"digest": "sha256:" + "0" * 64, "size": 100, "mediaType": "x"},
            "layers": [],
        }
    )
    client.put_image(repositoryName=repo_name, imageManifest=manifest, imageTag=tag)


def create_datasync_task(client, name):
    src = client.create_location_s3(
        S3BucketArn="arn:aws:s3:::src-bucket",
        S3Config={"BucketAccessRoleArn": "arn:aws:iam::123456789012:role/r"},
    )["LocationArn"]
    dst = client.create_location_s3(
        S3BucketArn="arn:aws:s3:::dst-bucket",
        S3Config={"BucketAccessRoleArn": "arn:aws:iam::123456789012:role/r"},
    )["LocationArn"]
    return client.create_task(
        SourceLocationArn=src, DestinationLocationArn=dst, Name=name
    )["TaskArn"]


def streaming(payload: dict) -> StreamingBody:
    body = json.dumps(payload).encode()
    return StreamingBody(io.BytesIO(body), len(body))


# --------------------------------------------------------------------------
# get_account_id (moto: sts)
# --------------------------------------------------------------------------

@mock_aws
def test_get_account_id_returns_account():
    session = boto3.Session(region_name="us-east-1")
    account_id = eds.get_account_id(session, "Source Account")
    assert account_id == "123456789012"


# --------------------------------------------------------------------------
# verify_image_exists (moto: ecr)
# --------------------------------------------------------------------------

@mock_aws
def test_verify_image_exists_found():
    client = boto3.client("ecr", region_name="us-east-1")
    make_ecr_image(client, "myrepo", "1.0")
    detail = eds.verify_image_exists(client, "myrepo", "1.0", "the Source Account")
    assert detail["imageTags"] == ["1.0"]


@mock_aws
def test_verify_image_exists_missing_tag_exits():
    client = boto3.client("ecr", region_name="us-east-1")
    client.create_repository(repositoryName="myrepo")
    with pytest.raises(SystemExit):
        eds.verify_image_exists(client, "myrepo", "missing-tag", "the Source Account")


# --------------------------------------------------------------------------
# show_vulnerabilities / start_and_wait_for_scan (moto: ecr scan findings)
# --------------------------------------------------------------------------

@mock_aws
def test_show_vulnerabilities_prints_summary(capsys):
    client = boto3.client("ecr", region_name="us-east-1")
    make_ecr_image(client, "myrepo", "1.0")
    eds.show_vulnerabilities(client, "myrepo", "1.0")
    out = capsys.readouterr().out
    assert "Vulnerability summary" in out
    assert "HIGH" in out  # moto seeds one fake HIGH finding on every scan


def test_show_vulnerabilities_trigger_scan_false_reads_existing_results(capsys):
    # Continuous-scan repos don't support/need a manual StartImageScan call;
    # trigger_scan=False must go straight to describe_image_scan_findings.
    # Using Stubber (not moto) here so a single queued response also proves
    # start_image_scan is never called for this path.
    client = boto3.client("ecr", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_image_scan_findings",
        {
            "imageScanStatus": {"status": "COMPLETE", "description": "ok"},
            "imageScanFindings": {
                "findingSeverityCounts": {"HIGH": 1},
                "findings": [{"name": "CVE-1", "severity": "HIGH", "description": "desc"}],
            },
        },
        expected_params={"repositoryName": "myrepo", "imageId": {"imageTag": "1.0"}},
    )
    with stubber:
        eds.show_vulnerabilities(client, "myrepo", "1.0", trigger_scan=False)
    stubber.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "continuous scanning" in out
    assert "HIGH" in out


def test_show_vulnerabilities_trigger_scan_false_handles_active_status(capsys):
    # Continuous-scanning repos report status ACTIVE indefinitely (no
    # "in progress -> complete" lifecycle). A single queued response also
    # proves this path makes exactly one call and never polls/hangs.
    client = boto3.client("ecr", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "describe_image_scan_findings",
        {
            "imageScanStatus": {"status": "ACTIVE"},
            "imageScanFindings": {
                "findingSeverityCounts": {"MEDIUM": 2},
                "findings": [{"name": "CVE-2", "severity": "MEDIUM", "description": "desc"}],
            },
        },
    )
    with stubber:
        eds.show_vulnerabilities(client, "myrepo", "1.0", trigger_scan=False)
    stubber.assert_no_pending_responses()
    out = capsys.readouterr().out
    assert "MEDIUM" in out
    assert "no final" in out.lower()


# --------------------------------------------------------------------------
# wait_for_image_in_account_b (moto: ecr, + fake clock for the timeout path)
# --------------------------------------------------------------------------

@mock_aws
def test_wait_for_image_in_account_b_found_immediately():
    client = boto3.client("ecr", region_name="us-east-1")
    make_ecr_image(client, "myrepo", "1.0")
    eds.wait_for_image_in_account_b(client, "myrepo", "1.0")  # must not raise


@mock_aws
def test_wait_for_image_in_account_b_times_out(fake_clock):
    client = boto3.client("ecr", region_name="us-east-1")
    client.create_repository(repositoryName="myrepo")  # image never pushed
    with pytest.raises(SystemExit):
        eds.wait_for_image_in_account_b(client, "myrepo", "1.0", timeout=2, interval=1)


# --------------------------------------------------------------------------
# resolve_datasync_arn (moto: datasync)
# --------------------------------------------------------------------------

@mock_aws
def test_resolve_datasync_arn_passthrough_for_arn():
    client = boto3.client("datasync", region_name="us-east-1")
    arn = "arn:aws:datasync:us-east-1:123456789012:task/task-1"
    assert eds.resolve_datasync_arn(client, arn) == arn


@mock_aws
def test_resolve_datasync_arn_resolves_unique_name():
    client = boto3.client("datasync", region_name="us-east-1")
    arn = create_datasync_task(client, "my-task")
    assert eds.resolve_datasync_arn(client, "my-task") == arn


@mock_aws
def test_resolve_datasync_arn_no_match_exits():
    client = boto3.client("datasync", region_name="us-east-1")
    with pytest.raises(SystemExit):
        eds.resolve_datasync_arn(client, "does-not-exist")


@mock_aws
def test_resolve_datasync_arn_multiple_matches_exits():
    client = boto3.client("datasync", region_name="us-east-1")
    create_datasync_task(client, "dup-task")
    create_datasync_task(client, "dup-task")
    with pytest.raises(SystemExit):
        eds.resolve_datasync_arn(client, "dup-task")


# --------------------------------------------------------------------------
# invoke_copy_lambda (Stubber: moto's Lambda invoke needs Docker to run code)
# --------------------------------------------------------------------------

def test_invoke_copy_lambda_success(capsys):
    client = boto3.client("lambda", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "invoke",
        {"StatusCode": 200, "Payload": streaming({"copied": True})},
        expected_params={
            "FunctionName": "ecr-image-sync",
            "InvocationType": "RequestResponse",
            "Payload": json.dumps({"repo": "cms/x", "tags": "1.0"}).encode(),
        },
    )
    with stubber:
        eds.invoke_copy_lambda(client, "ecr-image-sync", "cms/x", "1.0")
    stubber.assert_no_pending_responses()
    assert "StatusCode 200" in capsys.readouterr().out


def test_invoke_copy_lambda_function_error_exits():
    client = boto3.client("lambda", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "invoke",
        {
            "StatusCode": 200,
            "FunctionError": "Unhandled",
            "Payload": streaming({"errorMessage": "boom"}),
        },
    )
    with stubber:
        with pytest.raises(SystemExit):
            eds.invoke_copy_lambda(client, "ecr-image-sync", "cms/x", "1.0")


# --------------------------------------------------------------------------
# run_datasync_task (Stubber: moto's task-execution status never progresses
# past INITIALIZING on its own, so we drive the transitions ourselves)
# --------------------------------------------------------------------------

def test_run_datasync_task_success():
    client = boto3.client("datasync", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "start_task_execution",
        {"TaskExecutionArn": "exec-arn"},
        expected_params={"TaskArn": "task-arn"},
    )
    stubber.add_response(
        "describe_task_execution",
        {"TaskExecutionArn": "exec-arn", "Status": "LAUNCHING"},
    )
    stubber.add_response(
        "describe_task_execution",
        {"TaskExecutionArn": "exec-arn", "Status": "SUCCESS"},
    )
    with stubber:
        assert eds.run_datasync_task(client, "task-arn", "App DataSync Task") is True
    stubber.assert_no_pending_responses()


def test_run_datasync_task_error():
    client = boto3.client("datasync", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response("start_task_execution", {"TaskExecutionArn": "exec-arn"})
    stubber.add_response(
        "describe_task_execution",
        {"TaskExecutionArn": "exec-arn", "Status": "ERROR"},
    )
    with stubber:
        assert eds.run_datasync_task(client, "task-arn", "Infra DataSync Task") is False


# --------------------------------------------------------------------------
# find_new_pipeline_execution (Stubber + fake clock; moto doesn't implement
# list_pipeline_executions)
# --------------------------------------------------------------------------

def test_find_new_pipeline_execution_finds_newer_execution(fake_clock):
    client = boto3.client("codepipeline", region_name="us-east-1")
    stubber = Stubber(client)
    after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    stubber.add_response(
        "list_pipeline_executions",
        {
            "pipelineExecutionSummaries": [
                {
                    "pipelineExecutionId": "exec-old",
                    "status": "Succeeded",
                    "startTime": datetime(2025, 1, 1, tzinfo=timezone.utc),
                }
            ]
        },
    )
    stubber.add_response(
        "list_pipeline_executions",
        {
            "pipelineExecutionSummaries": [
                {
                    "pipelineExecutionId": "exec-new",
                    "status": "InProgress",
                    "startTime": datetime(2026, 1, 2, tzinfo=timezone.utc),
                }
            ]
        },
    )
    with stubber:
        result = eds.find_new_pipeline_execution(
            client, "my-pipeline", after, timeout=10, poll_interval=1
        )
    assert result == "exec-new"


def test_find_new_pipeline_execution_times_out(fake_clock):
    client = boto3.client("codepipeline", region_name="us-east-1")
    stubber = Stubber(client)
    after = datetime(2026, 1, 1, tzinfo=timezone.utc)
    # FakeClock: first time.time() call (deadline calc) returns 0 -> deadline=3.
    # Loop condition then sees 1 and 2 (both < 3) before seeing 3 and stopping,
    # so exactly two list_pipeline_executions calls happen.
    stubber.add_response("list_pipeline_executions", {"pipelineExecutionSummaries": []})
    stubber.add_response("list_pipeline_executions", {"pipelineExecutionSummaries": []})
    with stubber:
        with pytest.raises(SystemExit):
            eds.find_new_pipeline_execution(
                client, "my-pipeline", after, timeout=3, poll_interval=1
            )
    stubber.assert_no_pending_responses()


# --------------------------------------------------------------------------
# get_codebuild_build_id (Stubber + fake clock; moto doesn't implement
# list_action_executions)
# --------------------------------------------------------------------------

def test_get_codebuild_build_id_found(fake_clock):
    client = boto3.client("codepipeline", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response(
        "list_action_executions",
        {"actionExecutionDetails": []},
        expected_params={
            "pipelineName": "my-pipeline",
            "filter": {"pipelineExecutionId": "exec-1"},
        },
    )
    stubber.add_response(
        "list_action_executions",
        {
            "actionExecutionDetails": [
                {
                    "pipelineExecutionId": "exec-1",
                    "stageName": "Build",
                    "actionName": "Build",
                    "output": {"executionResult": {"externalExecutionId": "build-123"}},
                }
            ]
        },
    )
    with stubber:
        result = eds.get_codebuild_build_id(
            client, "my-pipeline", "exec-1", timeout=10, poll_interval=1
        )
    assert result == "build-123"


def test_get_codebuild_build_id_times_out(fake_clock):
    client = boto3.client("codepipeline", region_name="us-east-1")
    stubber = Stubber(client)
    stubber.add_response("list_action_executions", {"actionExecutionDetails": []})
    stubber.add_response("list_action_executions", {"actionExecutionDetails": []})
    with stubber:
        with pytest.raises(SystemExit):
            eds.get_codebuild_build_id(
                client, "my-pipeline", "exec-1", timeout=3, poll_interval=1
            )
    stubber.assert_no_pending_responses()


# --------------------------------------------------------------------------
# tail_codebuild_logs (Stubber for both codebuild and logs clients)
# --------------------------------------------------------------------------

def test_tail_codebuild_logs_streams_until_finished(capsys):
    codebuild = boto3.client("codebuild", region_name="us-east-1")
    logs = boto3.client("logs", region_name="us-east-1")
    cb_stub = Stubber(codebuild)
    logs_stub = Stubber(logs)

    # 1st poll: build running, log destination not assigned yet.
    cb_stub.add_response("batch_get_builds", {"builds": [{"id": "build-1", "buildStatus": "IN_PROGRESS"}]})
    # 2nd poll: still running, logs now available -> one get_log_events call.
    cb_stub.add_response(
        "batch_get_builds",
        {"builds": [{"id": "build-1", "buildStatus": "IN_PROGRESS", "logs": {"groupName": "/g", "streamName": "s"}}]},
    )
    logs_stub.add_response(
        "get_log_events",
        {
            "events": [{"message": "line1", "timestamp": 0, "ingestionTime": 0}],
            "nextForwardToken": "f/1",
            "nextBackwardToken": "b/1",
        },
    )
    # 3rd poll: build finished -> one more get_log_events call, then break.
    cb_stub.add_response(
        "batch_get_builds",
        {"builds": [{"id": "build-1", "buildStatus": "SUCCEEDED", "logs": {"groupName": "/g", "streamName": "s"}}]},
    )
    logs_stub.add_response(
        "get_log_events",
        {
            "events": [{"message": "line2", "timestamp": 0, "ingestionTime": 0}],
            "nextForwardToken": "f/2",
            "nextBackwardToken": "b/1",
        },
    )

    with cb_stub, logs_stub:
        status = eds.tail_codebuild_logs(codebuild, logs, "build-1")

    cb_stub.assert_no_pending_responses()
    logs_stub.assert_no_pending_responses()
    assert status == "SUCCEEDED"
    out = capsys.readouterr().out
    assert "line1" in out
    assert "line2" in out
    assert "CodeBuild finished with status: SUCCEEDED" in out


def test_tail_codebuild_logs_reports_failure():
    codebuild = boto3.client("codebuild", region_name="us-east-1")
    logs = boto3.client("logs", region_name="us-east-1")
    cb_stub = Stubber(codebuild)
    logs_stub = Stubber(logs)
    cb_stub.add_response("batch_get_builds", {"builds": [{"id": "build-1", "buildStatus": "FAILED"}]})

    with cb_stub, logs_stub:
        status = eds.tail_codebuild_logs(codebuild, logs, "build-1")

    assert status == "FAILED"


# --------------------------------------------------------------------------
# print_paged / prompt (no AWS involved, just input()/print())
# --------------------------------------------------------------------------

def test_print_paged_no_pause_when_short(capsys):
    eds.print_paged(["a", "b"], page_size=20)
    out = capsys.readouterr().out
    assert "a" in out and "b" in out


def test_print_paged_pauses_between_pages(monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    lines = [f"line-{i}" for i in range(25)]
    eds.print_paged(lines, page_size=20)
    out = capsys.readouterr().out
    assert "line-0" in out
    assert "line-24" in out


def test_prompt_falls_back_to_default(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a: "")
    assert eds.prompt("Question", "default-val") == "default-val"


def test_prompt_uses_typed_value(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *_a: "typed-value")
    assert eds.prompt("Question", "default-val") == "typed-value"
