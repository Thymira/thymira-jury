// Minimal DOM builder. Every value reaches the page as a text node or an attribute, never as parsed
// HTML, so Run data (prompts, tool arguments, artifact text) cannot inject markup.

export function h(tag, props, ...children) {
  const element = document.createElement(tag);
  for (const [key, value] of Object.entries(props ?? {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") {
      const names = Array.isArray(value) ? value.filter(Boolean).join(" ") : value;
      if (names) element.className = names;
    } else if (key === "dataset") {
      Object.assign(element.dataset, value);
    } else if (key === "style") {
      Object.assign(element.style, value);
    } else if (key.startsWith("on") && typeof value === "function") {
      element.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) {
      element.setAttribute(key, "");
    } else {
      element.setAttribute(key, String(value));
    }
  }
  return append(element, children);
}

export function append(parent, children) {
  for (const child of [children].flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    parent.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return parent;
}

export function replace(parent, ...children) {
  parent.replaceChildren();
  return append(parent, children);
}
