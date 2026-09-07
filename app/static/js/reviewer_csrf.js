(() => {
    const token = document.querySelector('meta[name="gnojo-csrf-token"]')?.content;
    if (!token) return;

    const modifyingMethods = new Set(["POST", "PUT", "PATCH", "DELETE"]);
    const isLocal = (url) => {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (_error) {
            return false;
        }
    };

    document.addEventListener("submit", (event) => {
        const form = event.target;
        if (!(form instanceof HTMLFormElement)) return;
        const method = (form.method || "GET").toUpperCase();
        if (!modifyingMethods.has(method) || !isLocal(form.action)) return;
        if (!form.querySelector('input[name="authenticity_token"]')) {
            const input = document.createElement("input");
            input.type = "hidden";
            input.name = "authenticity_token";
            input.value = token;
            form.appendChild(input);
        }
    }, true);

    const originalFetch = window.fetch.bind(window);
    window.fetch = (resource, options = {}) => {
        const method = String(options.method || resource?.method || "GET").toUpperCase();
        const url = typeof resource === "string" ? resource : resource?.url;
        if (!modifyingMethods.has(method) || !isLocal(url)) {
            return originalFetch(resource, options);
        }
        const headers = new Headers(options.headers || resource?.headers || {});
        if (!headers.has("X-CSRF-Token")) headers.set("X-CSRF-Token", token);
        return originalFetch(resource, {...options, headers});
    };
})();
