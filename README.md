# cms-ecr-and-data-sync

Copies an ECR image from the Source Account to the Destination Account,
then runs the DataSync -> CodePipeline -> CodeBuild pipeline that follows,
tailing the build logs to completion.

## What it does

1. Verifies the image exists in the Source Account's ECR and prints its
   vulnerability scan findings.
2. Asks you whether to continue, based on those findings.
3. Invokes a Lambda in the Destination Account to copy the image over.
4. Verifies the image now exists in the Destination Account's ECR.
5. Runs DataSync task 1, then (only if it succeeds) DataSync task 2.
6. Task 2 triggers a CodePipeline execution that runs a CodeBuild project;
   the script tails its CloudWatch logs live and reports the final status.

## Prerequisites

- Python 3.9+
- `pip install -r requirements.txt` (installs `boto3`)
- Two AWS CLI profiles configured in `~/.aws/config` / `~/.aws/credentials`:
  one for the Source Account, one for the Destination Account

### IAM permissions

**Source Account profile** needs:
- `sts:GetCallerIdentity`
- `ecr:DescribeImages`
- `ecr:DescribeImageScanFindings`
- `ecr:StartImageScan` (only if you wire in the `trigger_new_scan()` hook)

**Destination Account profile** needs:
- `sts:GetCallerIdentity`
- `ecr:DescribeImages`
- `lambda:InvokeFunction` on the copy Lambda
- `datasync:StartTaskExecution`, `datasync:DescribeTaskExecution`
- `codepipeline:ListPipelineExecutions`, `codepipeline:GetPipelineState`
- `codebuild:BatchGetBuilds`
- `logs:GetLogEvents` on the CodeBuild project's log group

## Usage

```
python3 ecr_data_sync.py \
  --profile-a source-account-profile \
  --profile-b dest-account-profile \
  --repo cms/iplus/iplus-payment835-extract-service/0.0.28-RELEASE \
  --datasync-task1 <task-id-or-arn> \
  --datasync-task2 <task-id-or-arn> \
  --pipeline-name <pipeline-name>
```

Any argument you omit is prompted for interactively, except:
- `--region` defaults to `us-east-1` if not given.
- `--lambda-name` defaults to `ecr-image-sync` if not given.

Before doing anything, the script prints every value it resolved (from
flags, prompts, or defaults) and asks for confirmation.

### Arguments

| Flag | Required? | Default / behavior if omitted |
|---|---|---|
| `--profile-a` | yes | prompted |
| `--profile-b` | yes | prompted |
| `--region` | no | `us-east-1` |
| `--repo` | yes | prompted; format `<repo-path>/<tag>`, e.g. `cms/iplus/my-service/1.2.3-RELEASE` |
| `--lambda-name` | no | `ecr-image-sync` |
| `--datasync-task1` | yes | prompted; bare task ID or full ARN |
| `--datasync-task2` | yes | prompted; bare task ID or full ARN |
| `--pipeline-name` | yes | prompted |

A bare DataSync task ID is turned into a full ARN using the Destination
Account's account ID and `--region`.

### Exit codes

- `0` — CodeBuild finished with status `SUCCEEDED`.
- `1` — CodeBuild finished with any other status.
- Non-zero with a message — aborted earlier (bad credentials, image not
  found, user declined a prompt, Lambda error, DataSync task failure, or a
  timeout waiting for the image/pipeline execution/CodeBuild action).

## Notes

- Vulnerability findings are read from the existing ECR scan; the script
  does not trigger a new scan by default. `trigger_new_scan()` in
  `ecr_data_sync.py` is an unused hook you can call if you later need to
  force a fresh scan and wait for it to complete.
- The Lambda copy step is confirmed two ways: the Lambda's response
  (`StatusCode` 200, no `FunctionError`) and a follow-up check that the
  image actually appears in the Destination Account's ECR.
