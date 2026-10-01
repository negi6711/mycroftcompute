// Runs before first paint. Applies a saved theme and marks whether motion pre-states may apply.
// If main.js never takes over (blocked or failed), the page un-hides itself after three seconds.
(function () {
  var root = document.documentElement;
  root.classList.add("js");
  try {
    var saved = localStorage.getItem("mycroft-theme");
    if (saved === "light" || saved === "dark") root.setAttribute("data-theme", saved);
  } catch (e) {}
  var reduce = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  if (!reduce) {
    root.classList.add("motion");
    setTimeout(function () {
      if (!root.classList.contains("motion-live")) root.classList.remove("motion");
    }, 3000);
  }
})();
