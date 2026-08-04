document.addEventListener("DOMContentLoaded", () => {
    const form = document.getElementById("workflowFeedbackForm");
    if (!form) return;
    const message = document.getElementById("workflowFeedbackMessage");
    const button = form.querySelector('button[type="submit"]');

    form.addEventListener("submit", async (event) => {
        event.preventDefault();
        if (!form.reportValidity()) return;
        button.disabled = true;
        message.textContent = "Saving feedback…";
        const values = new FormData(form);
        const payload = {
            solved: values.get("solved"),
            clarity: Number(values.get("clarity")),
            confusing_step: values.get("confusing_step"),
            comment: values.get("comment"),
        };
        try {
            const response = await fetch(form.dataset.feedbackUrl, {
                method: "POST",
                headers: {"Content-Type": "application/json", "Accept": "application/json"},
                body: JSON.stringify(payload),
            });
            const result = await response.json();
            if (!response.ok || !result.ok) throw new Error(result.error || "Feedback could not be saved.");
            form.classList.add("workflow-feedback--complete");
            form.innerHTML = '<i class="bi bi-check-circle-fill" aria-hidden="true"></i><div><strong>Thanks for the feedback.</strong><span>Your response is now included in Gnojo\'s local workflow analytics.</span></div>';
            form.setAttribute("role", "status");
            window.gnojoAnnounce?.("Feedback saved.");
        } catch (error) {
            message.textContent = error.message || "Feedback could not be saved.";
            button.disabled = false;
        }
    });
});
