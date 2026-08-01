document.addEventListener("DOMContentLoaded", () => {
    const searchInput = document.getElementById("globalSearchInput");
    const suggestionsBox = document.getElementById("searchSuggestions");

    if (!searchInput || !suggestionsBox) {
        return;
    }

    let debounceTimer;
    let activeRequest;

    const hideSuggestions = () => {
        suggestionsBox.hidden = true;
        suggestionsBox.innerHTML = "";
        searchInput.setAttribute("aria-expanded", "false");
    };

    const buildSuggestionUrl = (suggestion) => {
        if (suggestion.content_type === "Command") {
            return `/commands/${encodeURIComponent(suggestion.id)}`;
        }

        if (suggestion.content_type === "Article") {
            return `/knowledge/published/${encodeURIComponent(suggestion.id)}`;
        }

        return "#";
    };

    const renderSuggestions = (suggestions) => {
        suggestionsBox.innerHTML = "";

        if (suggestions.length === 0) {
            hideSuggestions();
            return;
        }

        suggestions.forEach((suggestion) => {
            const link = document.createElement("a");
            link.className = "search-suggestion-item";
            link.href = buildSuggestionUrl(suggestion);
            link.setAttribute("role", "option");

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

        if (query.length < 2) {
            hideSuggestions();
            return;
        }

        debounceTimer = setTimeout(() => {
            fetchSuggestions(query);
        }, 250);
    });

    document.addEventListener("click", (event) => {
        if (
            !suggestionsBox.contains(event.target) &&
            event.target !== searchInput
        ) {
            hideSuggestions();
        }
    });

    searchInput.addEventListener("keydown", (event) => {
        if (event.key === "Escape") {
            hideSuggestions();
            searchInput.blur();
        }
    });
});