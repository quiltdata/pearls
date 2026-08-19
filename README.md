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

Release 0.1.0 adds an alpha maturity-level 1→2 authority-compilation
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
are rejected. The trusted compiler identity needs permission to browse this
manifest; the resulting worker credential does not receive manifest-registry
read access automatically.

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

### Mapping to the four Scene roles

| Scene role | AWS-native enforcement |
| --- | --- |
| Input Control | Only selected control logical keys resolve to read statements. Sibling control files are absent from the allow set. |
| Input Content | Selected content keys receive exact S3 object ARNs. When a manifest entry has a version ID, the policy uses `s3:GetObjectVersion` with an `s3:VersionId` equality condition. |
| Output Content | `s3:PutObject` is limited to `<package-name>/*` in the declared output bucket. |
| Output Control | Quilt manifest blobs may be written under `.quilt/packages/*`; named-package pointers are limited to `.quilt/named_packages/<package-name>/*` in that same bucket. |

Every session policy explicitly denies deletes and bucket listing on all
resources. `NotResource` deny guardrails prevent bucket policies from restoring
out-of-scope `GetObject`, `GetObjectVersion`, or `PutObject` access; strict
version mode also denies latest and mismatched-version reads on selected keys.
The policy grants no IAM, STS, ACL, bucket-policy, or input-write action.
Because a session policy can only narrow its base role, the agent cannot add
authority during the session. Promotion of output into a later Scene's inputs
remains separately authorized.

### Policy-size behavior

STS limits an inline session policy to 2,048 plaintext characters. The
compiler minifies and measures exact per-object policy JSON before calling
AWS. It fails closed with `PolicySizeError` when the policy does not fit.

A sealed scope may explicitly set `allow_prefix_fallback` to `true`. The
compiler then emits common physical S3-prefix resources and reports
`mode="per-prefix"`, every widened resource, and loss of version enforcement.
This fallback can expose objects that are not in the selected manifest; it is
never automatic. Splitting work into smaller Scenes is the safer response.

### Base role

[`infra/scene-execution-role.yaml`](infra/scene-execution-role.yaml) is an
administrator-applied CloudFormation template. It creates no buckets and the
Python module never creates IAM resources. The role is deliberately broad
within a controlled project-bucket ARN pattern so the session policy provides
the real per-Scene bound.

An administrator can deploy it with an account-specific principal and bucket
pattern:

```console
aws cloudformation deploy \
  --template-file infra/scene-execution-role.yaml \
  --stack-name pearls-scene-execution \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    OrchestratorPrincipalArn=arn:aws:iam::123456789012:role/orchestrator \
    ProjectBucketArnPattern=arn:aws:s3:::my-project-*
```

Review the rendered IAM policy before deployment. The orchestrator needs
`sts:AssumeRole` permission as well as the role's trust. The template targets
the commercial `aws` partition in 0.1.0. `mint(external_id=...)` is available
for separately managed roles whose trust policy requires `sts:ExternalId`; the
shipped same-account template does not require one. The worker must not be able
to retrieve broader ambient task, instance, or execution-role credentials;
otherwise it can bypass the scoped credentials entirely.

### Live AWS authorization proof

[`tests/aws/test_authorization.py`](tests/aws/test_authorization.py) is an
opt-in proof against real IAM, STS, and S3. An unrestricted session of the
same base role first proves that the denied read/version and outside-write
operations are otherwise allowed. The scoped session then proves an in-scope
version read; a data, manifest, and named-pointer write; and denial of sibling
reads, wrong versions, outside writes, listing, object deletion, and version
deletion. The harness requires versioned disposable buckets, removes its proof
objects with the baseline session, does not provision infrastructure, and
never runs merely because AWS credentials are present.

Configure an administrator-owned sandbox, then run:

```console
export RAJA_STS_RUN_AWS_PROOF=1
export RAJA_STS_MANIFEST_URI='quilt+s3://...#package=...&top_hash=...'
export RAJA_STS_SCOPE_FILE="$PWD/examples/scope.json"
export RAJA_STS_ROLE_ARN='arn:aws:iam::123456789012:role/scene-execution'
export RAJA_STS_ALLOWED_READ='s3://bucket/key?versionId=expected-version'
export RAJA_STS_WRONG_VERSION_READ='s3://bucket/key?versionId=other-existing-version'
export RAJA_STS_DENIED_READ='s3://bucket/sibling-control.json?versionId=existing-version'
export RAJA_STS_ALLOWED_WRITE='s3://output-bucket/scenes/example/output/proof.txt'
export RAJA_STS_DENIED_WRITE='s3://output-bucket/outside-output/proof.txt'
export RAJA_STS_TRANSCRIPT="$PWD/evidence/aws-taj/aws-proof-transcript.jsonl"
pytest -m aws tests/aws/test_authorization.py
```

The JSONL transcript preserves operation outcomes, HTTP status codes, AWS
request IDs, assumed-role audit metadata, and the compilation report. It does
not record object bodies, access keys, secret keys, or session tokens. See
[`evidence/aws-taj/README.md`](evidence/aws-taj/README.md) before preserving a
run.

### Security and maturity boundaries

- `s3:PutObject` excludes deletes but can overwrite a key by creating a new
  current version. Strong append-only semantics require unique keys plus
  bucket versioning, Object Lock, conditional writes, or a trusted finalizer.
- The broad `.quilt/packages/*` output namespace is required because a new
  manifest's content hash is unknown before the worker builds it. The mutable
  named-package namespace remains package-specific. Only `PutObject` is
  granted: Quilt 8's default `Package.push()` also reads the latest pointer and
  lists manifests for display, so it is intentionally incompatible with these
  credentials. Use a least-authority writer or a separately trusted finalizer;
  do not add `ListBucket` merely to make the default client path work.
- The compiler emits commercial-partition (`arn:aws`) S3 resources in 0.1.0
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

Quilt manifest loading follows the public
[`Package.browse`](https://docs.quiltdata.com/api-reference/package) API.
Content was rephrased for compliance with licensing restrictions.