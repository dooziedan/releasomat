/* "Copy report" buttons: copy the text of the element named in data-copy.
   The clipboard API only works on https or localhost; Releasomat is usually opened at
   http://<server IP>, so there is a fallback with a temporary text field. */
(function () {
  function fallback(text) {
    var field = document.createElement("textarea");
    field.value = text;
    field.setAttribute("readonly", "");
    field.style.position = "fixed";
    field.style.top = "-1000px";
    document.body.appendChild(field);
    field.select();
    var ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    document.body.removeChild(field);
    return ok;
  }

  function show(button, ok) {
    var label = button.getAttribute("data-label") || button.textContent;
    button.setAttribute("data-label", label);
    button.textContent = ok ? "Copied" : "Copy failed: open the report and copy it by hand";
    setTimeout(function () { button.textContent = label; }, 2500);
  }

  document.addEventListener("click", function (event) {
    var button = event.target.closest("[data-copy]");
    if (!button) return;
    var source = document.getElementById(button.getAttribute("data-copy"));
    if (!source) return;
    var text = source.value || source.textContent;
    if (navigator.clipboard && window.isSecureContext) {
      navigator.clipboard.writeText(text).then(
        function () { show(button, true); },
        function () { show(button, fallback(text)); }
      );
    } else {
      show(button, fallback(text));
    }
  });
})();
