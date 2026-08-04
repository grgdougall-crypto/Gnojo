document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("[data-confirm-restart]").forEach((link) => {
        link.addEventListener("click", (event) => {
            if (!window.confirm("Restart this workflow from the beginning? Your current path will be replaced.")) {
                event.preventDefault();
            }
        });
    });
});
