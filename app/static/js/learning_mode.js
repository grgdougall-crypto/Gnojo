document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".learning-quiz-check").forEach((button) => {
        button.addEventListener("click", () => {
            const container = button.closest(".mt-3");
            const selected = container?.querySelector('input[name="knowledge_quiz"]:checked');
            const feedback = container?.querySelector(".learning-quiz-feedback");
            if (!feedback) return;
            if (!selected) {
                feedback.textContent = "Choose an answer first.";
                feedback.className = "small mt-2 mb-0 learning-quiz-feedback text-warning";
                return;
            }
            const correct = selected.value === button.dataset.correctAnswer;
            feedback.textContent = correct ? "Correct — you identified the key idea." : `Not quite. The correct answer is: ${button.dataset.correctAnswer}`;
            feedback.className = `small mt-2 mb-0 learning-quiz-feedback ${correct ? "text-success" : "text-danger"}`;
        });
    });
});
