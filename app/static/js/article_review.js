document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("articleReviewForm");
    const editor = document.querySelector('[data-review-panel="editor"]');
    const preview = document.querySelector('[data-review-panel="preview"]');
    const buttons = Array.from(document.querySelectorAll("[data-review-view]"));
    if (!form || !editor || !preview || !buttons.length) return;

    const lines = (value) => String(value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const pairs = (value) => lines(value).map((line) => {
        const split = line.indexOf("|");
        return split < 0 ? [line, ""] : [line.slice(0, split).trim(), line.slice(split + 1).trim()];
    });
    const replaceList = (list, values) => {
        list.replaceChildren(...values.map((value) => {
            const item = document.createElement("li");
            item.textContent = value;
            return item;
        }));
    };
    const refreshPreview = () => {
        const values = new FormData(form);
        preview.querySelector("h2").textContent = values.get("title") || "Untitled article";
        preview.querySelector(".article-preview-overview").textContent = values.get("overview") || "No overview yet.";
        replaceList(preview.querySelector("ol"), lines(values.get("checklist")));
        replaceList(preview.querySelector(".article-preview-columns ul"), lines(values.get("common_indicators")));

        const commandSection = document.getElementById("articlePreviewCommands");
        const commandItems = pairs(values.get("commands"));
        const commandBody = commandSection.querySelector("div");
        commandBody.replaceChildren(...commandItems.flatMap(([command, description]) => {
            const pre = document.createElement("pre");
            const code = document.createElement("code");
            code.textContent = command;
            pre.appendChild(code);
            const copy = document.createElement("p");
            copy.textContent = description;
            return [pre, copy];
        }));
        commandSection.hidden = commandItems.length === 0;

        const sourceSection = document.getElementById("articlePreviewSources");
        const sourceItems = pairs(values.get("sources"));
        const sourceList = sourceSection.querySelector("ul");
        sourceList.replaceChildren(...sourceItems.map(([title, url]) => {
            const item = document.createElement("li");
            const link = document.createElement("a");
            link.textContent = title;
            link.href = url || "#";
            link.target = "_blank";
            link.rel = "noopener noreferrer";
            item.appendChild(link);
            return item;
        }));
        sourceSection.hidden = sourceItems.length === 0;
    };

    buttons.forEach((button) => button.addEventListener("click", () => {
        const showPreview = button.dataset.reviewView === "preview";
        if (showPreview) refreshPreview();
        editor.hidden = showPreview;
        preview.hidden = !showPreview;
        buttons.forEach((item) => {
            const selected = item === button;
            item.classList.toggle("btn-primary", selected);
            item.classList.toggle("btn-outline-primary", !selected);
            item.setAttribute("aria-pressed", String(selected));
        });
    }));
});
