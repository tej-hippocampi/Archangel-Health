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
    this.style = {};
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
    return node;
  }
  removeChild(node) {
    const i = this.childNodes.indexOf(node);
    if (i !== -1) { this.childNodes.splice(i, 1); node.parentNode = null; }
    return node;
  }
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

  getBoundingClientRect() { return { top: 0, left: 0, width: 0, height: 0 }; }
  focus() { globalThis.document.activeElement = this; }
  contains(other) {
    for (let n = other; n; n = n.parentNode) if (n === this) return true;
    return false;
  }
}

const document = {
  activeElement: null,
  _byId: new Map(),
  _listeners: {},
  body: null,   // assigned below, once Element exists
  createElement(tag) { return new Element(tag); },
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

const scrollCalls = [];
const window = { scrollBy: (x, y) => scrollCalls.push([x, y]) };

globalThis.Node = Node;
globalThis.Element = Element;
globalThis.document = document;
globalThis.window = window;
globalThis.scrollCalls = scrollCalls;

module.exports = { Node, Element, TextNode, document, window, scrollCalls };
