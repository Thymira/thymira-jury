import { h } from "../../dom.js";
import { clip, plainQuestion, timestamp } from "../../format.js";
import { foldInterview } from "../../fold.js";
import { TERMINAL_STATUSES } from "../../status.js";
import { empty, errorNotice, loading, mono, notice, panel, table, tag } from "../../ui.js";

const SOURCES = {
  project_context: ["FROM CONTEXT", "active"],
  live_human: ["HUMAN", "review"],
};

export function renderInterview(ctx) {
  const answers = foldInterview(ctx.events);
  return h(
    "div",
    { class: "stack" },
    pending(ctx),
    panel(
      `Recorded answers (${answers.length})`,
      table(
        [
          { label: "Field", render: (row) => mono(row.field) },
          { label: "Question", render: (row) => clip(plainQuestion(row.question), 110) },
          { label: "Answer", render: (row) => clip(row.answer, 140) },
          {
            label: "Source",
            render: (row) => {
              const [label, tone] = SOURCES[row.source] ?? [String(row.source ?? "—").toUpperCase(), "neutral"];
              return tag(label, tone);
            },
          },
          {
            label: "Sufficient",
            render: (row) => (row.sufficient === null ? "—" : tag(row.sufficient ? "YES" : "NO", row.sufficient ? "ok" : "warn")),
          },
          { label: "When", render: (row) => timestamp(row.ts) },
        ],
        [...answers].reverse(),
        {
          emptyText: "No answer recorded yet.",
          expand: (row) =>
            h(
              "div",
              { class: "stack-tight" },
              h("p", { class: "question" }, plainQuestion(row.question)),
              h("p", { class: "prompt-text" }, row.answer),
              row.reason ? h("p", { class: "muted small" }, `Sufficiency: ${row.reason}`) : null,
              row.sourceRef ? h("p", { class: "muted small" }, `Read from ${row.sourceRef}`) : null,
            ),
        },
      ),
      { aside: h("span", { class: "muted small" }, "answers taken from context.md are recorded as evidence too") },
    ),
  );
}

function pending(ctx) {
  if (TERMINAL_STATUSES.has(ctx.run.status)) {
    return notice("The run has ended, so its interview takes no more answers.");
  }
  const holder = h("div", null, loading("Checking for a pending question…"));
  ctx
    .interviewStatus()
    .then((data) => {
      const question = data?.pending_question;
      if (question) {
        holder.replaceChildren(answerForm(ctx, question, Boolean(data.requires_human_review)));
        return;
      }
      holder.replaceChildren(
        empty(
          data?.requires_human_review
            ? "No question is pending. The profile needs a human review before any permissive decision; see Approvals."
            : "No question is pending. When the run needs information it stops and asks here.",
        ),
      );
    })
    .catch((error) => holder.replaceChildren(errorNotice(error)));
  return holder;
}

function answerForm(ctx, question, requiresHumanReview) {
  const answer = h("textarea", { class: "input", rows: 4, placeholder: "Answer in your own words; the answer is recorded as evidence." });
  const status = h("span", { class: "form-status", role: "status" });
  const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Record answer");
  const send = async () => {
    const text = answer.value.trim();
    if (!text) {
      status.textContent = "Write an answer first.";
      return;
    }
    submit.disabled = true;
    answer.disabled = true;
    status.textContent = "Recording the answer; the run continues from its interview…";
    try {
      const result = await ctx.api.answerRiskInterview(ctx.runId, text);
      answer.value = "";
      await ctx.onRunMutated(result?.run);
    } catch (error) {
      status.textContent = `${error?.code ?? "error"}: ${error?.message ?? error}`;
      submit.disabled = false;
      answer.disabled = false;
    }
  };
  answer.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
      event.preventDefault();
      send();
    }
  });
  return h(
    "form",
    {
      class: "callout tone-active",
      onSubmit: (event) => {
        event.preventDefault();
        send();
      },
    },
    h(
      "div",
      { class: "callout-head" },
      h("strong", null, `Question ${question.question_number}`),
      h("code", { class: "mono muted" }, question.field),
    ),
    h("p", { class: "question" }, plainQuestion(question.question)),
    answer,
    h("div", { class: "form-actions" }, status, h("span", { class: "muted small" }, "Ctrl+Enter"), submit),
    requiresHumanReview ? h("p", { class: "muted small" }, "This profile already requires a human review before any permissive decision.") : null,
    h(
      "p",
      { class: "muted small" },
      "Facts written in the project's context.md are answered automatically when a run starts; edit it under Inputs.",
    ),
  );
}
