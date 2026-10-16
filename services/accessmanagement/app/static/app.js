// Progressive enhancement only — all data and enforcement live on the server.
(function () {
  "use strict";

  // 1) Filter the Service dropdown to what the selected employee can actually
  //    request. Data comes from server-rendered attributes; the backend still
  //    validates every submission.
  document.querySelectorAll("form.service-form").forEach(function (form) {
    var mode = form.dataset.mode; // "grant" or "return"
    var target = form.querySelector(".target-select");
    var service = form.querySelector(".service-select");
    if (!target || !service) return;
    var options = Array.prototype.slice.call(service.options);

    function refresh() {
      var opt = target.selectedOptions[0];
      var has = (opt && opt.dataset.access ? opt.dataset.access.split(",") : []).filter(Boolean);
      service.innerHTML = "";
      service.appendChild(new Option(target.value ? "— select service —" : "— select employee first —", ""));
      var shown = 0;
      options.forEach(function (o) {
        if (!o.value) return;
        var owns = has.indexOf(o.value) !== -1;
        if ((mode === "grant" && !owns) || (mode === "return" && owns)) {
          service.appendChild(o.cloneNode(true));
          shown++;
        }
      });
      if (target.value && shown === 0) {
        var note = new Option(mode === "grant" ? "(already has every service)" : "(no services to return)", "");
        note.disabled = true;
        service.appendChild(note);
      }
    }
    target.addEventListener("change", refresh);
    refresh();
  });

  // 2) Confirm destructive submissions.
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) e.preventDefault();
    });
  });

  // 3) Auto-dismiss flash banners after a few seconds.
  setTimeout(function () {
    document.querySelectorAll(".flash").forEach(function (f) { f.style.display = "none"; });
  }, 5000);
})();
