const root = document.documentElement;
const toggleButton = document.getElementById("themeToggle");

function updateThemeIcon(theme) {
    const icon = toggleButton.querySelector("i");

    if (theme === "dark") {
        icon.className = "bi bi-sun";
    } else {
        icon.className = "bi bi-moon-stars";
    }
}

const savedTheme = localStorage.getItem("supportpilot-theme") || "light";

root.setAttribute("data-bs-theme", savedTheme);
updateThemeIcon(savedTheme);

toggleButton.addEventListener("click", () => {
    const currentTheme = root.getAttribute("data-bs-theme");
    const newTheme = currentTheme === "dark" ? "light" : "dark";

    root.setAttribute("data-bs-theme", newTheme);
    localStorage.setItem("supportpilot-theme", newTheme);
    updateThemeIcon(newTheme);
});

document.addEventListener("DOMContentLoaded", () => {

    const explainButton = document.getElementById(
        "explainCommandButton"
    );

    const explanationPanel = document.getElementById(
        "commandExplanationPanel"
    );

    if (!explainButton || !explanationPanel) {
        return;
    }

    explainButton.addEventListener("click", () => {

        const isHidden = explanationPanel.hidden;

        explanationPanel.hidden = !isHidden;

        explainButton.setAttribute(
            "aria-expanded",
            String(isHidden)
        );

    });

});