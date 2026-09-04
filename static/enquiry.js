/* Enquiry form: answer in place instead of reloading the page.
 *
 * Submitting used to re-render the whole document, which dropped the visitor at
 * the top with the "Thank you" somewhere below — they had to go looking for the
 * confirmation of the thing they just did.
 *
 * PROGRESSIVE ENHANCEMENT, not a rewrite. This posts the SAME body to the SAME
 * URL the form already targets and swaps in the #enquire section from the reply.
 * There is no API, no second code path and no duplicated validation: the server
 * remains the only thing that decides whether a lead is accepted, what the
 * consent stamp says, and what compliance copy comes back. Errors swap in the
 * same way, because the server returns the form with its error inside the very
 * same section.
 *
 * If anything at all goes wrong — no fetch, an offline network, a reply that
 * does not parse — it calls the plain submit and the page behaves exactly as it
 * did before. Nothing here is load-bearing.
 *
 * ~1KB, same-origin, under script-src 'self'. It reads nothing and sends
 * nothing the form was not already sending.
 */
(function () {
  "use strict";
  if (!window.fetch || !window.DOMParser || !window.FormData) return;

  var SECTION = "#enquire";

  function wire(section) {
    var form = section.querySelector("form");
    if (!form) return;                       // already submitted: nothing to bind

    form.addEventListener("submit", function (ev) {
      // Let the browser do its own required/pattern checks first, exactly as
      // before — this only takes over once the form is valid.
      if (form.checkValidity && !form.checkValidity()) return;
      if (ev.defaultPrevented) return;
      ev.preventDefault();

      var btn = form.querySelector("button[type=submit]");
      if (btn) { btn.disabled = true; btn.setAttribute("aria-busy", "true"); }

      fetch(form.action, {
        method: "POST",
        body: new URLSearchParams(new FormData(form)).toString(),
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        credentials: "same-origin"
      }).then(function (r) {
        if (!r.ok) throw new Error("bad status");
        return r.text();
      }).then(function (html) {
        var doc = new DOMParser().parseFromString(html, "text/html");
        var fresh = doc.querySelector(SECTION);
        var here = document.querySelector(SECTION);
        if (!fresh || !here) throw new Error("no section");
        here.replaceWith(fresh);

        /* The answer is now exactly where the form was, so there is nothing to
         * scroll to. Focus moves to it so a screen-reader user is told what
         * happened rather than left on a button that vanished. */
        var head = fresh.querySelector("h2");
        if (head) {
          head.setAttribute("tabindex", "-1");
          try { head.focus({ preventScroll: true }); } catch (e) { head.focus(); }
        }
        wire(fresh);                          // an error reply still has a form
      }).catch(function () {
        // Give the browser the submit back rather than stranding the visitor
        // with a dead button: a lead is worth more than the animation.
        if (btn) { btn.disabled = false; btn.removeAttribute("aria-busy"); }
        form.submit();
      });
    });
  }

  var section = document.querySelector(SECTION);
  if (section) wire(section);
})();
