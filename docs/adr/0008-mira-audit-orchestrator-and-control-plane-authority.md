# ADR-0008 — MIRA as the Audit Orchestrator and control-plane authority boundaries

- **Status:** Accepted — 2026-08-23
- **Deciders:** project owner; Runtime (P1); MIRA / Governance (P4)
- **Related:** ADR-0001, ADR-0004, `docs/contracts/contract-v0.1.md`,
  `docs/architecture/e2e-baseline-v2.md`, `docs/roadmap/product-final.md`

## Context

Thymira is evolving from independently testable runtime components into a multi-agent system
whose decisions and effects must be explainable after the fact. THY performs data-science work;
MIRA evaluates the evidence created by that work. The existing architecture already requires an
LLM to propose and deterministic code to authorize, requires MIRA to read evidence rather than
modify the workspace, and keeps THY and MIRA independent. Those rules need a sharper operational
boundary before persistent runs, approvals, and audit agents are assembled.

Without an explicit control-plane boundary, several unsafe shortcuts become tempting: an audit
agent could mark a run blocked directly, a finding could be interpreted as an authorization, a
policy implementation could mutate a Run, or THY and MIRA could become coupled through direct
calls. Each shortcut obscures who made an effect happen and makes replay, approval, recovery,
and independent audit weaker.

The distinction is especially important for MIRA. It must be able to observe a run, emit
evidence-backed findings, and ask for an intervention, but it must not itself become an
unreviewable authority. The persistent Run also needs a single writer so concurrent graph nodes,
clients, and approval handlers cannot create incompatible lifecycle states.

## Decision

1. **MIRA is the only Audit Orchestrator.** All audit-agent coordination, audit preparation,
   evidence evaluation, and aggregation of audit results belong to MIRA. Other components may
   expose audit data or implement deterministic controls, but they do not orchestrate audit
   agents or issue an audit verdict under a different authority.
2. **MIRA is an observer and proposer.** MIRA reads recorded events, artifact metadata, policy
   snapshots, and other declared evidence. It produces `AuditFinding` records and may emit an
   `ActionIntent` that requests a named control-plane action, such as pausing a run, requesting
   approval, reopening work, or reviewing findings. A request is not an authorization and has no
   direct effect on a Run, tool, workspace, artifact, or external system.
3. **MIRA never authorizes or directly changes state.** MIRA and every audit agent are forbidden
   from deciding that an action may execute, applying a policy decision, writing a persistent Run
   transition, invoking an execution tool, or modifying the workspace. A finding, confidence,
   recommendation, or model output is evidence only.
4. **The Policy Engine and Gate are the sole authority for effects.** The deterministic Policy
   Engine evaluates facts, policy rules, and findings. The Gate records the resulting
   authorization and, where required, manages the separate human-approval flow. Only an
   authorization issued by this path can permit an effect. A human approval resolves a required
   review; it does not bypass the Policy Engine or turn an arbitrary MIRA request into authority.
5. **RunController is the only persistent transition writer.** A future RunController in
   `thymira.core` validates an authorization context and applies the allowed transition. For the
   MVP, the local JSON/JSONL evidence and reconstruction rules in ADR-0010 apply; no graph,
   agent, API adapter, policy rule, or approval handler writes Run lifecycle state directly.
6. **THY and MIRA compose only in `thymira.core`.** They neither import nor invoke each other.
   `thymira.core` owns their ordering and their exchange through shared contracts and persisted
   evidence. Their common dependencies remain `thymira.schemas` and `thymira.events`; any
   cross-orchestrator data is represented as a contract or evidence, never as an internal call.

The intended authority path is:

```text
THY or MIRA proposal / finding
            ↓
Policy Engine (deterministic evaluation)
            ↓
Gate (decision record and, if needed, approval)
            ↓
RunController (authorized persistent transition and event)
```

## Alternatives considered

### Let MIRA block runs or call tools directly

Rejected. It makes an LLM-led audit path an authority, combines observation with execution, and
leaves no single place to prove whether an effect was policy-authorized. A fast emergency stop
remains possible only as a requested action evaluated through the Policy Engine and applied by
RunController.

### Let the Policy Engine mutate Run state

Rejected. The engine should remain a deterministic decision function over facts and policy. If it
also persists transitions, evaluation, authorization recording, and lifecycle mutation become
one hard-to-test responsibility and transaction semantics become implicit.

### Let each graph node or API endpoint update the Run projection

Rejected. Multiple writers invite lost updates, divergent transition checks, and paths that omit
an event or authorization record. It would make replay and audit dependent on knowing every
writer.

### Compose THY and MIRA through direct imports or messages

Rejected. Direct coupling violates their independence, makes it easier for MIRA to influence THY
without a policy decision, and prevents independent replacement or replay. Core composition and
persisted contracts keep the boundary inspectable.

### Introduce a separate control-plane service now

Deferred. A separately deployed service may be warranted for distributed execution, but it would
add operational failure modes before the single-deployment MVP proves the authority model.
RunController is the control-plane boundary now; its deployment location is not fixed by this
ADR.

## Consequences

### Positive

- Every externally observable effect has one authorization path and one persistent transition
  writer, making audit, replay, and incident investigation tractable.
- MIRA can grow from deterministic controls to multiple audit agents without gaining authority
  accidentally.
- THY and MIRA remain independently testable and replaceable; their composition stays visible in
  the runtime rather than leaking across layers.
- Human approval has a precise role: it resolves a policy-required review and is separately
  evidenced from both the policy decision and the state transition.

### Negative and risks

- The additional intent → authorization → transition path adds latency, data-model complexity,
  and more failure cases than a direct graph mutation. The user experience must explain pending
  requests clearly.
- The transition writer becomes a high-value correctness component. A defect there can halt
  legitimate runs or apply an incorrect transition; it needs strong invariants, recovery tests,
  and explicit enforcement of the MVP's single-writer restriction.
- Emergency response cannot rely on an auditor bypass. Policy rules and the controller must make
  emergency pause/block actions quick enough, or operators may pressure the system toward unsafe
  shortcuts.
- Existing components that currently append events or produce reports directly will need a
  deliberate migration to distinguish evidence production from an authorized state change.
