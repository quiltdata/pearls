"""``pearls genesis`` — operator-side immutable genesis package authoring.

The command runs with the caller-selected boto3 identity. It never assumes the
Scene role, constructs a worker session, or changes infrastructure. Preparation
freezes and validates every byte before approval; publication delegates to the
existing versioned, revision-safe Quilt publisher.
"""

from __future__ import annotations

import argparse
from typing import Any

from ..authoring import (
    DEFAULT_GENESIS_WORKFLOW,
    GenesisAuthoringPlan,
    GenesisAuthoringResult,
    generate_genesis_revision_id,
)
from ..genesis import SEALED_HANDLER_KEY
from ..publication import validate_revision_id
from ._common import (
    EXIT_OK,
    GENESIS_AUTHOR_SCHEMA,
    CliRefusal,
    Emitter,
    Runtime,
    add_aws_arguments,
    add_confirmation_argument,
    build_session,
    confirm,
    envelope,
    read_json_argument,
)


def add_parser(subparsers: Any) -> None:
    """Register the operator-authority ``genesis author`` command."""
    genesis = subparsers.add_parser(
        "genesis",
        help="author immutable genesis packages with operator authority",
        description=(
            "Operator-side package authoring. Uses only the selected caller identity; "
            "it never assumes a Scene role or changes IAM."
        ),
    )
    verbs = genesis.add_subparsers(dest="verb", metavar="VERB", required=True)
    author = verbs.add_parser(
        "author",
        help="freeze, validate, and publish one local genesis directory",
        description=(
            "Recursively freeze a local genesis directory, materialize any pinned "
            "foreign inputs, validate the required named destination workflow, and "
            "publish one immutable revision. --dry-run performs every validation but "
            "writes nothing and does not prompt."
        ),
    )
    author.add_argument("directory", metavar="DIRECTORY", help="local genesis tree to freeze")
    author.add_argument(
        "--destination",
        required=True,
        metavar="NAME@BUCKET",
        help="destination Quilt package and versioned S3 registry",
    )
    author.add_argument(
        "--revision-id",
        metavar="ID",
        help=(
            "reusable 2-64 character publication identity; generated when omitted; "
            "save and reuse it after a lost response"
        ),
    )
    author.add_argument(
        "--metadata",
        required=True,
        metavar="JSON|@FILE",
        help="required package user_meta as inline JSON or @path to a JSON file",
    )
    author.add_argument(
        "--message",
        required=True,
        help="required nonblank package commit message",
    )
    author.add_argument(
        "--handler-key",
        default=SEALED_HANDLER_KEY,
        metavar="LOGICAL_KEY",
        help=f"sealed handler inside the authored genesis (default: {SEALED_HANDLER_KEY})",
    )
    author.add_argument(
        "--seal-input",
        action="append",
        default=[],
        metavar="URI",
        help=(
            "materialize one exact foreign object; repeat as needed. URI grammar: "
            "quilt+s3://REGISTRY#package=NAME&top_hash=64HEX&path=LOGICAL_KEY"
        ),
    )
    author.add_argument(
        "--workflow",
        default=DEFAULT_GENESIS_WORKFLOW,
        metavar="ID",
        help=(
            "required named destination workflow (default: genesis); there is no no-workflow escape"
        ),
    )
    author.add_argument(
        "--dry-run",
        action="store_true",
        help="perform all reads and validation, emit the plan, write nothing, and do not prompt",
    )
    add_confirmation_argument(author)
    add_aws_arguments(author)
    author.set_defaults(run=author_command)


def _output_document(
    plan: GenesisAuthoringPlan,
    result: GenesisAuthoringResult | None,
) -> dict[str, Any]:
    publication = result.publication if result is not None else None
    payload = envelope(
        GENESIS_AUTHOR_SCHEMA,
        publication.status if publication is not None else "validated",
    )
    payload.update(
        {
            "dry_run": result is None,
            "entries": [entry.report() for entry in plan.entries],
            "handler_key": plan.handler_key,
            "message_provided": True,
            "metadata_keys": list(plan.metadata_keys),
            "mutations_performed": (
                []
                if publication is None or publication.write_disposition == "none"
                else ["publish_package"]
            ),
            "planned_revision_id": plan.revision_id,
            "output": {
                "bucket": plan.output.bucket,
                "is_latest": publication.is_latest if publication is not None else None,
                "latest_top_hash": (
                    publication.latest_top_hash if publication is not None else None
                ),
                "manifest_version_id": (
                    publication.manifest_version_id if publication is not None else None
                ),
                "package_name": plan.output.name,
                "pinned_uri": result.package_uri if result is not None else None,
                "previous_top_hash": (
                    publication.previous_top_hash if publication is not None else None
                ),
                "reused_revision": (
                    publication.reused_revision if publication is not None else None
                ),
                "revision_id": publication.revision_id if publication is not None else None,
                "revision_version_id": (
                    publication.revision_version_id if publication is not None else None
                ),
                "top_hash": publication.top_hash if publication is not None else None,
            },
            "seal_provenance": [seal.report() for seal in plan.seals],
            "workflow": plan.workflow,
        }
    )
    return payload


def _write_review(emitter: Emitter, plan: GenesisAuthoringPlan) -> None:
    """Show approval material on stderr, preserving URI-first human stdout."""
    emitter.note("Prepared genesis publication for approval:")
    emitter.note(f"  destination: {plan.output.name}@{plan.output.bucket}")
    emitter.note(f"  revision id: {plan.revision_id}")
    emitter.note(f"  workflow: {plan.workflow}")
    emitter.note(f"  handler key: {plan.handler_key}")
    emitter.note(f"  entries: {len(plan.entries)} ({len(plan.seals)} sealed input(s))")
    emitter.note(f"  metadata keys: {', '.join(plan.metadata_keys) or '<none>'}")
    emitter.note("  message: provided (value not echoed)")


def _write_dry_run(emitter: Emitter, plan: GenesisAuthoringPlan) -> None:
    emitter.line("Validated genesis authoring plan; dry-run performed no writes.")
    emitter.line(f"destination  {plan.output.name}@{plan.output.bucket}")
    emitter.line(f"planned id  {plan.revision_id}")
    emitter.line(f"workflow     {plan.workflow}")
    emitter.line(f"handler key  {plan.handler_key}")
    emitter.line(f"entries      {len(plan.entries)}")
    emitter.line(f"sealed       {len(plan.seals)}")
    for entry in plan.entries:
        emitter.line(
            f"  {entry.logical_key}  {entry.size} bytes  sha256:{entry.sha256}  {entry.origin}"
        )


def author_command(runtime: Runtime, args: argparse.Namespace) -> int:
    """Prepare, optionally approve, and publish one immutable genesis package."""
    emitter = Emitter(runtime, args.json_output)
    metadata = read_json_argument(args.metadata, label="--metadata")
    if not args.message.strip():
        raise CliRefusal("--message must not be blank")
    if not args.workflow.strip():
        raise CliRefusal("--workflow must not be blank")
    if not args.handler_key:
        raise CliRefusal("--handler-key must not be empty")

    revision_id = (
        generate_genesis_revision_id(runtime.clock())
        if args.revision_id is None
        else validate_revision_id(args.revision_id)
    )
    session = build_session(runtime, args)
    s3_client = session.client("s3")
    plan = runtime.prepare_genesis_authoring(
        args.directory,
        destination=args.destination,
        user_meta=metadata,
        message=args.message,
        s3_client=s3_client,
        revision_id=revision_id,
        seal_inputs=tuple(args.seal_input),
        workflow=args.workflow,
        handler_key=args.handler_key,
    )

    if args.dry_run:
        _write_dry_run(emitter, plan)
        emitter.document(_output_document(plan, None))
        return EXIT_OK

    _write_review(emitter, plan)
    confirm(
        runtime,
        question=f"Publish immutable genesis revision to {plan.output.name}@{plan.output.bucket}?",
        assume_yes=args.assume_yes,
    )
    result = runtime.publish_genesis_authoring(
        plan,
        s3_client=s3_client,
        timestamp=runtime.clock(),
    )

    # Keep the canonical pinned URI as the first human stdout line. Approval
    # material and diagnostics were deliberately emitted on stderr.
    emitter.line(result.package_uri)
    emitter.line(f"publication  {result.publication.status}")
    emitter.line(f"revision id  {result.publication.revision_id}")
    emitter.line(f"is latest    {str(result.publication.is_latest).lower()}")
    emitter.line(f"top hash     {result.publication.top_hash}")
    emitter.document(_output_document(plan, result))
    return EXIT_OK
