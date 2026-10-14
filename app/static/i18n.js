// Translated dialog texts: base.html stores them as JSON (#tm-i18n), keyed by English text.
// tmT("Error %(status)s", {status: 404}) → UI-language text with substituted values.
(function () {
  let messages = {};
  try { messages = JSON.parse(document.getElementById("tm-i18n").textContent); } catch (e) { /* English. */ }
  window.tmT = function (text, values) {
    return (messages[text] || text).replace(/%\((\w+)\)s/g, (m, key) => (values && key in values ? values[key] : m));
  };
})();
