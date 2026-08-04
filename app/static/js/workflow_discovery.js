document.addEventListener("DOMContentLoaded", () => {
    const grid = document.getElementById("workflowCardGrid");
    const search = document.getElementById("workflowFilterSearch");
    const empty = document.getElementById("workflowFilterEmpty");
    const buttons = Array.from(document.querySelectorAll("[data-workflow-category]"));
    if (!grid || !search || !empty || !buttons.length) return;

    const cards = Array.from(grid.querySelectorAll(".workflow-card-item"));
    let category = "all";

    const filter = () => {
        const query = search.value.trim().toLocaleLowerCase();
        let visible = 0;
        cards.forEach((card) => {
            const categoryMatches = category === "all"
                || (category === "favorites" && card.dataset.favorite === "true")
                || (category === "recent" && card.dataset.recent === "true")
                || card.dataset.category === category;
            const searchMatches = !query || card.dataset.search.toLocaleLowerCase().includes(query);
            card.hidden = !(categoryMatches && searchMatches);
            if (!card.hidden) visible += 1;
        });
        empty.hidden = visible !== 0;
    };

    buttons.forEach((button) => button.addEventListener("click", () => {
        category = button.dataset.workflowCategory;
        buttons.forEach((item) => {
            const selected = item === button;
            item.classList.toggle("btn-primary", selected);
            item.classList.toggle("btn-outline-primary", !selected);
            item.setAttribute("aria-pressed", String(selected));
        });
        filter();
    }));
    search.addEventListener("input", filter);
    document.addEventListener("workflow-favorites-changed", filter);
});
