// Applies the saved theme before the first paint, so the page never flashes
// the wrong one. "system" (nothing saved) follows the OS setting.
try {
  const saved = localStorage.getItem("redtree-theme");
  if (saved === "light" || saved === "dark") document.documentElement.dataset.theme = saved;
} catch {
  /* storage blocked (private window): follow the system */
}
