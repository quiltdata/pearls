# Pearls

**PEARLS** — Parallel-Execution Agentic Reconciliation and Learning Systems

> Learn faster by running safer, at scale.

Pearls is an architecture for letting AI agents act autonomously over private
data without surrendering control, provenance, or context to model vendors.
It resolves the usual tradeoff between centralized policy (safe but slow) and
local agent autonomy (fast but unauditable) by fixing the *execution context*
of every agent run and reconciling the results afterward.

## Architecture in one sentence

Fix the execution context in a versioned manifest, compile least authority
into a **TAJ**, isolate agent writes to descendant Packages, and reconcile
the resulting evidence through a branchable **Pearl** history.

## Core ideas

- **Scene** — the fixed context and capability frame for one agent
  execution, with four roles: Input Control, Input Content, Output Content,
  Output Control. Inputs are pinned and read-only; outputs are bounded and
  append-only. An agent cannot expand its own authority mid-run.
- **Pearl** — a continuing, versioned lineage of related Scenes: branch
  history, accepted control revisions, evaluations, and reconciliation
  decisions.
- **Package revision** — a sealed Scene manifest plus the immutable content
  it references. A Quilt Package manifest acts as a capability-addressed
  routing table from stable logical keys to exact S3 object versions.
- **TAJ (Translated Access JWT)** — a signed capability over a pinned
  manifest and a logical scope, minted once by **RAJA** (the authorization
  compiler) and enforced at runtime by **RAJEE** (an Envoy-based data-plane
  gateway) — no policy database in the data path.
- **Reconciliation** — TerminusDB versions the active graph of Scenes and
  Pearl lineages; independent branches merge structurally, then get
  checked against domain invariants before becoming operative.

Learning is separated from mutation: an agent's Output Control (proposed
steering, exceptions, evaluations) never changes its own authority. It
becomes active only through a separately authorized promotion into a later
Scene's Input Control.

## Why it matters

Safer isolation permits more parallel executions. More executions produce
more real-world evidence. Reconciliation converts that evidence into
reusable control. Better control makes later executions safer, cheaper,
and more autonomous — a compounding-autonomy flywheel instead of a
security/velocity tradeoff.

The same bounded-execution record also supports **Phase IV monitoring**:
continuous, post-deployment evaluation of agents, analogous to clinical
postmarketing surveillance.

## Reference stack (AWS)

| Plane | Components |
| --- | --- |
| Identity & policy | OIDC/IAM, Amazon Verified Permissions, KMS/Secrets Manager, CloudTrail/CloudWatch |
| Manifest & coordination | TerminusDB, Scene sealer, RAJA/RALE |
| Execution | EventBridge + SQS, Step Functions, Lambda/ECS/Batch, model adapters (Bedrock, local, external) |
| Data plane | RAJEE/Envoy, pinned manifest cache, Amazon S3 (versioned) |

Durable context, policy, evidence, and Package history stay in the
customer's own AWS account. Model providers are replaceable compute:
**models can be rented; context must be owned.**

## Document status

The Pearls architecture distinguishes three levels of maturity:

1. **Available primitives** — existing AWS services and Quilt Package
   semantics.
2. **Reference integration** — the proposed composition of those
   primitives into Scene-based execution isolation.
3. **Validation frontier** — mechanisms that still need implementation and
   stress testing (deterministic Scene sealing and cross-revision,
   domain-aware reconciliation).

Release 0.1.1 adds an alpha maturity-level 1→2 authority-compilation
primitive: `raja_sts` compiles a pinned Quilt Package scope into an AWS STS
session policy and temporary credentials. It is not a claim that the complete
Pearls architecture is production-ready.

## AWS-native TAJ (`raja_sts`)

`raja_sts` is a small Python 3.11+ compiler for minimum-viable Scene
isolation. The trusted orchestrator loads one immutable Quilt manifest,
resolves a sealed logical scope, and passes the resulting minified policy to
`sts:AssumeRole`. AWS evaluates the temporary credential as the intersection
of the administrator-created base role and this session policy. There is no
runtime gateway or policy database.

This AWS-native TAJ is an STS role session, not a literal JWT. Its policy
travels with the temporary credential and its `RoleSessionName` is the Scene
ID used for audit correlation.

### Install and test

```console
python -m pip install .

# Development checks
python -m pip install ".[dev]"
ruff format --check .
ruff check .
mypy
cfn-lint infra/*.yaml
pytest
python -m build
```

Direct runtime and development dependencies are pinned in `pyproject.toml`;
transitive resolution remains the installer/index's responsibility. Normal CI
runs only offline tests and never assumes a role or mutates AWS resources.

### Pinned manifest and scope contract

A manifest URI must name an S3-backed Quilt registry, package, and complete
64-character top hash:

```text
quilt+s3://manifest-bucket#package=project%2Fsealed-scene&top_hash=<64 hex characters>
```

The package name is URL encoded in the fragment. Floating package pointers,
hash prefixes, local files, wildcard-bearing S3 keys, and unpinned manifests
are rejected. The trusted compiler identity needs permission to resolve the
manifest. The resulting worker credential can read only that package revision's
exact `.quilt/packages/<top-hash>` manifest object from the input registry; it
does not receive floating-pointer or sibling-manifest access.

A scope is a mapping like [`examples/scope.json`](examples/scope.json):

```json
{
  "readable": ["input-control/worker.json", "input-content/"],
  "writable_package": "scenes/example/output@my-project-bucket",
  "require_version_ids": true,
  "allow_prefix_fallback": false
}
```

A `readable` value selects an exact logical key when one exists. Otherwise it
selects the logical directory of that name; a trailing slash makes directory
intent explicit. Every selector must match at least one object. Overlapping
selectors are de-duplicated deterministically. The output data prefix is
`<package-name>/` in the named bucket.

`require_version_ids` fails compilation if any selected object lacks an S3
version ID. Without it, those entries receive exact-key `s3:GetObject`
permission and the compilation report warns that IAM does not pin their
bytes. The runtime must then verify the Quilt content hash after download.

### Compile and mint

```python
from raja_sts import compile, mint

manifest_uri = "quilt+s3://manifest-bucket#package=project%2Fsealed-scene&top_hash=" + "a" * 64
scope = {
    "readable": ["input-control/worker.json", "input-content/"],
    "writable_package": "scenes/run-123/output@my-project-bucket",
    "require_version_ids": True,
}

compiled = compile(manifest_uri, scope)
print(compiled.report())  # safe authority metadata; no credentials

credentials = mint(
    manifest_uri,
    scope,
    role_arn="arn:aws:iam::123456789012:role/scene-execution",
    scene_id="scene-run-123",
)

# Pass only these temporary values to the isolated worker.
worker_session_kwargs = credentials.boto3_kwargs()
```

`CompiledPolicy.policy_json` is the exact inline policy sent to STS.
`CompiledPolicy.report()` records the mode, selected logical and physical
objects, output package, policy size, and any degraded guarantees. Secret
credential fields are excluded from `repr`, but remain bearer secrets and
must never be logged or committed.

### Run one bounded Scene

A sealed runner scope selects one exact Bedrock foundation-model or account-scoped
inference-profile ARN. The selected ARN is the `modelId` passed to Bedrock; a
separate exact `bedrock_invoke_resources` closure records every IAM resource
needed for that invocation. A direct foundation model derives its one-resource
closure automatically. An inference profile must explicitly include its own ARN
and every foundation-model ARN returned for that profile, including the
regionless support ARN used by a global profile. The public runner compiles and
mints once, reads and verifies only selected object versions,
passes one explicit scoped session to the handler, and publishes a Quilt output
Package without listing the bucket:

```python
from raja_sts import SceneHandlerResult, run_scene


def handler(inputs, session, context):
    prompt = inputs["input-content/prompt.txt"].content.decode()
    response = session.client("bedrock-runtime").converse(
        modelId=context.bedrock_model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    return SceneHandlerResult(
        objects={"result/response.txt": response["output"]["message"]["content"][0]["text"]},
        invocation={"request_id": response["ResponseMetadata"]["RequestId"]},
        output_meta={"stop_reason": response["stopReason"]},
    )


result = run_scene(
    manifest_uri=manifest_uri,
    scope_spec={
        **scope,
        "bedrock_model": "arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",
    },
    role_arn="arn:aws:iam::123456789012:role/scene-execution",
    scene_id="scene-run-123",
    handler=handler,
)
print(result.package_uri)
```

For an inference profile the routing closure is explicit and fail-closed. The
caller declares the expected resources, then supplies a loader backed by
`GetInferenceProfile` in the selected source Region:

```python
import boto3

from raja_sts import load_bedrock_profile_resources

profile_arn = (
    "arn:aws:bedrock:us-east-1:123456789012:"
    "inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0"
)
profile_scope = {
    **scope,
    "bedrock_model": profile_arn,
    "bedrock_invoke_resources": [
        profile_arn,
        "arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
        "arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0",
    ],
}
bedrock_control = boto3.client("bedrock", region_name="us-east-1")
result = run_scene(
    # ...the same arguments as above...
    scope_spec=profile_scope,
    bedrock_profile_loader=lambda arn: load_bedrock_profile_resources(bedrock_control, arn),
)
```

The compiler rejects unrelated resources, sorts the closure, and requires the
declared set to exactly match `GetInferenceProfile.models`; missing, stale, or
extra destinations fail before STS minting. The selected profile Region
determines the Bedrock Runtime client Region and the selected profile ARN remains
the reported `modelId`. Supporting ARNs are reported separately as IAM authority
and are conditioned on the exact selected profile with
`bedrock:InferenceProfileArn`, so they cannot be invoked directly. This follows
AWS's [inference-profile IAM guidance](https://docs.aws.amazon.com/bedrock/latest/userguide/inference-profiles-prereq.html).
Runner-generated policies omit the pinned manifest object's read permission after
the trusted compiler has resolved it; handlers receive materialized, hash-checked
inputs and never reread that manifest. The standalone `compile()`/`mint()` default
retains the manifest read for backward compatibility. This conservative narrowing
leaves enough of the STS plaintext budget for exact profile closures.

Handlers return bytes or strings, JSON-serializable invocation evidence, and
optional run-derived `output_meta`. Caller metadata and handler metadata are
secret-screened, finite-JSON validated, and merged only when their top-level
keys are disjoint; a collision is a `HandlerContractError`, never an implicit
winner. The merged document is what workflow schemas and domain invariants
validate and what the manifest records. The package always includes handler
objects, `run/scene.json`, and `run/invocation.json`. See
[`examples/hello_scene.py`](examples/hello_scene.py) for a thin one-Converse
consumer. Prompt and response bodies remain explicit package objects rather
than implicit metadata.

A caller can compose content-aware checks without adding domain vocabulary to
`raja_sts`. Each checker has a stable identifier and returns all findings it can
establish from the final single-revision material:

```python
from raja_sts import PublicationContext


class RequiredAssessment:
    identifier = "trial-assessment/v1"

    def check(self, pending: PublicationContext, /) -> tuple[str, ...]:
        findings = []
        if "assessment/verdict.json" not in pending.objects:
            findings.append("assessment/verdict.json is required")
        if "pass_count" not in pending.user_meta:
            findings.append("pass_count metadata is required")
        return tuple(findings)


result = run_scene(
    # ...the same arguments as above...
    output_invariants=(RequiredAssessment(),),
)
```

Workflow validation runs first, then all invariant checkers run over detached,
read-only views of the complete object set, merged metadata, message, and output
package name. Findings—including a checker that raises unexpectedly—are
aggregated in `DomainInvariantError`; either refusal occurs before any write.
The hook is intentionally single-revision: checks needing a prior manifest or
lineage state require separately authorized resolution outside this interface.

`scene_id` is also the collision-safe publication revision identity. A retry of
`publish_package` with the same identity and byte-for-byte intent reuses the
recorded revision without multiplying writes; different intent under that
identity raises `PublicationConflictError`. A matching revision that is already
`latest`, or that has been superseded by a newer `latest`, returns its durable
status without consulting a changed or deleted current workflow because no write
is possible. An interrupted retry whose original base was absent and remains
absent revalidates the workflow and domain invariants before a create-only
`latest` write. A retry whose recorded base was a present S3 VersionId does not
replay that transition: S3 destination writes cannot be conditioned on VersionId,
so it fails closed as the successful durable status `published-not-latest` rather
than risking an ETag ABA rollback. A deliberate re-execution uses a new Scene ID
and appends another revision, even in the same second. Publication writes data,
the immutable manifest, and a create-only revision pointer before conditionally
advancing `latest`: first publication uses `If-None-Match`, later ones use
`If-Match` on the observed ETag. A CAS loser still returns a durable
`PublicationResult` with `status="published-not-latest"`; it is not reported as
a total failure. The revision records its original CAS base ETag and S3 VersionId
so a retry can reject a restored, newer `latest` version even when its body and
ETag match the old base. After a conditional conflict the publisher rereads
`latest`, so another retry that advanced the same top hash is reported
accurately. `PublicationResult.write_disposition` separately records whether
this invocation performed no successful write, only recovered `latest`, or
entered the package-write path; durable status and invocation-local mutation are
intentionally distinct. `SceneRunResult.top_hash` remains a compatibility property
over `SceneRunResult.publication.top_hash`.

### Boot from one genesis package

`run_scene_from_genesis()` makes one pinned package the complete Scene brief.
The package must contain UTF-8 `README.md` and `outputs.json` controls, the fixed
`protocol/requirements.json` runtime declaration, and at least one entry below
`input/`. The handler receives only the `input/` entries in its `inputs` mapping
and reads the control prompt from `context.control_prompt`:

```json
{
  "quilt+s3://my-project-bucket/results/#package=scenes%2Frun-123%2Foutput": "result tree",
  "quilt+s3://my-project-bucket/summary.json#package=scenes%2Frun-123%2Foutput": "exact summary"
}
```

An `outputs.json` key is an unpinned output destination, not input authority.
No URI path grants the whole package, a trailing `/` grants a logical subtree,
and any other path grants one exact logical object. All entries must target one
package and must not overlap. Exact-object and subtree selectors may not cover
`run/scene.json` or `run/invocation.json`; those runner-owned keys are always
rejected from returned handler objects, including under a whole-package grant.

The requirements control uses one strict versioned shape:

```json
{
  "$schema": "https://quiltdata.com/schemas/raja-sts/handler-runtime-requirements/v1",
  "python": ">=3.11,<3.14",
  "imports": [
    {"module": "PIL", "requirement": "Pillow>=10,<11"},
    {"module": "packaging", "requirement": "packaging==26.3; python_version >= '3.11'"}
  ]
}
```

`python` is a nonblank PEP 440 specifier. Each `imports` entry maps one unique
top-level Python import identifier to a PEP 508 distribution requirement, so
distribution and module names may differ, as with `Pillow` and `PIL`. Direct
URLs and extras are refused because preflight installs nothing; environment
markers are evaluated only in ordinary requirement context, and a context-only
marker such as `extra` is a typed refusal rather than a silent omission. An
explicit empty `imports` array is valid, but the control itself is mandatory.
The v1 protocol bounds the UTF-8 source at 65,536 bytes and 128 imports;
`python`, `module`, and `requirement` are limited to 256, 128, and 1,024
characters respectively. Installed-version evidence is limited to 256
characters, and a refusal reports at most 20 findings of 512 characters each
with an omitted-count suffix. Authoring and runtime use the same limits, while
the S3 reader stops after 65,537 bytes instead of buffering an oversized
control.

At execution preflight, the current Python version must match, every applicable
distribution must be installed at a matching version, and each declared module
must be resolvable. Resolution uses `find_spec()` rather than importing the
module body. This proves neither that the named distribution supplied the
resolved module nor that import-time Python/native initialization, transitive
requirements, or the declaration's completeness will succeed. Pearls does not
install or resolve dependencies, validate ABIs, lock the environment, sandbox
code, or claim reproducibility.

```python
from raja_sts import SceneHandlerResult, run_scene_from_genesis


def genesis_handler(inputs, session, context):
    prompt = context.control_prompt
    assert prompt is not None
    return SceneHandlerResult(
        objects={"results/answer.txt": prompt},
        invocation={"input_count": len(inputs)},
    )


result = run_scene_from_genesis(
    manifest_uri,
    role_arn="arn:aws:iam::123456789012:role/scene-execution",
    scene_id="scene-run-123",
    handler=genesis_handler,
    bedrock_model="arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0",
)
```

The selected `bedrock_model` and optional `bedrock_invoke_resources` are
deliberately supplied by the execution plane and recorded separately in the
authority report; they are not derived from the sealed genesis package. A direct
foundation model needs no explicit resource list. An inference profile requires
the same complete exact closure shown above. The trusted bootstrap identity
resolves the pinned manifest and reads `README.md`, `outputs.json`, and
`protocol/requirements.json` by exact S3 version, verifies their Quilt hashes,
and verifies local runtime compatibility before policy compilation or STS
minting. `scene check`, `scene run`, and programmatic
`run_scene_from_genesis()` share this preflight. Existing immutable Genesis
packages without the requirements control fail closed and must be republished
as a new pinned revision with an explicit declaration.

Handlers run in-process as trusted execution code: they receive the scoped
session that publication later uses, so selector and reserved-key checks
constrain returned objects but are not a sandbox against a handler that makes
direct S3 calls or retains the session. The lower-level
`run_scene(manifest_uri, scope_spec, ...)` API remains available without a
Genesis requirements declaration; its `SceneRunResult.runtime_requirements` is
therefore `None`.

### Command line

Installing the package creates one `pearls` command with three authority groups:

```text
pearls
  genesis author DIRECTORY                                  operator package authority
  scene   check | run                                       bounded Scene authority
  infra   validate | plan | deploy | status | assume-policy  administrator IAM
```

The split is deliberate. `genesis` uses the selected operator identity to author
immutable packages, `scene` operates with bounded short-lived authority, and
`infra` mutates persistent IAM with administrator authority. Shared conventions:

| Concern | Convention |
| --- | --- |
| Credentials | Selected with `--profile` / `--region` and resolved by boto3; no command accepts key material or tokens |
| Output | Human-readable on stdout by default, `--json` for a stable versioned document |
| Confirmation | Read-only and `--dry-run` operations never prompt; mutating operator/admin verbs require confirmation or `--yes` |
| Exit codes | `0` success, `1` typed refusal or failure, `2` usage error |
| Safety | Receipts omit credentials, object bodies, metadata values, and tokens |

#### Author a genesis package

`pearls genesis author` freezes one local tree and publishes it with the
operator's boto3 identity. A valid tree contains UTF-8 `README.md` and
`outputs.json` controls, a strict UTF-8 `protocol/requirements.json`, a selected
Python handler that parses and statically binds module-level `handler`, and at
least one regular file below `input/`. Authoring validates the requirements
schema and PEP 440/508 syntax but deliberately does not require the declared
interpreter, distributions, or modules on the operator's machine; authoring and
execution environments may differ. The selected handler defaults to
`protocol/handler.py`; `--handler-key LOGICAL_KEY` chooses another safe relative
logical key, which must also be supplied to later `pearls scene` commands. The
requirements key remains fixed when a custom handler key is selected.
Traversal is recursive and sorted by relative POSIX logical key. Every regular
file is read once into an immutable plan; symlinks, devices/FIFOs, unreadable
files or directories, unsafe relative paths, and logical-key collisions are
refused. Traversal pins root and child directory descriptors so concurrent
symlink swaps cannot escape the selected tree. For each regular file, device,
inode, mode, size, modification time, and change time must remain identical at
discovery, immediately after open, and after the complete read while its
descriptor remains open. Authoring fails closed on platforms without
descriptor-relative filesystem APIs.

```console
pearls genesis author ./trial001-genesis \
  --destination proj/trial001-worker@quilt-dev \
  --revision-id genesis-trial001-v1 \
  --metadata @genesis-meta.json \
  --message "Author Trial 001 worker genesis" \
  --seal-input 'quilt+s3://source-registry#package=reference%2Frubric&top_hash=<64 hex characters>&path=rubrics%2Ftrial001.json' \
  --yes --json
```

`--destination NAME@BUCKET`, `--metadata`, and a nonblank `--message` are
required. Metadata uses the same inline JSON or `@file` convention as Scene
publication. `--revision-id ID` optionally supplies the reusable publication
identity using the same 2–64 safe-character grammar as Scene publication;
`latest` is reserved. When omitted, a collision-resistant ID is generated and
shown in approval material before the first write. Operators should save that
ID, or explicitly supply one, so a command restarted after a lost response can
assert the same idempotent intent instead of creating a second revision. Reusing
an ID with different bytes or declarations is refused. The declaration bound to
the create-only revision includes the selected handler key and canonical exact
seal provenance, so equal copied bytes cannot relabel an old revision with a
different source package, physical VersionId, or handler selection. The destination workflow
is the required named workflow `genesis` by default; `--workflow ID` selects
another named workflow, and there is deliberately no no-workflow escape for
authoring.

`--seal-input` is repeatable and has a dedicated strict grammar:

```text
quilt+s3://REGISTRY#package=NAME&top_hash=64HEX&path=LOGICAL_KEY
```

The package name and logical key are fragment values and should be percent
encoded. Raw leading or trailing URI whitespace is refused; a legitimate space
inside a logical key remains valid when encoded as `%20`. Floating references,
hash prefixes, duplicate/extra fields, empty or directory-like paths, malformed
encoding, duplicate sources, and destination collisions are refused. A source key `X/Y` becomes `input/X/Y`. The operator
client reads `.quilt/packages/<top_hash>`, parses it locally without registry
telemetry, recomputes the Quilt top hash, then reads the selected object by its
exact S3 `VersionId` and verifies its declared Quilt content hash. The bytes are
copied into the new package; a foreign physical S3 reference is never retained.

`--dry-run` performs the complete local layout, requirements syntax,
selected-handler, sealed-source, destination versioning, and workflow
metadata/entries/message/handle validation. It does not test local runtime
compatibility. It performs zero writes, never
prompts, and reports a non-durable `planned_revision_id` that can be saved for a
later real invocation. The durable revision ID, S3 version identities, top hash,
and pinned URI remain null rather than being invented. A real publication
prompts only after that preparation unless `--yes` is present; approval material
always includes the planned revision ID before any write. Publication then uses
the exact frozen bytes via the same revision-safe publisher used by Scenes. New
publications recheck versioning and the named workflow immediately before their
first write. A durable, same-intent retry that needs no write returns its stored
disposition even if the current workflow changed. An interrupted revision whose
original `latest` base was absent revalidates before a create-only recovery; one
whose base was present remains durable but does not replay an ETag-only transition
that cannot be conditioned on the recorded VersionId. A durable CAS loser is
still exit 0 with status `published-not-latest`. In JSON receipts,
`mutations_performed` lists `publish_package` only when this invocation completed
a package-path or `latest` write; completed and superseded status-only retries
report an empty list. Human success prints the canonical, round-trippable pinned
URI first; package-name slashes are percent encoded.

The authoring identity needs local read access; `s3:GetBucketVersioning` on the
destination; `s3:GetObject` for current destination workflow/config/schema and
package pointers; `s3:GetObjectVersion` for any workflow schema URL carrying a
`versionId` at its configured bucket/key; `s3:PutObject` for `<name>/*`,
`.quilt/packages/*`, and `.quilt/named_packages/<name>/*`; and, when sealing,
`s3:GetObject` for the foreign manifest plus `s3:GetObjectVersion` for selected
data. It needs no STS, Scene role, worker session, IAM, CloudFormation, bucket
listing, delete, or foreign write permission. Authoring validates code identity and static shape,
not handler safety: it never executes `protocol/handler.py`. Bucket Object Lock,
bucket policies, lifecycle rules, and cleanup of unreferenced versions after a
mid-publication failure remain operator responsibilities.

A sealed-genesis Scene carries its executable protocol at
`protocol/handler.py`, alongside the `README.md` control prompt, `outputs.json`
write grant, fixed `protocol/requirements.json`, and `input/` entries. `pearls
scene` reads the handler and requirements from the requested pinned revision by
exact S3 version and verifies their bytes against the Quilt content hashes in
the manifest.

Verifying and executing are deliberately separate steps, because a Python module
body runs as soon as it is imported. `scene check` verifies handler identity and
binding statically and uses metadata plus `find_spec()` for applicable runtime
requirements; it imports neither the handler nor a declared module body. `scene
run` performs the same shared requirements preflight before `AssumeRole`, then
executes the handler only after the policy is compiled, the Scene credential is
minted, and verified inputs are materialized. Thus malformed or incompatible
requirements spend no Scene authority and run no sealed code.

`scene check` is therefore a read-only preflight. It validates every argument
shape, resolves and hash-verifies the genesis controls and sealed handler,
verifies local declared runtime compatibility, compiles the session policy, and
prints the non-secret authority report. It performs no `AssumeRole`, no model
invocation, no write, and no handler or dependency module-body execution, so a
nonzero exit always precedes all of those:

```console
pearls scene check \
  --genesis 'quilt+s3://quilt-dev#package=proj%2Ftrial001-worker&top_hash=<64 hex characters>' \
  --role-arn arn:aws:iam::123456789012:role/scene-execution-haiku \
  --model-arn arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0
```

For an inference profile, `--model-arn` remains the one selected `modelId` and
`--model-resource-arn` is repeated for every foundation model in the expected IAM
closure (including a global profile's regionless support ARN). The bootstrap
identity calls `GetInferenceProfile` in the selected source Region and refuses
before minting unless the returned destinations match exactly:

```console
pearls scene check \
  --genesis 'quilt+s3://quilt-dev#package=proj%2Ftrial001-worker&top_hash=<64 hex characters>' \
  --role-arn arn:aws:iam::123456789012:role/scene-execution \
  --model-arn arn:aws:bedrock:us-east-1:123456789012:inference-profile/us.anthropic.claude-3-5-sonnet-20241022-v2:0 \
  --model-resource-arn arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0 \
  --model-resource-arn arn:aws:bedrock:us-west-2::foundation-model/anthropic.claude-3-5-sonnet-20241022-v2:0
```

Each reported check names the identity it needed. Checks labeled
`trusted-bootstrap` used the operator's own read access, because the genesis
controls must be read before a Scene credential can exist. The command also
prints what it cannot prove: Bedrock account model access, the base role's own
permissions, the output registry's workflow contract, handler safety, whether
the independently found module is supplied by the named distribution, or
whether import-time/native initialization and undeclared transitive dependencies
will succeed. Static handler binding also does not prove that the eventual value
is callable.

`scene run` executes one Scene. It calls `run_scene_from_genesis()` exactly once
and adds no authority, materialization, workflow, or publication logic of its
own:

```console
pearls scene run \
  --genesis 'quilt+s3://quilt-dev#package=proj%2Ftrial001-worker&top_hash=<64 hex characters>' \
  --role-arn arn:aws:iam::123456789012:role/scene-execution-haiku \
  --model-arn arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0 \
  --metadata @output-meta.json \
  --message "Trial 001 worker Scene run" \
  --json
```

`--scene-id` is accepted; when omitted a collision-resistant valid
`RoleSessionName` is generated. It is also the output revision identity, so a
new execution needs a new ID and reuse is an explicit idempotency assertion.
`--metadata` takes inline JSON or `@path`, and maps to the workflow-aware caller
half of `output_meta`; `--message` and `--workflow` / `--no-workflow` map to
`output_message` and `output_workflow`. The `scene check` receipt schema is v2
and the run receipt schema is v3. Both include exact requirements-control
bucket/key/VersionId, Quilt hash, source SHA-256/size, the actual and required
Python versions, and bounded per-import evidence. Each import records marker
applicability, distribution-version status, and module-resolution status as
separate fields; marker-skipped entries are `not-checked`, not “compatible.” A
summary reports declared, applicable, and skipped counts, and both receipt types
carry explicit limitations including the unproven distribution-to-module
association. The run receipt otherwise contains only non-secret identity: Scene
ID, genesis URI/top hash, verified handler provenance, assumed-role ARN, immutable package URI/top
hash, revision and S3 version identities, whether the revision was reused,
`latest` disposition, and timestamps. Its top-level status is `published` or
`published-not-latest`; either is exit 0 because the revision is durable.
Metadata from both caller and handler is reported by key only. `scene check` cannot know handler-derived keys
because it never executes the handler, and the CLI does not dynamically load
programmatic `output_invariants`. Diagnostics go to stderr so `--json` stdout
stays parseable.

Two boundaries survive the CLI unchanged. The **trusted bootstrap identity**
reads the pinned manifest and the sealed control objects by exact version before
minting anything; `--profile` and `--region` apply to those reads and to
`AssumeRole`, never to the Scene session, which is built only from the minted
credential and the selected model/profile ARN's region. The **trusted-handler**
boundary is unchanged too: verifying the handler's hash proves code identity,
not code safety. During a run the handler executes in-process and receives the scoped
session that publication later uses. Ordering execution after the authority
checks limits *when* sealed code runs, not what it can do once it runs. The
command claims no sandboxing and no ambient credential isolation.

### Mapping to the four Scene roles

| Scene role | AWS-native enforcement |
| --- | --- |
| Input Control | Only selected control logical keys resolve to versioned read resources. Sibling control files are absent from the allow set. |
| Input Content | Selected content keys receive exact S3 object ARNs. One set-based deny admits only the selected `s3:VersionId` values, and S3 rejects a selected version ID when requested against a different object key. |
| Output Content | `s3:PutObject` is limited to `<package-name>/*` in the declared output bucket. Quilt consumer validation may read `.quilt/workflows/*` in that bucket. |
| Output Control | Quilt manifest blobs may be written under `.quilt/packages/*`; named-package writes are limited to `.quilt/named_packages/<package-name>/*`, with exact reads of that package's `latest` pointer and the current Scene ID's revision pointer for CAS and idempotency. Sibling revision pointers remain unreadable. |

Every session policy explicitly denies deletes and bucket listing on all
resources. Action-specific `NotResource` guardrails prevent bucket policies
from restoring out-of-scope current-object reads or writes. To stay within the
STS plaintext budget, current-object `GetObject` and output `PutObject` use
compact wildcard allows paired with those exact denies; IAM deny precedence
keeps their effective resource sets equal to the enumerated boundaries. Exact
version mode uses one global mismatch deny containing the sealed set of S3
version IDs and one resource-set allow containing the selected object ARNs. Because S3 version
IDs uniquely identify stored object versions, using an admitted ID against a
different key does not return that key. The policy grants no IAM, STS, ACL,
bucket-policy, or input-write action.

The compiler deliberately does **not** add a blanket Bedrock `Deny` with
`NotResource` set only to the selected ARN: that would also deny the supporting
foundation resources Bedrock evaluates for a profile. Instead it emits one exact
allow for the selected profile and a separate support-resource allow conditioned
with `StringLike` on `bedrock:InferenceProfileArn`. The value is an exact ARN
with no wildcard, so a handler cannot invoke a support model directly. The
declared closure must match the source Region's
`GetInferenceProfile` response exactly, and every statement counts toward the
2,048-character STS limit; a closure that cannot fit is refused before minting.
The base-role/session-policy intersection excludes every resource outside the
operator inventory and this verified per-Scene closure.

Because a session policy can only narrow its base role, the agent cannot add
authority during the session. Promotion of output into a later Scene's inputs
remains separately authorized.

### Workflow contracts on output registries

Governed buckets register Quilt workflows (`.quilt/workflows/config.yml`) and
can require every package to name one and satisfy its JSON Schemas. Publication
honors that contract with quilt3 semantics, using only the caller-supplied S3
client (a scoped Scene session validates against exactly the registry its
credentials can see):

- `workflow=...` (default) resolves the registry's `default_workflow` and
  respects `is_workflow_required` (true by default when a config exists);
  `workflow=None` opts out explicitly and is refused when required; a named
  workflow must exist in the config.
- The caller and handler metadata maps are merged with collision refusal;
  that final package `user_meta` is validated against the workflow's
  `metadata_schema`, the entry list against its `entries_schema`, the commit
  `message` against `is_message_required`, and the package name against
  `handle_pattern`.
- Workflow validation happens first, then every caller-supplied
  `PublicationInvariant` checks the same final metadata plus object bytes,
  logical keys, message, and package name. Workflow refusal raises
  `WorkflowContractError`; invariant findings are aggregated in
  `DomainInvariantError`. Both leave nothing behind because they run before the
  first write.
- A conforming publication stamps `{id, config, schemas}` (S3 URIs with
  version IDs) into the manifest header exactly as quilt3 writes it, so
  catalogs and `quilt3` read the package as having passed the contract it
  actually passed.
- Schema documents must be reachable by the publishing credentials; under a
  Scene session that means the output registry's own `.quilt/workflows/*`
  prefix (already part of the compiled read scope). `$ref` is rejected and
  only draft-07 meta-schemas are supported, for parity with quilt3's
  validator.
- Contract objects must themselves be **versioned**: a config or schema
  without an S3 version ID (written before versioning was enabled, or while
  suspended) is refused, because the stamp could then only reference mutable
  latest content instead of the exact contract that was validated. This is
  stricter than quilt3, in the refuse direction only.

`run_scene` and `run_scene_from_genesis` expose these as `output_meta`,
`output_message`, `output_workflow`, and `output_invariants`.

### Policy-size behavior

STS limits an inline session policy to 2,048 plaintext characters. Exact mode
uses set-based version-ID and resource lists rather than two statements per
object, then minifies and measures the JSON before calling AWS. Inference-profile
ARNs and regional support resources are part of that same measured document;
they never bypass the limit. The five-object foundation-model Scene regression
compiles below the hard limit with all required Quilt metadata reads and remains
version-pinned. The compiler fails closed with `PolicySizeError` when a larger
policy does not fit.

A sealed scope may explicitly set `allow_prefix_fallback` to `true`. The
compiler then emits common physical S3-prefix resources and reports
`mode="per-prefix"`, every widened resource, and loss of version enforcement.
This fallback can expose objects that are not in the selected manifest; a root
or `.quilt/`-overlapping widened prefix can also expose sibling Quilt control
objects. These losses are listed in the compilation report and the fallback is
never automatic. Exact metadata-boundary claims apply only outside
`per-prefix` mode. Splitting work into smaller Scenes is the safer response.

### Base role

[`infra/scene-execution-role.yaml`](infra/scene-execution-role.yaml) is an
administrator-applied CloudFormation template. It creates no buckets and the
Python module never creates IAM resources. The role is broad for reads and
writes within a controlled project-bucket ARN pattern so the session policy
provides the per-Scene bound, but it deliberately has no object or version
delete permission. A caller that assumes the role without RAJA's session
policy therefore cannot turn the append-only execution role into cleanup
authority.

An administrator can deploy it with an account-specific principal and bucket
pattern:

```console
aws cloudformation deploy \
  --template-file infra/scene-execution-role.yaml \
  --stack-name pearls-scene-execution \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    OrchestratorPrincipalArn=arn:aws:iam::123456789012:role/orchestrator \
    ProjectBucketArnPattern=arn:aws:s3:::my-project-* \
    BedrockInvokeModelArns=arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0,arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0
```

`BedrockInvokeModelArns` is a required
[`CommaDelimitedList`](https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/parameters-section-structure.html).
Each element must
be an exact commercial-partition foundation-model or account-scoped
inference-profile ARN; wildcards, custom models, other partitions, and empty
entries are rejected. A profile inventory must also enumerate every exact
regional foundation-model ARN that Bedrock can route it to. The base list is an
operator inventory, not a Scene selection: each sealed Scene still selects one
model/profile and the effective permission is the intersection of this inventory
with that Scene's exact session policy. A missing inventory member therefore
fails closed.

The orchestrator needs `sts:AssumeRole` permission as
well as the role's trust. The template targets the commercial `aws` partition
in 0.1.1. `mint(external_id=...)` is available for separately managed roles
whose trust policy requires `sts:ExternalId`; the shipped same-account template
does not require one. The worker must not be able to retrieve broader ambient
task, instance, or execution-role credentials; otherwise it can bypass the
scoped credentials entirely.

### Deploying execution roles with `pearls infra`

The same template ships inside the wheel, so an installed Pearls can deploy it
without hand-assembling the parameters, remembering the named-IAM capability, or
keeping the stack name and `RoleName` aligned. A byte-identity test keeps the
packaged copy and
[`infra/scene-execution-role.yaml`](infra/scene-execution-role.yaml) the same
file, so the installed CLI cannot deploy a role that differs from the one
reviewed here.

`pearls infra validate` is fully offline. It parses the template, checks any
supplied values against the template's own `AllowedPattern`s and numeric bounds,
and checks the consistency the template cannot express. Every selected profile
or directly invocable foundation model must match the deployment Region. An
out-of-Region or regionless foundation ARN is accepted only when its model ID and
geographic shape can support an admitted profile in the deployment Region.
Because exact destinations are source-Region and profile specific, this offline
command does not claim routing membership; `scene check` verifies the per-Scene
closure against `GetInferenceProfile` before STS minting:

```console
pearls infra validate \
  --orchestrator-principal-arn arn:aws:iam::123456789012:user/orchestrator \
  --project-bucket-arn-pattern 'arn:aws:s3:::quilt-dev-*' \
  --bedrock-model-arn arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0 \
  --bedrock-model-arn arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0 \
  --role-name scene-execution-model-inventory \
  --region us-east-1 \
  --lint
```

`pearls infra plan` creates a change set, prints the parameter diff, the resource
changes, and the rendered trust policy and inline role policy, then deletes the
change set. It never executes it and changes no stack resource. A change set that
is never executed is always deleted before the command exits, whether it was
planned, declined, or failed to launch; only an executed one is left in the
stack's history. Planning a stack name that does not exist yet is the one visible
side effect: CloudFormation creates the stack in `REVIEW_IN_PROGRESS` with no
resources, and the command says so, because no `pearls` verb has delete authority.
A later `deploy` of the same stack name proceeds normally. `pearls infra deploy`
renders the same review material, requires confirmation unless `--yes`,
passes `CAPABILITY_NAMED_IAM` explicitly, waits for a terminal stack status, and
prints `SceneExecutionRoleArn` in a form usable directly as
`pearls scene run --role-arn`. Re-running `deploy` with unchanged parameters is a
reported no-op rather than an error, and a failed or rolled-back deployment exits
nonzero with the CloudFormation failure reason preserved.

The Trial 001 two-model project can use one role inventory. Repeat
`--bedrock-model-arn`; the CLI sorts and serializes the values for the
`CommaDelimitedList`, so argument order does not create a noisy parameter diff:

```console
pearls infra deploy \
  --orchestrator-principal-arn arn:aws:iam::123456789012:user/orchestrator \
  --project-bucket-arn-pattern 'arn:aws:s3:::quilt-dev-*' \
  --bedrock-model-arn arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-3-haiku-20240307-v1:0 \
  --bedrock-model-arn arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0 \
  --role-name scene-execution-trial001 \
  --region us-east-1 --yes
```

The orchestrator now needs `sts:AssumeRole` on that one role.
`pearls infra assume-policy` generates the document from deployed stacks and
prints it by default; applying it requires an explicit flag and confirmation,
and warns that it replaces any existing inline policy of that name:

```console
pearls infra assume-policy \
  --from-stack scene-execution-trial001

pearls infra assume-policy \
  --from-stack scene-execution-trial001 \
  --apply-to-user orchestrator --yes
```

`pearls infra status --stack-name scene-execution-trial001` reports the stack
status, parameters, and drift-relevant identity: role ARN, complete admitted
Bedrock resource list, bucket pattern, and maximum session duration. Plan,
deploy, no-op, and status JSON expose the inventory as a logical list even
though the CloudFormation API transports it as one comma-delimited string.

Three limits are reported by the commands themselves rather than papered over.
Bedrock account-level model access cannot be provisioned by this template and
remains a console prerequisite; the role alone is not sufficient. The trust
policy admits the orchestrator principal with no `sts:ExternalId` condition,
while `mint(external_id=...)` exists for separately managed roles. The template
targets the commercial `aws` partition only. These commands also never delete a
stack or a role: there is no cleanup authority here.

### Live AWS authorization proof

[`tests/aws/test_authorization.py`](tests/aws/test_authorization.py) is an
opt-in proof against real IAM, STS, and S3. An unrestricted session of the
same base role first proves that the denied read/version and outside-write
operations are otherwise allowed, and that even a bare base session cannot
delete versions. The scoped session then proves an in-scope version read;
reads of the pinned input manifest, output workflow configuration, and exact
output `latest` pointer; data, manifest, and named-pointer writes; and denial
of sibling reads, wrong versions, outside writes, listing, object deletion,
and version deletion. A separate administrator-managed cleanup role removes
the exact disposable versions in `finally`; cleanup authority is never present
on the Scene role or scoped worker. The harness requires versioned disposable
buckets, does not provision infrastructure, and never runs merely because AWS
credentials are present.

Configure an administrator-owned sandbox, then run:

```console
export RAJA_STS_RUN_AWS_PROOF=1
export RAJA_STS_MANIFEST_URI='quilt+s3://...#package=...&top_hash=...'
export RAJA_STS_SCOPE_FILE="$PWD/examples/scope.json"
export RAJA_STS_ROLE_ARN='arn:aws:iam::123456789012:role/scene-execution'
export RAJA_STS_CLEANUP_ROLE_ARN='arn:aws:iam::123456789012:role/scene-proof-cleanup'
export RAJA_STS_ALLOWED_READ='s3://bucket/key?versionId=expected-version'
# A different selected key paired with the expected version ID; S3 must return NoSuchVersion.
export RAJA_STS_CROSS_OBJECT_VERSION_READ='s3://bucket/other-selected-key?versionId=expected-version'
export RAJA_STS_WRONG_VERSION_READ='s3://bucket/key?versionId=other-existing-version'
export RAJA_STS_DENIED_READ='s3://bucket/sibling-control.json?versionId=existing-version'
export RAJA_STS_WORKFLOW_READ='s3://output-bucket/.quilt/workflows/config.yml'
export RAJA_STS_OUTPUT_LATEST_READ='s3://output-bucket/.quilt/named_packages/scenes/example/output/latest'
export RAJA_STS_ALLOWED_WRITE='s3://output-bucket/scenes/example/output/proof.txt'
export RAJA_STS_DENIED_WRITE='s3://output-bucket/outside-output/proof.txt'
# Runner proof: use a scope with an exact bedrock_model and a disposable output package base.
export RAJA_STS_RUNNER_SCOPE_FILE="$PWD/examples/scope.json"
export RAJA_STS_RUNNER_PROMPT_KEY='input-content/prompt.txt'
export RAJA_STS_ALTERNATE_MODEL_ID='arn:aws:bedrock:us-east-1::foundation-model/another-model'
export RAJA_STS_TRANSCRIPT="$PWD/evidence/aws-taj/aws-proof-transcript.jsonl"
pytest -m aws tests/aws/test_authorization.py
```

The cleanup role is sandbox-only and should grant version deletion solely for
the disposable proof resources. Set `RAJA_STS_CLEANUP_EXTERNAL_ID` if its trust
policy requires a distinct external ID.

The JSONL transcript preserves operation outcomes, HTTP status codes, AWS
request IDs, assumed-role audit metadata, and the compilation report. It does
not record object bodies, access keys, secret keys, or session tokens. See
[`evidence/aws-taj/README.md`](evidence/aws-taj/README.md) before preserving a
run.

### Security and maturity boundaries

- `pearls scene run` verifies the sealed handler's bytes against the pinned
  manifest before executing them, which establishes code identity, not code
  safety. The handler runs in-process with this process's authority and receives
  the scoped Scene session that publication later uses. Installing the CLI does
  not add a sandbox; a genesis package you would not run by hand is equally
  dangerous through the command. `pearls scene check` does not import the handler
  at all, so a preflight of an untrusted package executes none of it, but that
  makes `check` a safe inspection step rather than a safety guarantee about the
  subsequent run.
- Runtime preflight checks the current Python version, installed distribution
  metadata, and top-level module resolvability only. It does not install or
  resolve packages, prove a distribution supplied the found module, execute
  import-time/native initialization, validate undeclared transitive
  dependencies, or establish a reproducible environment.
- `pearls infra` verbs mutate persistent IAM with administrator authority and are
  kept in a separate group for that reason. `validate` and `plan` change nothing,
  `deploy` and `assume-policy --apply-to-*` require confirmation or `--yes`, and
  no verb can delete a stack, a role, or an object.
- The publisher requires output bucket versioning before its first write and
  requires every S3 write to return a non-null version ID. Each invocation gets
  a create-only revision pointer; `latest` is a final ETag compare-and-swap.
  Concurrent writers can create unreferenced object versions, but both the CAS
  winner and loser retain manifests that pin their exact data versions. The
  loser returns `published-not-latest` rather than disguising a durable revision
  as total failure. Bucket Object Lock or bucket-policy controls remain
  necessary to prevent unrelated principals from replacing or deleting
  versions.
- The broad `.quilt/packages/*` output namespace is required because a new
  manifest's content hash is unknown before the worker builds it. Quilt3's
  content-addressed API contract derives that key from the manifest bytes;
  enforcing the contract against raw S3 callers belongs to a separate system
  boundary. The worker may read the exact pinned input manifest, output
  workflow namespace, exact output `latest` pointer, and only its selected
  revision pointer for consumer, CAS, and idempotency paths. Sibling revisions
  and `ListBucket` remain denied. Quilt 8's public
  `Package.push()` performs an optional manifest listing after the write to
  render a short hash, so a no-list integration must suppress or replace that
  presentation step; do not grant broad listing merely for display.
- Multipart create/upload/complete operations use `s3:PutObject`, but
  `s3:AbortMultipartUpload` is intentionally absent. Failed multipart uploads
  can leave billable incomplete parts; output buckets should configure an
  `AbortIncompleteMultipartUpload` lifecycle rule.
- The compiler emits commercial-partition (`arn:aws`) S3 resources in 0.1.1
  and rejects GovCloud/China role ARNs rather than minting mismatched policy.
- The compiler bounds a namespace; it does not prove that an output Package
  is a lineage descendant. Scene sealing/reconciliation must establish that
  relationship.
- KMS permissions are intentionally absent. SSE-KMS inputs or outputs need a
  separately designed key policy and session-policy extension; do not add
  broad KMS authority casually.
- CloudTrail records `AssumeRole` as a management event. S3 object operations
  require separately configured S3 data-event logging and may incur charges;
  they are not present in ordinary event history automatically.
- Explicit read/write/list/delete denies preserve those boundaries even when a
  bucket policy grants directly to the role session. Administrators must still
  avoid granting unrelated S3 actions to sessions; permissions boundaries,
  service control policies, and VPC endpoint policies may further restrict
  access.
- STS credentials cannot revoke already-started work immediately. Use short
  durations and require the trusted orchestrator to recompile before refresh.
- This release has comprehensive offline policy/API tests. The live proof is
  environment-gated because CI has no AWS sandbox; do not treat a mocked test
  as evidence of AWS's authorization behavior.

The set-based version guard relies on Amazon S3 assigning a unique version ID
to each stored object version, as documented in
[Retaining multiple versions of objects with S3 Versioning](https://docs.aws.amazon.com/AmazonS3/latest/userguide/Versioning.html).
Quilt manifest loading follows the public
[`Package.browse`](https://docs.quiltdata.com/api-reference/package) API.
Content was rephrased for compliance with licensing restrictions.