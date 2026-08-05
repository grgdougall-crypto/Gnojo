document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("articleReviewForm");
    const editor = document.querySelector('[data-review-panel="editor"]');
    const preview = document.querySelector('[data-review-panel="preview"]');
    const buttons = Array.from(document.querySelectorAll("[data-review-view]"));
    if (!form || !editor || !preview || !buttons.length) return;

    const lines = (value) => String(value || "").split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    const pairs = (value, fromRight = false) => lines(value).map((line) => {
        const split = fromRight ? line.lastIndexOf("|") : line.indexOf("|");
        return split < 0 ? [line, ""] : [line.slice(0, split).trim(), line.slice(split + 1).trim()];
    });
    const replaceList = (list, values) => {
        list.replaceChildren(...values.map((value) => {
            const item = document.createElement("li");
            item.textContent = value;
            return item;
        }));
    };

    const sourceWorkspace = document.querySelector(".article-source-workspace");
    const findSourcesButton = document.getElementById("findArticleSourcesButton");
    const sourceMessage = document.getElementById("articleSourceFinderMessage");
    const sourceSuggestions = document.getElementById("articleSourceSuggestions");
    const sourcesValue = document.getElementById("articleSourcesValue");

    const attachSource = (source, button) => {
        const line = `${source.title} | ${source.url}`;
        const current = lines(sourcesValue.value);
        if (!current.includes(line)) current.push(line);
        sourcesValue.value = current.join("\n");
        sourcesValue.dispatchEvent(new Event("input", { bubbles: true }));
        button.disabled = true;
        button.textContent = "Attached";
        sourceMessage.textContent = "Source attached to the draft. Open it for review, then save the draft.";
    };

    const renderSourceSuggestions = (result) => {
        sourceSuggestions.replaceChildren();
        result.suggestions.forEach((source) => {
            const card = document.createElement("article");
            card.className = "article-source-suggestion";
            const content = document.createElement("div");
            const publisher = document.createElement("span");
            publisher.className = "section-eyebrow";
            publisher.textContent = source.publisher;
            const title = document.createElement("h3");
            title.textContent = source.title;
            const reason = document.createElement("p");
            reason.textContent = source.reason;
            content.append(publisher, title, reason);
            const actions = document.createElement("div");
            actions.className = "article-source-suggestion__actions";
            const open = document.createElement("a");
            open.className = "btn btn-sm btn-outline-primary";
            open.href = source.url;
            open.target = "_blank";
            open.rel = "noopener noreferrer";
            open.textContent = "Open source";
            const attach = document.createElement("button");
            attach.className = "btn btn-sm btn-success";
            attach.type = "button";
            attach.textContent = "Attach source";
            attach.addEventListener("click", () => attachSource(source, attach));
            actions.append(open, attach);
            card.append(content, actions);
            sourceSuggestions.appendChild(card);
        });
        sourceSuggestions.hidden = false;
        sourceMessage.textContent = `${result.provider} found ${result.suggestions.length} candidate source${result.suggestions.length === 1 ? "" : "s"}. Verify a page before attaching it.`;
    };

    findSourcesButton?.addEventListener("click", async () => {
        findSourcesButton.disabled = true;
        sourceMessage.classList.remove("is-error");
        sourceMessage.textContent = "Searching current authoritative documentation…";
        sourceSuggestions.hidden = true;
        try {
            const response = await fetch(sourceWorkspace.dataset.sourceFinderUrl, {
                method: "POST",
                headers: { "Accept": "application/json" },
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "Sources could not be found.");
            renderSourceSuggestions(result);
        } catch (error) {
            sourceMessage.textContent = error.message;
            sourceMessage.classList.add("is-error");
        } finally {
            findSourcesButton.disabled = false;
        }
    });
    const refreshPreview = () => {
        const values = new FormData(form);
        preview.querySelector("h2").textContent = values.get("title") || "Untitled article";
        preview.querySelector(".article-preview-overview").textContent = values.get("overview") || "No overview yet.";
        ["category", "difficulty", "estimated_time"].forEach((name) => {
            const item = preview.querySelector(`[data-preview-meta="${name}"]`);
            item.textContent = values.get(name) || "";
            item.hidden = !values.get(name);
        });
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
        const topicSection = document.getElementById("articlePreviewTopics");
        const topicItems = lines(values.get("related_topics"));
        topicSection.querySelector("div").replaceChildren(...topicItems.map((topic) => {
            const badge = document.createElement("span");
            badge.className = "badge text-bg-light border";
            badge.textContent = topic;
            return badge;
        }));
        topicSection.hidden = topicItems.length === 0;

        const quizSection = document.getElementById("articlePreviewQuiz");
        const quizQuestion = String(values.get("quiz_question") || "").trim();
        const quizAnswers = lines(values.get("quiz_answers"));
        quizSection.querySelector("p").textContent = quizQuestion;
        replaceList(quizSection.querySelector("ul"), quizAnswers);
        quizSection.hidden = !quizQuestion && quizAnswers.length === 0;

        const sourceItems = pairs(values.get("sources"), true);
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

        const tagSection = document.getElementById("articlePreviewTags");
        const tagItems = String(values.get("tags") || "").split(/[\n,]+/).map((tag) => tag.trim()).filter(Boolean);
        tagSection.querySelector("div").replaceChildren(...tagItems.map((tag) => {
            const badge = document.createElement("span");
            badge.className = "badge text-bg-light border";
            badge.textContent = tag;
            return badge;
        }));
        tagSection.hidden = tagItems.length === 0;
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
