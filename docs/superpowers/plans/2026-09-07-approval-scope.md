# F5 vertical slice: an approval carries a scope, and a closed scope authorizes nothing

1. Write the failing scope behaviour tests first, against the identifiers the slice will add:
   closure by cancel, by pass release, by expiry; a late answer; never before any responder;
   delegation-depth exactness; digest coverage; a malformed deadline.
2. Add `thymira.policies.approval_scope` -- `ApprovalScope`, `RecordedApprovalScope`,
   `ScopeClosure`/`ScopeClosureReason`, `lifecycle_closure` and `scope_closure`. Only
   `Actor.system()` events produced by `thymira.core` close a scope.
3. Add `ToolContext.delegation_depth`/`approval_ttl`/`now`, then `TicketOutcome`,
   `TicketDisposition` and `ticket_disposition`, which decides `never` before any responder is
   read, then rejection, then scope closure, then the unchanged currency and credit folds.
4. Rewire `ToolManager.execute` onto it: record the scope in the Gate's request details, deny
   outright on a run-terminal closure, and write `ticket_outcome`/`closed_scope_sha256` on
   `tool.denied` and `approval_scope_sha256`/`delegation_depth` on `tool.started`. Run
   `tests/thymira/test_tools.py` in full as the regression gate for the reordering.
5. Drop a pending approval the Run lifecycle closed, so `LocalApprovalService.resolve` and the
   governance routes refuse a stale answer. Expiry stays out of that fold on purpose.
6. Give MIRA the independent half: `ApprovalScopeLedger` recomputes each scope digest from the
   request's own four fields and folds Core's transitions through `trusted_lifecycle_command`,
   and A3 requires the credit to have been open when the start ran.
7. Give caller abort a production surface: `RunService.cancel` through `RunController`,
   `POST /runs/{id}/cancel`, `thymira cancel`. Narrow the delegate's tool context to its own
   task and depth -- the named consumer for `delegation_depth`.
8. Prove the two untested paths end to end: a resume proposing materially different arguments,
   and a cancelled parked Run read back through a freshly opened `events.jsonl`.
9. Run `just check`, write the Agent Note and the dated closure-record entry.

The [DSH acceptance record](2026-09-07-dsh-acceptance.md) governs foundation completion.
This slice does not complete client disconnect, within-pass cross-task scope release, or live
root-agent/user authority for human questions. F5.1, F5.2 and F5.4 stay open.
