import { h } from "../../dom.js";
import { bytes, integer, shortHash, timestamp } from "../../format.js";
import { button, codeBlock, copyable, downloadBytes, empty, errorNotice, kv, loading, mono, notice, tag } from "../../ui.js";

const CSV_PREVIEW_ROWS = 200;

export function renderArtifacts(ctx) {
  let items = [];
  let query = "";
  let showSuperseded = false;
  let selectedId = ctx.item ?? null;
  const list = h("ol", { class: "file-list" });
  const summary = h("p", { class: "list-summary muted small" });
  const preview = h("div", { class: "artifact-preview" }, empty("Select an artifact to preview it."));
  const tools = h(
    "div",
    { class: "list-tools" },
    h("input", {
      class: "input input-small",
      type: "search",
      placeholder: "Filter by name",
      "aria-label": "Filter artifacts by name",
      onInput: (event) => {
        query = event.target.value.trim().toLowerCase();
        draw();
      },
    }),
    h(
      "label",
      { class: "check" },
      h("input", {
        type: "checkbox",
        onChange: (event) => {
          showSuperseded = event.target.checked;
          draw();
        },
      }),
      "Superseded",
    ),
  );
  const listPane = h("section", { class: "artifact-list", "aria-label": "Artifacts" }, tools, loading());

  function fileRow(artifact) {
    return h(
      "li",
      null,
      h(
        "button",
        {
          type: "button",
          class: ["file", artifact.id === selectedId ? "is-selected" : null, artifact.valid ? null : "is-invalid"],
          onClick: () => select(artifact),
        },
        h("span", { class: "file-name mono" }, artifact.name),
        h("span", { class: "file-meta" }, [bytes(artifact.size_bytes), artifact.media_type, artifact.valid ? null : "superseded"].filter(Boolean).join(" · ")),
      ),
    );
  }

  function draw() {
    const visible = items.filter((artifact) => (showSuperseded || artifact.valid) && artifact.name.toLowerCase().includes(query));
    summary.textContent = `${integer(items.filter((artifact) => artifact.valid).length)} active · ${integer(items.length)} recorded`;
    if (!visible.length) {
      list.replaceChildren(h("li", null, empty(items.length ? "No artifact matches." : "This run has no artifacts yet.")));
      return;
    }
    const groups = new Map();
    for (const artifact of visible) {
      const kind = artifact.kind || "other";
      if (!groups.has(kind)) groups.set(kind, []);
      groups.get(kind).push(artifact);
    }
    const sortedKinds = [...groups.keys()].sort((left, right) => left.localeCompare(right));
    list.replaceChildren(
      ...sortedKinds.flatMap((kind) => [
        h("li", { class: "file-group" }, `${kind} (${groups.get(kind).length})`),
        ...groups.get(kind).map(fileRow),
      ]),
    );
  }

  async function select(artifact) {
    selectedId = artifact.id;
    draw();
    window.history.replaceState(null, "", `#/runs/${encodeURIComponent(ctx.runId)}/artifacts/${encodeURIComponent(artifact.id)}`);
    preview.replaceChildren(metadata(artifact), loading("Reading and verifying the content…"));
    try {
      const data = await ctx.api.artifact(ctx.runId, artifact.id);
      const nodes = await contentView(data);
      if (selectedId === artifact.id) preview.replaceChildren(...nodes);
    } catch (error) {
      if (selectedId === artifact.id) preview.replaceChildren(metadata(artifact), errorNotice(error));
    }
  }

  ctx.api
    .artifacts(ctx.runId)
    .then((body) => {
      items = body.items;
      listPane.replaceChildren(tools, summary, list);
      draw();
      const initial = items.find((artifact) => artifact.id === selectedId);
      if (initial) select(initial);
    })
    .catch((error) => listPane.replaceChildren(tools, errorNotice(error)));

  return h("div", { class: "artifacts" }, listPane, preview);
}

function metadata(artifact) {
  return kv([
    ["Name", mono(artifact.name)],
    ["Artifact", copyable(artifact.id)],
    ["Kind", artifact.kind],
    ["Media type", artifact.media_type],
    ["Size", bytes(artifact.size_bytes)],
    ["SHA-256", copyable(artifact.sha256)],
    ["Produced by", mono(artifact.produced_by)],
    ["Created", timestamp(artifact.created_at)],
    [
      "State",
      artifact.valid
        ? tag("ACTIVE", "ok")
        : [tag("SUPERSEDED"), artifact.invalidated_reason ? h("span", { class: "muted" }, ` ${artifact.invalidated_reason}`) : null],
    ],
    artifact.input_artifact_ids?.length ? ["Inputs", h("span", { class: "chips" }, artifact.input_artifact_ids.map((id) => mono(id)))] : null,
    artifact.execution_key ? ["Execution key", mono(artifact.execution_key)] : null,
  ]);
}

async function contentView(data) {
  const artifact = data.artifact;
  const nodes = [metadata(artifact)];
  if (data.content === null || data.content === undefined) {
    nodes.push(notice(data.withheld_reason ?? "The content is withheld.", "warn"));
    return nodes;
  }
  const raw = data.encoding === "base64" ? decodeBase64(data.content) : new TextEncoder().encode(data.content);
  nodes.push(await integrity(data, raw));
  nodes.push(
    h(
      "div",
      { class: "form-actions" },
      button(data.redacted ? "Download the redacted copy" : "Download", {
        onClick: () => downloadBytes(artifact.name.split("/").pop(), raw, artifact.media_type),
      }),
    ),
  );
  nodes.push(renderContent(artifact, data, raw));
  return nodes;
}

async function integrity(data, raw) {
  if (data.redacted) {
    return notice(
      "Redacted projection: sensitive values were masked for export, so this copy no longer matches the manifest digest. The API verified the stored bytes before masking them.",
      "warn",
    );
  }
  const digest = await sha256Hex(raw);
  if (digest === null) return notice("The API verified these bytes against the manifest digest.");
  return digest === data.artifact.sha256
    ? notice(`SHA-256 recomputed in this browser matches the manifest (${shortHash(digest)}).`, "ok")
    : notice(`SHA-256 recomputed in this browser (${shortHash(digest)}) does not match the manifest.`, "block");
}

function renderContent(artifact, data, raw) {
  if (data.encoding === "base64") {
    const mime = imageMime(artifact, raw);
    if (mime) return h("img", { class: "preview-image", alt: artifact.name, src: `data:${mime};base64,${data.content}` });
    return notice(`Binary content, ${bytes(raw.length)}. Download it to open it.`);
  }
  const name = artifact.name.toLowerCase();
  const media = String(artifact.media_type ?? "").toLowerCase();
  if (name.endsWith(".csv") || media === "text/csv") return csvView(data.content);
  if (name.endsWith(".json") || media.includes("json")) {
    try {
      return codeBlock(JSON.stringify(JSON.parse(data.content), null, 2));
    } catch {
      return codeBlock(data.content);
    }
  }
  return codeBlock(data.content, { className: "prose-code" });
}

function csvView(text) {
  const rows = parseCsv(text, CSV_PREVIEW_ROWS + 1);
  if (!rows.length) return empty("The file is empty.");
  const [header, ...body] = rows;
  const lines = text.split("\n").filter((line) => line.trim()).length - 1;
  return h(
    "div",
    { class: "stack-tight" },
    h(
      "p",
      { class: "muted small" },
      `${integer(Math.max(lines, 0))} rows · ${integer(header.length)} columns${lines > CSV_PREVIEW_ROWS ? ` · showing the first ${CSV_PREVIEW_ROWS}` : ""}`,
    ),
    h(
      "div",
      { class: "table-wrap csv" },
      h(
        "table",
        { class: "grid compact" },
        h("thead", null, h("tr", null, header.map((cell) => h("th", null, cell)))),
        h("tbody", null, body.slice(0, CSV_PREVIEW_ROWS).map((row) => h("tr", null, row.map((cell) => h("td", null, cell))))),
      ),
    ),
  );
}

function parseCsv(text, maxRows) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char !== '"') field += char;
      else if (text[index + 1] === '"') {
        field += '"';
        index += 1;
      } else quoted = false;
    } else if (char === '"') {
      quoted = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field.replace(/\r$/, ""));
      rows.push(row);
      row = [];
      field = "";
      if (rows.length >= maxRows) return rows;
    } else {
      field += char;
    }
  }
  if (field || row.length) {
    row.push(field.replace(/\r$/, ""));
    rows.push(row);
  }
  return rows;
}

function decodeBase64(content) {
  const binary = atob(content);
  const out = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) out[index] = binary.charCodeAt(index);
  return out;
}

async function sha256Hex(data) {
  if (!globalThis.crypto?.subtle) return null;
  const digest = await crypto.subtle.digest("SHA-256", data);
  return [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, "0")).join("");
}

function imageMime(artifact, data) {
  const declared = String(artifact.media_type ?? "").toLowerCase();
  if (/^image\/(png|jpeg|gif|webp)$/.test(declared)) return declared;
  const starts = (...signature) => signature.every((value, index) => data[index] === value);
  if (starts(0x89, 0x50, 0x4e, 0x47)) return "image/png";
  if (starts(0xff, 0xd8, 0xff)) return "image/jpeg";
  if (starts(0x47, 0x49, 0x46)) return "image/gif";
  if (starts(0x52, 0x49, 0x46, 0x46) && data[8] === 0x57 && data[9] === 0x45 && data[10] === 0x42 && data[11] === 0x50) return "image/webp";
  return null;
}
