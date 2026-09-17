import { h } from "../../dom.js";
import { bytes, shortHash, timestamp } from "../../format.js";
import { asyncBlock, button, mono, notice, panel, table, tag } from "../../ui.js";
import { renderProjectInputs } from "../inputs.js";

export function renderInputs(ctx) {
  return h(
    "div",
    { class: "stack" },
    notice(
      "Datasets and context belong to the project and are shared by every run. A change applies to runs started from now on; this run keeps the copies it registered, listed first.",
    ),
    panel(
      "Registered sources in this run",
      asyncBlock(() => ctx.api.artifacts(ctx.runId), (body) => sources(ctx, body.items)),
      { aside: h("span", { class: "muted small" }, "content-addressed copies MIRA audits against") },
    ),
    renderProjectInputs(),
    h(
      "div",
      { class: "form-actions" },
      h("span", { class: "form-status" }, "Start a new run with the same prompt and the current inputs."),
      button("Run again", { kind: "primary", onClick: () => ctx.runAgain() }),
    ),
  );
}

function sources(ctx, items) {
  const datasets = items.filter((artifact) => artifact.kind === "dataset" && artifact.name.startsWith("datasets/"));
  return table(
    [
      {
        label: "Source",
        render: (artifact) =>
          h(
            "a",
            { class: "mono", href: `#/runs/${encodeURIComponent(ctx.runId)}/artifacts/${encodeURIComponent(artifact.id)}` },
            artifact.name,
          ),
      },
      { label: "State", render: (artifact) => (artifact.valid ? tag("REGISTERED", "ok") : tag("SUPERSEDED")) },
      { label: "Size", numeric: true, render: (artifact) => bytes(artifact.size_bytes) },
      { label: "SHA-256", render: (artifact) => mono(shortHash(artifact.sha256)) },
      { label: "Registered", render: (artifact) => timestamp(artifact.created_at) },
    ],
    datasets,
    { emptyText: "This run has not registered a dataset yet. Inspect registers every declared dataset when the run starts." },
  );
}
