# 09d: TerminusDB Spike Results

> Executes the `09c-terminus-db-spike-brief.md` work order from the `260803-pearls` docs
> package. Harness code and raw verbatim evidence live alongside this file, under `evidence/`
> and `harness/` in this directory.

## Environment as actually run

```
docker run -d --name tdb -p 6363:6363 terminusdb/terminusdb-server:v12.0.4
```

`GET /api/info` confirmed:

```json
{"@type":"api:InfoResponse","api:info":{"authority":"terminusdb://system/data/User/admin","storage":{"version":"2"},"terminusdb":{"git_hash":"bca013b5fd150734f61e7d0ea98b38c3b2f3f52e","version":"12.0.4"},"terminusdb_store":{"version":"0.19.8"}},"api:status":"api:success"}
```

Requested version (v12.0.4) was available and used as-is — no substitution needed.

Schema: one class `Product` (`name`: string, `price`: decimal, `stock`: integer), database `admin/spike`.
Fixture: `Product/Widget` created with `price=9.99`, `stock=5`. That commit is `C0` =
`jomeio4x95rlw5e2nuaawgp1qepuvwv`. Branch `feature` created from `C0`.

---

### E1 — Merge semantics and merge base

**Verdict: CONTRADICTED (docs' own example is unsafe) + CONFIRMED (variant (a) is a genuine three-way merge)**

**Observed.** `POST /api/apply` performs a real three-way merge **only when `before_commit` is the
true common ancestor** (variant a). Given the true ancestor, non-overlapping changes compose
correctly (Trial A) and same-field changes are correctly detected as conflicts and refused
(Trial B). But variant (b) — `before_commit` set to the target branch's own tip, exactly as the
documentation's own example does — is not a "no-op-if-safe" fallback. It **silently discarded
main's own change in both trials**: in Trial A (non-overlapping fields) it reverted main's
`stock` change back to the old value while applying feature's `price` change; in Trial B
(same field) it **silently overwrote main's conflicting `price` value with feature's, with zero
conflict signal**. Variant (c) (omitting `before_commit`) is not possible — the API rejects the
request outright with a 400 `RequiredFieldsMissing` in every case, so the "opt-in required
field" risk does not exist for this parameter specifically.

The conflict witness shape actually observed also does **not** match what the brief hypothesized
from the docs. It is `{"@id":..., "<field>": {"@expected":X, "@found":Y, "@op":"Conflict"}}` per
conflicting field — not `{"@before":..., "@after_left":..., "@after_right":...}`.

**Raw evidence.**

Trial A (non-overlapping: main sets `stock` 5→7, feature sets `price` 9.99→14.99), full requests/responses:
`evidence/E1/trial_A_a.txt`, `trial_A_b.txt`, `trial_A_c.txt`.

- Variant a (`before_commit=C0`): `POST /api/apply/admin/spike/local/branch/main` →
  `{"@type":"api:ApplyResponse","api:status":"api:success"}` (HTTP 200). Read-back of main:
  `{"price":14.99,"stock":7}` — **both changes composed correctly.**
- Variant b (`before_commit`=main's own tip): same 200 success —
  `{"@type":"api:ApplyResponse","api:status":"api:success"}`. Read-back of main:
  `{"price":14.99,"stock":5}` — **main's `stock=7` change is silently lost**, reverted to 5.
- Variant c (`before_commit` omitted): HTTP 400 —
  `{"@type":"api:BadAPIDocumentErrorResponse","api:error":{"@type":"api:RequiredFieldsMissing",...,"api:expected":["before_commit","after_commit","commit_info","type"]}}`.
  Read-back of main unchanged (`price=9.99,stock=7`, main's own local edit only).

Trial B (same field: main sets `price` 9.99→12.99, feature sets `price` 9.99→14.99), full requests/responses:
`evidence/E1/trial_B_a.txt`, `trial_B_b.txt`, `trial_B_c.txt`.

- Variant a (`before_commit=C0`): HTTP **409** —
  `{"@type":"api:ApplyError","api:status":"api:conflict","api:witnesses":[{"@id":"Product/Widget","price":{"@expected":9.99,"@found":12.99,"@op":"Conflict"}}]}`.
  Read-back of main: `price=12.99` (main's own value) — **correctly refused, main's change survives untouched.**
- Variant b (`before_commit`=main's own tip): HTTP 200 success, **no conflict raised at all.**
  Read-back of main: `price=14.99` — **main's `price=12.99` change silently clobbered by feature's value.**
- Variant c (omitted): HTTP 400, same `RequiredFieldsMissing` as Trial A. Read-back unchanged (`price=12.99`).

**Design consequence.** The transaction service **must always compute the true common ancestor**
and pass it as `before_commit`; it must never take a branch tip as a shortcut, even for
"probably safe" non-overlapping cases — variant (b) proved unsafe in both the overlapping and
non-overlapping trial. **Flag prominently: the documented example pattern (tip-as-before_commit)
must never be copied into the service** — it is a silent data-loss bug waiting to happen, not a
convenience. Also record that the conflict witness schema is keyed by field with `@expected`/
`@found`/`@op`, not `@before`/`@after_left`/`@after_right` — any conflict-rendering code should be
built against the observed shape, not the docs' phrasing.

---

### E2 — Conditional commit (compare-and-swap)

**Verdict: CONFIRMED (CAS is real) + CONFIRMED (it is opt-in / unsafe by default)**

**Observed.** The `TerminusDB-Data-Version` header is a genuine CAS token. A write with the
current version succeeds and returns the new version. A write with a stale version is rejected
with a body that names the *actual* current version. But sending **no** header at all succeeds
unconditionally — TerminusDB does not enforce CAS unless the caller opts in on every request.

**Raw evidence.** `evidence/E2/step1.txt`, `step2.txt`, `step3.txt`.

- `GET` document: header `Terminusdb-Data-Version: branch:jomeio4x95rlw5e2nuaawgp1qepuvwv` (=`V0`).
- Step 1 — `PUT` with header `V0`: HTTP 200, response header
  `Terminusdb-Data-Version: branch:cn2a3wyayvusfholgui8fsermlehe9a` (=`V1`).
- Step 2 — `PUT` again with now-stale `V0`: HTTP **400** —
  `{"@type":"api:ReplaceDocumentErrorResponse","api:error":{"@type":"api:DataVersionMismatch","api:actual_data_version":"branch:cn2a3wyayvusfholgui8fsermlehe9a","api:requested_data_version":"branch:jomeio4x95rlw5e2nuaawgp1qepuvwv"},"api:message":"Requested data version in header does not match actual data version."}`.
  The rejection body **does** identify the current head (`api:actual_data_version`).
- Step 3 — `PUT` with **no** `TerminusDB-Data-Version` header at all: HTTP 200, succeeds
  unconditionally, returns a new version. Read-back confirms the write took effect
  (`price=15`), overwriting whatever was there with no version check performed.

**Design consequence.** CAS is real and its rejection body is directly usable (it echoes the
correct current version, so a client can retry without a separate read). But it is **opt-in**:
the service must send `TerminusDB-Data-Version` on every single document write, with no
exceptions, or it silently degrades to last-writer-wins. This should be enforced centrally (e.g.
a single write-path wrapper), not left to call sites to remember individually.

---

### E3 — Distinguishability of the two failure modes

**Verdict: CONFIRMED — the two failure modes are mechanically distinguishable on multiple axes**

**Observed.** Comparing the E1 Trial B variant (a) conflict against the E2 step 2 stale-write
rejection:

| | E1 conflict (merge) | E2 stale write (CAS) |
|---|---|---|
| HTTP status | 409 | 400 |
| `@type` | `api:ApplyError` | `api:ReplaceDocumentErrorResponse` |
| `api:status` | `api:conflict` | (absent; error nested under `api:error`) |
| error shape | `api:witnesses` array, per-field `@expected`/`@found`/`@op` | `api:error.@type = api:DataVersionMismatch`, single `api:actual_data_version` |

These differ on HTTP status code, top-level `@type`, and payload shape — a caller can
mechanically distinguish them with a single `switch` on HTTP status (409 vs 400) alone, and the
`@type`/`@error.@type` fields give a second, redundant signal.

**Design consequence.** The service's public interface should expose **two distinct error
branches** (merge-conflict vs stale-base), not a collapsed generic "write failed" — the
information needed to tell them apart is present and stable across both failure modes, so
collapsing them would be a pure loss with no simplification benefit.

---

### E4 — Which action gates `reset` and `squash`?

**Verdict: CONTRADICTED for `squash` (cannot be withheld) / CONFIRMED for `reset` (can be withheld) — mixed, blocking finding**

**Observed.** A custom role holding only `instance_read_access, instance_write_access,
commit_write_access, schema_read_access, class_frame, branch` was granted to a fresh user on the
`spike` database and probed against 11 actions:

| Action | Result |
|---|---|
| document read | 200 success |
| document write | 200 success |
| branch create | 200 success |
| `/api/diff` | 200 success |
| `/api/log` | **403** — requires `meta_read_access` (not granted) |
| `/api/apply` | 200 success |
| `/api/squash` | **200 success — not gated by any permission beyond what this role already has** |
| `/api/reset` | **403** — requires `commit_read_access` (not granted) |
| `/api/rebase` | 403 — requires `commit_read_access` |
| `/api/capabilities` (grant) | 403 — requires `manage_capabilities` |
| database delete | 403 — requires `create_database` at the org |

**Raw evidence.** `evidence/E4/04-probe-actions.txt` (full requests/responses),
role/user/grant setup in `01-create-role.txt`, `02-create-user.txt`, `03-grant-capability-attempt2.txt`.
Notable: the first grant attempt (`03-grant-capability-attempt1.txt`) failed with a raw
`api:message":"Unexpected failure in request handler"` (HTTP 500) when `scope`/`user`/`roles`
were passed as plain org/db path strings — the endpoint requires full `terminusdb://system/data/...`
URIs, not the `org/db` shorthand used elsewhere in the API. That inconsistency is itself worth
flagging (see "documentation got wrong" below).

**This is a possible negative finding, and it applies asymmetrically:**
- `reset` **can** be withheld — it requires `commit_read_access`, which is a separately
  grantable permission not implied by ordinary write access.
- `squash` **cannot** be withheld from anyone who already has ordinary write access
  (`commit_write_access` + the instance perms) — it succeeded under a role that was deliberately
  built to be minimal. There is no separate permission gating `/api/squash` that isn't already
  required for normal writes.

**Design consequence.** The mitigation "withhold `reset`/`squash` via a restricted role" **only
half-works**. `reset` can genuinely be kept out of a caller's hands by omitting
`commit_read_access` from their role (note: this may have side effects on other legitimate
read operations that also need that permission — worth checking before relying on it). `squash`
cannot be withheld this way at all; any caller who can write documents can squash. If squash-based
branch-pointer collapsing is a real risk, it must be prevented some other way — e.g. the service
never exposes a squash operation in its own API surface regardless of what the underlying engine
permission model allows, since the engine will not enforce that boundary for us.

---

### E5 — Is capability inheritance strictly additive?

**Verdict: CONFIRMED**

**Observed.** A user was granted the full write-capable `Admin Role` at **organization** level
(`terminusdb://system/data/Organization/admin`), then additionally granted the read-only
`Consumer Role` at **database** level on one database in that org. The database-level grant did
**not** narrow the organization-level grant: the user's write to that database succeeded anyway.

**Raw evidence.** `evidence/E5/02-grants.txt` (both grants, both HTTP 200) and
`03-write-attempt.txt`:
```
PUT /api/document/admin/spike/local/branch/main?author=e5_user&message=e5-write-attempt
→ ["terminusdb:///data/Product/Widget"]  HTTP 200
```
Read-back as admin confirms the write took effect (`price=20`).

**Design consequence.** Confirmed: database-level grants are additive on top of
organization-level grants, never subtractive. Narrowing/attenuating a delegation (e.g. "this
person can write org-wide except in this one database") is **not possible inside the engine** —
it must be implemented entirely in the transaction service's own authorization layer, which
cannot rely on TerminusDB's native capability system for anything beyond coarse allow-lists.

---

### E6 — Does reset orphan history, and is the commit still readable?

**Verdict: CONFIRMED, with a caveat about background GC**

**Observed.** Three commits were made on a branch (`C1`, `C2`, `C3`), then the branch was reset
to `C1`. `GET /api/log` afterward shows only `C1` and its ancestors — `C2` and `C3` are gone from
the branch's history view. But both discarded commits remain **fully readable** by
content-addressed commit ID (`GET /api/document/.../local/commit/{id}`), returning their exact
document state.

**Raw evidence.** `evidence/E6/01-reset-and-checks.txt`:
- Log before reset: 9 entries, tip = `C3` (`u6xyi90x4da0fwdpvgw3j2eu05zu6r0`).
- `POST /api/reset` to `C1` (`r9guzsnfn0rmab2b37ep6et2pwl8u9r`): `{"@type":"api:ResetResponse","api:status":"api:success"}`.
- Log after reset: tip is now `C1`; `C2` and `C3` no longer appear anywhere in the log.
- `GET .../local/commit/{C3}?id=Product/Widget`: HTTP 200, `{"price":23,"stock":5}` — C3's exact state, still readable.
- `GET .../local/commit/{C2}?id=Product/Widget`: HTTP 200, `{"price":22,"stock":5}` — same.

**Design consequence.** Pinning attestations to commit IDs (rather than branch names) is safe
against a `reset` **in the immediate term** — reset only moves the branch pointer, it does not
delete the underlying commit objects. However, the server logs at startup show
`auto-optimize plugin loaded: probabilistic GC (1%) and optimization (10%) enabled` — a
background garbage-collection process is on by default. This spike did not run long enough to
observe GC reaping an orphaned commit, so **long-term readability of a reset-orphaned commit is
not proven** — only its immediate post-reset readability is. If attestations must survive
indefinitely, the design should not assume orphaned commits are permanent without separately
verifying (or disabling) the probabilistic GC behavior.

---

### E7 — Commit metadata extensibility

**Verdict: CONTRADICTED (docs implied broader flexibility than observed) — narrow to `author`/`message` only**

**Observed.** A real (non-no-op) `apply` was issued with `commit_info` containing `author`,
`message`, plus three extra keys: `scene_id`, `invocation_id`, `idempotency_key`. The resulting
commit, inspected via `/api/log`, contains only `author`, `message` (plus the engine's own
`identifier`, `parent`, `timestamp`, `user`, `instance`, `schema` fields) — **the three extra keys
are silently dropped**, not rejected and not persisted.

**Raw evidence.** `evidence/E7/01-real-apply.txt`. Request:
```json
{"before_commit":"r9guzsnfn0rmab2b37ep6et2pwl8u9r","after_commit":"lo4rhvkakng38khtk453680hhs17tvt",
 "commit_info":{"author":"spike","message":"e7-provenance-real","scene_id":"scene-42","invocation_id":"inv-abc123","idempotency_key":"idem-xyz789"},
 "type":"instance"}
```
Resulting commit in `/api/log`:
```json
{"@id":"ValidCommit/2j7rbzbud1ijh86eqtzp8s3ejxz09cr","author":"spike","message":"e7-provenance-real","parent":"ValidCommit/r9guzsnfn0rmab2b37ep6et2pwl8u9r", ...}
```
No `scene_id`, `invocation_id`, or `idempotency_key` anywhere in the object — silently dropped,
no error, no warning, HTTP 200 throughout.

**Design consequence.** Provenance fields beyond `author`/`message` **cannot** bind to the commit
itself. They must be written as companion documents (or a dedicated provenance class instance)
in the same transaction as the data change, and correlated back to the commit ID after the fact
— the commit object itself is not an extensible metadata carrier.

---

### E8 — Native replay protection

**Verdict: CONFIRMED (as expected) — with a nuance on *why***

**Observed.** Two byte-identical `PUT` requests were issued back-to-back. Only **one** new
commit appears in `/api/log` afterward.

**Raw evidence.** `evidence/E8/01-replay.txt`. Both writes returned HTTP 200 with
identical bodies; `/api/log?count=3` shows only one new commit (`ymmg1yojm1ghe983vfxauprorknq8vc`,
message `e8-replay-test`) at the tip, not two.

**Design consequence — nuance worth flagging.** This is **not** a dedicated idempotency/replay
guard. TerminusDB's document write is diff-based against current state: if the target document
state you `PUT` is identical to what's already there, there is nothing to diff, so no commit is
produced — this is a side effect of the storage model, not an intentional safety feature keyed
on request identity. If the transaction service's writes express *deltas* (e.g. "increment stock
by 1") rather than *full target states*, this protection **would not apply** — replaying a delta
twice would apply it twice. Idempotency (in the sense of "this exact operation, if retried, has
no additional effect") remains entirely the service's responsibility, as expected, and this
distinction matters for how the service's write API is shaped.

---

### F1 — Fork readiness

**Verdict: CONFIRMED (buildable, with two undocumented prerequisites)**

**Observed.** Cloned `github.com/terminusdb/terminusdb` (`main` @ `a08d9ff2ad981ba7f7d99620d134ef598a36fc3f`,
2026-08-04) and built with `make dev` (the documented macOS-friendly target). Two prerequisites
were required that are **not mentioned anywhere in `GETTING_STARTED.md` or `docs/CONTRIBUTING.md`**:

1. **`protoc` (the Protocol Buffers compiler).** Without it, `cargo build` fails deep inside a
   transitive dependency (`terminusdb-grpc-labelstore-proto`'s build script) with
   `Could not find protoc installation`. Fixed with `brew install protobuf`.
2. **The SWI-Prolog `tus` pack must be fetched via `make install-deps` before `make dev`.**
   Without it, the Prolog bootstrap fails with `source_sink 'library(tus)' does not exist`. This
   step *is* documented as a Makefile target, but neither `GETTING_STARTED.md` nor
   `CONTRIBUTING.md`'s "Building on macOS" section mentions it needs to run before `make dev` —
   it's easy to miss since `make dev`'s own error message doesn't point at it.

**Toolchain versions used:**
- `rustc 1.92.0`, `cargo 1.92.0` (pre-installed)
- `swi-prolog 10.0.2` (installed via `brew install swi-prolog`)
- `protobuf 35.1` / `libprotoc 35.1` (installed via `brew install protobuf`)

**Raw evidence.** `evidence/F1/`:
- `build.log` — first attempt, fails on missing `protoc` (`101.13s user / 65.54s system`, ~1:57 wall).
- `install-deps.log` — `make install-deps` fetching the `tus` pack (fast, git clone + swipl pack install).
- `build2.log` — second attempt, Rust layer now compiles clean in `26.44s`, but Prolog bootstrap fails on missing `tus` pack.
- `build3.log` — third attempt, full success: `Development binary 'terminusdb' created successfully` (`~3.1s wall`, mostly cached from build2's Rust compile).
- `store-init.log`, `serve.log`, `api-info.txt` — binary started against fresh storage on port 6364 and answered:
  ```json
  {"@type":"api:InfoResponse","api:info":{"terminusdb":{"git_hash":"a08d9ff2ad981ba7f7d99620d134ef598a36fc3f","version":"12.0.6"}, ...},"api:status":"api:success"}
  ```
  Git hash matches the checked-out commit exactly, confirming the binary was actually built from
  this source tree (not a stale/cached one).

Total elapsed time for this exercise end-to-end (installing swi-prolog and protobuf via
Homebrew, cloning, hitting and fixing both undocumented prerequisites, and a clean successful
build): **under 6 minutes** in this environment, given `rustc`/`cargo` were already present. A
truly cold environment would additionally need a Rust toolchain install.

Runnable procedure: `harness/f1_fork_build.sh` (idempotent — installs nothing that
isn't already present, and documents both undocumented steps inline as comments).

**Design consequence.** A fork is buildable and maintainable if upstream support lapses, on a
timescale of minutes rather than days, given a documented list of prerequisites (which this
spike now provides and upstream's docs currently omit). Version note: `main` is currently at
`12.0.6`, two patch versions ahead of the `v12.0.4` docker image used for E1–E8 — worth pinning
explicitly when standing up a fork-tracking process, since "latest main" and "the tagged release
we tested against" are not the same commit.

---

## 1. Blocking findings

- **E1, most severe:** the documented example for `POST /api/apply` (`before_commit` = branch
  tip) silently discards concurrent changes in *both* the non-overlapping and same-field trials.
  **This pattern must never be copied into the service.** The service must always compute the
  true common ancestor.
- **E4:** `/api/squash` cannot be withheld from any caller who has ordinary document-write
  access — there is no independent permission gating it. The mitigation of using a restricted
  role to prevent branch-pointer collapsing **does not work for squash** (though it does work
  for `reset`, which requires the separately-grantable `commit_read_access`). If squash-based
  history collapsing is a real threat, it must be blocked by never exposing a squash capability
  in the service's own API, not by relying on TerminusDB's role system.

## 2. Confirmed adapter obligations — what the service must implement because the engine won't

- Compute and pass the true merge-base (common ancestor) on every `/api/apply` call (E1).
- Send `TerminusDB-Data-Version` on every single document write, unconditionally — the engine's
  CAS is opt-in and silently absent otherwise (E2).
- Implement two distinct error-handling branches for merge-conflict (409, `api:conflict`,
  `api:witnesses`) vs. stale-base (400, `api:DataVersionMismatch`) — the engine gives clean
  signals for both, so the service should preserve that distinction rather than collapsing it (E3).
- Never expose `/api/squash` (or equivalent branch-collapsing capability) to callers directly,
  regardless of their granted role — it cannot be gated at the engine level (E4).
- Implement attenuation (narrowing a delegated grant) entirely in the service's own
  authorization layer — the engine's capability inheritance is strictly additive and cannot
  express "less than the parent grant" (E5).
- Do not assume reset-orphaned commits are permanent; if attestations must survive
  indefinitely, verify or disable the engine's background probabilistic GC rather than relying
  on default behavior (E6).
- Persist any provenance beyond `author`/`message` (scene IDs, invocation IDs, idempotency
  keys) in companion documents/transactions, not in `commit_info` — the engine silently drops
  unrecognized commit metadata keys (E7).
- Implement idempotency at the service layer for any operation expressed as a delta rather than
  a full target state — the engine's apparent "replay protection" is an artifact of diff-based
  writes on identical target states, not a real request-identity guard (E8).

## 3. Anything the documentation got wrong

- **`POST /api/apply`'s own example** sets `before_commit` to a branch tip rather than the
  common ancestor — and following it silently loses data (E1). This is the most consequential
  documentation error found in this spike.
- The conflict witness schema is `{"@id", "<field>": {"@expected","@found","@op":"Conflict"}}`,
  not the `@before`/`@after_left`/`@after_right` shape one would infer from the docs' general
  description of three-way merge witnesses (E1).
- `/api/capabilities`'s `scope`/`user`/`roles` fields require full `terminusdb://system/data/...`
  resource URIs. Passing the `org/db`-style shorthand used by most other endpoints in this API
  (branch, document, apply, reset, etc.) does not produce a helpful validation error — it
  produces a bare HTTP 500 `"Unexpected failure in request handler"` (E4).
- Neither `GETTING_STARTED.md` nor `docs/CONTRIBUTING.md`'s macOS build instructions mention
  that `protoc` must be installed, or that `make install-deps` must run before `make dev` to
  fetch the `tus` Prolog pack — both are required for a clean build from a fresh clone (F1).

## Out of scope (per brief, not attempted)

Performance benchmarking, commit-rate behavior at scale, multi-tenancy, deletion/erasure, and
any work on the transaction service itself.
