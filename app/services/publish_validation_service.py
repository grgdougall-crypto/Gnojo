class PublishValidationService:
    """
    Validate whether a command draft is ready for publication.
    """

    def validate_command_draft(self, draft):
        """
        Return publication readiness and a list of missing sections.
        """

        missing_sections = []

        if not draft.get("command_name", "").strip():
            missing_sections.append("Command Name")

        if not draft.get("summary", "").strip():
            missing_sections.append("Summary")

        if not draft.get("syntax", "").strip():
            missing_sections.append("Syntax")

        if not draft.get("examples"):
            missing_sections.append("Examples")

        if not draft.get("official_references"):
            missing_sections.append("Official References")

        explanation = draft.get("explanation")

        if explanation is None:
            missing_sections.append("Knowledge Analysis")
        else:
            if not explanation.purpose.strip():
                missing_sections.append(
                    "Knowledge Analysis Purpose"
                )

            if not explanation.narrative.strip():
                missing_sections.append(
                    "Technician Narrative"
                )

        is_valid = len(missing_sections) == 0

        return is_valid, missing_sections