import { h } from "../../dom.js";
import { integer, relative, timestamp, usd } from "../../format.js";
import { foldAgents } from "../../fold.js";
import { outcomeTone } from "../../status.js";
import { copyable, kv, mono, panel, stat, table, tag } from "../../ui.js";

export function renderAgents(ctx) {
  const { agents } = foldAgents(ctx.events);
  const total = (field) => agents.reduce((sum, agent) => sum + agent[field], 0);
  return h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "summary-strip" },
      stat("Agents", integer(agents.length)),
      stat("Parked episodes", integer(total("parked"))),
      stat("Resumed episodes", integer(total("resumed"))),
      stat("Recorded cost", usd(total("cost"))),
    ),
    panel(
      "Delegation",
      table(
        [
          {
            label: "Agent",
            render: (agent) => [h("strong", null, agent.name), agent.framework ? [" ", tag(agent.framework)] : null],
          },
          { label: "Parent", render: (agent) => agent.parent ?? h("span", { class: "muted" }, "—") },
          { label: "Depth", numeric: true, render: (agent) => agent.depth ?? "—" },
          { label: "Status", render: (agent) => tag(agent.status, outcomeTone(agent.status)) },
          {
            label: "Episodes",
            render: (agent) => `${agent.parked + agent.completed} · ${agent.parked} parked · ${agent.resumed} resumed`,
          },
          {
            label: "Tokens in / out",
            numeric: true,
            render: (agent) => `${integer(agent.inputTokens)} / ${integer(agent.outputTokens)}`,
          },
          { label: "Cost", numeric: true, render: (agent) => usd(agent.cost) },
          { label: "Last event", render: (agent) => relative(agent.lastTs) },
        ],
        agents,
        {
          emptyText: "No agent has started yet.",
          expand: (agent) =>
            h(
              "div",
              { class: "stack-tight" },
              agent.objective ? h("p", { class: "prompt-text" }, agent.objective) : null,
              kv([
                ["Agent", copyable(agent.id)],
                ["Tasks", agent.taskIds.size ? h("span", { class: "chips" }, [...agent.taskIds].map((id) => mono(id))) : null],
                ["Stop reason", agent.stopReason],
                ["First event", timestamp(agent.firstTs)],
                ["Last event", timestamp(agent.lastTs)],
              ]),
            ),
        },
      ),
      {
        aside: h("span", { class: "muted small" }, "a parked episode waits for a review; a resumed one continues its saved conversation"),
        className: "is-thy",
      },
    ),
  );
}
