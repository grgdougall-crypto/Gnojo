document.addEventListener("DOMContentLoaded", () => {
    const liveRegion = document.getElementById("a11yStatus");
    let lastActivator = null;
    let announcementTimer;

    document.addEventListener("click", (event) => {
        const control = event.target.closest("button, a, [role='button']");
        if (control) lastActivator = control;
    }, true);

    document.querySelectorAll("dialog").forEach((dialog) => {
        dialog.setAttribute("aria-modal", "true");
        let opener = null;
        const observer = new MutationObserver(() => {
            if (!dialog.open) return;
            opener = lastActivator instanceof HTMLElement ? lastActivator : document.activeElement;
            window.setTimeout(() => {
                const preferred = dialog.querySelector("[autofocus], input:not([type='hidden']):not([disabled]), textarea:not([disabled]), select:not([disabled]), button:not([disabled])");
                preferred?.focus();
            }, 0);
        });
        observer.observe(dialog, {attributes: true, attributeFilter: ["open"]});
        dialog.addEventListener("close", () => {
            if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
        });
        dialog.addEventListener("cancel", () => {
            window.setTimeout(() => {
                if (opener instanceof HTMLElement && opener.isConnected) opener.focus();
            }, 0);
        });
    });

    const announce = (message) => {
        if (!liveRegion || !message) return;
        window.clearTimeout(announcementTimer);
        announcementTimer = window.setTimeout(() => {
            liveRegion.textContent = "";
            window.requestAnimationFrame(() => { liveRegion.textContent = message; });
        }, 80);
    };

    document.querySelectorAll("[data-a11y-live]").forEach((target) => {
        const observer = new MutationObserver(() => {
            if (!target.hidden) announce(target.textContent.trim());
        });
        observer.observe(target, {childList: true, subtree: true, attributes: true, attributeFilter: ["hidden"]});
    });

    document.querySelectorAll("form").forEach((form) => {
        form.addEventListener("invalid", (event) => {
            announce(event.target.validationMessage || "Check the highlighted field.");
        }, true);
    });

    document.querySelectorAll("[aria-pressed]").forEach((control) => {
        control.addEventListener("click", () => {
            window.setTimeout(() => {
                if (control.getAttribute("aria-pressed") === "true") announce(`${control.textContent.trim()} selected`);
            }, 0);
        });
    });
});
