"""``pearls scene`` — preflight and execute exactly one sealed-genesis Scene.

Both verbs take the same arguments so that a passing ``check`` describes the run
that ``run`` would perform. ``check`` performs only reads; ``run`` delegates to
:func:`pearls.run_scene_from_genesis` exactly once and adds no authority,
materialization, workflow, or publication logic of its own.
"""

from __future__ import annotations

import argparse
import secrets
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from ..bedrock import (
    BedrockInvocation,
    BedrockProfileLoader,
    bedrock_model_region,
    load_bedrock_profile_resources,
    parse_bedrock_invocation,
    parse_bedrock_invocation_declaration,
)
from ..errors import InvalidScopeError, PearlsError
from ..genesis import RuntimeRequirementsReport
from ..manifest import ManifestLoader, load_quilt_manifest, parse_manifest_uri
from ..models import ManifestObject, PinnedManifestURI
from ..policy import DEFAULT_MAX_POLICY_CHARACTERS
from ..runner import SceneContext, SceneHandler, SceneHandlerResult, SceneRunResult
from ..sealed import SEALED_HANDLER_KEY, SealedSource
from ..sts import validate_assume_role_request
from ._common import (
    EXIT_OK,
    SCENE_CHECK_SCHEMA,
    SCENE_RUN_SCHEMA,
    CliRefusal,
    Emitter,
    Runtime,
    add_aws_arguments,
    build_session,
    envelope,
    iso8601,
    positive_int,
    read_json_argument,
    render_table,
)

_BOOTSTRAP = "trusted-bootstrap"
_LOCAL = "local"
_DEFAULT_WORKFLOW = "<registry default>"


class SceneHandlerError(PearlsError):
    """The sealed handler itself failed during execution.

    Distinct from :class:`~pearls.HandlerContractError`, which means the handler
    returned something the runner refuses. This means the sealed code raised.
    """


UNPROVEN_CONDITIONS: tuple[str, ...] = (
    "Bedrock account-level access to the requested model or profile cannot be proven "
    "without invoking it; enabling model access stays a console prerequisite.",
    "The base role's trust policy and permissions are not read here. Effective "
    "authority is the intersection of the base role and this session policy, so a "
    "base inventory missing the selected model/profile or a required support resource "
    "fails closed at run time.",
    "The output registry's Quilt workflow contract is validated at publication "
    "with the Scene credential, not by this preflight.",
    "Verifying the sealed handler proves code identity. Runtime preflight separately "
    "checks declared distribution versions and top-level module resolution; it does "
    "not prove that the named distribution supplied the resolved module. Neither "
    "check executes dependency or handler module bodies, so declaration completeness, "
    "import-time/native initialization, and callable binding remain unproven. During "
    "a run the handler executes in-process as trusted code and receives the scoped "
    "session that publication later uses.",
    "The Scene credential is prefix-scoped for input reads and can read objects under "
    "the reported source prefixes that are not selected by the manifest scope. The "
    "runner requests each selected object by exact VersionId and verifies its Quilt "
    "hash, but this preflight does not prove the absence of extra reads.",
    "The packed STS session-policy budget is undocumented and cannot be calculated "
    "without calling AssumeRole. This preflight verifies plaintext shape and size but "
    "does not prove that AWS will accept the compressed policy.",
    "quilt3 manifest retrieval does not verify downloaded manifest bytes against "
    "the requested top hash (quiltdata/quilt#5181), so the genesis top hash is "
    "provenance, not tamper enforcement.",
)


def _requirements_summary(report: RuntimeRequirementsReport) -> str:
    return (
        f"Python {report.actual_python}; {report.applicable_count} applicable import(s) "
        "passed independent distribution-version and module-resolution checks; "
        f"{report.not_applicable_count} marker-skipped ({len(report.checks)} declared)"
    )


def _caching_loader(base: ManifestLoader | None) -> ManifestLoader:
    """Resolve each pinned revision once for both the preflight and the handler."""
    loader = base or load_quilt_manifest
    cache: dict[str, tuple[ManifestObject, ...]] = {}

    def load(manifest: PinnedManifestURI) -> Sequence[ManifestObject]:
        cached = cache.get(manifest.top_hash)
        if cached is None:
            cached = tuple(loader(manifest))
            cache[manifest.top_hash] = cached
        return cached

    return load


def generate_scene_id(now: datetime) -> str:
    """Return a collision-resistant Scene ID that is a valid RoleSessionName."""
    stamp = now.astimezone(UTC).strftime("%Y%m%dT%H%M%S")
    return f"scene-{stamp}Z-{secrets.token_hex(4)}"


def _add_shared_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--genesis",
        required=True,
        metavar="URI",
        help="top-hash-pinned genesis package, quilt+s3://BUCKET#package=NAME&top_hash=HASH",
    )
    parser.add_argument(
        "--role-arn",
        required=True,
        metavar="ARN",
        help="Scene execution role to assume for this run",
    )
    parser.add_argument(
        "--model-arn",
        required=True,
        metavar="ARN",
        help="exact Bedrock foundation-model or account-scoped inference-profile ARN selected",
    )
    parser.add_argument(
        "--model-resource-arn",
        action="append",
        default=[],
        metavar="ARN",
        help=(
            "expected exact supporting foundation-model ARN for an inference profile; "
            "repeat for every destination returned by GetInferenceProfile, including "
            "the regionless resource required by a global profile"
        ),
    )
    parser.add_argument("--scene-id", help="explicit Scene ID; generated when omitted")
    parser.add_argument(
        "--duration-seconds",
        type=positive_int,
        default=3600,
        help="Scene credential lifetime, 900-43200 (default: 3600)",
    )
    parser.add_argument(
        "--external-id",
        help="sts:ExternalId required by a separately managed role's trust policy",
    )
    parser.add_argument(
        "--handler-key",
        default=SEALED_HANDLER_KEY,
        metavar="LOGICAL_KEY",
        help=f"sealed handler inside the genesis revision (default: {SEALED_HANDLER_KEY})",
    )
    parser.add_argument(
        "--metadata",
        metavar="JSON|@FILE",
        help="output package user_meta as inline JSON or @path to a JSON file",
    )
    parser.add_argument("--message", help="output package commit message")
    workflow = parser.add_mutually_exclusive_group()
    workflow.add_argument(
        "--workflow",
        metavar="ID",
        help="name one workflow from the output registry's config (default: registry default)",
    )
    workflow.add_argument(
        "--no-workflow",
        action="store_true",
        help="explicitly opt out of a workflow; refused when the registry requires one",
    )
    add_aws_arguments(parser)


def add_parser(subparsers: Any) -> None:
    """Register the ``scene`` group and its verbs."""
    scene = subparsers.add_parser(
        "scene",
        help="preflight and execute one bounded Scene",
        description=(
            "Bounded Scene execution with short-lived Scene authority. Persistent IAM "
            "changes live in the separate 'pearls infra' group."
        ),
    )
    verbs = scene.add_subparsers(dest="verb", metavar="VERB", required=True)

    check = verbs.add_parser(
        "check",
        help="read-only preflight; mints nothing and writes nothing",
        description=(
            "Validate arguments, resolve and hash-verify the pinned genesis controls "
            "and sealed handler, compile the session policy, and report the non-secret "
            "authority. Performs no AssumeRole, no model invocation, and no write."
        ),
    )
    _add_shared_arguments(check)
    check.set_defaults(run=check_command)

    run = verbs.add_parser(
        "run",
        help="execute exactly one Scene and publish its output package",
        description=(
            "Load the sealed handler from the pinned genesis revision and call "
            "run_scene_from_genesis() exactly once. Mints one Scene credential and "
            "publishes one output package."
        ),
    )
    _add_shared_arguments(run)
    run.set_defaults(run=run_command)


def _resolve_workflow(args: argparse.Namespace) -> tuple[str | None | object, str | None]:
    """Return the workflow selector for publication and its display value."""
    if args.no_workflow:
        return None, None
    if args.workflow is not None:
        return args.workflow, args.workflow
    return ..., _DEFAULT_WORKFLOW


def _validate_arguments(
    runtime: Runtime, args: argparse.Namespace
) -> tuple[str, BedrockInvocation, dict[str, Any], str | None | object, str | None]:
    """Validate every argument shape offline, before any AWS call."""
    scene_id = args.scene_id or generate_scene_id(runtime.clock())
    validate_assume_role_request(
        role_arn=args.role_arn,
        scene_id=scene_id,
        duration_seconds=args.duration_seconds,
        external_id=args.external_id,
    )
    supplied_resources = tuple(args.model_resource_arn)
    closure: tuple[str, ...] | None = (
        (str(args.model_arn), *supplied_resources) if supplied_resources else None
    )
    declaration = parse_bedrock_invocation_declaration(args.model_arn, closure)
    if declaration is None:  # argparse marks --model-arn required.
        raise CliRefusal("--model-arn is required")
    metadata = read_json_argument(args.metadata, label="--metadata") if args.metadata else {}
    if args.message is not None and not args.message.strip():
        raise CliRefusal("--message must not be blank")
    if not args.handler_key:
        raise CliRefusal("--handler-key must not be empty")
    workflow, workflow_display = _resolve_workflow(args)
    return scene_id, declaration, metadata, workflow, workflow_display


def _verify_profile_destinations(
    session: Any,
    declaration: BedrockInvocation,
) -> tuple[BedrockInvocation, BedrockProfileLoader | None]:
    """Match a declared profile closure to the source Region's AWS metadata."""
    if declaration.selected.kind == "foundation-model":
        return declaration, None

    client = session.client(
        "bedrock",
        region_name=bedrock_model_region(declaration.selected_model_id),
    )

    def load(profile_arn: str) -> Sequence[str]:
        return load_bedrock_profile_resources(client, profile_arn)

    verified = parse_bedrock_invocation(
        declaration.selected_model_id,
        declaration.invoke_resources,
        profile_loader=load,
    )
    if verified is None:  # The declaration is non-optional.
        raise InvalidScopeError("Bedrock profile verification returned no invocation")

    def cached(profile_arn: str) -> Sequence[str]:
        if profile_arn != verified.selected_model_id:
            raise InvalidScopeError("cached profile identity does not match the selected model")
        return verified.supporting_foundation_models

    return verified, cached


def _genesis_document(genesis: PinnedManifestURI) -> dict[str, Any]:
    return {
        "uri": genesis.original,
        "bucket": genesis.bucket,
        "package_name": genesis.package_name,
        "top_hash": genesis.top_hash,
    }


def check_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Run the read-only preflight for one sealed-genesis Scene."""
    emitter = Emitter(runtime, args.json_output)
    checks: list[dict[str, str]] = []

    def record(name: str, identity: str, detail: str) -> None:
        checks.append({"name": name, "identity": identity, "status": "pass", "detail": detail})

    scene_id, declaration, metadata, _workflow, workflow_display = _validate_arguments(
        runtime, args
    )
    model_arn = declaration.selected_model_id
    genesis = parse_manifest_uri(args.genesis)
    record(
        "genesis-uri-pinned",
        _LOCAL,
        f"complete top hash {genesis.top_hash} for package {genesis.package_name}",
    )
    record(
        "argument-shapes",
        _LOCAL,
        f"role ARN, Scene ID {scene_id}, {args.duration_seconds}s duration"
        + (", external ID" if args.external_id else ""),
    )
    record("model-selection-exact", _LOCAL, model_arn)
    record(
        "model-authority-exact",
        _LOCAL,
        f"{len(declaration.invoke_resources)} exact bedrock:InvokeModel resource(s)",
    )
    record(
        "output-metadata-shapes",
        _LOCAL,
        f"{len(metadata)} declared metadata key(s), "
        f"message {'provided' if args.message else 'absent'}, "
        f"workflow {workflow_display if workflow_display is not None else '<explicitly none>'}",
    )

    # The bootstrap identity performs exact S3 reads and, for a profile,
    # one Bedrock control-plane read. It still mints nothing and writes nothing.
    session = build_session(runtime, args)
    invocation, profile_loader = _verify_profile_destinations(session, declaration)
    if invocation.selected.kind == "inference-profile":
        record(
            "model-profile-destinations-verified",
            _BOOTSTRAP,
            f"{len(invocation.supporting_foundation_models)} exact destination resource(s) "
            "matched GetInferenceProfile",
        )
    s3_client = session.client("s3")
    loader = _caching_loader(runtime.manifest_loader)

    preflight = runtime.preflight_genesis(
        args.genesis,
        bedrock_model=model_arn,
        bedrock_invoke_resources=invocation.invoke_resources,
        bedrock_profile_loader=profile_loader,
        manifest_loader=loader,
        genesis_s3_client=s3_client,
        max_policy_characters=DEFAULT_MAX_POLICY_CHARACTERS,
        output_revision_id=scene_id,
    )
    record(
        "genesis-manifest-resolved",
        _BOOTSTRAP,
        f"{len(preflight.objects)} manifest entries at the requested top hash",
    )
    record(
        "genesis-layout",
        _BOOTSTRAP,
        "README.md, outputs.json, protocol/requirements.json, and at least one input/ "
        "entry are present",
    )
    record(
        "genesis-controls-verified",
        _BOOTSTRAP,
        "README.md, outputs.json, and protocol/requirements.json were read by exact "
        "S3 version and matched their Quilt hashes",
    )
    requirements = preflight.runtime_requirements
    record(
        "runtime-requirements-sealed",
        _BOOTSTRAP,
        f"{requirements.descriptor.logical_key} matched its Quilt hash "
        f"(sha256 {requirements.source_sha256[:16]}...)",
    )
    record(
        "runtime-requirements-compatible",
        _LOCAL,
        _requirements_summary(requirements.compatibility),
    )
    compilation = preflight.compilation
    record(
        "input-integrity-enforcement-admitted",
        _LOCAL,
        f"{len(compilation.selected_objects)} selected descriptor(s) will be read by exact "
        "VersionId and verified against their declared Quilt hashes before handler execution",
    )
    record(
        "output-selectors",
        _BOOTSTRAP,
        f"{len(compilation.writable_selectors)} write selector(s) targeting "
        f"{compilation.output_package.name}@{compilation.output_package.bucket}",
    )

    # verify, never load: a preflight must not execute genesis code, because a
    # module body runs with this identity's authority, not the Scene's.
    sealed = runtime.verify_sealed_handler(
        args.genesis,
        manifest_loader=loader,
        genesis_s3_client=s3_client,
        handler_key=args.handler_key,
    )
    record(
        "sealed-handler-verified",
        _BOOTSTRAP,
        f"{args.handler_key} matched its Quilt hash (sha256 {sealed.source_sha256[:16]}...) "
        f"and binds a module-level handler; the bytes were not executed",
    )
    record(
        "session-policy-compiled",
        _LOCAL,
        f"{compilation.mode} authority; {compilation.size} of "
        f"{DEFAULT_MAX_POLICY_CHARACTERS} serialized characters; "
        f"{len(compilation.read_prefixes)} source prefix(es)",
    )

    payload = envelope(SCENE_CHECK_SCHEMA, "ok")
    payload.update(
        {
            "scene_id": scene_id,
            "genesis": _genesis_document(genesis),
            "role_arn": args.role_arn,
            "model_arn": model_arn,
            "model": invocation.report(),
            "duration_seconds": args.duration_seconds,
            "handler": sealed.report(),
            "runtime_requirements": requirements.report(),
            "authority": compilation.report(),
            "output": {
                "package_name": compilation.output_package.name,
                "bucket": compilation.output_package.bucket,
                "metadata_keys": sorted(metadata),
                "message_provided": bool(args.message),
                "workflow": workflow_display,
            },
            "checks": checks,
            "mutations_performed": [],
            "unproven": list(UNPROVEN_CONDITIONS),
        }
    )

    for entry in checks:
        emitter.line(f"pass  {entry['name']} ({entry['identity']})")
        emitter.line(f"      {entry['detail']}")
    emitter.line("")
    render_table(
        emitter,
        [
            ("scene id", scene_id),
            ("authority mode", compilation.mode),
            ("policy size", f"{compilation.size} characters"),
            (
                "runtime requirements",
                _requirements_summary(requirements.compatibility),
            ),
            (
                "output package",
                f"{compilation.output_package.name}@{compilation.output_package.bucket}",
            ),
        ],
    )
    emitter.line("")
    emitter.line("Checks marked trusted-bootstrap used this identity's read access, not")
    emitter.line("Scene credentials. No AssumeRole, model invocation, or write was performed,")
    emitter.line("and no byte of the sealed handler was executed.")
    emitter.line("Not proven by this preflight:")
    for condition in UNPROVEN_CONDITIONS:
        emitter.line(f"  - {condition}")
    emitter.document(payload)
    return EXIT_OK


def _receipt(
    *,
    scene_id: str,
    genesis: PinnedManifestURI,
    sealed: SealedSource,
    args: argparse.Namespace,
    invocation: BedrockInvocation,
    workflow_display: str | None,
    result: SceneRunResult,
) -> dict[str, Any]:
    publication = result.publication
    requirements = result.runtime_requirements
    if requirements is None:
        raise CliRefusal("Genesis Scene result omitted runtime-requirements preflight evidence")
    payload = envelope(SCENE_RUN_SCHEMA, publication.status)
    payload.update(
        {
            "scene_id": scene_id,
            "genesis": _genesis_document(genesis),
            "handler": sealed.report(),
            "runtime_requirements": requirements.report(),
            "authority": result.compilation.report(),
            "role_arn": args.role_arn,
            "model_arn": invocation.selected_model_id,
            "model": invocation.report(),
            "assumed_role_arn": result.assumed_role_arn,
            "output": {
                "package_name": result.output_package.name,
                "bucket": result.output_package.bucket,
                "top_hash": result.top_hash,
                "package_uri": result.package_uri,
                "revision_id": publication.revision_id,
                "revision_version_id": publication.revision_version_id,
                "is_latest": publication.is_latest,
                "reused_revision": publication.reused_revision,
                "previous_top_hash": publication.previous_top_hash,
                "latest_top_hash": publication.latest_top_hash,
                # Keys only: metadata values are never echoed back implicitly.
                "metadata_keys": list(publication.metadata_keys),
                "message_provided": bool(args.message),
                "workflow": workflow_display,
            },
            "timestamps": {
                "started_at": iso8601(result.started_at),
                "completed_at": iso8601(result.completed_at),
            },
        }
    )
    return payload


def _deferred_handler(sealed: SealedSource, emitter: Emitter) -> SceneHandler:
    """Return a handler that executes the sealed bytes at invocation time.

    Deferring execution is what orders the run correctly. The runner checks the
    genesis layout, compiles the session policy, mints the Scene credential, and
    materializes the verified inputs before it calls a handler, so nothing from
    the genesis package runs until every one of those checks has passed. An eager
    load would execute the module body first and make those checks unreachable.
    """

    def invoke(
        inputs: Mapping[str, Any], session: Any, context: SceneContext
    ) -> SceneHandlerResult:
        emitter.note(f"scene {context.scene_id}: executing {sealed.descriptor.logical_key}")
        try:
            return sealed.load().handler(inputs, session, context)
        except PearlsError:
            # Already a typed refusal, including the runner's own contract errors.
            raise
        except Exception as error:
            # Sealed code is arbitrary, so its failures are an expected outcome
            # rather than a defect in Pearls. Report one instead of a traceback.
            raise SceneHandlerError(
                f"sealed {sealed.descriptor.logical_key} raised {type(error).__name__}: {error}"
            ) from error

    return invoke


def run_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Execute exactly one Scene from its pinned genesis revision."""
    emitter = Emitter(runtime, args.json_output)
    scene_id, declaration, metadata, workflow, workflow_display = _validate_arguments(runtime, args)
    model_arn = declaration.selected_model_id
    genesis = parse_manifest_uri(args.genesis)

    session = build_session(runtime, args)
    invocation, profile_loader = _verify_profile_destinations(session, declaration)
    s3_client = session.client("s3")
    sts_client = session.client("sts")
    loader = _caching_loader(runtime.manifest_loader)

    sealed = runtime.verify_sealed_handler(
        args.genesis,
        manifest_loader=loader,
        genesis_s3_client=s3_client,
        handler_key=args.handler_key,
    )
    emitter.note(
        f"scene {scene_id}: verified sealed {args.handler_key} "
        f"(sha256 {sealed.source_sha256[:16]}...) from {genesis.top_hash}"
    )

    result = runtime.run_scene_from_genesis(
        args.genesis,
        role_arn=args.role_arn,
        scene_id=scene_id,
        handler=_deferred_handler(sealed, emitter),
        bedrock_model=model_arn,
        bedrock_invoke_resources=invocation.invoke_resources,
        bedrock_profile_loader=profile_loader,
        duration_seconds=args.duration_seconds,
        external_id=args.external_id,
        manifest_loader=loader,
        genesis_s3_client=s3_client,
        sts_client=sts_client,
        session_factory=runtime.worker_session_factory,
        clock=runtime.clock,
        output_meta=metadata or None,
        output_message=args.message,
        output_workflow=workflow,
    )

    runtime_requirements = result.runtime_requirements
    if runtime_requirements is None:
        raise CliRefusal("Genesis Scene result omitted runtime-requirements preflight evidence")
    payload = _receipt(
        scene_id=scene_id,
        genesis=genesis,
        sealed=sealed,
        args=args,
        invocation=invocation,
        workflow_display=workflow_display,
        result=result,
    )
    emitter.line(result.package_uri)
    render_table(
        emitter,
        [
            ("scene id", scene_id),
            ("assumed role", result.assumed_role_arn),
            ("publication", result.publication.status),
            (
                "runtime requirements",
                _requirements_summary(runtime_requirements.compatibility),
            ),
            ("revision id", result.publication.revision_id),
            ("is latest", str(result.publication.is_latest).lower()),
            ("output top hash", result.top_hash),
            ("started at", iso8601(result.started_at)),
            ("completed at", iso8601(result.completed_at)),
        ],
    )
    emitter.document(payload)
    return EXIT_OK
