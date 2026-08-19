# Changelog

All notable changes to this project are documented in this file.

## [0.1.0] - 2026-08-19

### Added

- Added the `raja_sts.compile()` authority compiler for immutable Quilt Package
  revisions and sealed logical read/output scopes.
- Added exact S3 object and version-policy generation, deterministic minified
  JSON, compilation reports, and a fail-closed 2,048-character STS preflight.
- Added an explicit, reportable physical-prefix fallback for callers that
  knowingly accept widened read authority.
- Added `raja_sts.mint()` and `mint_compiled()` for role-session credentials
  whose session name carries the Scene audit identity.
- Added an administrator-applied CloudFormation template for the broad
  `scene-execution` base role.
- Added Python 3.11-3.13 CI, static checks, build validation, 95% minimum
  coverage, and security-boundary tests.
- Added an opt-in live AWS proof for allowed reads and denied sibling reads,
  outside writes, and deletes, with sanitized JSONL evidence capture.
- Added public usage, Scene-role mapping, deployment, evidence, and security
  boundary documentation.

### Security

- Session policies explicitly deny object/version deletion and bucket listing
  everywhere. `NotResource` guardrails prevent resource policies from
  restoring out-of-scope reads or writes, and strict mode denies latest and
  mismatched-version reads on selected keys.
- Input read authority excludes every manifest key not selected by the sealed
  logical scope.
- Output authority is limited to one package data prefix, Quilt's immutable
  manifest namespace, and that package's named-pointer namespace.
- Floating manifests, hash prefixes, unsafe wildcard resources, unknown scope
  fields, unmatched selectors, invalid session identities, and oversized
  policies fail closed.
- Temporary credential secrets are excluded from object representations and
  live proof transcripts.

### Known limitations

- `s3:PutObject` does not prevent overwriting a key; stronger append-only
  behavior needs bucket-level controls or a trusted package finalizer. Quilt
  8's default `Package.push()` needs output-pointer reads and bucket listing,
  so it is intentionally unsupported by the no-list session.
- The 0.1.0 policy compiler supports the commercial AWS partition only.
- Physical-prefix fallback deliberately weakens logical and version isolation
  and therefore requires explicit opt-in.
- SSE-KMS permissions, output-lineage proof, ambient credential isolation,
  CloudTrail S3 data-event provisioning, and automatic package promotion are
  outside the 0.1.0 authority-compilation primitive.
