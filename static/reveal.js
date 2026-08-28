/* Reveal-on-scroll for the public pages — the prayaancapital.com entrance:
 * an element crossing into view triggers a TIMED, once-only rise (never
 * scrubbed by scroll position, which lags the finger on main-thread Chromes).
 *
 * Failure-safe by construction: content is visible by default, and is hidden
 * ONLY after this script tags <html> with .js-anim — before first paint, since
 * this file loads blocking in <head> (≈¾KB, cached). No JS, blocked JS, old
 * browser, or Reduce Motion → the page simply shows everything, static.
 *
 * This is the one script the public surface runs (CSP script-src 'self').
 * It reads nothing, sends nothing, and touches only class names and one
 * custom property.
 *
 * Two additions serve the landing page and no-op everywhere else (a customer
 * page has neither a [data-stagger] group nor a .navsentinel), so the
 * behaviour of the pages this script was written for is unchanged.
 */
(function () {
  "use strict";
  if (window.matchMedia
      && window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  if (!("IntersectionObserver" in window)) return;

  document.documentElement.className += " js-anim";   // pre-paint: arms the CSS

  window.addEventListener("DOMContentLoaded", function () {

    /* staggerChildren, without a framework. Each child of a [data-stagger]
     * container is told its index and the CSS turns that into a delay, so a
     * list of any length cascades — no nth-child ladder to keep in sync.
     * Written through the CSSOM on purpose: a style ATTRIBUTE in the markup
     * would be dropped by style-src 'self'. */
    var groups = document.querySelectorAll("[data-stagger]");
    for (var g = 0; g < groups.length; g++) {
      var kids = groups[g].children;
      for (var k = 0; k < kids.length; k++) {
        kids[k].style.setProperty("--i", k);
      }
    }

    /* The landing nav lifts once the page has moved. A sentinel and an
     * observer rather than a scroll listener: nothing runs on the main thread
     * between the two crossings. */
    var sentinel = document.querySelector(".navsentinel");
    var nav = document.querySelector(".nav");
    if (sentinel && nav) {
      new IntersectionObserver(function (e) {
        nav.classList.toggle("scrolled", !e[0].isIntersecting);
      }, { threshold: 0 }).observe(sentinel);
    }

    /* The landing page's dot rail: mark the section currently holding the
     * viewport, and flip the rail to its light treatment over dark sections.
     * A customer page has no [data-dot], so this whole block no-ops there. */
    var dots = document.querySelectorAll("[data-dot]");
    if (dots.length) {
      var byId = {};
      for (var d = 0; d < dots.length; d++) {
        byId[(dots[d].getAttribute("href") || "").slice(1)] = dots[d];
      }
      var secs = document.querySelectorAll("main > section[id]");
      var railIo = new IntersectionObserver(function (entries) {
        for (var i = 0; i < entries.length; i++) {
          if (!entries[i].isIntersecting) continue;
          var el = entries[i].target;
          for (var k = 0; k < dots.length; k++) dots[k].classList.remove("on");
          if (byId[el.id]) byId[el.id].classList.add("on");
          document.body.classList.toggle(
            "ondark-rail", el.classList.contains("ondark"));
        }
      }, { threshold: 0.55 });
      for (var s = 0; s < secs.length; s++) railIo.observe(secs[s]);
    }

    var sections = document.querySelectorAll(".reveal");
    if (!sections.length) return;

    var fired = false;
    var io = new IntersectionObserver(function (entries) {
      fired = true;
      for (var i = 0; i < entries.length; i++) {
        if (entries[i].isIntersecting) {
          entries[i].target.classList.add("in");       // children cascade in CSS
          io.unobserve(entries[i].target);             // play once, then done
        }
      }
    }, { rootMargin: "0px 0px -10% 0px", threshold: 0.05 });

    /* Anything already on screen is revealed NOW rather than waited for. The
     * CSS hides content the moment .js-anim is set, so a first callback that
     * never arrives — a throttled or backgrounded tab defers delivery
     * indefinitely — would otherwise leave the hero blank. Measuring is
     * cheaper than trusting. */
    for (var j = 0; j < sections.length; j++) {
      var box = sections[j].getBoundingClientRect();
      if (box.top < window.innerHeight && box.bottom > 0) {
        sections[j].classList.add("in");
      } else {
        io.observe(sections[j]);
      }
    }

    /* Last resort for everything below the fold: if the observer has not
     * delivered anything at all by now, it is not going to. Dropping .js-anim
     * un-hides the whole page in one go, because every rule that hides
     * anything is scoped under it. */
    setTimeout(function () {
      if (fired) return;
      document.documentElement.className =
        document.documentElement.className.replace(/\s*js-anim\b/, "");
    }, 2500);
  });
})();
