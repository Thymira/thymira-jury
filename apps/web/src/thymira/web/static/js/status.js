// Vocabulary for the closed enums the API returns, mapped to labels and colour tones.

export const TERMINAL_STATUSES = new Set(["COMPLETED", "BLOCKED", "FAILED"]);

export function isResumableState(state) {
  return state?.condition === "paused";
}

// The workflow position a Run moves through (RunStage, packages/schemas/run_state.py).
// EXPERIMENTING is a sibling of EXECUTING, not a later step, so it shares its slot. `owner` marks
// which orchestrator drives the step -- THY for everything but the MIRA-run audit -- so the
// stepper can show whose turn it is (console.css `.step.is-owner-*`).
const STAGE_STEPS = [
  { id: "planning", label: "Plan", stages: ["planning"], owner: "thy" },
  { id: "executing", label: "Execute", stages: ["executing", "experimenting"], owner: "thy" },
  { id: "auditing", label: "Audit", stages: ["auditing"], owner: "mira" },
  { id: "reporting", label: "Report", stages: ["reporting"], owner: "thy" },
];

export function stageSteps() {
  return STAGE_STEPS;
}

// -1 before the first step (CREATED), 0..n-1 while in a step, STAGE_STEPS.length once REPORTING
// has finished (a terminal outcome was recorded for it).
export function stageStepIndex(state) {
  const stage = String(state?.stage ?? "").toLowerCase();
  const index = STAGE_STEPS.findIndex((step) => step.stages.includes(stage));
  if (index === -1) return stage ? STAGE_STEPS.length : -1;
  if (index === STAGE_STEPS.length - 1 && state?.condition === "terminal") return STAGE_STEPS.length;
  return index;
}

const STATUS = {
  CREATED: ["Created", "neutral"],
  PLANNING: ["Planning", "active"],
  RUNNING: ["Running", "active"],
  EXPERIMENTING: ["Experimenting", "active"],
  AUDITING: ["Auditing", "active"],
  WAITING_FOR_APPROVAL: ["Waiting for review", "review"],
  COMPLETED: ["Completed", "ok"],
  BLOCKED: ["Blocked", "block"],
  FAILED: ["Failed", "block"],
};

export const STATUS_ORDER = Object.keys(STATUS);

export function statusLabel(status) {
  return STATUS[status]?.[0] ?? String(status ?? "Unknown");
}

export function statusTone(status) {
  return STATUS[status]?.[1] ?? "neutral";
}

// WARNING gets its own lighter green (not full --ok): the run completed successfully, distinct
// from a clean PASS, but it is a good outcome -- not the amber --warn severity tone findings use.
const DECISION = { PASS: "ok", WARNING: "caution", REQUIRE_HUMAN_REVIEW: "review", BLOCK: "block" };

export function decisionTone(decision) {
  return DECISION[decision] ?? "neutral";
}

// Display text only -- the wire value (PolicyDecision.decision, a closed Contract enum) is never
// renamed. WARNING alone reads as if the run stalled; a WARNING run completed, with findings.
const DECISION_LABEL = { WARNING: "COMPLETED WITH WARNING" };

export function decisionLabel(decision) {
  return DECISION_LABEL[decision] ?? decision;
}

const SEVERITY = { CRITICAL: "block", HIGH: "block", MEDIUM: "warn", LOW: "neutral" };

export function severityTone(severity) {
  return SEVERITY[String(severity ?? "").toUpperCase()] ?? "neutral";
}

const RISK = { critical: "block", high: "block", medium: "warn", low: "ok", unknown: "neutral" };

export function riskTone(level) {
  return RISK[String(level ?? "").toLowerCase()] ?? "neutral";
}

// A loose reading for open-ended outcome strings (tool results, todo states, audit statuses).
export function outcomeTone(value) {
  const text = String(value ?? "").toUpperCase();
  if (/(FAIL|BLOCK|DENIED|REJECT|ERROR|TAMPER)/.test(text)) return "block";
  if (/(WARN|PARK|PENDING|WAIT|SKIP)/.test(text)) return "warn";
  if (/(PASS|SUCCESS|COMPLETE|DONE|APPROVED|ACTIVE)/.test(text)) return "ok";
  if (/(RUN|START|PROGRESS)/.test(text)) return "active";
  return "neutral";
}
