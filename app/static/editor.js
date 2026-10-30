// Editor visivo dei campi: posizionamento manuale, spostamento e ridimensionamento.
// Le coordinate sono frazioni (0..1) della pagina come visualizzata, quindi indipendenti dallo zoom.
(function () {
  var data = JSON.parse(document.getElementById("editor-data").textContent);
  var fields = data.fields || [];
  var labels = data.labels;
  var pagesEl = document.getElementById("pages");
  var listEl = document.getElementById("field-list");
  var msgEl = document.getElementById("editor-msg");
  var selected = null;
  var activePage = 0;

  function pageBox(n) { return pagesEl.querySelector('.page-box[data-page="' + n + '"]'); }

  function render() {
    pagesEl.querySelectorAll(".fld").forEach(function (e) { e.remove(); });
    listEl.innerHTML = "";
    fields.forEach(function (f) {
      var box = pageBox(f.page);
      if (!box) return;
      var el = document.createElement("div");
      el.className = "fld" + (f.type === "signature" ? " sig" : "") + (selected === f.type ? " sel" : "");
      el.style.left = (f.x * 100) + "%"; el.style.top = (f.y * 100) + "%";
      el.style.width = (f.w * 100) + "%"; el.style.height = (f.h * 100) + "%";
      el.textContent = labels[f.type];
      el.dataset.type = f.type;
      var grip = document.createElement("span"); grip.className = "grip"; el.appendChild(grip);
      box.querySelector(".overlay").appendChild(el);
      bindDrag(el, grip, f, box);

      var li = document.createElement("li");
      li.className = selected === f.type ? "sel" : "";
      li.innerHTML = "<span>" + labels[f.type] + " <span class='muted small'>· pagina " + (f.page + 1) + "</span></span>";
      var rm = document.createElement("button"); rm.type = "button"; rm.className = "rm"; rm.title = "Rimuovi"; rm.textContent = "×";
      rm.addEventListener("click", function (e) { e.stopPropagation(); fields = fields.filter(function (x) { return x !== f; }); selected = null; render(); });
      li.appendChild(rm);
      li.addEventListener("click", function () { selected = f.type; render(); el.scrollIntoView({ block: "center", behavior: "smooth" }); });
      listEl.appendChild(li);
    });
    if (!fields.length) listEl.innerHTML = "<li class='muted'>Nessun campo. Aggiungi almeno il campo Firma.</li>";
  }

  function clamp(v, min, max) { return Math.max(min, Math.min(max, v)); }

  function bindDrag(el, grip, f, box) {
    var mode = null, start = null;
    function onDown(e) {
      e.preventDefault();
      mode = (e.target === grip) ? "resize" : "move";
      var r = box.getBoundingClientRect();
      start = { px: e.clientX, py: e.clientY, x: f.x, y: f.y, w: f.w, h: f.h, bw: r.width, bh: r.height };
      selected = f.type; el.classList.add("sel");
      el.setPointerCapture(e.pointerId);
    }
    function onMove(e) {
      if (!mode) return;
      var dx = (e.clientX - start.px) / start.bw, dy = (e.clientY - start.py) / start.bh;
      if (mode === "move") {
        f.x = clamp(start.x + dx, 0, 1 - f.w); f.y = clamp(start.y + dy, 0, 1 - f.h);
      } else {
        f.w = clamp(start.w + dx, 0.03, 1 - f.x); f.h = clamp(start.h + dy, 0.012, 1 - f.y);
      }
      el.style.left = (f.x * 100) + "%"; el.style.top = (f.y * 100) + "%";
      el.style.width = (f.w * 100) + "%"; el.style.height = (f.h * 100) + "%";
    }
    function onUp(e) { if (mode) { mode = null; try { el.releasePointerCapture(e.pointerId); } catch (_) {} render(); } }
    el.addEventListener("pointerdown", onDown);
    el.addEventListener("pointermove", onMove);
    el.addEventListener("pointerup", onUp);
    el.addEventListener("pointercancel", onUp);
  }

  // pagina "attiva" = l'ultima toccata o la più visibile
  pagesEl.querySelectorAll(".page-box").forEach(function (b) {
    b.addEventListener("pointerdown", function () { activePage = parseInt(b.dataset.page, 10); });
  });

  document.querySelectorAll(".add-field").forEach(function (btn) {
    btn.addEventListener("click", function () {
      var type = btn.dataset.type;
      var existing = fields.filter(function (f) { return f.type === type; })[0];
      if (existing) {
        selected = type; render();
        var el = pagesEl.querySelector('.fld[data-type="' + type + '"]');
        if (el) el.scrollIntoView({ block: "center", behavior: "smooth" });
        msgEl.textContent = "Il campo " + labels[type] + " è già presente: puoi spostarlo o ridimensionarlo.";
        return;
      }
      var sig = type === "signature";
      var n = fields.length;
      fields.push({ id: type, type: type, page: activePage, x: 0.12, y: 0.12 + n * 0.05, w: sig ? 0.42 : 0.3, h: sig ? 0.08 : 0.025 });
      selected = type; msgEl.textContent = ""; render();
    });
  });

  var tplCheck = document.getElementById("save-template");
  var tplName = document.getElementById("template-name");
  tplCheck.addEventListener("change", function () { tplName.classList.toggle("hidden", !tplCheck.checked); });

  document.getElementById("save-fields").addEventListener("click", function () {
    if (!fields.some(function (f) { return f.type === "signature"; })) {
      msgEl.textContent = "Aggiungi il campo Firma prima di continuare."; msgEl.className = "small err"; return;
    }
    msgEl.textContent = "Salvataggio…"; msgEl.className = "small muted";
    fetch(data.saveUrl, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": data.csrf },
      body: JSON.stringify({ fields: fields, save_template: tplCheck.checked, template_name: tplName.value })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (j.ok) { window.location.href = j.next; } else { msgEl.textContent = j.error || "Errore"; msgEl.className = "small err"; }
    }).catch(function () { msgEl.textContent = "Errore di rete."; msgEl.className = "small err"; });
  });

  render();
})();
