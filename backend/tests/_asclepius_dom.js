// Minimal DOM shim for exercising asclepius.js render helpers under node.
//
// There is no jsdom in this environment and pulling one in for a handful of
// assertions is not worth the dependency. This implements exactly the surface
// the extracted functions touch: element creation, the child list, class /
// dataset / attribute mutation, event dispatch, and the two querySelector forms
// the step list uses ('.class' and '[data-step-idx="N"]').
//
// It deliberately has NO layout: getBoundingClientRect returns a stub. That is
// fine — the property under test is node IDENTITY (which rows survive a
// toggle), which is what the browser's scroll anchor actually keys off.

class Node {}

class TextNode extends Node {
  constructor(text) { super(); this.nodeValue = String(text); this.parentNode = null; }
  get textContent() { return this.nodeValue; }
  get childNodes() { return []; }
}

class Element extends Node {
  constructor(tagName) {
    super();
    this.tagName = String(tagName).toUpperCase();
    this.childNodes = [];
    this.parentNode = null;
    this.attributes = {};
    this.dataset = {};
    // A real CSSStyleDeclaration returns "" for an unset property, never
    // undefined. Code that saves-and-restores an inline style (scrollByInstant)
    // depends on that, so the shim has to match it.
    this.style = new Proxy({}, {
      get: (t, k) => (typeof k === 'string' && !(k in t) ? '' : t[k]),
      set: (t, k, v) => { t[k] = v == null ? '' : String(v); return true; },
    });
    this.hidden = false;
    this.disabled = false;
    this.value = '';
    this._className = '';
    this._listeners = {};
    this.classList = {
      add: (c) => { const s = this._set(); s.add(c); this._flush(s); },
      remove: (c) => { const s = this._set(); s.delete(c); this._flush(s); },
      contains: (c) => this._set().has(c),
      toggle: (c, on) => {
        const s = this._set();
        const want = on === undefined ? !s.has(c) : !!on;
        if (want) s.add(c); else s.delete(c);
        this._flush(s);
        return want;
      },
    };
  }

  _set() { return new Set(this._className.split(/\s+/).filter(Boolean)); }
  _flush(s) { this._className = Array.from(s).join(' '); }

  get className() { return this._className; }
  set className(v) { this._className = String(v == null ? '' : v); }

  get id() { return this.attributes.id || ''; }
  set id(v) { this.attributes.id = String(v); }

  get firstChild() { return this.childNodes[0] || null; }
  // `children` is element-only; `childNodes` includes text nodes.
  get children() { return this.childNodes.filter((c) => c instanceof Element); }

  get textContent() {
    return this.childNodes.map((c) => c.textContent).join('');
  }
  set textContent(v) {
    this.childNodes.forEach((c) => { c.parentNode = null; });
    this.childNodes = [];
    this.appendChild(new TextNode(v));
  }

  setAttribute(k, v) { this.attributes[k] = String(v); }
  getAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k) ? this.attributes[k] : null; }
  hasAttribute(k) { return Object.prototype.hasOwnProperty.call(this.attributes, k); }
  removeAttribute(k) { delete this.attributes[k]; }

  appendChild(node) {
    if (node.parentNode) node.parentNode.removeChild(node);
    node.parentNode = this;
    this.childNodes.push(node);
    // A browser makes an element with an id findable by getElementById as soon
    // as it is in the document, and code that PORTALS a node to <body> and then
    // looks it up by id (a modal, an overlay) depends on exactly that. The shim
    // used to require an explicit document.register(), which meant such code
    // silently found nothing here and its tests could only assert around the
    // gap. Registering on insert is what the browser does.
    _registerTree(node);
    return node;
  }
  /* Insert before a reference child, or append when the reference is null.
   * Real DOM semantics, added when the admin console's ingest drawer needed
   * it: without it the shim throws and the whole page fails to render, which
   * looks like a mounting bug rather than a missing shim method. */
  insertBefore(node, ref) {
    if (node.parentNode) node.parentNode.removeChild(node);
    const i = ref ? this.childNodes.indexOf(ref) : -1;
    if (i === -1) { this.childNodes.push(node); }
    else { this.childNodes.splice(i, 0, node); }
    node.parentNode = this;
    _registerTree(node);
    return node;
  }

  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i !== -1) { this.childNodes.splice(i, 1); node.parentNode = null; _unregisterTree(node); }
    return node;
  }
  /** ``el.remove()``. The portal's overlays tear themselves down with it —
      without it a "close" handler throws and the overlay stays on screen, which
      is both the browser behaviour and the thing worth testing. */
  remove() { if (this.parentNode) this.parentNode.removeChild(this); return this; }
  replaceChild(fresh, stale) {
    const i = this.childNodes.indexOf(stale);
    if (i === -1) throw new Error('replaceChild: node is not a child');
    if (fresh.parentNode) fresh.parentNode.removeChild(fresh);
    this.childNodes[i] = fresh;
    fresh.parentNode = this;
    stale.parentNode = null;
    return stale;
  }

  addEventListener(type, fn) { (this._listeners[type] = this._listeners[type] || []).push(fn); }
  removeEventListener(type, fn) {
    const l = this._listeners[type] || [];
    const i = l.indexOf(fn);
    if (i !== -1) l.splice(i, 1);
  }
  dispatch(type, event) {
    const ev = Object.assign({ type, target: this, currentTarget: this, preventDefault() {}, stopPropagation() {} }, event || {});
    (this._listeners[type] || []).slice().forEach((fn) => fn(ev));
  }

  // Only the two selector forms the step list actually uses.
  _matches(sel) {
    if (sel[0] === '.') return this.classList.contains(sel.slice(1));
    const m = /^\[([-\w]+)="(.*)"\]$/.exec(sel);
    if (m) {
      const key = m[1].replace(/^data-/, '').replace(/-([a-z])/g, (_, c) => c.toUpperCase());
      return m[1].indexOf('data-') === 0
        ? this.dataset[key] === m[2]
        : this.getAttribute(m[1]) === m[2];
    }
    return false;
  }
  querySelector(sel) {
    for (const c of this.childNodes) {
      if (!(c instanceof Element)) continue;
      if (c._matches(sel)) return c;
      const deep = c.querySelector(sel);
      if (deep) return deep;
    }
    return null;
  }
  querySelectorAll(sel) {
    const out = [];
    for (const c of this.childNodes) {
      if (!(c instanceof Element)) continue;
      if (c._matches(sel)) out.push(c);
      out.push.apply(out, c.querySelectorAll(sel));
    }
    return out;
  }

  // No layout engine. A test that actually depends on geometry installs
  // `globalThis.__measure`, which returns the height of one element; positions
  // are then derived by summing preceding siblings. Everything else keeps the
  // zero rect, which is honest about the shim knowing nothing.
  getBoundingClientRect() {
    const measure = globalThis.__measure;
    if (typeof measure !== 'function') return { top: 0, left: 0, width: 0, height: 0 };
    let top = 0;
    for (let n = this; n; n = n.parentNode) {
      const siblings = n.parentNode ? n.parentNode.children : [];
      for (const s of siblings) {
        if (s === n) break;
        top += measure(s);
      }
    }
    return { top, left: 0, width: 0, height: measure(this) };
  }
  focus() { globalThis.document.activeElement = this; }
  /** Nearest self-or-ancestor matching `sel`, or null.
   *
   *  Added when tutTick started asking whether the caret is inside the current
   *  step's own target. Without it `el.closest` was undefined, the guard's
   *  `typing.closest &&` short-circuited, and a test written to prove the tour
   *  holds still while somebody types passed for the wrong reason: the guard
   *  was never evaluated at all.
   *
   *  Comma-separated selector lists are supported because TOUR_TARGETS uses
   *  them (`[data-substage="refine"], [data-substage="from_scratch"]`).
   */
  closest(sel) {
    const parts = String(sel).split(',').map((s) => s.trim()).filter(Boolean);
    for (let n = this; n; n = n.parentNode) {
      if (!(n instanceof Element)) continue;
      for (const part of parts) if (n._matches(part)) return n;
    }
    return null;
  }
  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
}

/** Index (or drop) every id in a subtree as it enters (or leaves) the document.
 *  Defined before ``document`` so both are hoisted together; the guard on
 *  ``document`` covers the one call that happens while it is still being built.
 */
function _registerTree(node) {
  if (!(node instanceof Element) || typeof document === 'undefined' || !document._byId) return;
  if (node.id) document._byId.set(node.id, node);
  node.childNodes.forEach(_registerTree);
}

function _unregisterTree(node) {
  if (!(node instanceof Element) || typeof document === 'undefined' || !document._byId) return;
  // Only drop the entry if it still points at THIS node: a re-render that
  // replaces a node with a new one carrying the same id registers the new one
  // first, and dropping it on the old node's removal would un-find the live one.
  if (node.id && document._byId.get(node.id) === node) document._byId.delete(node.id);
  node.childNodes.forEach(_unregisterTree);
}

const document = {
  activeElement: null,
  _byId: new Map(),
  _listeners: {},
  body: null,   // assigned below, once Element exists
  createElement(tag) { return new Element(tag); },
  // The referral share row builds its brand marks as real SVG nodes. It has to:
  // that module is held to zero innerHTML by
  // test_no_innerhtml_and_no_long_dashes_in_the_copy, so it cannot take the
  // markup-string shortcut the rail icons use. The namespace is irrelevant to
  // a shim that never lays anything out, so this is createElement with the
  // namespace argument dropped -- but WITHOUT it the whole page throws on
  // render and every DOM test in this file fails at once.
  createElementNS(_ns, tag) { return new Element(tag); },
  createTextNode(t) { return new TextNode(t); },
  getElementById(id) { return document._byId.get(id) || null; },
  register(el) { document._byId.set(el.id, el); return el; },
  addEventListener(type, fn) { (document._listeners[type] = document._listeners[type] || []).push(fn); },
  removeEventListener(type, fn) {
    const l = document._listeners[type] || [];
    const i = l.indexOf(fn);
    if (i !== -1) l.splice(i, 1);
  },
  // Document-level dispatch (Escape / Tab on a modal) — capture-phase only,
  // which is all the code under test registers.
  dispatch(type, event) {
    const ev = Object.assign(
      { type, preventDefault() { ev.defaultPrevented = true; }, defaultPrevented: false },
      event || {});
    (document._listeners[type] || []).slice().forEach((fn) => fn(ev));
    return ev;
  },
};
document.body = new Element('body');

document.documentElement = new Element('html');

// Each entry records the scroll-behavior in effect AT CALL TIME, so a test can
// tell an instant correction from an animated one.
const scrollCalls = [];
const window = {
  scrollBy: (x, y) => scrollCalls.push({
    x, y, behavior: document.documentElement.style.scrollBehavior || '(css default)',
  }),
};

globalThis.Node = Node;
globalThis.Element = Element;
globalThis.document = document;
globalThis.window = window;
globalThis.scrollCalls = scrollCalls;

module.exports = { Node, Element, TextNode, document, window, scrollCalls };
