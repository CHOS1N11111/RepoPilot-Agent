function render_validation_error_panel(message) {
  const panel = document.querySelector("[data-validation-error]");
  panel.textContent = message;
  panel.hidden = false;
}

function clear_validation_error_panel() {
  const panel = document.querySelector("[data-validation-error]");
  panel.textContent = "";
  panel.hidden = true;
}
