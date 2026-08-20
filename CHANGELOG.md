<!-- markdownlint-disable MD024 -->

# Changelog

All notable changes to this project are documented in this file.

## [0.1.4] - 2026-08-20

### Added

- Added an installed `pearls` console entry point with two command groups.
  `pearls scene check | run` covers the reusable operator side of one
  sealed-genesis Scene run; `pearls infra validate | plan | deploy | status |
  assume-policy` wraps the administrator-applied execution-role template. Both
  groups share one skeleton: `--profile` / `--region` resolved by boto3,
  human-readable stdout with `--json` for a stable versioned document,
  diagnostics on stderr, read-only verbs that never prompt, mutating verbs that
  require confirmation or `--yes`, and `0` / `1` / `2` exit codes for success,
  typed refusal, and usage error.
- Added the `protocol/handler.py` sealed-handler convention, split into a
  verification step and an execution step because importing a Python module runs
  its body. `verify_sealed_handler()` reads that entry from the requested pinned
  revision by exact S3 version, verifies its bytes against the manifest's Quilt
  content hash, and checks statically, by parsing, that they bind a module-level
  `handler`, executing nothing. `SealedSource.load()` is the single point at which
  sealed bytes run and re-checks that the binding is callable;
  `load_sealed_handler()` remains the eager convenience API. `report()` records
  the version ID, Quilt hash, and source SHA-256 that were verified.
- Added `preflight_genesis()`, which resolves, verifies, and compiles one pinned
  genesis package without minting. `run_scene_from_genesis()` now uses it too, so
  `pearls scene check` and `pearls scene run` agree by construction on layout,
  authority, and compiled policy rather than by duplicated logic.
- Added `read_manifest_object()`, `validate_assume_role_request()`,
  `parse_bedrock_model()`, and `bedrock_model_region()` as public primitives, so
  the CLI can verify an object, an AssumeRole request, and a model ARN offline
  without restating the library's rules.
- Added the execution-role template to the wheel as
  `raja_sts/templates/scene-execution-role.yaml`, kept byte-identical to
  `infra/scene-execution-role.yaml` by test. `pearls infra` reads parameter
  constraints from the template itself, so client-side validation cannot drift
  from what CloudFormation enforces.

### Security

- `pearls scene check` performs only reads. It calls no `AssumeRole`, invokes no
  model, writes no object, and executes no byte of the sealed handler, and it
  labels every check with the identity that performed it, so the reads that
  require the trusted bootstrap identity are visible rather than implied.
- `pearls scene run` defers handler execution to the moment the runner invokes it,
  so the genesis layout check, the compiled session policy, the minted Scene
  credential, and the verified input materialization all precede any sealed code
  running. An eager import would have run the module body first and made those
  checks unreachable.
- `pearls scene check` reports what it cannot prove: Bedrock account-level model
  access, the base role's own permissions, the output registry's workflow
  contract, the safety as distinct from the identity of the sealed handler, and,
  because it does not import the handler, whether the handler imports cleanly or
  is callable at all.
- Credential resolution failures, missing regions, and AWS API refusals are
  reported as typed refusals with a nonzero exit rather than as tracebacks.
- `--profile` and `--region` apply only to the operator's own calls, never to the
  Scene session, which is built from the minted credential and the model ARN's
  region alone. The two boundaries are documented as boundaries: verifying the
  handler's bytes proves code identity, not code safety.
- JSON receipts contain only non-secret run identity and status. Output package
  metadata is reported by key, never by value, and no command prints credentials
  or handler inputs and outputs.
- `pearls infra validate` and `plan` create, update, and delete no stack
  resource. `deploy` passes `CAPABILITY_NAMED_IAM` explicitly, reports that named
  IAM resources are involved, and requires confirmation unless `--yes`. A model
  ARN whose region differs from the deployment region is refused before any stack
  mutation, because that mismatch would otherwise surface only as a fail-closed
  Scene run. `assume-policy` prints by default and mutates IAM only behind an
  explicit flag plus confirmation, warning that it replaces the inline policy of
  that name. No verb deletes a stack, a role, or an object.

### Known limitations

- Verified sealed handlers execute in-process with the calling process's ambient
  authority. Hash verification is a provenance boundary, not a sandbox, and the
  CLI adds no isolation over the existing `run_scene_from_genesis()` contract.
  Ordering execution after the authority checks constrains when sealed code runs,
  not what it can do once it runs.
- The static check that sealed bytes bind a module-level `handler` cannot prove
  the binding is callable or that the module imports cleanly. Those are only known
  once the bytes execute, which during a run is after the Scene credential has
  been minted.
- The sealed handler is read by the trusted bootstrap identity, not by the Scene
  credential, because `run_scene_from_genesis()` compiles reads from `input/`
  only. Executing sealed code therefore still depends on the operator's own read
  access to the genesis registry.
- quilt3 resolves the pinned manifest with its own ambient credentials, so
  `--profile` does not apply to manifest resolution, only to the object reads and
  AWS calls Pearls makes directly.
- `pearls infra plan` creates and then deletes a CloudFormation change set; the
  change set itself is an API object, even though no stack resource is created,
  updated, or deleted. `--keep-change-set` retains it for out-of-band review.
  Planning a stack name that does not exist yet leaves that stack in
  `REVIEW_IN_PROGRESS` with no resources; the command reports this, and removing an
  abandoned shell is an explicit administrator action because no verb here can
  delete.
- `deploy` confirms an executed change by waiting for CloudFormation to leave the
  stack's pre-execution state, using `LastUpdatedTime` and observed in-progress
  statuses. If neither changes within `--wait-seconds` it reports that the outcome
  could not be confirmed rather than reporting stale outputs as success.
- The rendered trust and inline policies substitute only the `Ref` and `Fn::Sub`
  forms this template uses. Other intrinsics are shown as marked placeholders
  rather than guessed, and rendering is for review, not a substitute for reading
  the CloudFormation change set.
- Multi-Scene trial orchestration, retries, and reuse of an existing output
  package name remain out of scope pending re-execution semantics; inference
  profiles and handler-derived package metadata remain separate concerns.

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
