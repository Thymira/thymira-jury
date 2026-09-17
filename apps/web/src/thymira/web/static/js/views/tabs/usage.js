import { h } from "../../dom.js";
import { clip, integer, usd } from "../../format.js";
import { foldAgents, foldUsage } from "../../fold.js";
import { empty, mono, panel, stat, table } from "../../ui.js";

// THY's execution agents run once per Run; MIRA's per-framework evidence checks (named after the
// governance pack, e.g. "credit_risk", "euaiact") re-run on every audit cycle and never call a
// model, so grouping by the raw agent_id would show one zero-cost row per cycle. Group by name.
function byName(agents) {
  const groups = new Map();
  for (const agent of agents) {
    const key = agent.name;
    const group = groups.get(key) ?? { name: key, cost: 0, runs: 0 };
    group.cost += agent.cost;
    group.runs += 1;
    groups.set(key, group);
  }
  return [...groups.values()];
}

export function renderUsage(ctx) {
  const { agents } = foldAgents(ctx.events);
  const usage = foldUsage(ctx.events, agents);
  const ranked = byName(agents).sort((left, right) => right.cost - left.cost);
  const top = ranked[0]?.cost ?? 0;
  return h(
    "div",
    { class: "stack" },
    h(
      "div",
      { class: "summary-strip" },
      stat("Agent episode cost", usd(usage.agentCost)),
      stat("Model calls", integer(usage.calls)),
      stat("Input tokens", integer(usage.inputTokens)),
      stat("Cached input", integer(usage.cachedTokens)),
      stat("Output tokens", integer(usage.outputTokens)),
    ),
    panel(
      "Cost by agent",
      ranked.length
        ? h(
            "ol",
            { class: "bars" },
            ranked.map((agent) =>
              h(
                "li",
                { class: "bar-row" },
                h("span", { class: "bar-label" }, agent.runs > 1 ? `${agent.name} ×${agent.runs}` : agent.name),
                h("span", { class: "bar-track" }, h("i", { class: "bar-fill", style: { width: `${top ? (agent.cost / top) * 100 : 0}%` } })),
                h("span", { class: "bar-value mono" }, usd(agent.cost)),
              ),
            ),
          )
        : empty("No agent has recorded usage yet."),
      { aside: h("span", { class: "muted small" }, "summed from agent.parked and agent.completed") },
    ),
    panel(
      "Model selections",
      table(
        [
          { label: "Model", render: (entry) => mono(entry.model) },
          { label: "Tier", render: (entry) => entry.tier },
          { label: "Role", render: (entry) => entry.role },
          { label: "Tasks", render: (entry) => [...entry.tasks].join(", ") || "—" },
          { label: "Calls", numeric: true, render: (entry) => integer(entry.count) },
          { label: "Latest reason", render: (entry) => clip(entry.reason, 100) || "—" },
        ],
        usage.models,
        { emptyText: "No model has been selected yet." },
      ),
      { aside: h("span", { class: "muted small" }, "routed by code and recorded as model.selected") },
    ),
  );
}
