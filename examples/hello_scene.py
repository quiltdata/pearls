"""Thin one-call Bedrock Converse consumer of :func:`raja_sts.run_scene`."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from raja_sts import SceneContext, SceneHandlerResult, run_scene


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def hello_handler(
    inputs: dict[str, Any], session: Any, context: SceneContext
) -> SceneHandlerResult:
    """Build a prompt from one selected input and make exactly one Converse call."""
    prompt = next(iter(inputs.values())).content.decode("utf-8")
    started_at = _now()
    response = session.client("bedrock-runtime").converse(
        modelId=context.bedrock_model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    completed_at = _now()
    text = response["output"]["message"]["content"][0]["text"]
    metadata = response.get("ResponseMetadata", {})
    return SceneHandlerResult(
        objects={"result/prompt.txt": prompt, "result/response.txt": text},
        invocation={
            "model_id": response.get("modelId", context.bedrock_model_id),
            "request_id": metadata.get("RequestId"),
            "usage": response.get("usage"),
            "stop_reason": response.get("stopReason"),
            "started_at": started_at,
            "completed_at": completed_at,
        },
    )


def run_hello(*, manifest_uri: str, scope: dict[str, object], role_arn: str, scene_id: str) -> str:
    """Run Hello Scene and return its immutable output Package URI."""
    result = run_scene(
        manifest_uri=manifest_uri,
        scope_spec=scope,
        role_arn=role_arn,
        scene_id=scene_id,
        handler=hello_handler,
    )
    return result.package_uri
