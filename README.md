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

This repository's `docs/` white paper distinguishes three levels of
maturity:

1. **Available primitives** — existing AWS services, Quilt Package
   semantics, the public RAJA prototype, and documented TerminusDB
   capabilities.
2. **Reference integration** — the proposed composition of those
   primitives into Scene-based execution isolation.
3. **Validation frontier** — mechanisms that still need implementation and
   stress testing (deterministic Scene sealing, output-package creation
   semantics, domain-aware reconciliation).

This is an architecture proposal, not a claim that the complete system is
production-ready. See *The Pearls Architecture: AWS White Paper* for the
full paper, including the implementation roadmap (Phases 0–5) and open
technical questions.
