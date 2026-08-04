document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("globalSearchInput");
    const suggestionsBox = document.getElementById("searchSuggestions");

    if (!searchInput || !suggestionsBox) {
        return;
    }

    let debounceTimer;
    let activeRequest;
    let activeIndex = -1;

    const getSuggestionItems = () => {
        return Array.from(
            suggestionsBox.querySelectorAll(".search-suggestion-item")
        );
    };

    const clearActiveSuggestion = () => {
        const items = getSuggestionItems();

        items.forEach((item) => {
            item.classList.remove("active");
            item.setAttribute("aria-selected", "false");
        });

        activeIndex = -1;
    };

    const setActiveSuggestion = (index) => {
        const items = getSuggestionItems();

        if (items.length === 0) {
            activeIndex = -1;
            return;
        }

        items.forEach((item) => {
            item.classList.remove("active");
            item.setAttribute("aria-selected", "false");
        });

        if (index < 0) {
            activeIndex = items.length - 1;
        } else if (index >= items.length) {
            activeIndex = 0;
        } else {
            activeIndex = index;
        }

        const activeItem = items[activeIndex];

        activeItem.classList.add("active");
        activeItem.setAttribute("aria-selected", "true");
        activeItem.scrollIntoView({
            block: "nearest",
        });
    };

    const hideSuggestions = () => {
        suggestionsBox.hidden = true;
        suggestionsBox.innerHTML = "";
        searchInput.setAttribute("aria-expanded", "false");
        clearActiveSuggestion();
    };

    const buildSuggestionUrl = (suggestion) => {
        if (suggestion.content_type === "Command") {
            return `/commands/${encodeURIComponent(suggestion.id)}`;
        }

        if (suggestion.content_type === "Article") {
            return `/knowledge/published/${encodeURIComponent(
                suggestion.id
            )}`;
        }

        if (suggestion.content_type === "Workflow") {
            return `/wizard?workflow=${encodeURIComponent(suggestion.id)}`;
        }

        return "#";
    };

    const renderSuggestions = (suggestions) => {
        suggestionsBox.innerHTML = "";
        activeIndex = -1;

        if (suggestions.length === 0) {
            hideSuggestions();
            return;
        }

        suggestions.forEach((suggestion, index) => {
            const link = document.createElement("a");

            link.className = "search-suggestion-item";
            link.href = buildSuggestionUrl(suggestion);
            link.setAttribute("role", "option");
            link.setAttribute("aria-selected", "false");
            link.dataset.index = String(index);

            const type = document.createElement("span");
            type.className = "search-suggestion-type";
            type.textContent = suggestion.content_type;

            const content = document.createElement("span");
            content.className = "search-suggestion-content";

            const title = document.createElement("strong");
            title.className = "search-suggestion-title";
            title.textContent = suggestion.title;

            const summary = document.createElement("span");
            summary.className = "search-suggestion-summary";
            summary.textContent = suggestion.summary;

            content.append(title, summary);
            link.append(type, content);
            suggestionsBox.append(link);

            link.addEventListener("mouseenter", () => {
                setActiveSuggestion(index);
            });
        });

        suggestionsBox.hidden = false;
        searchInput.setAttribute("aria-expanded", "true");
    };

    const fetchSuggestions = async (query) => {
        if (activeRequest) {
            activeRequest.abort();
        }

        activeRequest = new AbortController();

        try {
            const response = await fetch(
                `/api/search/suggestions?q=${encodeURIComponent(query)}`,
                {
                    signal: activeRequest.signal,
                }
            );

            if (!response.ok) {
                throw new Error("Suggestion request failed.");
            }

            const data = await response.json();
            renderSuggestions(data.suggestions || []);
        } catch (error) {
            if (error.name !== "AbortError") {
                console.error(error);
                hideSuggestions();
            }
        }
    };

    searchInput.addEventListener("input", () => {
        const query = searchInput.value.trim();

        clearTimeout(debounceTimer);
        clearActiveSuggestion();

        if (query.length < 2) {
            hideSuggestions();
            return;
        }

        debounceTimer = setTimeout(() => {
            fetchSuggestions(query);
        }, 250);
    });

    searchInput.addEventListener("keydown", (event) => {
        const items = getSuggestionItems();

        if (event.key === "ArrowDown") {
            if (items.length === 0 || suggestionsBox.hidden) {
                return;
            }

            event.preventDefault();
            setActiveSuggestion(activeIndex + 1);
            return;
        }

        if (event.key === "ArrowUp") {
            if (items.length === 0 || suggestionsBox.hidden) {
                return;
            }

            event.preventDefault();
            setActiveSuggestion(activeIndex - 1);
            return;
        }

        if (event.key === "Enter") {
            if (
                activeIndex >= 0 &&
                activeIndex < items.length &&
                !suggestionsBox.hidden
            ) {
                event.preventDefault();
                items[activeIndex].click();
            }

            return;
        }

        if (event.key === "Escape") {
            hideSuggestions();
            searchInput.blur();
        }
    });

    document.addEventListener("click", (event) => {
        if (
            !suggestionsBox.contains(event.target) &&
            event.target !== searchInput
        ) {
            hideSuggestions();
        }
    });
});
