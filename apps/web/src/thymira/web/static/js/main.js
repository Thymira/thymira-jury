// Console entry point: the connect dialog, the API health light and fragment-based routing.
//   #/                          home
//   #/new                       create a Run
//   #/runs/<run_id>[/<tab>[/<item>]]
//   #/settings                  live model routing configuration
//
// The console asks for a token only when the API answers 401, so it never assumes how the API
// authenticates; it only relays what the operator provides.

import { api, clearToken, getToken, setToken } from "./api.js";
import { HomeView } from "./views/home.js";
import { NewRunView } from "./views/new-run.js";
import { RunRail } from "./views/rail.js";
import { RunView } from "./views/run.js";
import { SettingsView } from "./views/settings.js";

const HEALTH_MS = 15000;

const main = document.getElementById("main");
const dialog = document.getElementById("connect");
const form = document.getElementById("connect-form");
const tokenInput = document.getElementById("token");
const connectError = document.getElementById("connect-error");
const cancelButton = document.getElementById("connect-cancel");
const lockButton = document.getElementById("lock");
const apiState = document.getElementById("api-state");

const rail = new RunRail(document.getElementById("rail"));
let view = null;
let dismissed = false;

function routeParts() {
  return window.location.hash
    .replace(/^#\/?/, "")
    .split("/")
    .filter(Boolean)
    .map((part) => decodeURIComponent(part));
}

function show(next) {
  view?.dispose?.();
  view = next;
}

function route() {
  const [section, id, tab, item] = routeParts();
  if (section === "runs" && id) {
    rail.select(id);
    if (view instanceof RunView && view.runId === id) {
      view.setTab(tab, item);
      return;
    }
    show(
      new RunView(main, id, tab, item, {
        onRunChanged: (run) => rail.upsert(run),
        onRunCreated: (run) => {
          rail.upsert(run);
          window.location.hash = `#/runs/${encodeURIComponent(run.id)}`;
        },
      }),
    );
    return;
  }
  rail.select(null);
  if (section === "new") {
    show(
      new NewRunView(main, {
        onCreated: (run) => {
          rail.upsert(run);
          window.location.hash = `#/runs/${encodeURIComponent(run.id)}`;
        },
      }),
    );
    return;
  }
  if (section === "settings") {
    show(new SettingsView(main));
    return;
  }
  show(new HomeView(main));
}

function syncLock() {
  lockButton.textContent = getToken() ? "Disconnect" : "Connect";
}

function openConnect(message) {
  connectError.textContent = message ?? "";
  connectError.hidden = !message;
  tokenInput.value = "";
  if (!dialog.open) dialog.showModal();
  tokenInput.focus();
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const token = tokenInput.value.trim();
  if (!token) {
    openConnect("Paste the API token first.");
    return;
  }
  setToken(token);
  tokenInput.value = "";
  try {
    await api.listRuns({ limit: 1 });
    dismissed = false;
    dialog.close();
    syncLock();
    show(null);
    route();
    rail.refresh();
  } catch (error) {
    clearToken();
    syncLock();
    openConnect(error?.status === 401 ? "The API did not accept that token." : (error?.message ?? "The API could not be reached."));
  }
});

cancelButton.addEventListener("click", () => {
  dismissed = true;
  dialog.close();
});

lockButton.addEventListener("click", () => {
  if (getToken()) {
    clearToken();
    syncLock();
    rail.refresh();
  }
  openConnect();
});

window.addEventListener("thymira:unauthorized", () => {
  const hadToken = Boolean(getToken());
  clearToken();
  syncLock();
  if (dialog.open || (dismissed && !hadToken)) return;
  openConnect(hadToken ? "The API rejected the token. Paste the current one." : null);
});

async function pollHealth() {
  const online = await api.health();
  apiState.dataset.state = online ? "online" : "offline";
  apiState.textContent = online ? "API online" : "API offline";
}

window.addEventListener("hashchange", route);
syncLock();
route();
rail.refresh();
pollHealth();
setInterval(() => {
  if (!document.hidden) pollHealth();
}, HEALTH_MS);
