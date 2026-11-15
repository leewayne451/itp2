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

  // 4) The authenticated chat assistant uses same-origin JSON requests only.
  var assistant = document.getElementById("chat-assistant");
  if (assistant) {
    var panel = document.getElementById("chat-panel");
    var toggle = document.getElementById("chat-toggle");
    var close = document.getElementById("chat-close");
    var form = document.getElementById("chat-form");
    var input = document.getElementById("chat-input");
    var log = document.getElementById("chat-log");

    function showPanel(open) {
      panel.hidden = !open;
      toggle.setAttribute("aria-expanded", String(open));
      if (open) input.focus();
    }

    function addMessage(kind, text, actions) {
      var message = document.createElement("div");
      message.className = "chat-message " + kind;
      message.textContent = text;
      log.appendChild(message);

      (actions || []).forEach(function (action) {
        try {
          var target = new URL(action.url, window.location.href);
          if (target.origin !== window.location.origin) return;
          var link = document.createElement("a");
          link.className = "chat-action";
          link.href = target.href;
          link.textContent = action.label;
          log.appendChild(link);
        } catch (_e) {
          return;
        }
      });
      log.scrollTop = log.scrollHeight;
    }

    function sendMessage(text) {
      var message = text.trim();
      if (!message) return Promise.resolve();
      addMessage("user", message);
      input.value = "";
      var button = form.querySelector("button[type=submit]");
      button.disabled = true;
      return fetch(assistant.dataset.endpoint, {
        method: "POST",
        credentials: "same-origin",
        headers: {"Content-Type": "application/json", "Accept": "application/json"},
        body: JSON.stringify({message: message})
      }).then(function (response) {
        return response.json().catch(function () {
          return {error: "The assistant returned an unexpected response."};
        }).then(function (data) {
          if (!response.ok) {
            addMessage("assistant", data.error || "I couldn't process that request. Please try again.");
            return;
          }
          addMessage("assistant", data.reply, data.actions);
        });
      }).catch(function () {
        addMessage("assistant", "I couldn't reach the assistant. Please try again.");
      }).finally(function () {
        button.disabled = false;
        input.focus();
      });
    }

    toggle.addEventListener("click", function () {
      showPanel(panel.hidden);
    });
    close.addEventListener("click", function () {
      showPanel(false);
      toggle.focus();
    });
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      sendMessage(input.value);
    });
    document.querySelectorAll("[data-chat-prompt]").forEach(function (button) {
      button.addEventListener("click", function () {
        sendMessage(button.dataset.chatPrompt);
      });
    });
    if (assistant.dataset.preview === "true") showPanel(true);
  }
})();
