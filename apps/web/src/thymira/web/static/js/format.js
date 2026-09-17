// Pure display formatting. Nothing here reads or changes Run state. The console is English, so
// dates and numbers use an English locale rather than whatever the browser defaults to.

const LOCALE = "en-GB";
const RELATIVE = new Intl.RelativeTimeFormat(LOCALE, { numeric: "auto", style: "narrow" });
const NUMBER = new Intl.NumberFormat(LOCALE);

function parsed(iso) {
  const time = Date.parse(iso ?? "");
  return Number.isNaN(time) ? null : new Date(time);
}

export function relative(iso) {
  const date = parsed(iso);
  if (!date) return "—";
  const seconds = Math.round((date.getTime() - Date.now()) / 1000);
  const size = Math.abs(seconds);
  if (size < 45) return "just now";
  if (size < 3600) return RELATIVE.format(Math.round(seconds / 60), "minute");
  if (size < 86400) return RELATIVE.format(Math.round(seconds / 3600), "hour");
  return RELATIVE.format(Math.round(seconds / 86400), "day");
}

export function timestamp(iso) {
  const date = parsed(iso);
  if (!date) return "—";
  return date.toLocaleString(LOCALE, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function clock(iso) {
  const date = parsed(iso);
  if (!date) return "—";
  return date.toLocaleTimeString(LOCALE, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hour12: false,
  });
}

export function shortId(id) {
  if (!id) return "—";
  const cut = id.indexOf("_");
  if (cut < 0 || id.length - cut < 12) return id;
  return `${id.slice(0, cut + 1)}${id.slice(cut + 1, cut + 5)}…${id.slice(-4)}`;
}

export function shortHash(hash) {
  return hash ? `${hash.slice(0, 10)}…${hash.slice(-6)}` : "—";
}

export function usd(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  return `$${value.toFixed(value >= 1 ? 2 : 4)}`;
}

export function integer(value) {
  return typeof value === "number" && Number.isFinite(value) ? NUMBER.format(value) : "—";
}

export function bytes(value) {
  if (typeof value !== "number" || !Number.isFinite(value)) return "—";
  if (value < 1024) return `${value} B`;
  const units = ["KB", "MB", "GB"];
  let size = value / 1024;
  let unit = 0;
  while (size >= 1024 && unit < units.length - 1) {
    size /= 1024;
    unit += 1;
  }
  return `${size.toFixed(size >= 10 ? 0 : 1)} ${units[unit]}`;
}

export function percent(value) {
  return typeof value === "number" && Number.isFinite(value) ? `${Math.round(value * 100)}%` : "—";
}

export function headline(text, max = 120) {
  const line = String(text ?? "").trim().split(/\r?\n/, 1)[0] ?? "";
  if (!line) return "(empty prompt)";
  return line.length > max ? `${line.slice(0, max - 1).trimEnd()}…` : line;
}

export function clip(text, max = 140) {
  const value = String(text ?? "").replace(/\s+/g, " ").trim();
  return value.length > max ? `${value.slice(0, max - 1)}…` : value;
}

export function plainQuestion(text) {
  return String(text ?? "").replace(/\*\*/g, "").trim();
}

export function list(values) {
  return Array.isArray(values) && values.length ? values.join(", ") : "—";
}
