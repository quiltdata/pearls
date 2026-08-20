<!-- markdownlint-disable MD024 -->

# Changelog

All notable changes to this project are documented in this file.

## [0.1.3] - 2026-08-20

### Added

- Publication now honors the destination registry's Quilt **workflow
  contract** with quilt3 semantics, validated client-side through the one
  caller-supplied S3 client before anything is written: default-workflow
  resolution, `is_workflow_required` (default true when a config exists),
  metadata and entries JSON Schemas, `is_message_required`, and
  `handle_pattern`. A conforming publication stamps the workflow (config and
  schema URIs with version IDs) into the manifest header exactly as quilt3
  does; a nonconforming one raises `WorkflowContractError` and leaves no
  partial writes.
- Contract objects must themselves be versioned: a workflow config or schema
  with no S3 version ID is refused, so the manifest stamp always pins the
  exact contract that was validated (stricter than quilt3, refuse-only).
- `publish_package`, `run_scene`, and `run_scene_from_genesis` accept
  package-level `user_meta` / `output_meta`, a commit `message` /
  `output_message`, and a `workflow` / `output_workflow` selector, so Scene
  outputs can enter governed buckets whose schemas require declared metadata.

## [0.1.2] - 2026-08-19

### Added

- Added `run_scene_from_genesis()`, which boots a Scene from one top-hash-pinned
  Quilt package, derives exact reads from its `input/` entries, exposes its
  UTF-8 `README.md` as `SceneContext.control_prompt`, and compiles its
  `outputs.json` as the write grant.
- Added the `{unpinned quilt+s3 URI: description}` output grammar. URI paths
  distinguish whole-package, trailing-slash subtree, and exact-object grants
  while retaining `writable_package` for the low-level compatibility API.
- Added selector-aware output validation and deterministic authority reporting
  for genesis-derived runs.

### Security

- Genesis control objects are read by exact S3 version and verified against
  their Quilt content hashes before STS credentials are minted.
- Write selectors must target one package and may not overlap. Exact-object and
  subtree grants cannot select the runner-owned `run/scene.json` or
  `run/invocation.json` records; returned handler objects cannot replace those
  records even under a whole-package grant. Narrow grants receive only their
  declared data resources plus the exact runtime records and required Quilt
  publication namespaces.
- Handler output keys are checked against the immutable genesis write scope
  before any package object is published.

### Known limitations

- Handlers execute in-process as trusted code and receive the same scoped
  session used for publication. Selector and reserved-key validation applies
  to returned objects; isolating a malicious handler from direct S3 calls or
  retained credentials requires a separate execution plane.
- A genesis package supports one output Quilt package per run. Cross-package
  output grants and promotion remain outside the runner contract.
- The trusted bootstrap identity reads `README.md` and `outputs.json` before
  Scene credentials exist. The genesis top hash must not be described as
  tamper-enforced until Quilt manifest retrieval verifies downloaded bytes
  against the requested top hash (quiltdata/quilt#5181).

## [0.1.1] - 2026-08-19

### Added

- Added the public `run_scene()` bounded-execution primitive, which compiles a
  pinned manifest once, mints one Scene-named STS session, materializes exact
  selected object versions, invokes one caller handler, and publishes one
  descendant Quilt Package.
- Added exact `bedrock:InvokeModel` authority for one sealed foundation-model
  ARN and a thin Hello Scene example that performs one Bedrock Converse call
  through the scoped session.
- Added standard `run/scene.json` and `run/invocation.json` records containing
  non-secret authority, lineage, audit identity, invocation, and timestamp
  metadata.
- Added an explicit-session, Quilt-backed v1 publication adapter that uses
  Quilt's canonical Package manifest and top-hash implementation, requires a
  versioned output bucket and version IDs from every write, and performs no
  bucket listing or deletion; `latest` remains the final arbitration write.

### Security

- The runner rejects prefix, mixed, unversioned, unhashed, and unsupported-hash
  inputs. Every selected read sends its sealed S3 version ID and verifies the
  manifest content hash before handler execution.
- Input reads, handler SDK clients, and package publication share one explicit
  `boto3.Session` built only from the minted temporary credential. Handler
  outputs and JSON metadata are validated before any output write, and
  credential-shaped metadata is rejected.
- Bedrock authority admits only `bedrock:InvokeModel` on the exact model ARN;
  alternate models and broad `bedrock:*` capabilities remain outside the
  compiled policy.

### Known limitations

- V1 runs exactly one handler without retries, streaming, tools, task graphs,
  cleanup, promotion, KMS integration, or generalized model-provider support.
- Output lineage is declared in the execution record but is not a
  cryptographic proof. Failed or losing concurrent publications can leave
  unreferenced object versions, immutable manifests, or revision pointers for
  administrator lifecycle cleanup because Scene credentials cannot delete.

## [0.1.0] - 2026-08-19

### Added

- Added the `raja_sts.compile()` authority compiler for immutable Quilt Package
  revisions and sealed logical read/output scopes.
- Added compact, set-based exact S3 object/version policy generation,
  deterministic minified JSON, compilation reports, and a fail-closed
  2,048-character STS preflight. A five-object Scene remains exact at 1,767
  characters instead of requiring prefix fallback.
- Added an explicit, reportable physical-prefix fallback for callers that
  knowingly accept widened read authority.
- Added `raja_sts.mint()` and `mint_compiled()` for role-session credentials
  whose session name carries the Scene audit identity.
- Added an administrator-applied CloudFormation template for the broad
  `scene-execution` base role, with all object/version delete authority removed.
- Added Python 3.11-3.13 CI, static checks, build validation, 95% minimum
  coverage, and security-boundary tests.
- Added an opt-in live AWS proof for selected content and required Quilt
  metadata reads, denied sibling reads, outside writes, and deletes, with a
  separate cleanup role and sanitized JSONL evidence capture.
- Added public usage, Scene-role mapping, deployment, evidence, and security
  boundary documentation.

### Security

- Session policies explicitly deny object/version deletion and bucket listing
  everywhere. Action-specific `NotResource` guardrails prevent resource
  policies from restoring out-of-scope current reads or writes; a global
  version-ID-set deny preserves exact version reads compactly.
- Input read authority includes only objects selected by the sealed logical
  scope plus that package revision's exact content-addressed manifest.
- Output write authority is limited to one package data prefix, Quilt's
  immutable manifest namespace, and that package's named-pointer namespace.
  Read authority additionally admits workflow validation and the exact output
  `latest` pointer required for conflict checks.
- Floating manifests, hash prefixes, unsafe wildcard resources, unknown scope
  fields, unmatched selectors, invalid session identities, and oversized
  policies fail closed.
- Temporary credential secrets are excluded from object representations and
  live proof transcripts.

### Known limitations

- `s3:PutObject` does not prevent overwriting a key; stronger append-only
  behavior needs bucket-level controls or a trusted package finalizer. The
  shipped execution role has no delete authority. Quilt 8's public
  `Package.push()` performs a post-write manifest listing to display a short
  hash; no-list integrations must suppress or replace that optional
  presentation step.
- Multipart abort authority is absent; output buckets need an
  `AbortIncompleteMultipartUpload` lifecycle rule to remove incomplete parts.
- The 0.1.0 policy compiler supports the commercial AWS partition only.
- Physical-prefix fallback deliberately weakens logical and version isolation,
  can include sibling Quilt control objects when a widened prefix overlaps
  `.quilt/`, and therefore requires explicit opt-in.
- SSE-KMS permissions, output-lineage proof, ambient credential isolation,
  CloudTrail S3 data-event provisioning, and automatic package promotion are
  outside the 0.1.0 authority-compilation primitive.
