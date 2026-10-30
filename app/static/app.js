// Comportamenti generali: conferme sui form distruttivi, input OTP numerico.
document.querySelectorAll("form[data-confirm]").forEach(function (f) {
  f.addEventListener("submit", function (e) {
    if (!window.confirm(f.getAttribute("data-confirm"))) e.preventDefault();
  });
});
document.querySelectorAll(".otp-input").forEach(function (i) {
  i.addEventListener("input", function () { i.value = i.value.replace(/\D/g, "").slice(0, i.maxLength); });
  if (!i.disabled) i.focus();
});
document.querySelectorAll("input.upper").forEach(function (i) {
  i.addEventListener("input", function () { i.value = i.value.toUpperCase(); });
});

// Geolocalizzazione facoltativa (pagina cliente): inviata solo se il cliente acconsente.
(function () {
  var note = document.getElementById("geo-note");
  if (!note || !navigator.geolocation || window.sessionStorage.getItem("geo-sent")) return;
  navigator.geolocation.getCurrentPosition(function (pos) {
    fetch(note.dataset.url, {
      method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": note.dataset.csrf },
      body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude, acc: pos.coords.accuracy })
    }).then(function () { window.sessionStorage.setItem("geo-sent", "1"); note.textContent = "Posizione condivisa: verrà annotata nell'Audit Trail."; }).catch(function () {});
  }, function () { note.textContent = "Posizione non condivisa: nell'Audit Trail comparirà N/A."; }, { timeout: 8000, maximumAge: 60000 });
})();
