import { listAllRuns } from "../api.js";
import { h, replace } from "../dom.js";
import { clock, headline, integer, relative, shortId } from "../format.js";
import { decisionTone, statusLabel, statusTone } from "../status.js";

const FILTERS = [
  ["", "All statuses"],
  ["WAITING_FOR_APPROVAL", "Waiting for review"],
  ["PLANNING", "Planning"],
  ["RUNNING", "Running"],
  ["AUDITING", "Auditing"],
  ["COMPLETED", "Completed"],
  ["BLOCKED", "Blocked"],
  ["FAILED", "Failed"],
  ["CREATED", "Created"],
];

function newestFirst(runs) {
  return [...runs].sort((left, right) => String(right.created_at).localeCompare(String(left.created_at)));
}

// The run list is fetched on first load, on a filter change and when the operator asks for it; it
// is never polled. Listing Runs makes the API verify every Run's event log to report its current
// status, so a timer would keep the API busy. The Run open in the main view keeps its own row
// current through upsert() instead.
export class RunRail {
  constructor(root) {
    this.status = "";
    this.selected = null;
    this.runs = [];
    this.busy = false;
    this.count = h("span", { class: "rail-count" });
    this.updated = h("p", { class: "rail-updated" });
    this.message = h("p", { class: "rail-message" });
    this.list = h("ol", { class: "rail-list" });
    this.refreshButton = h(
      "button",
      { class: "btn btn-small", type: "button", title: "Reload the run list from the API", onClick: () => this.refresh() },
      "Refresh",
    );
    const filter = h(
      "select",
      {
        class: "input input-small",
        "aria-label": "Filter runs by status",
        onChange: (event) => {
          this.status = event.target.value;
          this.refresh();
        },
      },
      FILTERS.map(([value, label]) => h("option", { value }, label)),
    );
    replace(
      root,
      h(
        "div",
        { class: "rail-head" },
        h("span", { class: "rail-title" }, "Runs"),
        this.count,
        h("a", { class: "btn btn-small btn-primary", href: "#/new" }, "New run"),
      ),
      h("div", { class: "rail-tools" }, filter, this.refreshButton),
      this.updated,
      this.message,
      this.list,
    );
  }

  async refresh() {
    if (this.busy) return;
    this.busy = true;
    this.refreshButton.disabled = true;
    const started = performance.now();
    this.updated.textContent = "Refreshing…";
    if (!this.runs.length) {
      this.say("Loading runs. The API verifies each run's event log to report its current status, so a long history takes a while.");
    }
    try {
      this.runs = newestFirst(await listAllRuns({ status: this.status || undefined }));
      this.say(this.runs.length ? "" : this.status ? "No run has this status." : "No runs yet.");
      const seconds = ((performance.now() - started) / 1000).toFixed(1);
      this.updated.textContent = `Updated ${clock(new Date().toISOString())} · took ${seconds} s`;
      this.render();
    } catch (error) {
      this.updated.textContent = "";
      if (error?.status === 401) {
        this.runs = [];
        this.render();
        this.say("Connect to the API to list runs.");
      } else {
        this.say(error?.message ?? "Runs could not be listed.");
      }
    } finally {
      this.busy = false;
      this.refreshButton.disabled = false;
    }
  }

  upsert(run) {
    if (!run?.id) return;
    const others = this.runs.filter((candidate) => candidate.id !== run.id);
    const visible = !this.status || run.status === this.status;
    this.runs = newestFirst(visible ? [...others, run] : others);
    if (this.runs.length) this.say("");
    this.render();
  }

  say(text) {
    this.message.textContent = text;
  }

  render() {
    this.count.textContent = this.runs.length ? integer(this.runs.length) : "";
    this.list.replaceChildren(...this.runs.map((run) => this.item(run)));
  }

  item(run) {
    return h(
      "li",
      null,
      h(
        "a",
        {
          class: ["rail-item", run.id === this.selected ? "is-selected" : null],
          href: `#/runs/${encodeURIComponent(run.id)}`,
          dataset: { runId: run.id },
          title: run.prompt,
        },
        h("i", { class: `sq tone-${statusTone(run.status)}`, title: statusLabel(run.status) }),
        h("span", { class: "rail-item-title" }, headline(run.prompt, 110)),
        h(
          "span",
          { class: "rail-item-meta" },
          h("span", { class: "mono" }, shortId(run.id)),
          h("span", null, relative(run.created_at)),
          run.final_decision
            ? h("span", { class: `decision-text tone-${decisionTone(run.final_decision)}` }, run.final_decision)
            : h("span", null, statusLabel(run.status)),
        ),
      ),
    );
  }

  select(runId) {
    this.selected = runId;
    for (const link of this.list.querySelectorAll(".rail-item")) {
      link.classList.toggle("is-selected", link.dataset.runId === runId);
    }
  }
}
