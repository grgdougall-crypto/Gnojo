(() => {
  const button = document.getElementById("copyScript");
  const source = document.getElementById("scriptSource");
  if (!button || !source) return;
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(source.textContent);
      button.textContent = "Copied";
      window.setTimeout(() => { button.innerHTML = '<i class="bi bi-copy me-1"></i>Copy'; }, 1600);
    } catch (_error) { button.textContent = "Copy unavailable"; }
  });
})();
