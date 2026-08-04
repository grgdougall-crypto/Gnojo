document.addEventListener("DOMContentLoaded", () => {
    const buttons = Array.from(document.querySelectorAll("[data-workflow-favorite]"));
    const count = document.getElementById("favoriteWorkflowCount");
    if (!buttons.length) return;

    const updateCount = () => {
        if (count) count.textContent = String(
            buttons.filter((button) => button.getAttribute("aria-pressed") === "true").length
        );
    };

    buttons.forEach((button) => button.addEventListener("click", async () => {
        if (button.disabled) return;
        button.disabled = true;
        try {
            const response = await fetch(button.dataset.favoriteUrl, {
                method: "POST",
                headers: {"Accept": "application/json"},
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "Favorite could not be updated.");
            const favorite = Boolean(result.favorite);
            const card = button.closest(".workflow-card-item");
            if (card) card.dataset.favorite = String(favorite);
            button.setAttribute("aria-pressed", String(favorite));
            button.setAttribute("title", favorite ? "Remove from favorites" : "Add to favorites");
            const workflowName = card?.querySelector("h3")?.textContent.trim() || "workflow";
            button.setAttribute("aria-label", `${favorite ? "Remove" : "Add"} ${workflowName} ${favorite ? "from" : "to"} favorites`);
            const icon = button.querySelector("i");
            icon?.classList.toggle("bi-star-fill", favorite);
            icon?.classList.toggle("bi-star", !favorite);
            updateCount();
            document.dispatchEvent(new CustomEvent("workflow-favorites-changed"));
            window.gnojoAnnounce?.(`${workflowName} ${favorite ? "added to" : "removed from"} favorites.`);
        } catch (error) {
            window.gnojoAnnounce?.(error.message || "Favorite could not be updated.");
        } finally {
            button.disabled = false;
        }
    }));
});
