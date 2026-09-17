import { h } from "../../dom.js";
import { clip, clock, integer, shortId } from "../../format.js";
import { foldAgents, foldTools } from "../../fold.js";
import { outcomeTone } from "../../status.js";
import { codeBlock, kv, mono, notice, panel, stat, table, tag } from "../../ui.js";

export function renderTools(ctx) {
  const { byTask } = foldAgents(ctx.events);
  const calls = foldTools(ctx.events, byTask).reverse();
  const executed = calls.filter((call) => call.attempts.some((attempt) => attempt.type === "tool.completed")).length;
  const held = calls.reduce((sum, call) => sum + call.denials, 0);
  const unsuccessful = calls.filter((call) => call.resultCode && call.resultCode !== "SUCCESS").length;
  return h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "summary-strip" },
      stat("Distinct calls", integer(calls.length)),
      stat("Executed", integer(executed)),
      stat("Held for review", integer(held)),
      stat("Not successful", integer(unsuccessful)),
    ),
    panel(
      "Tool calls",
      table(
        [
          { label: "Tool", render: (call) => mono(call.tool) },
          { label: "Agent", render: (call) => call.agent ?? h("span", { class: "muted" }, "—") },
          { label: "Status", render: (call) => tag(call.status, outcomeTone(call.status)) },
          { label: "Result", render: (call) => (call.resultCode ? tag(call.resultCode, outcomeTone(call.resultCode)) : "—") },
          { label: "Exit", numeric: true, render: (call) => call.exitCode ?? "—" },
          {
            label: "Sandbox",
            render: (call) => [call.sandboxMode ?? "—", call.enforcement ? h("span", { class: "muted" }, ` · ${call.enforcement}`) : null],
          },
          { label: "Reviews", numeric: true, render: (call) => call.denials || "—" },
          { label: "Artifacts", numeric: true, render: (call) => call.artifactIds.length || "—" },
          { label: "Last", render: (call) => clock(call.lastTs) },
        ],
        calls,
        { emptyText: "No tool call recorded yet.", expand: (call) => detail(ctx, call) },
      ),
      { aside: h("span", { class: "muted small" }, "one row per tool intent digest") },
    ),
  );
}

function detail(ctx, call) {
  const executed = call.attempts.some((attempt) => attempt.type === "tool.completed");
  return h(
    "div",
    { class: "stack-tight" },
    call.error ? notice(typeof call.error === "string" ? call.error : JSON.stringify(call.error), "block") : null,
    call.reason && !executed ? notice(call.reason, "warn") : null,
    h(
      "div",
      { class: "columns" },
      h("div", { class: "stack-tight" }, h("span", { class: "muted small" }, "Arguments"), codeBlock(call.arguments ?? {})),
      h(
        "div",
        { class: "stack-tight" },
        h("span", { class: "muted small" }, "Attempts"),
        table(
          [
            { label: "Seq", numeric: true, render: (attempt) => attempt.seq },
            { label: "Event", render: (attempt) => mono(attempt.type) },
            { label: "Decision", render: (attempt) => (attempt.decisionId ? mono(shortId(attempt.decisionId)) : "—") },
            { label: "Detail", render: (attempt) => clip(attempt.detail, 90) || "—" },
          ],
          call.attempts,
          { className: "compact" },
        ),
      ),
    ),
    call.artifactIds.length
      ? kv([
          [
            "Artifacts",
            h(
              "span",
              { class: "chips" },
              call.artifactIds.map((id) =>
                h("a", { class: "mono", href: `#/runs/${encodeURIComponent(ctx.runId)}/artifacts/${encodeURIComponent(id)}` }, shortId(id)),
              ),
            ),
          ],
        ])
      : null,
  );
}
