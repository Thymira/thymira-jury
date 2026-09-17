import { h, replace } from "../dom.js";
import { STATUS_ORDER, statusLabel, statusTone } from "../status.js";

export class HomeView {
  constructor(root) {
    replace(
      root,
      h(
        "section",
        { class: "home" },
        h("p", { class: "eyebrow" }, "Thymira console"),
        h("h1", null, "Pick a run from the list, or start a new one."),
        h(
          "p",
          { class: "lede" },
          "Every screen here is read from the API: the Run record, its hash-chained event log, its artifact manifest and MIRA's audit. The console keeps no Run state of its own.",
        ),
        h("div", { class: "home-actions" }, h("a", { class: "btn btn-primary", href: "#/new" }, "New run")),
        h(
          "section",
          { class: "legend", "aria-label": "Orchestrators" },
          h("h2", null, "Orchestrators"),
          h(
            "ul",
            { class: "legend-prose" },
            h("li", null, h("span", { class: "status tone-thy" }, h("i", { class: "sq" }), "THY — runs the data-science work")),
            h(
              "li",
              null,
              h("span", { class: "status tone-mira" }, h("i", { class: "sq" }), "MIRA — audits it against methodology and regulation"),
            ),
          ),
        ),
        h(
          "section",
          { class: "legend", "aria-label": "Run statuses" },
          h("h2", null, "Run statuses"),
          h(
            "ul",
            null,
            STATUS_ORDER.map((status) =>
              h("li", null, h("span", { class: `status tone-${statusTone(status)}` }, h("i", { class: "sq" }), statusLabel(status))),
            ),
          ),
        ),
      ),
    );
  }
}
