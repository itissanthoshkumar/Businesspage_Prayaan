/* Admin-only behaviour: seamless live preview, phone handling, validation.
 *
 * The ONE place the service runs JavaScript, only under /admin, behind login, at
 * script-src 'self'. Public customer pages stay at script-src 'none'. Dependency-
 * free; if it fails, the form still submits and saves the old way.
 */
(function () {
  "use strict";
  var form = document.getElementById("page-form");
  var frame = document.getElementById("preview-frame");
  if (!form || !frame) return;

  var csrfEl = form.querySelector('input[name="csrf"]');
  var CSRF = csrfEl ? csrfEl.value : "";
  var variant = 27;
  var timer = null;
  var lastPayload = "";      // skip refetch when nothing changed
  var ready = false;         // the iframe has done its one full load
  var pending = null;        // latest html waiting for the iframe to be ready
  var followSel;             // preview-follows-field: section selector, "top", or undefined

  /* ---- form -> preview payload ------------------------------------------- */
  function serialize() {
    var data = new URLSearchParams();
    var els = form.elements;
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (!el.name || el.type === "file" || el.disabled) continue;
      if ((el.type === "checkbox" || el.type === "radio") && !el.checked) continue;
      data.append(el.name, el.value);
    }
    data.set("variant", String(variant));
    return data;
  }

  /* ---- SEAMLESS paint ----------------------------------------------------
   * The old code did `frame.srcdoc = html` on every keystroke, which reloads the
   * whole document — re-parsing HTML, RE-FETCHING the stylesheets and Google
   * Fonts, and resetting scroll: the constant flicker. Instead we load the doc
   * ONCE, then on every update swap only <body>'s innerHTML. The <head> (all the
   * CSS + fonts) persists, so there is no network, no reflash, no scroll jump. */
  function paint(html) {
    if (!ready) {
      // first render: one honest full load, and remember when it is done
      pending = null;
      frame.srcdoc = html;
      return;
    }
    var doc = frame.contentDocument;
    if (!doc || !doc.body) { frame.srcdoc = html; return; }
    var incoming;
    try {
      incoming = new DOMParser().parseFromString(html, "text/html");
    } catch (e) { frame.srcdoc = html; return; }
    if (!incoming || !incoming.body) { frame.srcdoc = html; return; }
    var scroller = doc.scrollingElement || doc.documentElement;
    var y = scroller ? scroller.scrollTop : 0;
    doc.body.className = incoming.body.className;
    doc.body.innerHTML = incoming.body.innerHTML;   // same-origin, no re-fetch
    // Follow the field being edited when one is active; otherwise hold the
    // reader's place exactly as before.
    if (followSel !== undefined) scrollPreview();
    else if (scroller) scroller.scrollTop = y;
  }

  frame.addEventListener("load", function () {
    ready = true;
    if (pending !== null) { var h = pending; pending = null; paint(h); }
  });

  function refresh(force) {
    var body = serialize().toString();
    if (!force && body === lastPayload) return;     // nothing changed; don't refetch
    lastPayload = body;
    setBusy(true);
    fetch("/admin/preview", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: body,
      credentials: "same-origin"
    }).then(function (r) { return r.ok ? r.text() : null; })
      .then(function (html) {
        setBusy(false);
        if (html === null) return;
        if (!ready) { pending = html; frame.srcdoc = html; }   // trigger first load
        else paint(html);
      }).catch(function () { setBusy(false); });
  }

  function debounced() { if (timer) clearTimeout(timer); timer = setTimeout(refresh, 220); }

  var busyDot = document.getElementById("preview-busy");
  function setBusy(on) { if (busyDot) busyDot.classList.toggle("on", !!on); }

  form.addEventListener("input", debounced);
  form.addEventListener("change", debounced);

  /* ---- device width toggle + fit-to-scale (unchanged behaviour) ---------- */
  var stage = document.getElementById("preview-stage");
  var holder = document.getElementById("frameholder");
  var mode = "390";

  function fit() {
    if (!stage || !holder) return;
    var logicalW = mode === "0" ? 1200 : 390;
    var logicalH = mode === "0" ? 1500 : 760;
    var avail = stage.clientWidth - 24;
    var scale = Math.min(1, avail / logicalW);
    frame.style.width = logicalW + "px";
    frame.style.height = logicalH + "px";
    frame.style.transform = "scale(" + scale + ")";
    holder.style.width = Math.round(logicalW * scale) + "px";
    holder.style.height = Math.round(logicalH * scale) + "px";
  }
  var devBtns = document.querySelectorAll("[data-device]");
  Array.prototype.forEach.call(devBtns, function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(devBtns, function (x) { x.removeAttribute("aria-current"); });
      b.setAttribute("aria-current", "true");
      mode = b.getAttribute("data-device"); fit();
    });
  });
  window.addEventListener("resize", fit);

  var varBtns = document.querySelectorAll("[data-variant]");
  Array.prototype.forEach.call(varBtns, function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(varBtns, function (x) { x.removeAttribute("aria-current"); });
      b.setAttribute("aria-current", "true");
      variant = parseInt(b.getAttribute("data-variant"), 10) || 27;
      refresh(true);            // variant is not a form field, so force the refetch
    });
  });

  /* ---- phone numbers: two 10-digit inputs -> hidden `phones` ------------- */
  var phone1 = document.getElementById("phone1");
  var phone2 = document.getElementById("phone2");
  var phonesHidden = document.getElementById("phones-hidden");
  var phoneErr = document.getElementById("phone-err");

  function digitsOnly(el) {
    var d = (el.value || "").replace(/\D/g, "").slice(0, 10);
    if (d !== el.value) el.value = d;   // numeric-only, live
    return d;
  }
  function syncPhones() {
    if (!phonesHidden) return;
    var lines = [];
    [phone1, phone2].forEach(function (el) {
      if (!el) return;
      var d = digitsOnly(el);
      if (d) lines.push("+91 " + d);
    });
    phonesHidden.value = lines.join("\n");
  }
  function phoneValid() {
    var bad = false;
    [phone1, phone2].forEach(function (el) {
      if (!el) return;
      var d = (el.value || "").replace(/\D/g, "");
      var ok = d.length === 0 || d.length === 10;   // blank is fine; else exactly 10
      el.classList.toggle("invalid", !ok);
      if (!ok) bad = true;
    });
    if (phoneErr) {
      phoneErr.hidden = !bad;
      if (bad) phoneErr.textContent = "Enter a 10-digit number (just the digits — +91 is added for you).";
    }
    return !bad;
  }
  [phone1, phone2].forEach(function (el) {
    if (!el) return;
    el.addEventListener("input", function () { syncPhones(); phoneValid(); });
    el.addEventListener("blur", phoneValid);
  });
  syncPhones();   // seed the hidden field from any prefilled values

  /* ---- web address composer (create form only) ---------------------------
   * base/STATE/branch/<slug>: the first two segments echo the State and Branch
   * fields live; the slug auto-fills from the business name until the team
   * edits it by hand. The hidden web_address is submitted ONLY when hand-edited,
   * so an untouched auto slug keeps the server's -2/-3 collision suffixing. */
  var slugInput = document.getElementById("slug-input");
  var waHidden = document.getElementById("wa-hidden");
  var addrState = document.getElementById("addr-state");
  var addrBranch = document.getElementById("addr-branch");
  var stField = document.getElementById("st");
  var brField = document.getElementById("br");
  var bnField = document.getElementById("bn0") || form.querySelector('input[name="business_name"]');
  var stateEcho = document.getElementById("state-name-echo");
  var stateNameHidden = document.getElementById("state-name-hidden");
  var slugDirty = false;

  // mirrors store.slugify: ascii, lower, hyphens
  function slugify(t) {
    return String(t || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
      .toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "")
      .replace(/-{2,}/g, "-").slice(0, 60);
  }

  var STATE_NAMES = { TN: "Tamil Nadu", KA: "Karnataka", AP: "Andhra Pradesh",
    TS: "Telangana", TG: "Telangana", KL: "Kerala", PY: "Puducherry",
    MH: "Maharashtra", GJ: "Gujarat", DL: "Delhi", RJ: "Rajasthan",
    MP: "Madhya Pradesh", UP: "Uttar Pradesh", WB: "West Bengal", OD: "Odisha",
    BR: "Bihar", PB: "Punjab", HR: "Haryana", JH: "Jharkhand", CG: "Chhattisgarh",
    AS: "Assam", GA: "Goa", HP: "Himachal Pradesh", UK: "Uttarakhand" };

  function composeAddress() {
    if (!slugInput) return;                       // edit form: address is fixed
    var st = (stField && stField.value || "TN").toUpperCase().slice(0, 3);
    if (addrState) addrState.textContent = st || "TN";
    if (addrBranch) addrBranch.textContent = slugify(brField && brField.value) || "branch";
    if (!slugDirty) slugInput.value = slugify(bnField && bnField.value);
    // only a HAND-EDITED slug travels to the server
    if (waHidden) waHidden.value = slugDirty ? slugify(slugInput.value) : "";
    // state name derives from the code; unknown codes just leave it blank
    var full = STATE_NAMES[st] || "";
    if (stateEcho) stateEcho.textContent = full;
    if (stateNameHidden && slugInput) stateNameHidden.value = full;
  }
  if (slugInput) {
    slugInput.addEventListener("input", function () {
      slugDirty = slugInput.value.trim() !== "";  // emptied by hand = back to auto
      slugInput.value = slugify(slugInput.value) || slugInput.value
        .toLowerCase().replace(/[^a-z0-9-]/g, "");
      composeAddress();
    });
    [stField, brField, bnField].forEach(function (el) {
      if (el) el.addEventListener("input", composeAddress);
    });
    composeAddress();
  } else if (stateNameHidden) {
    // edit form: no composer, but keep any empty state_name aligned with code
    composeAddress = null;
  }

  /* ---- chip editors: Add-button lists -> the hidden newline fields --------
   * "One per line" textareas asked the team to know an invisible convention;
   * a chip with an Add button asks nothing. The hidden inputs keep the exact
   * server contract, so saving is unchanged. */
  function makeChip(text, onRemove) {
    var chip = document.createElement("span");
    chip.className = "chipitem";
    var t = document.createElement("span");
    t.textContent = text;
    chip.appendChild(t);
    var x = document.createElement("button");
    x.type = "button";
    x.textContent = "×";
    x.setAttribute("aria-label", "Remove " + text);
    x.addEventListener("click", onRemove);
    chip.appendChild(x);
    return chip;
  }

  Array.prototype.forEach.call(document.querySelectorAll("[data-chips]"), function (root) {
    var hidden = document.getElementById(root.getAttribute("data-chips") + "-hidden");
    var list = root.querySelector("[data-list]");
    var input = root.querySelector("[data-in]");
    var addBtn = root.querySelector("[data-add]");
    if (!hidden || !list || !input || !addBtn) return;
    var max = parseInt(root.getAttribute("data-max"), 10) || 99;
    var items = (hidden.value || "").split("\n")
      .map(function (s) { return s.trim(); }).filter(Boolean);

    function render() {
      list.innerHTML = "";
      items.forEach(function (text, i) {
        list.appendChild(makeChip(text, function () {
          items.splice(i, 1); sync();
        }));
      });
      var full = items.length >= max;
      input.disabled = full;
      addBtn.disabled = full;
    }
    function sync() { hidden.value = items.join("\n"); render(); debounced(); }
    function add() {
      var v = (input.value || "").trim();
      if (!v || items.length >= max) return;
      items.push(v); input.value = ""; sync(); input.focus();
    }
    addBtn.addEventListener("click", add);
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); add(); }
    });
    render();
  });

  (function () {
    var root = document.querySelector("[data-figs]");
    if (!root) return;
    var hidden = document.getElementById("figures-hidden");
    var list = root.querySelector("[data-list]");
    var labelIn = root.querySelector("[data-fig-label]");
    var valueIn = root.querySelector("[data-fig-value]");
    var addBtn = root.querySelector("[data-add]");
    if (!hidden || !list || !labelIn || !valueIn || !addBtn) return;
    var max = parseInt(root.getAttribute("data-max"), 10) || 4;
    var items = (hidden.value || "").split("\n").map(function (line) {
      var parts = line.split("|");
      if (parts.length < 2) return null;
      return { label: parts[0].trim(), value: parts.slice(1).join("|").trim() };
    }).filter(Boolean);

    function render() {
      list.innerHTML = "";
      items.forEach(function (f, i) {
        list.appendChild(makeChip(f.label + " — " + f.value, function () {
          items.splice(i, 1); sync();
        }));
      });
      var full = items.length >= max;
      labelIn.disabled = full; valueIn.disabled = full; addBtn.disabled = full;
    }
    function sync() {
      hidden.value = items.map(function (f) { return f.label + " | " + f.value; }).join("\n");
      render(); debounced();
    }
    function add() {
      var l = (labelIn.value || "").trim();
      var v = (valueIn.value || "").trim();
      if (!l || !v || items.length >= max) return;
      items.push({ label: l, value: v });
      labelIn.value = ""; valueIn.value = ""; sync(); labelIn.focus();
    }
    addBtn.addEventListener("click", add);
    [labelIn, valueIn].forEach(function (el) {
      el.addEventListener("keydown", function (e) {
        if (e.key === "Enter") { e.preventDefault(); add(); }
      });
    });
    render();
  })();

  /* ---- the preview follows the field being edited -------------------------
   * Editing "Hours" while looking at the hero told the team nothing; now focus
   * in a field scrolls the preview to the section that field feeds, and every
   * repaint re-lands there. Fields without a mapped section keep the old
   * hold-your-place behaviour. */
  function sectionFor(el) {
    var chips = el.closest ? el.closest("[data-chips],[data-figs]") : null;
    if (chips) {
      if (chips.hasAttribute("data-figs")) return "#sec-figures";
      return chips.getAttribute("data-chips") === "languages" ? "#sec-visiting" : "#sec-offerings";
    }
    var id = el.id || "";
    if (id === "sum") return ".lede";
    if (id === "ab") return "#sec-about";
    if (id === "hr") return "#sec-visiting";
    if (id === "bn" || id === "bn0" || id === "on" || id === "cat" || id === "ey" ||
        id === "st" || id === "br" || id === "slug-input" || id === "loc" ||
        id === "dis" || id === "tr" || id === "ts" || id === "mu" ||
        id === "photo-file" || (el.hasAttribute && el.hasAttribute("data-phone"))) return "top";
    return undefined;
  }
  function scrollPreview(smooth) {
    var doc = frame.contentDocument;
    if (!doc || !doc.body) return;
    // scrollIntoView, NOT scrollTop assignment: inside the scaled preview
    // iframe this engine silently ignores root scrollTop writes, while
    // scrollIntoView lands. "instant" (not "auto") so the per-keystroke
    // repaint doesn't restart the page's own smooth-scroll animation; only a
    // focus CHANGE glides.
    var el = followSel === "top" ? doc.body : doc.querySelector(followSel);
    if (!el) return;
    try { el.scrollIntoView({ behavior: smooth ? "smooth" : "instant", block: "start" }); }
    catch (e) { el.scrollIntoView(true); }
  }
  form.addEventListener("focusin", function (e) {
    var sel = sectionFor(e.target);
    if (sel !== undefined) { followSel = sel; scrollPreview(true); }
  });

  /* ---- submit guard ------------------------------------------------------ */
  var saveHint = document.getElementById("save-hint");
  form.addEventListener("submit", function (e) {
    syncPhones();
    var problems = [];
    if (!phoneValid()) problems.push("phone");
    // native `required` covers business_name / state / branch; catch them too so
    // the message is consistent and the first bad field gets focus.
    var firstBad = null;
    Array.prototype.forEach.call(form.querySelectorAll("[required]"), function (el) {
      if (!el.value.trim()) { el.classList.add("invalid"); problems.push(el.name); if (!firstBad) firstBad = el; }
      else el.classList.remove("invalid");
    });
    if (problems.length) {
      e.preventDefault();
      var target = firstBad || (phone1 && phone1.classList.contains("invalid") ? phone1 : null);
      if (target) target.focus();
      if (saveHint) {
        saveHint.textContent = "Please fix the highlighted field" + (problems.length > 1 ? "s" : "") + " before saving.";
        saveHint.classList.add("bad");
      }
    }
  });
  form.addEventListener("input", function () {
    if (saveHint) saveHint.classList.remove("bad");
  });

  /* ---- photo upload (unchanged) ----------------------------------------- */
  var fileInput = document.getElementById("photo-file");
  var hidden = form.querySelector('input[name="photo_url"]');
  var thumb = document.getElementById("photo-thumb");
  var note = document.getElementById("photo-note");
  function setNote(msg, kind) { if (!note) return; note.textContent = msg; note.className = "uploadnote" + (kind ? " " + kind : ""); }
  if (fileInput) {
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (file.size > 6 * 1024 * 1024) { setNote("That image is larger than 6 MB. Please choose a smaller one.", "bad"); fileInput.value = ""; return; }
      setNote("Uploading …", "");
      var reader = new FileReader();
      reader.onload = function () {
        fetch("/admin/upload", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ csrf: CSRF, data: String(reader.result) }),
          credentials: "same-origin"
        }).then(function (r) { return r.json().then(function (j) { return { ok: r.ok, j: j }; }); })
          .then(function (res) {
            if (!res.ok || res.j.error) { setNote(res.j.error || "Upload failed.", "bad"); return; }
            if (hidden) hidden.value = res.j.url;
            if (thumb) { thumb.src = res.j.url; thumb.hidden = false; }
            setNote(res.j.committed ? "Uploaded and saved to the image store."
                                    : "Uploaded (image store note: " + res.j.note + ").",
                    res.j.committed ? "good" : "warn");
            refresh(true);
          }).catch(function () { setNote("Upload failed. Check your connection and try again.", "bad"); });
      };
      reader.readAsDataURL(file);
    });
  }

  fit();
  refresh(true);   // first paint
})();
