from __future__ import annotations

import json
from pathlib import Path

from .models import AuditResult, Finding


class AuditReportWriter:
    PARTITIONS = {
        "defects.json": lambda item: item.classification == "defect",
        "risks.json": lambda item: item.classification == "risk",
        "opportunities.json": lambda item: item.classification == "opportunity",
        "recommendations.json": lambda item: item.classification == "recommendation",
        "workflow_findings.json": lambda item: item.domain == "workflow",
        "source_findings.json": lambda item: item.domain == "source",
        "relationship_findings.json": lambda item: item.finding_type in {"malformed_relationship", "orphaned_content", "article_candidate"},
        "application_findings.json": lambda item: item.domain in {"application", "test"},
        "taxonomy_findings.json": lambda item: item.domain == "taxonomy",
    }

    def write(self, result: AuditResult, output_root: Path) -> Path:
        run_directory = output_root / result.run_id
        run_directory.mkdir(parents=True, exist_ok=False)
        self._json(run_directory / "audit_results.json", result.to_dict())
        self._json(run_directory / "inventory.json", {
            "run_id": result.run_id,
            "items": [item.to_dict() for item in result.inventory],
        })
        self._json(run_directory / "coverage_gaps.json", result.coverage)
        self._json(run_directory / "knowledge_tasks.json", result.knowledge_tasks)
        self._json(run_directory / "knowledge_debt.json", result.knowledge_debt)
        self._json(run_directory / "knowledge_health.json", result.knowledge_health)
        self._json(run_directory / "lessons_learned.json", result.lessons_learned)
        self._json(run_directory / "memory_summary.json", result.memory_summary)
        for filename, predicate in self.PARTITIONS.items():
            self._json(run_directory / filename, {
                "run_id": result.run_id,
                "findings": [item.to_dict() for item in result.findings if predicate(item)],
            })
        (run_directory / "audit_summary.md").write_text(self._markdown(result), encoding="utf-8")
        (run_directory / "audit.log.jsonl").write_text(
            json.dumps({"event": "audit_completed", "run_id": result.run_id, "summary": result.summary()}, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return run_directory

    @staticmethod
    def _json(path: Path, value) -> None:
        path.write_text(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")

    @staticmethod
    def _markdown(result: AuditResult) -> str:
        summary = result.summary()
        lines = [
            "# Gnojo Curator Audit Summary", "",
            f"- Run: `{result.run_id}`", f"- Auditor: `{result.auditor_version}`",
            f"- Started: {result.started_at}", f"- Completed: {result.completed_at}",
            f"- Findings: **{summary['findings']}**", "", "## Inventory", "",
        ]
        lines.extend(f"- {name}: {count}" for name, count in summary["inventory"].items())
        lines.extend(["", "## Finding Classification", ""])
        lines.extend(f"- {name.title()}: {count}" for name, count in sorted(summary["findings_by_classification"].items()))
        sections = [
            ("Critical Defects", lambda item: item.classification == "defect" and item.severity in {"critical", "high"}),
            ("Other Defects", lambda item: item.classification == "defect" and item.severity not in {"critical", "high"}),
            ("Content Risks", lambda item: item.classification == "risk"),
            ("Knowledge Opportunities", lambda item: item.classification == "opportunity"),
            ("Editorial Recommendations", lambda item: item.classification == "recommendation" and item.finding_type != "coverage_imbalance" and item.content_type != "application"),
            ("Coverage Improvements", lambda item: item.finding_type == "coverage_imbalance"),
            ("System Improvements", lambda item: item.classification == "recommendation" and item.content_type == "application" and item.finding_type != "coverage_imbalance"),
        ]
        if not result.findings:
            lines.extend(["", "## Critical Defects", "", "No findings matched this run."])
        for heading, predicate in sections:
            items = [item for item in result.findings if predicate(item)]
            lines.extend(["", f"## {heading}", ""])
            if not items:
                lines.extend(["No observations in this section.", ""])
                continue
            for item in items:
                lines.extend([
                    f"### [{item.severity.upper()}] {item.title}", "",
                    f"- ID: `{item.identifier}`", f"- Class: {item.classification.title()}",
                    f"- Affected: `{item.content_type}:{item.content_identifier}`",
                    f"- Domain: {item.domain}", f"- Confidence: {item.confidence}", f"- Rule: `{item.rule}`",
                    *([f"- Safety level: {item.safety_level}"] if item.safety_level is not None else []), "",
                    item.explanation, "", "Evidence:", *[f"- {value}" for value in item.evidence], "",
                    f"Recommended human action: {item.recommended_action}", "",
                ])
        suggestions = [item.recommended_action for item in result.findings if item.classification in {"opportunity", "recommendation"}]
        lines.extend(["", "## Curator Suggestions", ""])
        if suggestions:
            lines.extend(f"- {value}" for value in dict.fromkeys(suggestions))
        else:
            lines.append("No suggestions in this run.")
        task_summary = result.knowledge_tasks.get("summary", {})
        lines.extend(["", "## Knowledge Operations", "",
                      f"- Open tasks: {task_summary.get('open', 0)}",
                      f"- Resolved this run: {task_summary.get('resolved_this_run', 0)}",
                      f"- Knowledge debt: {result.knowledge_debt.get('total', 0)} ({result.knowledge_debt.get('trend', 'baseline')})",
                      f"- Knowledge health: {result.knowledge_health.get('overall_score', 'n/a')} ({result.knowledge_health.get('trend', 'baseline')})",
                      "", "## Lessons Learned", ""])
        lessons = result.lessons_learned.get("lessons", [])
        lines.extend((f"- {lesson['observation']}" for lesson in lessons) if lessons else ["No recurring patterns have enough audit history yet."])
        lines.extend(["## Coverage", "", "```json", json.dumps(result.coverage, indent=2, sort_keys=True), "```", ""])
        return "\n".join(lines)
