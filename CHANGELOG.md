<!-- markdownlint-disable MD024 -->

# Changelog

All notable changes to this project are documented in this file.

## [0.2.2] - 2026-08-22

### Changed

- **Prefix-scoped input authority (#23):** session policies now contribute one
  physical read prefix per source registry instead of enumerating selected object
  ARNs and VersionIds. The four per-object/mixed/prefix policy modes and
  `allow_prefix_fallback` are removed; every compilation reports
  `mode="prefix-scoped"` and its admitted prefixes.
- Session policies now use plain resource-scoped allows for input reads, exact
  Quilt metadata reads, output writes, bucket-versioning checks, and Bedrock
  invocation. The broad allows paired with `Deny`/`NotResource` guardrails were
  removed because the session policy already intersects with the base role.
- GitHub Actions now pins `actions/checkout` v5, removing the Node 20
  deprecation warning emitted by the v4 runtime.
- Runner preflight now rejects missing or unsupported input integrity metadata
  before `AssumeRole`; materialization still requests the exact `VersionId` and
  verifies returned bytes against the declared Quilt hash. STS
  `PackedPolicyTooLarge` responses become a typed `PackedPolicyTooLargeError`
  naming selected-object and prefix counts.

### Security

- Input provenance is enforced at the content boundary: the runner admits only
  selected descriptors with exact S3 versions and supported Quilt hashes, then
  verifies both before trusted handler code runs. IAM bounds read blast radius
  to source prefixes rather than attempting to prove content identity.

### Known limitations

- A prefix-scoped Scene credential can read known objects under an admitted
  source prefix that the manifest scope did not select. `scene check` reports
  this under `unproven`; Pearls verifies delivered selected bytes but does not
  prove the absence of extra reads by the trusted in-process handler.
- The STS packed-policy budget is undocumented and cannot be calculated during
  preflight. The constant-size input scope avoids object-count growth, but AWS
  can still reject the complete output/Bedrock policy at mint time; that refusal
  is surfaced as `PackedPolicyTooLargeError`.

## [0.2.1] - 2026-08-22

### Security

- **Releases are refused for any ref but `main`.** A PyPI Trusted Publisher
  matches on owner, repository, workflow filename, and environment, not on the
  ref, so a `workflow_dispatch` against an unmerged branch could permanently
  publish code that is not on `main`. The release job now requires
  `refs/heads/main` for both the push and the dispatch path. 0.2.0 was in fact
  published this way, deliberately, to register the name before the rename PR
  merged.

### Fixed

- `pearls.__version__` no longer reports a version belonging to different code.
  Reading installed distribution metadata removed the drift of a restated
  literal, but metadata describes the distribution rather than the module that
  was imported: a source checkout on `sys.path` shadows a differently versioned
  install, and an editable install's metadata freezes at install time. Version
  resolution now trusts metadata only when the installed distribution actually
  contains the imported file, falls back to the checkout's own
  `pyproject.toml`, and reports `0.0.0+unknown` rather than guessing. This
  matters because the value is stamped into every JSON receipt as
  `pearls_version`, where it is a provenance claim.

### Known limitations

- `0.0.0+unknown` is reported for code that neither an installed distribution
  nor a readable `pyproject.toml` identifies, for instance a package imported
  from a zip or vendored into another tree. A receipt then records that the
  version could not be established, which is weaker than a version but not a
  false one.

## [0.2.0] - 2026-08-21

### Changed

- **Renamed the project to `pearls`:** the distribution is now `pearls`
  rather than `raja-sts`, and the import package is now `pearls` rather than
  `raja_sts`. The installed console script was already `pearls` and is
  unchanged. Nothing was ever published to PyPI under `raja-sts`, so no
  compatibility alias or deprecation shim exists: `import raja_sts` simply
  fails. The public API is otherwise identical except for the names below.
  Earlier entries in this file still refer to `raja_sts`, because that is what
  those revisions actually shipped.
- The base exception `RajaStsError` is now `PearlsError`. Every derived error
  keeps its own name.
- `pearls.__version__` is now read from the installed distribution metadata
  instead of being restated in `__init__.py`, so `pearls --version`, the
  `pearls_version` field in every JSON receipt, and the published distribution
  cannot disagree. `pyproject.toml` is the single declaration. Imported from an
  uninstalled source tree it reports `0.0.0+unknown`.
- Report and receipt `$schema` URIs moved from
  `https://quiltdata.com/schemas/raja-sts/...` to
  `https://quiltdata.com/schemas/pearls/...`. Document versions are unchanged,
  because only the namespace moved, not the shape: scene-execution/v1,
  handler-runtime-requirements/v1, genesis-publication-intent/v1,
  cli-scene-check/v2, cli-scene-run/v3, cli-genesis-author/v1,
  cli-infra-\*/v1 and v2. A consumer matching on the exact URI must be updated.
- Revision-pointer S3 object metadata keys moved from `raja-*` to `pearls-*`
  (`pearls-intent-sha256`, `pearls-manifest-version-id`,
  `pearls-previous-state`, `pearls-previous-top-hash`, `pearls-previous-etag`,
  `pearls-previous-version-id`, `pearls-published-at`).
- The live-proof environment variables moved from `RAJA_STS_*` to `PEARLS_*`.
- Toolchain is uv rather than pip: CI resolves with `uv pip install`, builds
  with `uv build`, and releases with `uv publish`. No `uv.lock` is added, so
  direct pins in `pyproject.toml` remain the only dependency declaration and
  transitive resolution stays the installer's responsibility.
- `RAJA` still names the authorization component in the architecture
  documentation. Only the Python distribution, import package, and the
  identifiers above were renamed.

### Added

- **Apache License 2.0.** The repository now carries the verbatim license text
  in `LICENSE` and attribution in `NOTICE`. `pyproject.toml` declares it as the
  PEP 639 SPDX expression `Apache-2.0`, so both artifacts publish
  `License-Expression: Apache-2.0` under Metadata 2.4 and ship both files. No
  earlier revision stated any license terms.
- CI now also builds, checks, and releases the distribution. Releases are not
  tag-triggered: a merge to `main` publishes to PyPI through Trusted
  Publishing when the version in `pyproject.toml` is one PyPI does not already
  have, and is skipped otherwise, so bumping the version is the whole release
  action. `workflow_dispatch` runs the same guarded path to retry a failed
  upload. No API token is stored in the repository. Before any upload, CI
  refuses a version with no `CHANGELOG.md` section and proves that the built
  wheel installs, imports as `pearls`, and reports the declared version
  through the `pearls` CLI. Every event builds and checks the same artifacts,
  and the release job publishes exactly those. Publishing lives in `ci.yml`
  rather than a separate workflow because a Trusted Publisher is bound to one
  workflow filename.

### Known limitations

- A revision pointer written by 0.1.x carries `raja-*` metadata, which 0.2.0
  does not read. A retry of such a publication is refused as recording
  different intent rather than reconciled as the same intent. This is
  fail-closed, not silent divergence, but it does mean in-flight 0.1.x
  publications cannot be resumed by 0.2.0.
- The name `pearls` was unregistered on PyPI when this entry was written.
  Registration is only proven by a successful upload, because PyPI also
  rejects names it considers too similar to an existing project. `pearls-ai` is
  the fallback if `pearls` is refused.
- On TestPyPI the name `pearls` belongs to an unrelated project, so the release
  workflow has no TestPyPI rehearsal path.

## [0.1.5] - 2026-08-21

### Added

- **Bounded Bedrock authority (#12, #18):** one execution role can inventory
  several foundation-model and inference-profile ARNs while each Scene selects
  exactly one. An inference-profile ARN remains the exact Bedrock `modelId`;
  trusted `GetInferenceProfile` resolution validates its bounded foundation-model
  closure, and the session policy admits only the profile-conditioned support
  resources needed by that selection.
- **Revision-safe publication (#10):** publication derives an idempotent revision
  identity, writes create-only revision pointers, advances `latest` conditionally,
  and records a durable `published-not-latest` result for a losing writer.
  Version-ID-safe retries reconcile the exact published revision, and
  `PublicationWriteDisposition` distinguishes no write, `latest`-only recovery,
  and package writes. Before an initial present-base attempt, publication
  reconfirms the recorded ETag and `VersionId`, so a same-body rewrite during the
  data, manifest, and revision-pointer writes becomes an arbitration loss. The
  final operation remains the ETag/value CAS that S3 supports.
- **Run-derived output metadata (#13):** a handler can return
  `SceneHandlerResult.output_meta`; publication merges it with caller metadata
  only after refusing key collisions, non-finite/non-JSON values, and credential
  material, then validates and reports the final merged metadata.
- **Publication domain invariants (#20):** callers can supply identified
  `PublicationInvariant` hooks. They inspect a detached pending-revision context,
  and all findings are aggregated into one pre-write refusal.
- **Operator-side Genesis authoring (#16):** `pearls genesis author` freezes a
  local tree, prepares and optionally publishes one revision, binds the exact
  sealed inputs and handler/seal declarations into reusable revision intent,
  supports dry-run review, and reports the mutations actually performed.
- **Sealed runtime requirements (#17):** every Genesis revision now carries a
  bounded `protocol/requirements.json`. Preflight verifies its exact S3 version
  and content hash, validates PEP 440/508 declarations, and checks exact installed
  distribution versions and top-level module resolution before policy
  compilation or STS minting. Check receipts are v2, run receipts are v3, and
  `packaging==26.3` is now a direct dependency.

### Security

- Inference-profile closure validation runs with the trusted bootstrap identity;
  the minted Scene session remains limited to one declared selection rather than
  inheriting the execution role's whole model inventory.
- Publication revalidates workflow contracts and caller invariants against the
  pending revision before mutation. Retry and concurrent-winner paths use the
  same checks and never relabel another writer's mutations as their own.
- Genesis authoring rejects symlinks, non-regular files, path escapes, and files
  that change while being frozen. Published declarations remain bound to the
  same revision identity on reuse.
- Runtime-requirement parsing bounds bytes, counts, strings, and diagnostics;
  malformed or hostile JSON is normalized into a typed refusal before sealed
  code executes or credentials are minted. Distribution checks and module checks
  are reported separately so neither is overstated.

### Known limitations

- The base execution role remains inventory-wide; Pearls adds no Bedrock
  `Deny`/`NotResource` guard. Least authority is enforced in the per-Scene
  session policy.
- A failed publication or writer that loses `latest` arbitration can leave
  unreferenced versioned objects. A retry whose recorded base `latest` version
  was present does not replay that stale transition, because S3 offers no
  destination-`VersionId` precondition; the revision remains durably
  `published-not-latest`. A fresh present-base attempt reconfirms the exact
  generation before its ETag/value CAS, narrowing but not closing the final
  read-to-write window. A same-body rewrite inside that window is
  indistinguishable because Quilt's raw top-hash pointer carries no generation
  token. `is_latest` therefore denotes current content, not pointer-generation
  ownership; exact generation fencing requires another coordination protocol.
  Every create-only revision remains durable regardless.
- Domain invariants are caller code over one detached pending revision. The CLI
  does not dynamically load invariant implementations.
- Genesis authoring proves the frozen bytes, layout, and static declaration
  shape, not handler safety or compatibility with the authoring machine's local
  runtime.
- Runtime preflight does not install or resolve dependencies, validate ABI or
  transitive compatibility, import module bodies, sandbox execution, or prove a
  reproducible environment.

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
- Credential resolution failures, missing regions, AWS API refusals, and
  exceptions raised by the sealed handler itself are reported as typed refusals
  with a nonzero exit rather than as tracebacks. A handler that raises becomes
  `SceneHandlerError`; a handler that returns something the runner refuses keeps
  its own `HandlerContractError`.
- The review material for a mutating `infra` verb is written to stderr under
  `--json` rather than suppressed, so an interactive confirmation is never
  requested without showing the parameter diff and rendered policies. stdout
  remains a single parseable document.
- `--external-id` is validated against the character set AWS accepts, not only its
  length, so a value like `tenant one` is refused offline instead of at
  `AssumeRole`.
- `--metadata` is parsed strictly: `NaN`, `Infinity`, and `-Infinity`, which
  `json.loads` accepts by default, are refused, because publication serializes
  with `allow_nan=False`. A preflight and a run now agree on metadata validity.
- A stack name is validated locally against CloudFormation's own narrower rules.
  The template admits role names containing `_ + = , . @` or starting with a
  digit, so defaulting the stack name from such a role name is refused with a
  pointer to `--stack-name` rather than failing at the AWS call.
- `deploy --plan-only` shares one code path with `plan`, so the documented alias
  cannot report a different status for a no-op or drop the `REVIEW_IN_PROGRESS`
  warning.
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
  been minted. The check requires a real binding, so an annotation-only
  `handler: object` is refused.
- `SceneHandlerError` includes the sealed handler's own exception text so a failure
  is debuggable. That text is not screened for credential material, because the
  handler could have embedded anything in it; it is strictly less exposure than the
  traceback it replaces, but a handler that formats its session into an exception
  will still surface it.
- The sealed handler is read by the trusted bootstrap identity, not by the Scene
  credential, because `run_scene_from_genesis()` compiles reads from `input/`
  only. Executing sealed code therefore still depends on the operator's own read
  access to the genesis registry.
- quilt3 resolves the pinned manifest with its own ambient credentials, so
  `--profile` does not apply to manifest resolution, only to the object reads and
  AWS calls Pearls makes directly.
- `pearls infra plan` creates and then deletes a CloudFormation change set; the
  change set itself is an API object, even though no stack resource is created,
  updated, or deleted. `--keep-change-set` retains it for out-of-band review. A
  change set that is never executed, because the operator declined, stdin could
  not answer, or the call failed, is deleted before the command exits; an executed
  one is left in the stack's history. Planning or abandoning a deploy for a stack
  name that does not exist yet leaves that stack in `REVIEW_IN_PROGRESS` with no
  resources; the commands report this, and removing an abandoned shell is an
  explicit administrator action because no verb here can delete a stack.
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
