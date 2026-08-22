from pathlib import Path


def test_base_role_is_admin_applied_and_session_policy_ready() -> None:
    template = Path("infra/scene-execution-role.yaml").read_text(encoding="utf-8")
    assert "AWS::IAM::Role" in template
    assert "OrchestratorPrincipalArn" in template
    assert "sts:AssumeRole" in template
    assert "s3:GetObjectVersion" in template
    assert "s3:PutObject" in template
    assert "BedrockInvokeModelArns" in template
    assert "Type: CommaDelimitedList" in template
    assert "BedrockModelArn" not in template
    assert "bedrock:InvokeModel" in template
    assert "bedrock:*" not in template
    assert "s3:DeleteObject" not in template
    assert "s3:DeleteObjectVersion" not in template
    assert "AWS::S3::Bucket" not in template


def test_packaged_template_is_byte_identical_to_the_repository_template() -> None:
    """The wheel ships the template so `pearls infra` works when installed.

    Keeping the copy byte-identical is what stops the installed CLI from
    deploying a different role than the one reviewed in this repository.
    """
    repository = Path("infra/scene-execution-role.yaml").read_bytes()
    packaged = Path("src/pearls/templates/scene-execution-role.yaml").read_bytes()

    assert packaged == repository


def test_installed_template_is_the_one_the_cli_loads() -> None:
    from pearls.cli._template import load_role_template

    template = load_role_template()

    assert template.body == Path("infra/scene-execution-role.yaml").read_text(encoding="utf-8")


def test_documented_deployment_supplies_every_required_role_parameter() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    deploy = readme.split("aws cloudformation deploy", 1)[1].split("```", 1)[0]

    assert "OrchestratorPrincipalArn=" in deploy
    assert "ProjectBucketArnPattern=" in deploy
    assert "BedrockInvokeModelArns=arn:aws:bedrock:" in deploy


def test_documented_cli_deployment_covers_the_two_model_trial_example() -> None:
    """The Trial 001 worked example must stay complete and runnable."""
    readme = Path("README.md").read_text(encoding="utf-8")
    deploy = readme.split("pearls infra deploy", 2)[2].split("```", 1)[0]

    assert "--orchestrator-principal-arn" in deploy
    assert "--project-bucket-arn-pattern" in deploy
    assert deploy.count("--bedrock-model-arn arn:aws:bedrock:") >= 2
    assert "--role-name" in deploy
    assert "--yes" in deploy
    assert "assume-policy" in readme
    assert "--apply-to-user" in readme


def test_documented_cli_states_the_two_trust_boundaries() -> None:
    # The README is hard-wrapped, so compare against unwrapped prose.
    prose = " ".join(Path("README.md").read_text(encoding="utf-8").split())

    assert "trusted bootstrap identity" in prose
    assert "trusted-handler" in prose
    assert "proves code identity, not code safety" in prose
    assert "claims no sandboxing and no ambient credential isolation" in prose
    assert "remains a console prerequisite" in prose


def test_documented_cli_records_the_shared_command_conventions() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    conventions = readme.split("### Command line", 1)[1].split("###", 1)[0]

    for verb in ("scene", "infra", "validate", "plan", "deploy", "status", "assume-policy"):
        assert verb in conventions
    assert "`--json`" in conventions
    assert "`0` success" in conventions
