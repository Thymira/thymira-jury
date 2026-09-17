import { h } from "../../dom.js";
import { clip, timestamp } from "../../format.js";
import { foldApprovals } from "../../fold.js";
import { TERMINAL_STATUSES } from "../../status.js";
import { codeBlock, copyable, empty, kv, mono, panel, table, tag } from "../../ui.js";

export function renderApprovals(ctx) {
  const { pending, resolved, parkedDecisionId, focus } = foldApprovals(ctx.events);
  const waiting = ctx.run.status === "WAITING_FOR_APPROVAL";
  const ended = TERMINAL_STATUSES.has(ctx.run.status);
  return h(
    "div",
    { class: "stack" },
    panel(
      `${ended ? "Never answered" : "Pending review"} (${pending.length})`,
      pending.length
        ? h(
            "div",
            { class: "cards" },
            [...pending].reverse().map((request) =>
              reviewCard(ctx, request, {
                answerable: waiting && focus?.decisionId === request.decisionId,
                parked: waiting && request.decisionId === parkedDecisionId,
                waiting,
                ended,
              }),
            ),
          )
        : empty(ended ? "Every review this run raised was answered." : "Nothing is waiting for a human answer."),
      { aside: h("span", { class: "muted small" }, "an approval authorizes exactly one call, once") },
    ),
    panel(
      `Answered (${resolved.length})`,
      table(
        [
          { label: "Answered", render: (request) => timestamp(request.resolution.ts) },
          {
            label: "Answer",
            render: (request) => [
              tag(request.resolution.approved ? "APPROVED" : "REJECTED", request.resolution.approved ? "ok" : "block"),
              request.resolution.automatic ? [" ", tag("AUTOMATIC")] : null,
            ],
          },
          { label: "Request", render: (request) => clip(request.summary || request.reason, 100) },
          { label: "Tool", render: (request) => (request.toolCall ? mono(request.toolCall.tool) : h("span", { class: "muted" }, "run")) },
          {
            label: "By",
            render: (request) => mono(`${request.resolution.actor?.kind ?? "?"}/${request.resolution.actor?.id ?? "?"}`),
          },
          { label: "Note", render: (request) => clip(request.resolution.note, 80) || "—" },
        ],
        [...resolved].reverse(),
        { emptyText: "No review has been answered yet.", expand: (request) => detail(request) },
      ),
    ),
  );
}

function detail(request) {
  return h(
    "div",
    { class: "stack-tight" },
    kv([
      ["Decision", copyable(request.decisionId)],
      ["Rule", request.ruleId ? mono(request.ruleId) : null],
      ["Reason", request.reason],
      ["Requested", timestamp(request.ts)],
      ["Note", request.resolution?.note],
    ]),
    request.toolCall ? codeBlock(request.toolCall.arguments ?? {}) : null,
  );
}

function unanswerableHint({ waiting, ended }) {
  if (ended) return "The run ended before this review was answered; it can no longer be answered.";
  if (waiting) return "The run is parked on a different review; answer that one first.";
  return "The run is not waiting on this review right now.";
}

function reviewCard(ctx, request, { answerable, parked, waiting, ended }) {
  const note = h("textarea", { class: "input", rows: 2, placeholder: "Note recorded with your answer (optional)" });
  const status = h("span", { class: "form-status", role: "status" });
  const approve = h("button", { class: "btn btn-approve", type: "button" }, request.toolCall ? "Approve this call" : "Approve");
  const reject = h("button", { class: "btn btn-danger", type: "button" }, "Reject");
  const setBusy = (busy) => {
    approve.disabled = busy;
    reject.disabled = busy;
    note.disabled = busy;
  };
  const decide = (approved) => async () => {
    setBusy(true);
    status.textContent = approved ? "Recording the approval; the run continues inside this request…" : "Recording the rejection…";
    try {
      const text = note.value.trim();
      const run = approved ? await ctx.api.approve(ctx.runId, text) : await ctx.api.reject(ctx.runId, text);
      note.value = "";
      status.textContent = approved ? "Approved." : "Rejected.";
      await ctx.onRunMutated(run);
    } catch (error) {
      status.textContent = `${error?.code ?? "error"}: ${error?.message ?? error}`;
      setBusy(false);
    }
  };
  approve.addEventListener("click", decide(true));
  reject.addEventListener("click", decide(false));
  return h(
    "article",
    { class: ["card", answerable ? "is-answerable" : null] },
    h("header", { class: "card-head" }, h("strong", null, request.summary || "Review requested"), parked ? tag("RUN PARKED HERE", "review") : null),
    kv([
      ["Reason", request.reason],
      ["Rule", request.ruleId ? mono(request.ruleId) : null],
      ["Decision", copyable(request.decisionId)],
      ["Requested", timestamp(request.ts)],
      request.sandboxMode ? ["Sandbox", mono(request.sandboxMode)] : null,
      request.expiresAt ? ["Expires", timestamp(request.expiresAt)] : null,
    ]),
    request.toolCall
      ? h(
          "div",
          { class: "stack-tight" },
          h("span", { class: "muted small" }, "Tool ", mono(request.toolCall.tool), " with these exact arguments"),
          codeBlock(request.toolCall.arguments ?? {}),
        )
      : null,
    answerable
      ? h("div", { class: "decide" }, note, h("div", { class: "form-actions" }, status, reject, approve))
      : h("p", { class: "muted small" }, unanswerableHint({ waiting, ended })),
  );
}
