// The project inputs every new Run reads: its declared datasets and .thymira/context.md. They
// belong to the project, not to one Run: a change reaches every Run started afterwards, while a Run
// keeps the dataset copies it registered when it started.

import { api, uploadDataset } from "../api.js";
import { h, replace } from "../dom.js";
import { bytes, relative, shortHash } from "../format.js";
import { button, empty, errorNotice, loading, notice, panel, tag } from "../ui.js";

const NAME_PATTERN = /^[a-z0-9][a-z0-9_-]*$/;
const EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
const knownColumns = new Map();

export function renderProjectInputs({ onUseDataset } = {}) {
  const datasets = h("div", { class: "stack-tight" }, loading("Reading the declared datasets…"));
  const context = h("div", { class: "stack-tight" }, loading("Reading context.md…"));
  const options = { onUseDataset };
  loadDatasets(datasets, options);
  loadContext(context);
  return h(
    "div",
    { class: "inputs" },
    panel("Datasets", datasets, {
      aside: h("span", { class: "muted small" }, "each is registered as a source when a run starts"),
    }),
    panel("Context · .thymira/context.md", context, {
      aside: h("span", { class: "muted small" }, "read by the interview, the plan and every agent"),
    }),
  );
}

async function loadDatasets(holder, options, flash) {
  try {
    const body = await api.projectDatasets();
    replace(
      holder,
      flash ? notice(flash.text, flash.tone) : null,
      body.items.length
        ? h("ul", { class: "dataset-list" }, body.items.map((dataset) => datasetRow(holder, options, dataset)))
        : empty("The project declares no dataset yet. Add a CSV or Parquet file below."),
      addForm(holder, options, body.items),
    );
  } catch (error) {
    replace(holder, errorNotice(error));
  }
}

function datasetRow(holder, options, dataset) {
  const status = h("span", { class: "form-status", role: "status" });
  const columns = knownColumns.get(dataset.name) ?? [];
  const listId = `columns-${dataset.name}`;
  const target = h("input", {
    class: "input input-small",
    value: dataset.target ?? "",
    placeholder: "no target",
    list: columns.length ? listId : null,
    "aria-label": `Target column of ${dataset.name}`,
  });
  const replaceFile = h("input", { type: "file", accept: ".csv,.parquet", hidden: true });
  replaceFile.addEventListener("change", () => {
    const file = replaceFile.files?.[0];
    if (file) upload(holder, options, { name: dataset.name, file, target: null, status });
  });
  const saveTarget = button("Set target", {
    kind: "small",
    onClick: async () => {
      const value = target.value.trim() || null;
      status.textContent = "Saving the target…";
      try {
        await api.setDatasetTarget(dataset.name, value);
        await loadDatasets(holder, options, {
          text: value ? `${dataset.name} now predicts ${value}.` : `${dataset.name} has no target column.`,
          tone: "ok",
        });
      } catch (error) {
        status.textContent = `${error.code}: ${error.message}`;
      }
    },
  });
  const remove = button("Remove", {
    kind: ["small", "danger"],
    onClick: async () => {
      if (!window.confirm(`Stop declaring ${dataset.name}? Its file stays in ${dataset.path}.`)) return;
      try {
        await api.removeDataset(dataset.name);
        await loadDatasets(holder, options, {
          text: `${dataset.name} is no longer declared; its file stays on disk.`,
          tone: "neutral",
        });
      } catch (error) {
        status.textContent = `${error.code}: ${error.message}`;
      }
    },
  });
  return h(
    "li",
    { class: "dataset" },
    h(
      "div",
      { class: "dataset-head" },
      h("code", { class: "mono dataset-name" }, dataset.name),
      dataset.present ? tag("FILE PRESENT", "ok") : tag("FILE MISSING", "block"),
      h("span", { class: "muted small mono" }, dataset.path),
      dataset.present
        ? h("span", { class: "muted small" }, `${bytes(dataset.size_bytes)} · changed ${relative(dataset.modified_at)}`)
        : null,
    ),
    h(
      "div",
      { class: "dataset-actions" },
      h("label", { class: "field-inline" }, h("span", { class: "muted small" }, "Target"), target),
      columns.length ? h("datalist", { id: listId }, columns.map((column) => h("option", { value: column }))) : null,
      saveTarget,
      options.onUseDataset ? button("Use in prompt", { kind: "small", onClick: () => options.onUseDataset(dataset) }) : null,
      button("Replace file", { kind: "small", onClick: () => replaceFile.click() }),
      remove,
      replaceFile,
    ),
    status,
  );
}

function addForm(holder, options, items) {
  // The native file control labels itself in the operating system's language; a hidden input
  // behind a console button keeps the form in one language and one look.
  const file = h("input", { type: "file", accept: ".csv,.parquet", hidden: true, "aria-label": "Dataset file" });
  const pick = h("button", { class: "btn btn-small", type: "button", onClick: () => file.click() }, "Choose file");
  const chosenName = h("span", { class: "chosen-file muted small mono" }, "no file chosen");
  const name = h("input", { class: "input input-small", placeholder: "dataset_name", autocomplete: "off", "aria-label": "Dataset name" });
  const target = h("input", { class: "input input-small", placeholder: "target column (optional)", "aria-label": "Target column" });
  const status = h("span", { class: "form-status", role: "status" });
  const progress = h("progress", { class: "upload-progress", max: 1, value: 0, hidden: true });
  const submit = h("button", { class: "btn btn-primary btn-small", type: "submit" }, "Upload and declare");
  file.addEventListener("change", () => {
    const chosen = file.files?.[0];
    chosenName.textContent = chosen ? `${chosen.name} · ${bytes(chosen.size)}` : "no file chosen";
    chosenName.title = chosen?.name ?? "";
    if (chosen && !name.value.trim()) name.value = datasetNameFrom(chosen.name);
  });
  return h(
    "form",
    {
      class: "dataset-add",
      onSubmit: async (event) => {
        event.preventDefault();
        const chosen = file.files?.[0];
        const datasetName = name.value.trim();
        if (!chosen) {
          status.textContent = "Choose a CSV or Parquet file.";
          return;
        }
        if (!NAME_PATTERN.test(datasetName)) {
          status.textContent = "A dataset name uses lowercase letters, digits, '_' and '-'.";
          return;
        }
        if (items.some((item) => item.name === datasetName) && !window.confirm(`Replace the file of ${datasetName}?`)) return;
        await upload(holder, options, { name: datasetName, file: chosen, target: target.value.trim() || null, status, progress, submit });
      },
    },
    h("p", { class: "field-label" }, "Add a dataset"),
    h("div", { class: "dataset-add-row" }, pick, chosenName, name, target, submit, file),
    progress,
    status,
  );
}

async function upload(holder, options, { name, file, target, status, progress, submit }) {
  if (submit) submit.disabled = true;
  if (progress) {
    progress.hidden = false;
    progress.value = 0;
  }
  status.textContent = `Uploading ${file.name}…`;
  try {
    const result = await uploadDataset(name, file, {
      target,
      onProgress: (ratio) => {
        if (progress) progress.value = ratio;
        status.textContent = ratio < 1 ? `Uploading ${Math.round(ratio * 100)}%…` : "Checking that the file can be registered…";
      },
    });
    knownColumns.set(result.dataset.name, result.columns);
    const targetText = result.dataset.target
      ? `, target ${result.dataset.target}`
      : "; set a target column if the project predicts one";
    await loadDatasets(holder, options, {
      text: `${result.dataset.name} declared: ${result.rows} rows, ${result.columns.length} columns${targetText}. The next run registers it as a source.`,
      tone: "ok",
    });
  } catch (error) {
    status.textContent = `${error.code}: ${error.message}`;
    if (submit) submit.disabled = false;
    if (progress) progress.hidden = true;
  }
}

function datasetNameFrom(filename) {
  const stem = filename
    .replace(/\.[^.]+$/, "")
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "_")
    .replace(/^[^a-z0-9]+/, "")
    .replace(/_+$/, "");
  return stem.slice(0, 64) || "dataset";
}

async function loadContext(holder) {
  try {
    const doc = await api.projectContext();
    replace(holder, contextEditor(doc));
  } catch (error) {
    replace(holder, errorNotice(error));
  }
}

function contextEditor(doc) {
  let saved = doc.text.replace(/\r\n/g, "\n");
  let digest = doc.sha256;
  const area = h(
    "textarea",
    { class: "input context-editor", rows: 14, spellcheck: "false", readonly: doc.redacted, "aria-label": "Project context document" },
    saved,
  );
  const state = h("span", { class: "muted small" });
  const status = h("span", { class: "form-status", role: "status" });
  const save = h("button", { class: "btn btn-primary btn-small", type: "button" }, "Save context");
  const revert = h("button", { class: "btn btn-small", type: "button" }, "Revert");
  const sync = () => {
    const dirty = area.value !== saved;
    save.disabled = doc.redacted || !dirty;
    revert.disabled = !dirty;
    if (dirty) state.textContent = "Unsaved changes";
    else if (digest === EMPTY_SHA256) state.textContent = "No context.md yet; write one and save it.";
    else state.textContent = `Saved · sha256 ${shortHash(digest)}`;
  };
  area.addEventListener("input", sync);
  area.addEventListener("keydown", (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "s") {
      event.preventDefault();
      if (!save.disabled) save.click();
    }
  });
  revert.addEventListener("click", () => {
    area.value = saved;
    status.textContent = "";
    sync();
  });
  save.addEventListener("click", async () => {
    save.disabled = true;
    status.textContent = "Saving…";
    try {
      const next = await api.saveProjectContext(area.value, digest);
      saved = area.value;
      digest = next.sha256;
      status.textContent = "Saved. Runs started from now on read this version.";
    } catch (error) {
      status.textContent =
        error.code === "context_conflict"
          ? "context.md changed after you opened it. Copy your text, then reopen this view."
          : `${error.code}: ${error.message}`;
    }
    sync();
  });
  sync();
  return h(
    "div",
    { class: "stack-tight" },
    doc.redacted
      ? notice("This document holds values the API masks for export, such as e-mail addresses, so it is read-only here. Edit .thymira/context.md directly.", "warn")
      : null,
    area,
    h("div", { class: "form-actions" }, state, status, revert, save),
  );
}
