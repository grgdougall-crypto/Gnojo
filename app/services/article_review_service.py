import re

from app.knowledge.article_validator import ArticleValidator


class ArticleReviewError(ValueError):
    pass


class ArticleReviewService:
    CHECKS = {
        "technical_accuracy": "Technical steps and claims were verified",
        "user_safety": "Instructions are safe and appropriately scoped",
        "sources_verified": "Sources are authoritative and support the guidance",
        "commands_reviewed": "Commands, permissions, and risks were reviewed",
    }
    RISK_PATTERNS = (
        "remove-item", "format ", "diskpart", "reg delete", "del ",
        "shutdown", "reset-thispc", "clear-disk", "rm ",
    )

    def analyze(self, article):
        fields = {
            "Title": bool(str(article.get("title", "")).strip()),
            "Category": bool(str(article.get("category", "")).strip()),
            "Estimated time": bool(str(article.get("estimated_time", "")).strip()),
            "Overview": bool(str(article.get("overview", "")).strip()),
            "Checklist": bool(article.get("checklist")),
            "Common indicators": bool(article.get("common_indicators")),
            "Knowledge check": bool(article.get("quiz")),
            "Sources": bool(article.get("sources")),
        }
        score = round((sum(fields.values()) / len(fields)) * 100)
        warnings = []
        combined = " ".join(str(value) for value in (
            article.get("title"), article.get("overview"),
            " ".join(article.get("checklist", []) if isinstance(article.get("checklist"), list) else []),
        )).lower()
        if any(term in combined for term in ("placeholder", "to be determined", "requires human completion")):
            warnings.append({"kind": "placeholder", "level": "high", "message": "Placeholder content remains and must be replaced before publication."})
        if not article.get("sources"):
            warnings.append({"kind": "sources", "level": "medium", "message": "No authoritative sources are listed yet."})
        commands = article.get("commands", []) if isinstance(article.get("commands"), list) else []
        risky = []
        for command in commands:
            text = str(command.get("command", "") if isinstance(command, dict) else command).lower()
            if any(pattern in text for pattern in self.RISK_PATTERNS):
                risky.append(text)
        if risky:
            warnings.append({"kind": "command_risk", "level": "high", "message": f"{len(risky)} command entry may change or remove system data. Verify permissions, recovery, and warnings."})
        elif commands:
            warnings.append({"kind": "commands", "level": "low", "message": "Confirm every command's permissions, side effects, and expected output."})
        validation_errors = ArticleValidator.validate(article)
        return {
            "score": score,
            "fields": fields,
            "warnings": warnings,
            "validation_errors": validation_errors,
            "required_checks": self.CHECKS,
            "can_publish": (
                not validation_errors
                and article.get("review", {}).get("status") == "approved"
                and self.checks_complete(article)
            ),
        }

    def update_from_form(self, article, form):
        updated = dict(article)
        for field in ("title", "category", "estimated_time", "overview"):
            value = form.get(field, "")
            if not isinstance(value, str):
                raise ArticleReviewError(f"{field.replace('_', ' ').title()} must be text.")
            updated[field] = value.strip()
        difficulty = form.get("difficulty")
        if difficulty not in {"Beginner", "Intermediate", "Advanced"}:
            raise ArticleReviewError("Choose a valid difficulty.")
        updated["difficulty"] = difficulty
        updated["checklist"] = self._lines(form.get("checklist", ""))
        updated["common_indicators"] = self._lines(form.get("common_indicators", ""))
        updated["related_topics"] = self._lines(form.get("related_topics", ""))
        updated["commands"] = self._pairs(form.get("commands", ""), "Command")
        updated["sources"] = [
            {"title": item["command"], "url": item["description"]}
            for item in self._pairs(form.get("sources", ""), "Source")
        ]
        question = form.get("quiz_question", "").strip()
        answers = self._lines(form.get("quiz_answers", ""))
        correct = form.get("quiz_correct_answer", "").strip()
        updated["quiz"] = [{"question": question, "answers": answers, "correct_answer": correct}] if question or answers or correct else []
        review = dict(updated.get("review") or {})
        checks = {key: form.get(f"review_{key}") == "on" for key in self.CHECKS}
        review["checks"] = checks
        notes = form.get("review_notes", "")
        review["notes"] = self._lines(notes)
        action = form.get("review_action", "save")
        if action == "submit":
            review["status"] = "pending_review"
        elif action == "approve":
            if not all(checks.values()):
                raise ArticleReviewError("Complete every technical review check before approval.")
            review["status"] = "approved"
        elif action == "reject":
            if not review["notes"]:
                raise ArticleReviewError("Add a review note explaining what needs revision.")
            review["status"] = "rejected"
        elif action != "save":
            raise ArticleReviewError("Unknown review action.")
        updated["review"] = review
        return updated

    def checks_complete(self, article):
        checks = article.get("review", {}).get("checks", {})
        return all(checks.get(key) is True for key in self.CHECKS)

    @staticmethod
    def _lines(value):
        return [line.strip() for line in str(value or "").splitlines() if line.strip()]

    @staticmethod
    def _pairs(value, label):
        result = []
        for index, line in enumerate(str(value or "").splitlines(), 1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.split("|", 1)]
            if len(parts) != 2 or not all(parts):
                raise ArticleReviewError(f"{label} line {index} must use: name | description")
            result.append({"command": parts[0], "description": parts[1]})
        return result
