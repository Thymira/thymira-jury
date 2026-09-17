import { api } from "../api.js";
import { h, replace } from "../dom.js";
import { renderProjectInputs } from "./inputs.js";

export class NewRunView {
  constructor(root, { onCreated }) {
    const prompt = h("textarea", {
      id: "new-run-prompt",
      class: "input prompt-input",
      rows: 12,
      placeholder:
        "Say what to analyse and what to deliver. Use a declared dataset by name, for example: profile `german_credit` and write reports/eda.md.",
    });
    const status = h("span", { class: "form-status", role: "status" });
    const submit = h("button", { class: "btn btn-primary", type: "submit" }, "Create and start run");
    const create = async () => {
      const text = prompt.value.trim();
      if (!text) {
        status.textContent = "Write a prompt first.";
        prompt.focus();
        return;
      }
      submit.disabled = true;
      prompt.disabled = true;
      status.textContent = "Creating the run…";
      try {
        const body = await api.createRun(text);
        onCreated(body?.run ?? body);
      } catch (error) {
        status.textContent = `${error?.code ?? "error"}: ${error?.message ?? error}`;
        submit.disabled = false;
        prompt.disabled = false;
      }
    };
    prompt.addEventListener("keydown", (event) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        create();
      }
    });
    const useDataset = (dataset) => {
      const insert = `\`${dataset.name}\``;
      const start = prompt.selectionStart ?? prompt.value.length;
      const end = prompt.selectionEnd ?? prompt.value.length;
      prompt.setRangeText(insert, start, end, "end");
      prompt.focus();
    };
    const form = h(
      "form",
      {
        class: "new-run",
        onSubmit: (event) => {
          event.preventDefault();
          create();
        },
      },
      h("p", { class: "eyebrow" }, "New run"),
      h("h1", null, "Describe the work"),
      h("label", { class: "field-label", for: "new-run-prompt" }, "Prompt"),
      prompt,
      h(
        "ul",
        { class: "checklist" },
        h("li", null, "The run starts as soon as you create it. Inspect registers every declared dataset as a source first."),
        h("li", null, "The risk interview answers what context.md already covers and asks you the rest under Interview."),
        h("li", null, "Tool calls with side effects wait for you under Approvals; MIRA audits the run before it completes."),
      ),
      h("div", { class: "form-actions" }, status, h("span", { class: "muted small" }, "Ctrl+Enter"), h("a", { class: "btn", href: "#/" }, "Cancel"), submit),
    );
    replace(
      root,
      h(
        "div",
        { class: "new-run-layout" },
        form,
        h(
          "aside",
          { class: "new-run-inputs", "aria-label": "Project inputs" },
          h("p", { class: "eyebrow" }, "Inputs the run will read"),
          renderProjectInputs({ onUseDataset: useDataset }),
        ),
      ),
    );
    prompt.focus();
  }
}
