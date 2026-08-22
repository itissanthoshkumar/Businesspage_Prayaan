/* Admin-only behaviour: live preview + photo upload.
 *
 * This is the ONE place the service runs JavaScript, and it runs only under
 * /admin, behind login, at script-src 'self'. Public customer pages stay at
 * script-src 'none' — nothing here loads there. Kept dependency-free and small;
 * if it fails, the form still submits and saves the old way.
 */
(function () {
  "use strict";
  var form = document.getElementById("page-form");
  var frame = document.getElementById("preview-frame");
  if (!form || !frame) return;

  var csrf = form.querySelector('input[name="csrf"]');
  var CSRF = csrf ? csrf.value : "";
  var variant = 27;          // default house variant; the toggle changes it
  var timer = null;

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

  function refresh() {
    fetch("/admin/preview", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: serialize().toString(),
      credentials: "same-origin"
    }).then(function (r) {
      return r.ok ? r.text() : null;
    }).then(function (html) {
      if (html !== null) frame.srcdoc = html;   // same-origin, inherits admin CSP
    }).catch(function () { /* leave the last good preview in place */ });
  }

  function debounced() {
    if (timer) clearTimeout(timer);
    timer = setTimeout(refresh, 300);
  }

  // any edit re-renders the preview
  form.addEventListener("input", debounced);
  form.addEventListener("change", debounced);

  // device width toggle + fit-to-scale.
  // The preview column is far narrower than a desktop viewport, so a full-width
  // iframe would trip the page's MOBILE breakpoints even in "Desktop" mode. We
  // render at a real logical width and scale it down to fit — so Desktop shows
  // the true desktop layout, shrunk, and Phone renders near 1:1.
  var stage = document.getElementById("preview-stage");
  var holder = document.getElementById("frameholder");
  var mode = "390";                      // phones are the audience; default to it

  function fit() {
    if (!stage || !holder) return;
    var logicalW = mode === "0" ? 1200 : 390;
    var logicalH = mode === "0" ? 1500 : 760;
    var avail = stage.clientWidth - 24;  // stage padding
    var scale = Math.min(1, avail / logicalW);
    frame.style.width = logicalW + "px";
    frame.style.height = logicalH + "px";
    frame.style.transform = "scale(" + scale + ")";
    // the holder occupies the SCALED footprint so nothing overflows the stage
    holder.style.width = Math.round(logicalW * scale) + "px";
    holder.style.height = Math.round(logicalH * scale) + "px";
  }

  var devBtns = document.querySelectorAll("[data-device]");
  Array.prototype.forEach.call(devBtns, function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(devBtns, function (x) { x.removeAttribute("aria-current"); });
      b.setAttribute("aria-current", "true");
      mode = b.getAttribute("data-device");
      fit();
    });
  });
  window.addEventListener("resize", fit);

  // variant toggle
  var varBtns = document.querySelectorAll("[data-variant]");
  Array.prototype.forEach.call(varBtns, function (b) {
    b.addEventListener("click", function () {
      Array.prototype.forEach.call(varBtns, function (x) { x.removeAttribute("aria-current"); });
      b.setAttribute("aria-current", "true");
      variant = parseInt(b.getAttribute("data-variant"), 10) || 27;
      refresh();
    });
  });

  /* ---- photo upload ------------------------------------------------------
   * Read the file in the browser, base64 it, POST as JSON — no multipart on
   * the server. The server sniffs the bytes, stores in the git-backed repo and
   * returns the URL, which becomes the hidden photo_url the save reads. */
  var fileInput = document.getElementById("photo-file");
  var hidden = form.querySelector('input[name="photo_url"]');
  var thumb = document.getElementById("photo-thumb");
  var note = document.getElementById("photo-note");

  function setNote(msg, kind) {
    if (!note) return;
    note.textContent = msg;
    note.className = "uploadnote" + (kind ? " " + kind : "");
  }

  if (fileInput) {
    fileInput.addEventListener("change", function () {
      var file = fileInput.files && fileInput.files[0];
      if (!file) return;
      if (file.size > 6 * 1024 * 1024) {
        setNote("That image is larger than 6 MB. Please choose a smaller one.", "bad");
        fileInput.value = "";
        return;
      }
      setNote("Uploading …", "");
      var reader = new FileReader();
      reader.onload = function () {
        fetch("/admin/upload", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ csrf: CSRF, data: String(reader.result) }),
          credentials: "same-origin"
        }).then(function (r) {
          return r.json().then(function (j) { return { ok: r.ok, j: j }; });
        }).then(function (res) {
          if (!res.ok || res.j.error) {
            setNote(res.j.error || "Upload failed.", "bad");
            return;
          }
          if (hidden) hidden.value = res.j.url;
          if (thumb) { thumb.src = res.j.url; thumb.hidden = false; }
          setNote(res.j.committed ? "Uploaded and saved to the image store."
                                  : "Uploaded (image store note: " + res.j.note + ").",
                  res.j.committed ? "good" : "warn");
          refresh();
        }).catch(function () {
          setNote("Upload failed. Check your connection and try again.", "bad");
        });
      };
      reader.readAsDataURL(file);
    });
  }

  fit();
  refresh();   // first paint
})();
