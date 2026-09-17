// Live model routing: which model THY, MIRA and each sub-agent tier actually call. Backed by the
// durable settings API's "models" namespace (compare-and-set, one mutable user layer). A saved
// value reaches the very next routed model call in the running API process -- no restart -- but
// never bypasses that run's own frozen model-route allowlist: a model this deployment does not
// permit is still refused the moment it is used, exactly as an environment-configured one would be.

import { api } from "../api.js";
import { h, replace } from "../dom.js";
import { button, errorNotice, loading, panel } from "../ui.js";

const NAMESPACE = "models";

const FIELDS = [
  ["THYMIRA_THY_MODEL", "THY (orchestrator)", "e.g. anthropic/claude-opus-5"],
  ["THYMIRA_MIRA_MODEL", "MIRA (auditor)", "e.g. anthropic/claude-opus-5"],
  ["THYMIRA_MODEL_FRONTIER", "Frontier tier", "e.g. anthropic/claude-opus-5"],
  ["THYMIRA_MODEL_STANDARD", "Standard tier", "e.g. anthropic/claude-sonnet-5"],
  ["THYMIRA_MODEL_FAST", "Fast tier", "e.g. anthropic/claude-haiku-4-5-20251001"],
];

// Common LiteLLM-routed ids (`thymira.agents.llm.litellm_provider`, "anthropic/<model>"). The API
// exposes no canonical model list, so this suggests rather than restricts: a datalist keeps the
// field free-text, since a deployment may legitimately route to another provider or a newer id.
const MODEL_SUGGESTIONS = [
  "anthropic/claude-opus-5",
  "anthropic/claude-sonnet-5",
  "anthropic/claude-haiku-4-5-20251001",
  "anthropic/claude-fable-5-1",
];
const MODEL_LIST_ID = "model-suggestions";

// The two orchestrators get a THY/MIRA colour swatch on their own field; the shared tiers below
// them route generic sub-agent work and belong to neither, so they stay unmarked.
const OWNER_TONE = { THYMIRA_THY_MODEL: "thy", THYMIRA_MIRA_MODEL: "mira" };

function fieldLabel(key, label) {
  const tone = OWNER_TONE[key];
  return tone ? [h("i", { class: `sq tone-${tone}`, "aria-hidden": "true" }), label] : label;
}

async function readSnapshot() {
  try {
    return await api.getSettings(NAMESPACE);
  } catch (error) {
    if (error?.status === 404) return { namespace: NAMESPACE, revision: 0, values: {} };
    throw error;
  }
}

export class SettingsView {
  constructor(root) {
    this.body = h("div", { class: "stack-tight" }, loading("Reading the live model configuration…"));
    replace(
      root,
      h(
        "section",
        { class: "settings" },
        h("p", { class: "eyebrow" }, "Thymira console"),
        h("h1", null, "Model routing"),
        h(
          "p",
          { class: "lede" },
          "Which model THY, MIRA and each sub-agent tier call for the running API process. Leave a field blank to fall back to the server's own environment configuration.",
        ),
        panel("Live override", this.body, {
          aside: h("span", { class: "muted small" }, "takes effect on the next routed call · no restart"),
        }),
      ),
    );
    this.load();
  }

  async load() {
    try {
      replace(this.body, this.form(await readSnapshot()));
    } catch (error) {
      replace(this.body, errorNotice(error));
    }
  }

  form(snapshot, message = "") {
    const inputs = new Map(
      FIELDS.map(([key, label, placeholder]) => [
        key,
        h("input", {
          class: "input",
          type: "text",
          list: MODEL_LIST_ID,
          value: snapshot.values[key] ?? "",
          placeholder,
          autocomplete: "off",
          spellcheck: "false",
          "aria-label": label,
        }),
      ]),
    );
    const status = h("span", { class: "form-status", role: "status" }, message);
    const save = button("Save", {
      kind: "primary",
      onClick: async () => {
        const values = {};
        for (const [key, input] of inputs) {
          const value = input.value.trim();
          if (value) values[key] = value;
        }
        save.disabled = true;
        status.textContent = "Saving…";
        try {
          const next = await api.putSettings(NAMESPACE, values, snapshot.revision);
          replace(this.body, this.form(next, "Saved. The next routed model call uses this."));
        } catch (error) {
          save.disabled = false;
          status.textContent =
            error?.code === "settings_conflict"
              ? "Someone else changed this since you opened it. Reload the page to see the current values."
              : `${error.code}: ${error.message}`;
        }
      },
    });
    return h(
      "div",
      { class: "stack-tight" },
      h("datalist", { id: MODEL_LIST_ID }, MODEL_SUGGESTIONS.map((model) => h("option", { value: model }))),
      FIELDS.map(([key, label]) =>
        h("label", { class: "field" }, h("span", { class: "field-label" }, fieldLabel(key, label)), inputs.get(key)),
      ),
      h("div", { class: "form-actions" }, status, save),
    );
  }

  dispose() {}
}
