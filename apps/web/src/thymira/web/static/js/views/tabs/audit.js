import { h } from "../../dom.js";
import { clip, integer, shortHash } from "../../format.js";
import { countBy } from "../../fold.js";
import { outcomeTone, severityTone } from "../../status.js";
import {
  asyncBlock,
  button,
  codeBlock,
  copyable,
  decisionTag,
  downloadBytes,
  empty,
  errorNotice,
  findingList,
  kv,
  loading,
  mono,
  panel,
  stat,
  table,
  tag,
} from "../../ui.js";

export function renderAudit(ctx) {
  return h(
    "div",
    { class: "stack" },
    asyncBlock(() => ctx.api.audit(ctx.runId), auditReport, {
      quiet: { audit_not_found: "MIRA has not completed an audit for this run yet." },
    }),
    panel("Assurance bundle", assurance(ctx)),
    panel("Experiments", asyncBlock(() => ctx.api.experiments(ctx.runId), (body) => experiments(body.items))),
    panel("MLflow tracker", mlflow(ctx)),
  );
}

function auditReport({ report, decision }) {
  const controls = [...(report.controls ?? [])].sort((left, right) =>
    left.control_id.localeCompare(right.control_id, undefined, { numeric: true }),
  );
  const counts = countBy(controls, (control) => control.status);
  let filter = "ALL";
  const holder = h("div");
  const draw = () => {
    const rows = filter === "ALL" ? controls : controls.filter((control) => control.status === filter);
    holder.replaceChildren(
      table(
        [
          { label: "Control", render: (control) => mono(control.control_id) },
          { label: "Title", render: (control) => control.title },
          { label: "Status", render: (control) => tag(control.status, outcomeTone(control.status)) },
          { label: "Severity", render: (control) => tag(control.severity, severityTone(control.severity)) },
          { label: "Detail", render: (control) => clip(control.detail, 120) || "—" },
        ],
        rows,
        {
          emptyText: "No control has this status.",
          rowClass: (control) => (control.status === "FAILED" ? "row-fail" : null),
          expand: (control) =>
            h(
              "div",
              { class: "stack-tight" },
              kv([
                ["Detail", control.detail],
                ["Failure code", control.failure_code ? mono(control.failure_code) : null],
              ]),
              control.evidence?.length ? codeBlock(control.evidence) : null,
            ),
        },
      ),
    );
  };
  const select = h(
    "select",
    {
      class: "input input-small",
      "aria-label": "Filter controls by status",
      onChange: (event) => {
        filter = event.target.value;
        draw();
      },
    },
    h("option", { value: "ALL" }, `All controls (${controls.length})`),
    [...counts].map(([status, count]) => h("option", { value: status }, `${status} (${count})`)),
  );
  draw();
  const legend = h(
    "details",
    { class: "control-legend" },
    h("summary", null, `Control reference (${controls.length})`),
    h(
      "dl",
      { class: "kv" },
      controls.flatMap((control) => [h("dt", null, mono(control.control_id)), h("dd", null, control.title)]),
    ),
  );
  return [
    panel("MIRA audit", [
      h(
        "div",
        { class: "summary-strip" },
        stat("Report", tag(report.status, outcomeTone(report.status))),
        stat("Decision", decision ? decisionTag(decision.decision) : "—"),
        stat("Passed", integer(counts.get("PASSED") ?? 0)),
        stat("Failed", integer(counts.get("FAILED") ?? 0)),
        stat("Not applicable", integer(counts.get("NOT_APPLICABLE") ?? 0)),
        stat("Findings", integer(report.findings?.length ?? 0)),
      ),
      decision
        ? kv([
            ["Decision reason", decision.reason],
            ["Rule", mono(decision.rule_id)],
            ["Policy", decision.policy_name],
          ])
        : null,
      kv([
        ["Control set", mono(report.control_set)],
        ["Terminal event hash", mono(shortHash(report.terminal_hash))],
        ["Manifest sha256", mono(shortHash(report.manifest_sha256))],
        ["Policy sha256", mono(shortHash(report.policy_sha256))],
        ["Graph definition", mono(shortHash(report.graph_definition_hash))],
      ]),
    ], { aside: h("span", { class: "muted small" }, "controls recomputed from the log and the manifest"), className: "is-mira" }),
    report.findings?.length ? panel("Findings", findingList(report.findings), { className: "is-mira" }) : null,
    panel("Controls", [legend, holder], { aside: select }),
  ];
}

function assurance(ctx) {
  const holder = h("div", { class: "stack-tight" });
  const load = button("Load and verify the bundle", {
    onClick: async () => {
      holder.replaceChildren(loading("Reading the assurance bundle…"));
      try {
        const bundle = await ctx.api.assurance(ctx.runId);
        holder.replaceChildren(
          kv([
            ["Bundle sha256", copyable(bundle.bundle_sha256)],
            ["Run", mono(bundle.run_id)],
          ]),
          h(
            "div",
            { class: "form-actions" },
            button("Download the bundle JSON", {
              onClick: () =>
                downloadBytes(`${ctx.runId}-assurance.json`, new TextEncoder().encode(JSON.stringify(bundle, null, 2)), "application/json"),
            }),
          ),
        );
      } catch (error) {
        holder.replaceChildren(
          error?.code === "assurance_not_found" ? empty("No assurance bundle was produced for this run.") : errorNotice(error),
        );
      }
    },
  });
  holder.append(
    h("p", { class: "muted small" }, "The API re-verifies the bundle against the event log and the artifact manifest before returning it."),
    h("div", null, load),
  );
  return holder;
}

function metrics(values) {
  const entries = Object.entries(values ?? {});
  if (!entries.length) return "—";
  return h(
    "span",
    { class: "chips" },
    entries.map(([name, value]) => tag(`${name}=${typeof value === "number" ? Number(value.toFixed(4)) : value}`)),
  );
}

function experiments(items) {
  return table(
    [
      { label: "Name", render: (experiment) => experiment.name },
      { label: "Status", render: (experiment) => tag(experiment.status, outcomeTone(experiment.status)) },
      { label: "Metrics", render: (experiment) => metrics(experiment.metrics) },
      { label: "Seed", numeric: true, render: (experiment) => experiment.seed ?? "—" },
      { label: "Tracker run", render: (experiment) => (experiment.tracker_run_id ? mono(experiment.tracker_run_id) : "—") },
    ],
    items,
    {
      emptyText: "No experiment recorded for this run.",
      expand: (experiment) => h("div", { class: "stack-tight" }, experiment.error ? errorNotice({ message: experiment.error }) : null, codeBlock(experiment.parameters ?? {})),
    },
  );
}

function mlflow(ctx) {
  const holder = h("div", { class: "stack-tight" });
  const query = button("Query MLflow", {
    onClick: async () => {
      holder.replaceChildren(loading("Querying the tracker…"));
      try {
        const body = await ctx.api.mlflow(ctx.runId);
        holder.replaceChildren(
          table(
            [
              { label: "Experiment", render: (run) => run.experiment_name || "—" },
              { label: "Status", render: (run) => tag(run.status || "—", outcomeTone(run.status)) },
              { label: "Tracker run", render: (run) => mono(run.tracker_run_id) },
              { label: "Metrics", render: (run) => metrics(run.metrics) },
            ],
            body.items,
            { emptyText: "The tracker holds no run for this Run.", expand: (run) => codeBlock(run.params ?? {}) },
          ),
        );
      } catch (error) {
        holder.replaceChildren(errorNotice(error));
      }
    },
  });
  holder.append(h("p", { class: "muted small" }, "Read through the query_mlflow tool; it needs a reachable tracker."), h("div", null, query));
  return holder;
}
