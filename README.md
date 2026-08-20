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
   stress testing (deterministic Scene sealing, output-package creation
   semantics, domain-aware reconciliation).

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

A sealed runner scope adds one exact Bedrock foundation-model ARN. The public
runner compiles and mints once, reads and verifies only selected object
versions, passes one explicit scoped session to the handler, and publishes a
Quilt output Package without listing the bucket:

```python
from raja_sts import SceneHandlerResult, run_scene


def handler(inputs, session, context):
    prompt = inputs["input-content/prompt.txt"].content.decode()
    response = session.client("bedrock-runtime").converse(
        modelId=context.bedrock_model_arn,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
    )
    return SceneHandlerResult(
        objects={"result/response.txt": response["output"]["message"]["content"][0]["text"]},
        invocation={"request_id": response["ResponseMetadata"]["RequestId"]},
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

Handlers return bytes or strings plus JSON-serializable invocation metadata.
The package always includes handler objects, `run/scene.json`, and
`run/invocation.json`. See [`examples/hello_scene.py`](examples/hello_scene.py)
for a thin one-Converse consumer. Prompt and response bodies remain explicit
package objects rather than implicit metadata.

### Boot from one genesis package

`run_scene_from_genesis()` makes one pinned package the complete Scene brief.
The package must contain a UTF-8 `README.md`, an `outputs.json` control object,
and at least one entry below `input/`. The handler receives only the `input/`
entries in its `inputs` mapping and reads the control prompt from
`context.control_prompt`:

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

The `bedrock_model` is deliberately supplied by the execution plane and
recorded in the authority report; it is not derived from the sealed genesis
package. The trusted bootstrap identity resolves the pinned manifest and reads
the two control objects by exact version before minting the worker credential.
Their Quilt hashes are verified. Handlers run in-process as trusted execution
code: they receive the scoped session that publication later uses, so selector
and reserved-key checks constrain returned objects but are not a sandbox
against a handler that makes direct S3 calls or retains the session. The
low-level `run_scene(manifest_uri, scope_spec, ...)` API remains available
unchanged.

### Mapping to the four Scene roles

| Scene role | AWS-native enforcement |
| --- | --- |
| Input Control | Only selected control logical keys resolve to versioned read resources. Sibling control files are absent from the allow set. |
| Input Content | Selected content keys receive exact S3 object ARNs. One set-based deny admits only the selected `s3:VersionId` values, and S3 rejects a selected version ID when requested against a different object key. |
| Output Content | `s3:PutObject` is limited to `<package-name>/*` in the declared output bucket. Quilt consumer validation may read `.quilt/workflows/*` in that bucket. |
| Output Control | Quilt manifest blobs may be written under `.quilt/packages/*`; named-package pointers are limited to `.quilt/named_packages/<package-name>/*`, with an exact read of that package's `latest` pointer for conflict checking. |

Every session policy explicitly denies deletes and bucket listing on all
resources. Action-specific `NotResource` guardrails prevent bucket policies
from restoring out-of-scope current-object reads or writes. Exact version mode
uses one global mismatch deny containing the sealed set of S3 version IDs and
one resource-set allow containing the selected object ARNs. Because S3 version
IDs uniquely identify stored object versions, using an admitted ID against a
different key does not return that key. The policy grants no IAM, STS, ACL,
bucket-policy, or input-write action.
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
- Package `user_meta` is validated against the workflow's `metadata_schema`,
  the entry list against its `entries_schema`, the commit `message` against
  `is_message_required`, and the package name against `handle_pattern`.
- Validation happens **before any write**; a refusal raises
  `WorkflowContractError` and leaves nothing behind — a refusal transcript is
  evidence.
- A conforming publication stamps `{id, config, schemas}` (S3 URIs with
  version IDs) into the manifest header exactly as quilt3 writes it, so
  catalogs and `quilt3` read the package as having passed the contract it
  actually passed.
- Schema documents must be reachable by the publishing credentials; under a
  Scene session that means the output registry's own `.quilt/workflows/*`
  prefix (already part of the compiled read scope). `$ref` is rejected and
  only draft-07 meta-schemas are supported, for parity with quilt3's
  validator.

`run_scene` and `run_scene_from_genesis` expose these as `output_meta`,
`output_message`, and `output_workflow`.

### Policy-size behavior

STS limits an inline session policy to 2,048 plaintext characters. Exact mode
uses set-based version-ID and resource lists rather than two statements per
object, then minifies and measures the JSON before calling AWS. The five-object
Scene regression compiles to 1,767 characters with all required Quilt metadata
reads and remains version-pinned. The compiler fails closed with
`PolicySizeError` when a larger policy does not fit.

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
    BedrockModelArn=arn:aws:bedrock:us-east-1::foundation-model/amazon.nova-micro-v1:0
```

Review the rendered IAM policy before deployment. `BedrockModelArn` must be the
same exact foundation-model ARN selected by the sealed Scene scope; a mismatch
fails closed because the effective permission is the intersection of the base
role and session policy. The orchestrator needs `sts:AssumeRole` permission as
well as the role's trust. The template targets the commercial `aws` partition
in 0.1.1. `mint(external_id=...)` is available for separately managed roles
whose trust policy requires `sts:ExternalId`; the shipped same-account template
does not require one. The worker must not be able to retrieve broader ambient
task, instance, or execution-role credentials; otherwise it can bypass the
scoped credentials entirely.

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

- The publisher requires output bucket versioning before its first write and
  requires every S3 write to return a non-null version ID. Concurrent writers
  can create unreferenced object versions, but the winning `latest` manifest
  continues to pin the exact data versions it published. Bucket Object Lock or
  bucket-policy controls remain necessary to prevent unrelated principals from
  replacing or deleting versions.
- The broad `.quilt/packages/*` output namespace is required because a new
  manifest's content hash is unknown before the worker builds it. Quilt3's
  content-addressed API contract derives that key from the manifest bytes;
  enforcing the contract against raw S3 callers belongs to a separate system
  boundary. The worker may read the exact pinned input manifest, output
  workflow namespace, and exact output `latest` pointer needed by the consumer
  and conflict-check paths. `ListBucket` remains denied. Quilt 8's public
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