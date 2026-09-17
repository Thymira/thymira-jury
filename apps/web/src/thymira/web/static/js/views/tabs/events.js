import { h } from "../../dom.js";
import { clip, clock, integer, shortId } from "../../format.js";
import { countBy } from "../../fold.js";
import { codeBlock, kv, mono } from "../../ui.js";

const PAGE_ROWS = 400;

export function renderEvents(ctx) {
  let events = ctx.events;
  let typeFilter = "";
  let search = "";
  let limit = PAGE_ROWS;
  const expanded = new Set();

  const typeSelect = h("select", {
    class: "input input-small",
    "aria-label": "Filter events by type",
    onChange: (event) => {
      typeFilter = event.target.value;
      draw();
    },
  });
  const searchInput = h("input", {
    class: "input input-small",
    type: "search",
    placeholder: "Search type, actor or subject",
    "aria-label": "Search events",
    onInput: (event) => {
      search = event.target.value.trim().toLowerCase();
      draw();
    },
  });
  const summary = h("span", { class: "muted small" });
  const body = h("tbody");
  const more = h(
    "button",
    {
      class: "btn btn-small",
      type: "button",
      hidden: true,
      onClick: () => {
        limit += PAGE_ROWS;
        draw();
      },
    },
    "Show older events",
  );

  function drawTypes() {
    const counts = [...countBy(events, (event) => event.type)].sort((left, right) => right[1] - left[1]);
    typeSelect.replaceChildren(
      h("option", { value: "" }, `All types (${integer(events.length)})`),
      ...counts.map(([type, count]) => h("option", { value: type, selected: type === typeFilter }, `${type} (${integer(count)})`)),
    );
  }

  function matches(event) {
    if (typeFilter && event.type !== typeFilter) return false;
    if (!search) return true;
    return `${event.type} ${event.actor?.kind}/${event.actor?.id} ${event.subject_id ?? ""}`.toLowerCase().includes(search);
  }

  function rows(event) {
    const open = expanded.has(event.seq);
    const row = h(
      "tr",
      {
        class: ["is-expandable", open ? "is-open" : null],
        onClick: () => {
          if (open) expanded.delete(event.seq);
          else expanded.add(event.seq);
          draw();
        },
      },
      h("td", { class: "num mono" }, event.seq),
      h("td", { class: "mono" }, clock(event.ts)),
      h("td", null, h("span", { class: `event-type family-${event.type.split(".")[0]}` }, event.type)),
      h("td", { class: "mono" }, `${event.actor?.kind ?? "?"}/${event.actor?.id ?? "?"}`),
      h("td", { class: "mono" }, event.subject_id ? shortId(event.subject_id) : "—"),
      h("td", { class: "event-summary" }, clip(summarize(event), 160)),
    );
    if (!open) return [row];
    return [
      row,
      h(
        "tr",
        { class: "row-detail" },
        h(
          "td",
          { colspan: 6 },
          h(
            "div",
            { class: "stack-tight" },
            kv([
              ["Event", mono(event.event_id)],
              ["Source hash", mono(event.source_hash)],
              ["Producer", `${event.producer ?? "?"} ${event.producer_version ?? ""}`],
              ["Correlation", event.correlation_id ? mono(event.correlation_id) : null],
              ["Causation", event.causation_id ? mono(event.causation_id) : null],
            ]),
            codeBlock(event.payload ?? {}),
          ),
        ),
      ),
    ];
  }

  function draw() {
    const matched = events.filter(matches);
    body.replaceChildren(...matched.slice(-limit).reverse().flatMap(rows));
    more.hidden = matched.length <= limit;
    summary.textContent = `${integer(matched.length)} of ${integer(events.length)} events · newest first · redacted projections of the hash-chained log`;
  }

  drawTypes();
  draw();
  const node = h(
    "div",
    { class: "stack" },
    h("div", { class: "toolbar" }, typeSelect, searchInput, summary),
    h(
      "div",
      { class: "table-wrap" },
      h(
        "table",
        { class: "grid events" },
        h("thead", null, h("tr", null, ["Seq", "Time", "Type", "Actor", "Subject", "Summary"].map((label, index) => h("th", { class: index === 0 ? "num" : null }, label)))),
        body,
      ),
    ),
    h("div", null, more),
  );
  return {
    node,
    update(next) {
      events = next.events;
      drawTypes();
      draw();
    },
  };
}

function summarize(event) {
  const payload = event.payload ?? {};
  switch (event.type) {
    case "run.transitioned":
      return `${payload.command ?? "transition"}: ${payload.previous_stage ?? "?"} → ${payload.stage ?? "?"}${payload.outcome ? ` (${payload.outcome})` : ""}`;
    case "policy.decision":
      return [payload.decision, payload.rule_id, payload.subject_kind].filter(Boolean).join(" · ");
    case "model.selected":
      return [payload.model, payload.tier_applied, payload.task].filter(Boolean).join(" · ");
    case "model.response_chunk":
      return `input ${payload.input_tokens ?? 0} · output ${payload.output_tokens ?? 0} tokens`;
    case "human.approval_requested":
      return payload.summary ?? payload.reason ?? "";
    case "human.approval":
      return `${payload.approved ? "approved" : "rejected"}${payload.note ? ` · ${payload.note}` : ""}`;
    case "artifact.created":
    case "artifact.invalidated":
      return payload.name ?? payload.artifact_id ?? "";
    case "audit.finding":
      return [payload.finding?.severity, payload.finding?.title].filter(Boolean).join(" · ");
    case "audit.completed":
      return payload.status ?? payload.audit_report?.status ?? "";
    case "risk.classified":
      return [payload.risk_profile?.risk_level, payload.risk_profile?.activity_category].filter(Boolean).join(" · ");
    case "activity_profile.questioned":
      return payload.field ?? "";
    case "activity_profile.answered":
      return `${payload.field ?? ""}: ${payload.answer ?? ""}`;
    case "turn.ended":
      return payload.end_reason ?? "";
    default:
      if (event.type.startsWith("agent.") || event.type === "subagent.settled") {
        return [payload.agent, payload.status, payload.end_reason ?? payload.stop_reason].filter(Boolean).join(" · ");
      }
      if (event.type.startsWith("tool.")) return [payload.tool, payload.result_code, payload.reason].filter(Boolean).join(" · ");
      return Object.keys(payload).slice(0, 4).join(", ");
  }
}
