// One Run: header, section tabs and the live event stream that keeps them current.
//
// The Run record arrives quickly; the event page takes longer, because the API verifies the hash
// chain and redacts every payload before it answers. The header and the tabs that read their own
// API resource render as soon as the record arrives; the tabs that fold the event log show a
// loading state until it does.

import { api, followEvents, readAllEvents } from "../api.js";
import { h, replace } from "../dom.js";
import { headline, integer, timestamp } from "../format.js";
import { foldApprovals, foldInterview, latestRunState, waitingForInformation } from "../fold.js";
import { TERMINAL_STATUSES, isResumableState, stageStepIndex, stageSteps } from "../status.js";
import { button, copyable, decisionTag, errorNotice, fact, loading, mono, notice, statusTag, stepper } from "../ui.js";
import { renderAgents } from "./tabs/agents.js";
import { renderApprovals } from "./tabs/approvals.js";
import { renderArtifacts } from "./tabs/artifacts.js";
import { renderAudit } from "./tabs/audit.js";
import { renderEvents } from "./tabs/events.js";
import { renderInputs } from "./tabs/inputs.js";
import { renderInterview } from "./tabs/interview.js";
import { renderOverview } from "./tabs/overview.js";
import { renderPlan } from "./tabs/plan.js";
import { renderTools } from "./tabs/tools.js";
import { renderUsage } from "./tabs/usage.js";

// "live" tabs are folds over the event log and redraw as events arrive; the others read an API
// resource once and refresh when revisited.
const TABS = [
  { id: "overview", label: "Overview", render: renderOverview, live: true },
  { id: "interview", label: "Interview", render: renderInterview, live: true },
  { id: "plan", label: "Plan", render: renderPlan, live: false },
  { id: "agents", label: "Agents", render: renderAgents, live: true },
  { id: "approvals", label: "Approvals", render: renderApprovals, live: true },
  { id: "tools", label: "Tools", render: renderTools, live: true },
  { id: "artifacts", label: "Artifacts", render: renderArtifacts, live: false },
  { id: "inputs", label: "Inputs", render: renderInputs, live: false },
  { id: "audit", label: "Audit", render: renderAudit, live: false },
  { id: "events", label: "Events", render: renderEvents, live: true },
  { id: "usage", label: "Usage", render: renderUsage, live: true },
];

const RUN_RECORD_EVENTS = new Set([
  "run.transitioned",
  "run.completed",
  "run.failed",
  "turn.ended",
  "human.approval",
  "human.approval_requested",
  "audit.completed",
  "audit.block",
  "artifact.created",
  "audit.finding",
]);
const UPDATE_DELAY_MS = 400;

// RunOutcome → stepper tone; undefined while the Run has not reached a terminal outcome.
function outcomeTone(state) {
  if (state?.condition !== "terminal") return undefined;
  if (state.outcome === "completed") return "ok";
  if (state.outcome === "blocked" || state.outcome === "failed") return "block";
  return "neutral";
}

function tabEntry(tab) {
  return TABS.find((entry) => entry.id === tab) ?? TABS[0];
}

function isEditing(container) {
  const active = document.activeElement;
  if (active && container.contains(active) && active.matches("input, textarea, select")) return true;
  return [...container.querySelectorAll("textarea")].some((field) => field.value.trim());
}

// The interview status only changes when the interview or the Run's lifecycle records something.
function interviewKey(events) {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const type = events[index].type;
    if (type.startsWith("activity_profile.") || type === "run.transitioned" || type === "human.approval") {
      return events[index].seq;
    }
  }
  return -1;
}

export class RunView {
  constructor(root, runId, tab, item, hooks = {}) {
    this.runId = runId;
    this.hooks = hooks;
    this.tab = tabEntry(tab).id;
    this.item = item ?? null;
    this.run = null;
    this.events = [];
    this.eventsLoaded = false;
    this.lastSeq = -1;
    this.current = null;
    this.disposed = false;
    this.catchingUp = false;
    this.runRecordStale = false;
    this.updateTimer = null;
    this.interviewCache = null;
    this.controller = new AbortController();
    this.head = h("section", { class: "run-head" }, loading("Loading run…"));
    this.actionStatus = h("span", { class: "action-status", role: "status" });
    this.staleButton = h("button", { class: "tab-stale", type: "button", hidden: true, onClick: () => this.renderBody() }, "New events · refresh view");
    this.tabBar = h("nav", { class: "tabs", "aria-label": "Run sections" });
    this.body = h("div", { class: "tab-body" });
    replace(root, h("article", { class: "run" }, this.head, this.tabBar, this.body));
    this.load();
  }

  dispose() {
    this.disposed = true;
    this.controller.abort();
    clearTimeout(this.updateTimer);
  }

  async load() {
    const pendingEvents = readAllEvents(this.runId, { signal: this.controller.signal });
    pendingEvents.catch(() => {});
    try {
      this.run = await api.getRun(this.runId);
      if (this.disposed) return;
      this.hooks.onRunChanged?.(this.run);
      this.renderAll();
      const events = await pendingEvents;
      if (this.disposed) return;
      this.events = events;
      this.eventsLoaded = true;
      this.lastSeq = events.length ? events[events.length - 1].seq : -1;
      this.renderHead();
      this.renderTabBar();
      if (tabEntry(this.tab).live) this.renderBody();
      followEvents(this.runId, {
        since: this.lastSeq,
        signal: this.controller.signal,
        keepGoing: () => !this.disposed && !TERMINAL_STATUSES.has(this.run?.status),
        onEvent: (event) => this.accept(event),
      });
    } catch (error) {
      if (this.disposed || error?.name === "AbortError") return;
      if (this.run) {
        replace(this.body, errorNotice(error));
        return;
      }
      replace(this.head, h("p", { class: "eyebrow" }, "Run"), h("h1", { class: "run-title" }, this.runId), errorNotice(error));
    }
  }

  accept(event) {
    if (!event || event.seq <= this.lastSeq) return;
    if (event.seq !== this.lastSeq + 1) {
      this.catchUp();
      return;
    }
    this.events.push(event);
    this.lastSeq = event.seq;
    if (RUN_RECORD_EVENTS.has(event.type)) this.runRecordStale = true;
    clearTimeout(this.updateTimer);
    this.updateTimer = setTimeout(() => this.applyUpdate(), UPDATE_DELAY_MS);
  }

  async catchUp() {
    if (this.catchingUp) return;
    this.catchingUp = true;
    try {
      const events = await readAllEvents(this.runId, { afterSeq: this.lastSeq, signal: this.controller.signal });
      for (const event of events) this.accept(event);
    } catch {
      // The live stream reconnects and repairs again.
    } finally {
      this.catchingUp = false;
    }
  }

  async applyUpdate() {
    if (this.disposed || !this.run) return;
    if (this.runRecordStale) {
      this.runRecordStale = false;
      try {
        const previous = this.run.status;
        this.run = await api.getRun(this.runId);
        if (previous !== this.run.status) this.hooks.onRunChanged?.(this.run);
      } catch {
        // Keep the last snapshot.
      }
    }
    this.renderHead();
    this.renderTabBar();
    if (!tabEntry(this.tab).live) return;
    if (this.current?.update) {
      this.current.update(this.context());
      return;
    }
    if (isEditing(this.body)) {
      this.staleButton.hidden = false;
      return;
    }
    this.renderBody();
  }

  interviewStatus() {
    const key = interviewKey(this.events);
    if (!this.interviewCache || this.interviewCache.key !== key) {
      const promise = api.riskInterview(this.runId);
      this.interviewCache = { key, promise };
      promise.catch(() => {
        if (this.interviewCache?.promise === promise) this.interviewCache = null;
      });
    }
    return this.interviewCache.promise;
  }

  context() {
    return {
      api,
      run: this.run,
      events: this.events,
      runId: this.runId,
      item: this.item,
      onRunMutated: (run) => this.onRunMutated(run),
      interviewStatus: () => this.interviewStatus(),
      runAgain: () => this.runAgain(),
    };
  }

  async onRunMutated(run) {
    if (this.disposed) return;
    if (run?.id === this.runId) this.run = run;
    try {
      const events = await readAllEvents(this.runId, { afterSeq: this.lastSeq, signal: this.controller.signal });
      for (const event of events) {
        if (event.seq !== this.lastSeq + 1) continue;
        this.events.push(event);
        this.lastSeq = event.seq;
      }
      this.run = await api.getRun(this.runId);
    } catch {
      // The live stream catches up.
    }
    this.hooks.onRunChanged?.(this.run);
    if (!this.disposed) this.renderAll();
  }

  async mutate(action, done) {
    this.actionStatus.textContent = "Working…";
    this.actionStatus.className = "action-status";
    try {
      const run = await action();
      this.actionStatus.textContent = done;
      await this.onRunMutated(run);
    } catch (error) {
      this.actionStatus.textContent = `${error?.code ?? "error"}: ${error?.message ?? error}`;
      this.actionStatus.className = "action-status tone-block";
    }
  }

  async runAgain() {
    this.actionStatus.textContent = "Creating a new run…";
    this.actionStatus.className = "action-status";
    try {
      const body = await api.createRun(this.run.prompt);
      this.hooks.onRunCreated?.(body?.run ?? body);
    } catch (error) {
      this.actionStatus.textContent = `${error?.code ?? "error"}: ${error?.message ?? error}`;
      this.actionStatus.className = "action-status tone-block";
    }
  }

  setTab(tab, item) {
    const next = tabEntry(tab).id;
    const nextItem = item ?? null;
    if (next === this.tab && nextItem === this.item && this.current) return;
    this.tab = next;
    this.item = nextItem;
    if (!this.run) return;
    this.renderTabBar();
    this.renderBody();
  }

  renderAll() {
    this.renderHead();
    this.renderTabBar();
    this.renderBody();
  }

  renderHead() {
    const run = this.run;
    const state = this.eventsLoaded ? latestRunState(this.events) : null;
    const active = !TERMINAL_STATUSES.has(run.status);
    const resumable = isResumableState(state);
    const prompt = String(run.prompt ?? "");
    const pendingValue = () => h("span", { class: "muted" }, "…");
    replace(
      this.head,
      h(
        "div",
        { class: "run-head-top" },
        h("div", { class: "run-ident" }, h("span", { class: "eyebrow" }, "Run"), copyable(run.id)),
        h(
          "div",
          { class: "run-actions" },
          this.actionStatus,
          resumable
            ? button("Resume", {
                title: "Continue from the last durable checkpoint",
                onClick: () => this.mutate(() => api.resume(this.runId), "Resume requested."),
              })
            : null,
          active ? button("Cancel run", { kind: "danger", onClick: () => this.cancel() }) : null,
          active ? null : button("Run again", { title: "Start a new run with this prompt and the current project inputs", onClick: () => this.runAgain() }),
        ),
      ),
      h("h1", { class: "run-title" }, headline(prompt, 180)),
      prompt.length > 180 || prompt.includes("\n")
        ? h("details", { class: "prompt" }, h("summary", null, "Full prompt"), h("p", { class: "prompt-text" }, prompt))
        : null,
      this.eventsLoaded ? stepper(stageSteps(), stageStepIndex(state), outcomeTone(state)) : null,
      h(
        "div",
        { class: "facts" },
        fact("Status", statusTag(run.status)),
        fact(
          "Stage",
          !this.eventsLoaded
            ? pendingValue()
            : state
              ? [state.stage ?? "—", state.outcome ? h("span", { class: "muted" }, ` · ${state.outcome}`) : null]
              : "—",
        ),
        fact("Decision", decisionTag(run.final_decision)),
        fact("Created", timestamp(run.created_at)),
        fact("Events", this.eventsLoaded ? integer(this.events.length) : pendingValue()),
        fact("Agents", integer(run.agent_ids?.length ?? 0)),
        fact("Tool calls", integer(run.tool_call_ids?.length ?? 0)),
        fact("Artifacts", integer(run.artifact_ids?.length ?? 0)),
        fact("Findings", integer(run.finding_ids?.length ?? 0)),
        fact("Commit", run.git_commit ? mono(run.git_commit.slice(0, 10)) : "—"),
      ),
      run.error ? notice(run.error, "block") : null,
    );
  }

  cancel() {
    if (!window.confirm("Cancel this run? Its pending reviews stop being answerable.")) return;
    this.mutate(() => api.cancel(this.runId, "Cancelled from the web console."), "Run cancelled.");
  }

  renderTabBar() {
    const pending = this.eventsLoaded ? foldApprovals(this.events).pending.length : 0;
    const asking = this.eventsLoaded && waitingForInformation(this.events);
    const counts = {
      interview: this.eventsLoaded ? foldInterview(this.events).length : 0,
      agents: this.run.agent_ids?.length ?? 0,
      approvals: pending,
      tools: this.run.tool_call_ids?.length ?? 0,
      artifacts: this.run.artifact_ids?.length ?? 0,
      events: this.eventsLoaded ? this.events.length : 0,
    };
    const attention = {
      interview: asking,
      approvals: Boolean(pending) && this.run.status === "WAITING_FOR_APPROVAL",
    };
    this.tabBar.replaceChildren(
      ...TABS.map((entry) =>
        h(
          "a",
          {
            class: ["tab", entry.id === this.tab ? "is-active" : null, attention[entry.id] ? "needs-attention" : null],
            href: `#/runs/${encodeURIComponent(this.runId)}/${entry.id}`,
            "aria-current": entry.id === this.tab ? "page" : null,
          },
          entry.label,
          attention[entry.id] && !counts[entry.id]
            ? h("span", { class: "tab-count" }, "!")
            : counts[entry.id]
              ? h("span", { class: "tab-count" }, integer(counts[entry.id]))
              : null,
        ),
      ),
      this.staleButton,
    );
  }

  renderBody() {
    this.staleButton.hidden = true;
    const entry = tabEntry(this.tab);
    if (entry.live && !this.eventsLoaded) {
      this.current = null;
      replace(this.body, loading("Reading the event log…"));
      return;
    }
    const result = entry.render(this.context());
    this.current = result instanceof Node ? { node: result } : result;
    replace(this.body, this.current.node);
  }
}
