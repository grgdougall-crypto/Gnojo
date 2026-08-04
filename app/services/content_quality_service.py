from collections import Counter, defaultdict


class ContentQualityService:
    def build(self, workflows, history_records, draft_filenames=None):
        draft_filenames = draft_filenames or {}
        histories = defaultdict(list)
        for record in history_records:
            workflow_id = record.get("workflow_id")
            if workflow_id:
                histories[workflow_id].append(record)

        workflow_rows = []
        action_queue = []
        for workflow_id, workflow in workflows.items():
            nodes = workflow.get("nodes", {}) if isinstance(workflow, dict) else {}
            records = histories.get(workflow_id, [])
            completed = [item for item in records if item.get("status") == "completed"]
            abandoned = [item for item in records if item.get("status") == "abandoned"]
            feedback = [item["feedback"] for item in records if isinstance(item.get("feedback"), dict)]
            solved = sum(item.get("solved") == "yes" for item in feedback)
            clarity = [item.get("clarity") for item in feedback if isinstance(item.get("clarity"), int)]
            instructional = [node for node in nodes.values() if isinstance(node, dict) and node.get("type") == "instruction"]
            guided = [node for node in nodes.values() if isinstance(node, dict) and node.get("type") in {"question", "instruction"}]
            knowledge_count = sum(bool(node.get("knowledge_article")) for node in instructional)
            help_count = sum(bool(node.get("help_text")) for node in guided)
            confusing = Counter(item.get("confusing_step") for item in feedback if item.get("confusing_step"))
            sessions = len(records)
            solved_rate = round((solved / len(feedback)) * 100) if feedback else None
            clarity_average = round(sum(clarity) / len(clarity), 1) if clarity else None
            abandonment_rate = round((len(abandoned) / sessions) * 100) if sessions else 0
            knowledge_coverage = round((knowledge_count / len(instructional)) * 100) if instructional else 100
            learning_coverage = round((help_count / len(guided)) * 100) if guided else 100
            filename = draft_filenames.get(workflow_id)
            row = {
                "workflow_id": workflow_id,
                "name": workflow.get("name") or workflow_id.replace("_", " ").title(),
                "category": workflow.get("category") or "Uncategorized",
                "platform": workflow.get("platform") or "Cross-platform",
                "node_count": len(nodes),
                "sessions": sessions,
                "completed": len(completed),
                "feedback_count": len(feedback),
                "solved_rate": solved_rate,
                "clarity": clarity_average,
                "abandonment_rate": abandonment_rate,
                "knowledge_coverage": knowledge_coverage,
                "learning_coverage": learning_coverage,
                "filename": filename,
            }
            workflow_rows.append(row)

            def add_issue(kind, title, detail, priority="medium", node_id=None):
                action_queue.append({
                    "kind": kind, "title": title, "detail": detail,
                    "priority": priority, "workflow_id": workflow_id,
                    "workflow_name": row["name"], "filename": filename,
                    "node_id": node_id,
                })

            if len(feedback) >= 2 and solved_rate < 70:
                add_issue("effectiveness", "Low problem-solved rate", f"Only {solved_rate}% of {len(feedback)} responses reported a full resolution.", "high")
            if len(clarity) >= 2 and clarity_average < 4:
                add_issue("clarity", "Guidance needs clarification", f"Average clarity is {clarity_average}/5 across {len(clarity)} responses.", "high" if clarity_average < 3 else "medium")
            if sessions >= 2 and abandonment_rate >= 30:
                add_issue("abandonment", "High session abandonment", f"{abandonment_rate}% of {sessions} sessions ended before a resolution.", "high")
            for node_id, count in confusing.most_common(3):
                add_issue("confusing_step", "Frequently confusing step", f"This step was reported as confusing {count} time{'s' if count != 1 else ''}.", "high" if count >= 3 else "medium", node_id)
            if instructional and knowledge_coverage < 25:
                add_issue("knowledge", "Knowledge coverage is thin", f"Only {knowledge_coverage}% of instructional steps link to supporting articles.", "medium")
            if guided and learning_coverage < 50:
                add_issue("learning", "Learning guidance is incomplete", f"Only {learning_coverage}% of questions and instructions include specific help text.", "medium")
            if len(completed) >= 3 and not feedback:
                add_issue("feedback", "No quality feedback yet", f"{len(completed)} sessions completed without a survey response.", "low")

        priority_order = {"high": 0, "medium": 1, "low": 2}
        action_queue.sort(key=lambda item: (priority_order[item["priority"]], item["workflow_name"].lower(), item["title"]))
        workflow_rows.sort(key=lambda item: (-item["sessions"], item["name"].lower()))
        categories = Counter(row["category"] for row in workflow_rows)
        platforms = Counter(row["platform"] for row in workflow_rows)
        return {
            "summary": {
                "workflows": len(workflow_rows),
                "issues": len(action_queue),
                "high_priority": sum(item["priority"] == "high" for item in action_queue),
                "with_feedback": sum(item["feedback_count"] > 0 for item in workflow_rows),
            },
            "action_queue": action_queue,
            "workflows": workflow_rows,
            "coverage": {
                "categories": sorted(categories.items()),
                "platforms": sorted(platforms.items()),
            },
        }
