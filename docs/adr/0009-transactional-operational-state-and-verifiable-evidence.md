# ADR-0009 — Transactional operational state and verifiable evidence

- **Status:** Superseded by ADR-0010 — 2026-08-25
- **Accepted:** 2026-08-23
- **Superseded by:** ADR-0010 (`0010-local-jsonl-operational-state-and-verifiable-evidence.md`)
- **Deciders:** project owner; Runtime (P1); MIRA / Governance (P4)
- **Related:** ADR-0001, ADR-0002, ADR-0008,
  `docs/contracts/contract-v0.1.md`, `docs/architecture/e2e-baseline-v2.md`,
  `docs/roadmap/product-final.md`

## Context

A durable multi-agent Run needs two views of the same facts. Operators need a current, queryable
Run projection for status, recovery, approvals, and scheduling. Auditors need an ordered,
tamper-evident history from which MIRA can recompute conclusions instead of trusting a mutable
status field. The current repository has hash-chained JSONL and local artifact evidence, while
the MVP target architecture names PostgreSQL as runtime state and deliberately places RabbitMQ
and Kafka-class infrastructure after the first vertical slice.

Writing the mutable projection and its corresponding event independently creates a dangerous
split-brain: a status may claim an action happened without evidence, or evidence may claim an
action that the current Run does not reflect. Conversely, treating a JSONL file as the live
database makes approval handling, concurrency, querying, and recovery needlessly fragile.

## Decision

1. **PostgreSQL is the operational source of truth.** It stores the current Run projection,
   authorization and approval records, operational event records, and the data required to resume
   or inspect a run. Artifact bytes and MLflow remain owned by their respective stores; PostgreSQL
   stores their operational references and integrity metadata.
2. **The current Run projection is mutable.** It represents the latest valid operational state
   and may be updated only by RunController. It is not the evidentiary history and must not be
   used as proof that a past action occurred.
3. **Events are append-only and logically immutable.** Once an event is committed, neither its
   envelope nor its payload is changed or deleted through normal runtime behavior. Corrections,
   redactions required by policy, or compensating actions are represented by later events and
   explicit retention procedures, not in-place edits. Database permissions and application
   interfaces must enforce this design; logical immutability is not a claim that a privileged
   database administrator cannot alter storage.
4. **Projection and event commit in one database transaction.** Every authorized persistent Run
   transition writes its new projection and its event(s) in the same PostgreSQL transaction. The
   transaction either commits both or neither. Event sequence allocation, chain linkage, and the
   authorization reference are part of that transaction. Side effects outside PostgreSQL are not
   declared complete merely because an intent was stored; their completion is recorded by a later
   transaction after evidence is available.
5. **Hash-chained JSONL is a verifiable export, not operational state.** A deterministic export
   of committed events forms a portable JSONL evidence package with canonical serialization,
   redaction, per-event hash linkage, and a stated terminal hash. It can be verified independently
   of PostgreSQL and may be regenerated. It is not the write path, lock manager, or recovery
   mechanism for a live run.
6. **Do not introduce RabbitMQ or Kafka yet.** The initial single-deployment implementation uses
   transactional PostgreSQL coordination. The event envelope and `ActionIntent` contract must not
   assume in-process delivery, so a transactional outbox and broker can be introduced later
   without redefining authorizations or evidence.
7. **Run PostgreSQL locally first and make its endpoint configurable.** Local PostgreSQL is the
   initial development and MVP deployment target. Connection and deployment details are
   configuration, not domain logic, so managed PostgreSQL or another compatible deployment can
   replace it without changing contracts or authority boundaries.

## Alternatives considered

### JSONL as the live source of truth

Rejected. JSONL is excellent portable evidence but poor operational coordination: it lacks
transactional updates across the current projection and approvals, safe concurrent writers,
queryability, and reliable resume semantics.

### PostgreSQL projection without persisted events

Rejected. Mutable status alone cannot establish what happened, in what order, under which policy
snapshot, or whether the recorded history was altered. It would defeat MIRA's reconstruction
model.

### Event sourcing with an immutable event store as the only state from day one

Deferred. It offers a strong audit narrative but adds projection rebuilding, migration, replay,
and operational complexity before the MVP needs them. PostgreSQL event records plus an explicit
mutable projection preserve a migration path without committing the first release to a full
event-sourcing framework.

### RabbitMQ or Kafka immediately

Rejected for the MVP. A broker introduces delivery guarantees, duplicate handling, dead-letter
queues, observability, security, and local-operational costs before there are distributed workers
to justify them. PostgreSQL transactions solve the immediate consistency problem; they do not
pretend to solve future asynchronous execution.

### Database-specific domain behavior or a permanently local store

Rejected. PostgreSQL is selected for operational behavior, but connection, credentials,
provisioning, backup, and deployment stay outside the domain contract. This avoids binding the
runtime to one local topology while retaining a concrete MVP baseline.

## Consequences

### Positive

- Status, authorization, and evidence cannot normally diverge for a committed transition.
- MIRA can reconstruct a run from ordered evidence while clients get efficient current-state
  queries.
- JSONL exports provide portable, independently verifiable evidence for review, retention, and
  external handoff.
- The system remains small enough to run locally while preserving an explicit path to an outbox,
  broker, workers, and managed PostgreSQL.

### Negative and risks

- PostgreSQL becomes an availability dependency and a new local-development prerequisite;
  backups, migrations, credentials, upgrades, and disaster recovery need ownership before a
  production claim is made.
- A single transaction cannot atomically include an external tool, artifact store, MLflow, or a
  future broker. The system must handle retries, idempotency, orphaned intents, and compensating
  events; it must never claim exactly-once external execution without evidence.
- Hash chains detect many accidental or unauthorized changes but do not prevent a privileged
  actor from rewriting both the database and a regenerated export. Stronger tamper resistance
  (off-host anchoring, signatures, retention controls, or a WORM store) is future work.
- Mutable projections require concurrency control and migration discipline. A projection bug can
  affect current operations even when the event history remains intact.
- Deferring a broker limits initial throughput, background execution, and independently scalable
  workers. It is a scope decision, not a claim that PostgreSQL polling is a permanent queue.
