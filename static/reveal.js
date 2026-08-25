/* Reveal-on-scroll for the public pages — the prayaancapital.com entrance:
 * an element crossing into view triggers a TIMED, once-only rise (never
 * scrubbed by scroll position, which lags the finger on main-thread Chromes).
 *
 * Failure-safe by construction: content is visible by default, and is hidden
 * ONLY after this script tags <html> with .js-anim — before first paint, since
 * this file loads blocking in <head> (≈½KB, cached). No JS, blocked JS, old
 * browser, or Reduce Motion → the page simply shows everything, static.
 *
 * This is the one script the public surface runs (CSP script-src 'self').
 * It reads nothing, sends nothing, and touches only class names.
 */
(function () {
  "use strict";
  if (window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (!("IntersectionObserver" in window)) return;

  document.documentElement.className += " js-anim";   // pre-paint: arms the CSS

  window.addEventListener("DOMContentLoaded", function () {
    var sections = document.querySelectorAll(".reveal");
    if (!sections.length) return;
    var io = new IntersectionObserver(function (entries) {
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          entries[i].target.classList.add("in");       // children cascade in CSS
          io.unobserve(entries[i].target);             // play once, then done
        }
      }
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.05 });
    for (var j = 0; j < sections.length; j++) io.observe(sections[j]);
  });
})();
