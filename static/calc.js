/* The referral reward calculator on the landing page.
 *
 * Progressive by construction: the markup already contains a CORRECT worked
 * example (the slider's default amount and its reward, rendered server-side
 * from the same constant), so no-JS, blocked-JS and old browsers all read a
 * true statement. This file only makes that example draggable.
 *
 * The rate is never written here. It arrives on [data-rate], rendered from
 * REFERRAL_RATE_PCT in main.py — the one place the number exists.
 *
 * CSP: script-src 'self', style-src 'self'. Nothing inline; the fill on the
 * slider track is a custom property written through the CSSOM, which is not a
 * style attribute and so is not blocked.
 *
 * No network, no storage, no cookies. It multiplies two numbers.
 */
(function () {
  "use strict";

  var root = document.querySelector("[data-calc]");
  if (!root) return;                                   // not the landing page

  var slider = root.querySelector("[data-calc-range]");
  var outAmt = root.querySelector("[data-calc-amount]");
  var outWord = root.querySelector("[data-calc-words]");
  var outRew = root.querySelector("[data-calc-reward]");
  var chips = root.querySelectorAll("[data-calc-preset]");
  if (!slider || !outAmt || !outRew) return;

  var rate = parseFloat(root.getAttribute("data-rate"));
  if (!isFinite(rate) || rate <= 0) return;            // rate pulled → stay static

  /* Indian digit grouping (10,00,000 — not 1,000,000). Intl does this
   * natively; the manual fallback matters because a shop owner reading
   * "1,000,000" would have to count zeroes to know what it says. */
  var fmt;
  try {
    fmt = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  } catch (e) {
    fmt = null;
  }
  function rupees(n) {
    n = Math.round(n);
    if (fmt) return fmt.format(n);
    var s = String(n), last3 = s.slice(-3), rest = s.slice(0, -3);
    if (!rest) return last3;
    return rest.replace(/\B(?=(\d{2})+(?!\d))/g, ",") + "," + last3;
  }

  /* How the amount is actually said out loud in a Tamil Nadu shop: lakhs, and
   * crores past a hundred of them. "₹12.5 lakh" lands where "₹12,50,000"
   * has to be decoded. */
  function inWords(n) {
    if (n >= 10000000) return trim(n / 10000000) + " crore";
    if (n >= 100000) return trim(n / 100000) + " lakh";
    return rupees(n);
  }
  function trim(v) {
    // one decimal, but never a trailing ".0" — "10 lakh", not "10.0 lakh"
    return String(Math.round(v * 10) / 10);
  }

  function paint() {
    var amt = parseInt(slider.value, 10) || 0;
    var reward = amt * rate / 100;

    outAmt.textContent = "₹" + rupees(amt);
    if (outWord) outWord.textContent = "₹" + inWords(amt);
    outRew.textContent = "₹" + rupees(reward);

    /* The track fills up to the thumb. Written as a custom property so the
     * gradient lives in the stylesheet, not in here. */
    var min = parseInt(slider.min, 10) || 0;
    var max = parseInt(slider.max, 10) || 100;
    var pct = max > min ? ((amt - min) / (max - min)) * 100 : 0;
    root.style.setProperty("--pct", pct.toFixed(2) + "%");

    /* One spoken string instead of a bare number, and no aria-live: a live
     * region on a slider announces on every step of a drag, which is unusable.
     * valuetext is re-read only when the user stops. */
    slider.setAttribute(
      "aria-valuetext",
      "Loan of ₹" + inWords(amt) + " — referral reward ₹" + rupees(reward));

    for (var i = 0; i < chips.length; i++) {
      var on = parseInt(chips[i].getAttribute("data-calc-preset"), 10) === amt;
      chips[i].classList.toggle("on", on);
      chips[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
  }

  slider.addEventListener("input", paint);

  for (var i = 0; i < chips.length; i++) {
    chips[i].addEventListener("click", function (ev) {
      slider.value = this.getAttribute("data-calc-preset");
      paint();
      ev.preventDefault();
    });
  }

  root.classList.add("live");    // reveals the controls the static copy replaces
  paint();
})();
