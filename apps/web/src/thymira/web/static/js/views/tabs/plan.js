import { h } from "../../dom.js";
import { integer, shortHash, timestamp } from "../../format.js";
import { outcomeTone } from "../../status.js";
import { asyncBlock, codeBlock, kv, panel, table, tag } from "../../ui.js";

export function renderPlan(ctx) {
  return asyncBlock(
    () => ctx.api.plan(ctx.runId),
    (plan) => [
      panel(
        "Goal",
        kv([
          ["Goal", plan.goal],
          ["Goal status", plan.goal_status ? tag(plan.goal_status, outcomeTone(plan.goal_status)) : null],
          ["Mode", plan.mode],
          ["Revision", integer(plan.revision)],
          ["Autonomous rounds", integer(plan.autonomous_rounds)],
          ["Blocked", plan.blocked_cause ? `${plan.blocked_cause} (${plan.blocked_count}×)` : null],
        ]),
      ),
      panel(
        `Items (${plan.items.length})`,
        table(
          [
            { label: "#", numeric: true, render: (item) => item.ordinal + 1 },
            { label: "Item", render: (item) => item.text },
            { label: "State", render: (item) => tag(item.state, outcomeTone(item.state)) },
            { label: "Depends on", numeric: true, render: (item) => item.dependency_ids?.length || "—" },
          ],
          [...plan.items].sort((left, right) => left.ordinal - right.ordinal),
          { emptyText: "The plan has no items." },
        ),
      ),
      plan.completions?.length
        ? panel(
            "Completions",
            table(
              [
                { label: "#", numeric: true, render: (completion) => completion.ordinal + 1 },
                { label: "Completed", render: (completion) => timestamp(completion.completed_at) },
                { label: "Summary", render: (completion) => completion.summary ?? "—" },
                { label: "Revision", numeric: true, render: (completion) => completion.source_revision },
              ],
              plan.completions,
            ),
          )
        : null,
      plan.review_feedback?.length
        ? panel("Review feedback", h("ul", { class: "plain-list" }, plan.review_feedback.map((feedback) => h("li", null, feedback))))
        : null,
      plan.rounds?.length
        ? panel(
            "Autonomous rounds",
            table(
              [
                { label: "Round", numeric: true, render: (round) => round.round_number },
                { label: "Revision", numeric: true, render: (round) => round.revision },
                { label: "Result", render: (round) => tag(round.succeeded ? "SUCCEEDED" : "FAILED", round.succeeded ? "ok" : "block") },
                { label: "Cause", render: (round) => round.cause ?? "—" },
                { label: "Recorded", render: (round) => timestamp(round.recorded_at) },
              ],
              plan.rounds,
            ),
          )
        : null,
      plan.artifact
        ? panel("Rendered plan", codeBlock(plan.artifact.content, { className: "prose-code" }), {
            aside: h("span", { class: "muted small mono" }, `sha256 ${shortHash(plan.artifact.sha256)}`),
          })
        : null,
    ],
    { quiet: { plan_not_found: "THY has not published a plan for this run yet." } },
  );
}
