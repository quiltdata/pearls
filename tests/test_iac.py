from pathlib import Path


def test_base_role_is_admin_applied_and_session_policy_ready() -> None:
    template = Path("infra/scene-execution-role.yaml").read_text(encoding="utf-8")
    assert "AWS::IAM::Role" in template
    assert "OrchestratorPrincipalArn" in template
    assert "sts:AssumeRole" in template
    assert "s3:GetObjectVersion" in template
    assert "s3:PutObject" in template
    assert "BedrockModelArn" in template
    assert "bedrock:InvokeModel" in template
    assert "bedrock:*" not in template
    assert "s3:DeleteObject" not in template
    assert "s3:DeleteObjectVersion" not in template
    assert "AWS::S3::Bucket" not in template


def test_documented_deployment_supplies_every_required_role_parameter() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")
    deploy = readme.split("aws cloudformation deploy", 1)[1].split("```", 1)[0]

    assert "OrchestratorPrincipalArn=" in deploy
    assert "ProjectBucketArnPattern=" in deploy
    assert "BedrockModelArn=arn:aws:bedrock:" in deploy
