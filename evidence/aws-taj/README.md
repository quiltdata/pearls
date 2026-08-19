# AWS-native TAJ proof evidence

This directory is the destination for sanitized transcripts produced by
`tests/aws/test_authorization.py` against an administrator-owned AWS sandbox.
The live proof is intentionally absent from normal CI: mocked AWS responses do
not prove IAM session-policy intersection.

Before preserving a transcript:

1. Use dedicated, non-production, versioned S3 buckets, the
   `scene-execution` role from `infra/scene-execution-role.yaml`, and a separate
   sandbox cleanup role restricted to version deletion for the disposable proof
   resources. The Scene role must not receive delete authority.
2. Confirm that the denied read and wrong-version read point to real objects
   readable by the unrestricted base role. Configure the cross-object read with
   the admitted version ID from the allowed read but a different selected key;
   S3 must return `NoSuchVersion`. Seed readable objects at the pinned input
   input manifest key, an output `.quilt/workflows/*` key, and the exact output
   `.quilt/named_packages/<name>/latest` pointer. Both write URIs must be
   disposable; the allowed URI must be under the output data prefix and the
   denied URI outside all three output namespaces. The harness removes each
   created proof version with the cleanup role in a `finally` block.
3. Set `RAJA_STS_TRANSCRIPT` to
   `evidence/aws-taj/aws-proof-transcript.jsonl` and run the command documented
   in the root README.
4. Inspect every JSONL field before committing. The harness omits credentials
   and object bodies, but account IDs, role ARNs, package names, object version
   IDs, and AWS request IDs may still be sensitive in your environment.
5. Record the AWS account alias or test-environment identifier, region,
   bucket-versioning state, base-role stack revision, package top hash, and
   test command in the pull request that adds the evidence.

A valid transcript contains successful unrestricted-baseline reads/writes,
followed by these scoped outcomes:

- `AssumeRole:baseline`: `allowed`
- `AssumeRole:cleanup`: `allowed` with an ARN distinct from the Scene role
- `Baseline:DeleteObjectVersion:anywhere`: `denied` with `AccessDenied`
- `AssumeRole:scoped`: `allowed`
- `GetObject:in-scope`: `allowed`
- `GetObject:pinned-manifest`: `allowed`
- `GetObject:output-workflow`: `allowed`
- `GetObject:output-latest`: `allowed`
- `GetObject:out-of-scope`: `denied` with `AccessDenied`
- `GetObject:wrong-version`: `denied` with `AccessDenied`
- `GetObject:admitted-version-wrong-object`: `not-found` with `NoSuchVersion`
- `GetObject:selected-current-version`: `denied` with `AccessDenied`
- sibling manifest/package and non-`latest` output-pointer reads: `denied`
- three `PutObject:in-scope:*` records for data, immutable-manifest, and named-pointer namespaces
- `PutObject:outside-output`: `denied` with `AccessDenied`
- `ListBucket:anywhere`: `denied` with `AccessDenied`
- `DeleteObject:anywhere`: `denied` with `AccessDenied`
- `DeleteObjectVersion:anywhere`: `denied` with `AccessDenied`
- `Cleanup:DeleteObjectVersion`: `allowed` for every created proof version

Do not hand-edit service outcomes or fabricate a transcript. Re-run the proof
when the compiler, base role, bucket controls, or AWS test environment changes.
