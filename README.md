# cms-ecr-and-data-sync

Optionally copies an ECR image from the Source Account to the Destination
Account, then runs the DataSync -> CodePipeline -> CodeBuild pipeline that
follows, tailing the build logs to completion. Defaults to a dry run, and
pauses for confirmation before every major step.

## What it does

1. Prompts for (or accepts as flags) the Source/Destination profiles, the
   image `repo:tag`, the two DataSync task names/ARNs, and the pipeline
   name, then prints the resolved configuration and asks for confirmation.
2. Verifies AWS credentials in both accounts (`sts:GetCallerIdentity`).
3. **If an image was given:**
   - Verifies the image exists in the Source Account's ECR.
   - Triggers a fresh vulnerability scan and prints the findings.
   - Asks whether to continue, based on those findings.
   - Invokes a Lambda in the Destination Account to copy the image over.
   - Verifies the image now exists in the Destination Account's ECR.

   Pass `-` instead of a `repo:tag` to skip this whole step (see
   [Skipping the image copy](#skipping-the-image-copy)).
4. Asks whether to continue, then runs the App DataSync Task and waits for
   it to succeed.
5. Asks whether to continue, then runs the Infra DataSync Task and waits
   for it to succeed. This task is expected to trigger a new CodePipeline
   execution.
6. Asks whether to continue, then finds that new pipeline execution, finds
   the CodeBuild build it ran, tails its CloudWatch logs live, and reports
   the final build status.

The script never starts, stops, or approves anything in CodePipeline or
CodeBuild — steps 6 only reads/polls state that the Infra DataSync Task is
expected to have triggered on its own.

## Prerequisites

- Python 3.9+
- A virtualenv with dependencies installed:
  ```
  python3 -m venv venv
  source venv/bin/activate
  pip install -r requirements.txt   # installs boto3
  ```
- Two AWS CLI profiles configured in `~/.aws/config` / `~/.aws/credentials`:
  one for the Source Account, one for the Destination Account. The script
  defaults to profiles named `cms-dev` (source) and `cms-test`
  (destination) if you don't override them.

### IAM permissions

**Source Account profile** needs (only used when an image is given):
- `sts:GetCallerIdentity`
- `ecr:DescribeImages`
- `ecr:StartImageScan`
- `ecr:DescribeImageScanFindings`

**Destination Account profile** needs:
- `sts:GetCallerIdentity`
- `ecr:DescribeImages` (only used when an image is given)
- `lambda:InvokeFunction` on the copy Lambda (only used when an image is given)
- `datasync:ListTasks`, `datasync:StartTaskExecution`, `datasync:DescribeTaskExecution`
- `codepipeline:ListPipelineExecutions`, `codepipeline:ListActionExecutions`
- `codebuild:BatchGetBuilds`
- `logs:GetLogEvents` on the CodeBuild project's log group

## Usage

The script defaults to a **dry run**: it resolves and confirms
configuration, verifies credentials, and (if an image was given) verifies
the image and shows vulnerability findings, but stops short of making any
changes and just prints what it would do. Pass `--no-dry-run` to actually
copy the image and run DataSync/CodePipeline.

```
python3 ecr_data_sync.py \
  --profile-a cms-dev \
  --profile-b cms-test \
  --repo cms/iplus/iplus-payment835-extract-service:0.0.28-RELEASE \
  --app-datasync-task <task-name-or-arn> \
  --infra-datasync-task <task-name-or-arn> \
  --pipeline-name <pipeline-name> \
  --no-dry-run
```

Any argument you omit is prompted for interactively, with the defaults
shown below.

### Arguments

| Flag | Default / behavior if omitted |
|---|---|
| `--profile-a` | prompted, defaulting to `cms-dev` |
| `--profile-b` | prompted, defaulting to `cms-test` |
| `--region` | `us-east-1` |
| `--repo` | prompted; format `<repo-path>:<tag>`, e.g. `cms/iplus/my-service:1.2.3-RELEASE`; pass `-` for no image |
| `--lambda-name` | `ecr-image-sync` |
| `--app-datasync-task` | prompted; bare task name or full ARN |
| `--infra-datasync-task` | prompted; bare task name or full ARN |
| `--pipeline-name` | prompted |
| `--no-dry-run` | omit to dry-run (default); pass to make real changes |

A bare DataSync task name is resolved to its ARN via `datasync:ListTasks`
in the Destination Account; the script exits if zero or more than one task
matches that name.

### Skipping the image copy

Some runs don't need an image copied (e.g. a DataSync/pipeline-only re-run).
Pass `--repo -` (or type `-` at the interactive prompt) to skip the image
verification, vulnerability scan, and Lambda copy entirely — the
Destination Account's ECR is left untouched, and the script moves straight
to the DataSync/CodePipeline confirmations and steps.

### Confirmations

With defaults, you'll be asked to confirm at each of these points before
anything below it happens:
1. After the initial configuration is resolved (all values, before any AWS calls).
2. After reviewing vulnerability findings, before copying the image (skipped with `--repo -`).
3. After the image copy is verified, before running the DataSync tasks (skipped with `--repo -`).
4. After the App DataSync Task succeeds, before running the Infra DataSync Task.
5. After the Infra DataSync Task succeeds, before waiting on CodePipeline/CodeBuild.

Answering anything other than `y` at any of these aborts the run immediately.

### Exit codes

- `0` — dry run completed, or CodeBuild finished with status `SUCCEEDED`.
- `1` — CodeBuild finished with any other status.
- Non-zero with a message — aborted earlier (bad credentials, image not
  found, user declined a prompt, Lambda error, DataSync task failure, or a
  timeout waiting for the image, a new pipeline execution, or the
  CodeBuild action to start).

## Functions (`ecr_data_sync.py`)

| Function | Purpose |
|---|---|
| `prompt(text, default)` | `input()` wrapper that falls back to a default. |
| `parse_args()` | Defines and parses all CLI flags. |
| `resolve_config(args)` | Merges flags, prompts, and defaults into a single config dict; handles the `-` (no image) sentinel. |
| `confirm_config(cfg)` | Prints the resolved configuration and asks for the initial go-ahead. |
| `get_account_id(session, label)` | Verifies credentials via `sts:GetCallerIdentity`. |
| `verify_image_exists(...)` | Confirms an image tag exists in a repository. |
| `start_and_wait_for_scan(...)` | Triggers a fresh ECR vulnerability scan and polls until it completes. |
| `show_vulnerabilities(...)` | Prints the severity summary and a paged list of findings. |
| `print_paged(lines, page_size)` | Prints a list of lines in pages, pausing between pages. |
| `invoke_copy_lambda(...)` | Invokes the copy Lambda and checks its response for errors. |
| `wait_for_image_in_account_b(...)` | Polls the Destination Account's ECR until the copied image appears. |
| `resolve_datasync_arn(...)` | Resolves a bare DataSync task name to its ARN (or passes an ARN through). |
| `run_datasync_task(...)` | Starts a DataSync task execution and polls it to `SUCCESS`/`ERROR`. |
| `find_new_pipeline_execution(...)` | Polls for a CodePipeline execution that started after a given time. |
| `get_codebuild_build_id(...)` | Resolves the CodeBuild build ID tied to a pipeline execution. |
| `tail_codebuild_logs(...)` | Polls the build's status and streams new CloudWatch log lines until it finishes. |
| `main()` | Orchestrates the full flow described above. |

## Testing

`test_ecr_data_sync.py` unit-tests each function above against fake AWS
clients — no real AWS account or credentials needed, and no changes are
made to `ecr_data_sync.py` itself to support it. It combines two
approaches:

- **[moto](https://github.com/getmoto/moto)** (`@mock_aws`) for services it
  emulates well end-to-end: ECR (image lookup, vulnerability scan
  findings), STS, and DataSync (task resolution).
- **`botocore.stub.Stubber`** for Lambda invoke (moto's invoke needs Docker
  to actually run code) and for CodePipeline/CodeBuild/CloudWatch Logs
  (moto doesn't implement `list_pipeline_executions`,
  `list_action_executions`, or the exact `batch_get_builds`/`get_log_events`
  polling sequence this script relies on).

A `FakeClock` fixture replaces `time.time()` so the timeout-based polling
loops (`find_new_pipeline_execution`, `get_codebuild_build_id`,
`wait_for_image_in_account_b`) run a deterministic number of iterations
instead of racing the real clock, and `time.sleep` is patched to a no-op.

Run it with:
```
pip install moto pytest   # not in requirements.txt; test-only deps
pytest test_ecr_data_sync.py -v
```

Note: these tests call each function directly with a fake client — they
don't run `main()` end-to-end, so the CLI/prompt wiring and the full
sequenced flow aren't covered.

## Notes

- Vulnerability findings always come from a freshly triggered scan
  (`start_and_wait_for_scan`); if a scan is already in progress, the script
  waits for it instead of starting another.
- The Lambda copy step is confirmed two ways: the Lambda's response
  (`StatusCode` 200, no `FunctionError`) and a follow-up check that the
  image actually appears in the Destination Account's ECR.
- The CodeBuild build ID is resolved via
  `codepipeline:ListActionExecutions` filtered by the pipeline execution
  ID, reading `output.executionResult.externalExecutionId` — not via
  `GetPipelineState`, whose `ActionExecution` shape has no
  `pipelineExecutionId` field to match against.
