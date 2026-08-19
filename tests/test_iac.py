from pathlib import Path


def test_base_role_is_admin_applied_and_session_policy_ready() -> None:
    template = Path("infra/scene-execution-role.yaml").read_text(encoding="utf-8")
    assert "AWS::IAM::Role" in template
    assert "OrchestratorPrincipalArn" in template
    assert "sts:AssumeRole" in template
    assert "s3:GetObjectVersion" in template
    assert "s3:PutObject" in template
    # The broad role deliberately includes deletion so the session policy's explicit
    # deny proves attenuation rather than merely repeating the base role.
    assert "s3:DeleteObjectVersion" in template
    assert "AWS::S3::Bucket" not in template
