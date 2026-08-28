/* Hero constellation for the landing page.
 *
 * SUBJECT, not decoration. One bright node is a customer's page; the quieter
 * nodes ringed around it are the circle that page travels through — family,
 * neighbours, suppliers, regular customers. Every couple of seconds a share
 * leaves the centre, travels out along a connection, and the node it reaches
 * brightens for a moment. That is the whole product in one drawing.
 *
 * An earlier version morphed a cloud of dots between icon outlines. It read as
 * scattered noise rather than an object, because low-resolution stroke
 * sampling never resolves into a recognisable shape at that scale. A tight,
 * dense, connected figure holds together at any size — which is also why the
 * reference site's cluster is small and compact rather than spread wide.
 *
 * 2D canvas, not WebGL: this runs on mid-range Android over 3G, and ~37 nodes
 * cost almost nothing. Same-origin file, so script-src 'self' is untouched.
 *
 * Failure-safe: Reduce Motion, no canvas, or any exception -> draws nothing,
 * and the CSS dust layer underneath stays as a static field. Stops entirely
 * when scrolled away or the tab is hidden. Device pixel ratio capped at 2.
 */
(function () {
  "use strict";
  try {
    var cv = document.getElementById("pfield");
    if (!cv || !cv.getContext) return;
    if (window.matchMedia &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
    var ctx = cv.getContext("2d");
    if (!ctx) return;

    var INK = "38,34,57";      // Prayaan indigo
    var ACC = "11,122,218";    // Prayaan blue
    var RINGS = [
      { n: 7,  r: 0.32, s: 1.9 },
      { n: 12, r: 0.60, s: 1.5 },
      { n: 17, r: 0.88, s: 1.2 }
    ];
    var SHARE_EVERY = 2400;    // ms between shares leaving the centre
    var LINK_DIST = 0.44;      // how near two nodes must be to be joined

    /* ---- the figure ------------------------------------------------------ */
    var N = [{ a: 0, r: 0, s: 3.4, lit: 1, wob: 0, sp: 0 }];   // the page itself
    for (var g = 0; g < RINGS.length; g++) {
      var ring = RINGS[g];
      for (var i = 0; i < ring.n; i++) {
        N.push({
          a: (i / ring.n) * 6.2832 + g * 0.7,    // stagger each ring's spokes
          r: ring.r + (Math.random() - 0.5) * 0.07,
          s: ring.s + Math.random() * 0.5,
          lit: 0,                                 // brightens when a share lands
          wob: Math.random() * 6.2832,
          sp: (0.6 + Math.random() * 0.5) * (g % 2 ? -1 : 1)   // orbit direction
        });
      }
    }
    /* links: centre out to the inner ring, then node to near neighbour */
    var L = [];
    for (var a = 1; a < N.length; a++) {
      if (N[a].r < 0.42) L.push([0, a]);
      for (var b = a + 1; b < N.length; b++) {
        var da = Math.abs(N[a].a - N[b].a);
        if (da > 3.1416) da = 6.2832 - da;
        var d = Math.sqrt(N[a].r * N[a].r + N[b].r * N[b].r -
                          2 * N[a].r * N[b].r * Math.cos(da));
        if (d < LINK_DIST) L.push([a, b]);
      }
    }

    var shares = [];           // travelling pulses: {l:[from,to], t}
    var w = 0, h = 0, dpr = 1, R = 0, cx = 0, cy = 0;
    var raf = 0, running = false, lastShare = 0;

    function size() {
      var r = cv.getBoundingClientRect();
      if (!r.width || !r.height) return;
      dpr = Math.min(window.devicePixelRatio || 1, 2);
      w = r.width; h = r.height;
      cv.width = Math.round(w * dpr);
      cv.height = Math.round(h * dpr);
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      R = Math.min(w, h) * 0.20;                 // compact on purpose
      cx = w >= 900 ? w * 0.72 : w * 0.5;        // clear of the headline
      cy = h * 0.5;
    }

    function pos(node, t) {
      var ang = node.a + t / 42000 * node.sp;                  // slow orbit
      var rad = node.r * R * (1 + Math.sin(t / 4200 + node.wob) * 0.035);
      return [cx + Math.cos(ang) * rad, cy + Math.sin(ang) * rad];
    }

    function frame(t) {
      if (!running) return;
      ctx.clearRect(0, 0, w, h);

      if (t - lastShare > SHARE_EVERY) {
        lastShare = t;
        if (L.length) shares.push({ l: L[(Math.random() * L.length) | 0], t: 0 });
        if (shares.length > 6) shares.shift();
      }

      var p = [], k;
      for (k = 0; k < N.length; k++) p.push(pos(N[k], t));

      /* the centre's halo sits underneath everything */
      var halo = ctx.createRadialGradient(p[0][0], p[0][1], 0,
                                          p[0][0], p[0][1], R * 0.62);
      halo.addColorStop(0, "rgba(" + ACC + ",0.13)");
      halo.addColorStop(1, "rgba(" + ACC + ",0)");
      ctx.beginPath();
      ctx.arc(p[0][0], p[0][1], R * 0.62, 0, 6.2832);
      ctx.fillStyle = halo;
      ctx.fill();

      /* links, so nodes sit on top of them */
      ctx.lineWidth = 0.7;
      ctx.strokeStyle = "rgba(" + INK + ",0.14)";
      for (k = 0; k < L.length; k++) {
        var A = p[L[k][0]], B = p[L[k][1]];
        ctx.beginPath();
        ctx.moveTo(A[0], A[1]);
        ctx.lineTo(B[0], B[1]);
        ctx.stroke();
      }

      /* shares travelling outward */
      for (k = shares.length - 1; k >= 0; k--) {
        var s = shares[k];
        s.t += 0.015;
        if (s.t >= 1) { N[s.l[1]].lit = 1; shares.splice(k, 1); continue; }
        var e = s.t < 0.5 ? 2 * s.t * s.t : 1 - Math.pow(-2 * s.t + 2, 2) / 2;
        var P0 = p[s.l[0]], P1 = p[s.l[1]];
        var x = P0[0] + (P1[0] - P0[0]) * e, y = P0[1] + (P1[1] - P0[1]) * e;
        ctx.beginPath();
        ctx.moveTo(P0[0], P0[1]);
        ctx.lineTo(x, y);
        ctx.strokeStyle = "rgba(" + ACC + "," + (0.36 * (1 - s.t)) + ")";
        ctx.lineWidth = 1.2;
        ctx.stroke();
        ctx.lineWidth = 0.7;
        ctx.strokeStyle = "rgba(" + INK + ",0.14)";
        ctx.beginPath();
        ctx.arc(x, y, 2.1, 0, 6.2832);
        ctx.fillStyle = "rgba(" + ACC + ",0.92)";
        ctx.fill();
      }

      /* nodes */
      for (k = 0; k < N.length; k++) {
        var n = N[k];
        n.lit *= 0.966;                                  // fade after a share lands
        var acc = k === 0 ? 1 : n.lit;
        ctx.beginPath();
        ctx.arc(p[k][0], p[k][1], n.s * (1 + n.lit * 0.85), 0, 6.2832);
        ctx.fillStyle = acc > 0.02
          ? "rgba(" + ACC + "," + (0.5 + acc * 0.45) + ")"
          : "rgba(" + INK + ",0.36)";
        ctx.fill();
      }
      raf = window.requestAnimationFrame(frame);
    }

    function start() {
      if (!running) { running = true; raf = window.requestAnimationFrame(frame); }
    }
    function stop() {
      running = false;
      if (raf) window.cancelAnimationFrame(raf);
      raf = 0;
    }

    size();
    window.addEventListener("resize", size);
    document.addEventListener("visibilitychange", function () {
      if (document.hidden) stop(); else start();
    });
    if ("IntersectionObserver" in window) {
      new IntersectionObserver(function (e) {
        if (e[0].isIntersecting) start(); else stop();
      }, { threshold: 0 }).observe(cv);
    } else {
      start();
    }
  } catch (e) {
    /* the CSS dust layer is the fallback */
  }
})();
