// The login form's behaviour, a file of its own rather than inline in
// login.html: the dashboard's CSP (nginx/default.conf) allows scripts from
// this origin only, so an inline <script> would never run. Served outside
// the passcode gate, like login.html itself.
(function () {
  var COOKIE = "ma_passcode";
  var form = document.getElementById("f");
  var input = document.getElementById("p");
  var error = document.getElementById("e");

  // This page is only ever rendered as the body of a 401. So if we get
  // here having just submitted, the passcode we set was rejected — that
  // flag is the only way a static page can tell a first visit from a
  // failed attempt.
  try {
    if (sessionStorage.getItem("tried") === "1") {
      error.hidden = false;
      sessionStorage.removeItem("tried");
    }
  } catch (ignored) {
    // Private mode, or storage blocked. The form still works; it just
    // cannot explain a rejection.
  }

  form.addEventListener("submit", function (event) {
    event.preventDefault();
    var value = input.value.trim();
    if (!value) return;

    try {
      sessionStorage.setItem("tried", "1");
    } catch (ignored) {
      /* see above */
    }

    // Session-length by default. Secure only over HTTPS, or the cookie
    // would be dropped on a plain-HTTP local run and the gate would look
    // broken rather than open.
    var cookie =
      COOKIE +
      "=" +
      encodeURIComponent(value) +
      "; path=/; max-age=2592000; samesite=lax";
    if (location.protocol === "https:") cookie += "; secure";
    document.cookie = cookie;

    // Back to whatever was originally asked for: this page is the 401
    // body for that URL, so the address bar still holds it and a reload
    // retries it with the cookie attached.
    location.reload();
  });
})();
