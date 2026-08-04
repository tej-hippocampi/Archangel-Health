/* ============================================================
   Asclepius — Earnings view behavior. Vanilla JS, no framework,
   matching the product's style. Wire-up: call pxEarnings.init(root)
   after the earnings fragment is in the DOM, or just include this
   file on a page that already contains the markup (it self-inits
   on DOMContentLoaded).
   ============================================================ */
(function (global) {
  'use strict';

  var DOT = { success: 'dot-green', error: 'dot-pink', info: 'dot-lime' };

  function ensureToastRegion() {
    var r = document.getElementById('pxToastRegion');
    if (!r) {
      r = document.createElement('div');
      r.id = 'pxToastRegion';
      r.className = 'px-toast-region';
      r.setAttribute('aria-live', 'assertive');
      document.body.appendChild(r);
    }
    return r;
  }

  function toast(msg, kind) {
    kind = kind || 'info';
    var region = ensureToastRegion();
    var t = document.createElement('div');
    t.className = 'px-toast';
    var d = document.createElement('span');
    d.className = 'dot ' + (DOT[kind] || 'dot-lime');
    var s = document.createElement('span');
    s.textContent = msg;
    t.appendChild(d); t.appendChild(s);
    region.appendChild(t);
    setTimeout(function () {
      t.style.transition = 'opacity .2s ease';
      t.style.opacity = '0';
      setTimeout(function () { t.remove(); }, 220);
    }, kind === 'error' ? 5000 : 3000);
  }

  function init(root) {
    root = root || document;
    // "See all" — hook this to the real history route during integration.
    root.querySelectorAll('[data-px-action="see-all"]').forEach(function (btn) {
      btn.addEventListener('click', function () {
        toast('Preview mode — the full history opens here', 'info');
      });
    });
    var signout = document.getElementById('pxSignout');
    if (signout && !signout.dataset.pxBound) {
      signout.dataset.pxBound = '1';
      signout.addEventListener('click', function () { toast('Preview mode — sign-out is disabled', 'info'); });
    }
  }

  global.pxEarnings = { init: init, toast: toast };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', function () { init(document); });
  } else {
    init(document);
  }
})(window);
