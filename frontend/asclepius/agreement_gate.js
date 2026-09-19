/* Shared interpretation of the agreement refusal. Signing always uses the
 * existing portal agreement screen; a new tab keeps an unfinished form intact. */
(function () {
  'use strict';
  function isRequired(err) {
    if (!err || err.status !== 403) return false;
    if (err.agreementGate) return true;
    const d = err.detail;
    return !!(d && typeof d === 'object' && d.error === 'agreement_required');
  }
  function signingLink() {
    const link = document.createElement('a');
    link.href = (window.__REALM === 'sandbox' ? '/sandbox' : '') + '/asclepius#agreement';
    link.target = '_blank';
    link.rel = 'noopener';
    link.textContent = 'Read and sign the agreement';
    return link;
  }
  window.AsclepiusAgreementGate = { isRequired, signingLink };
})();
