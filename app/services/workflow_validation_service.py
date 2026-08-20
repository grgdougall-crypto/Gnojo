from app.services.workflow_quality_validator import WorkflowQualityValidator


class WorkflowValidationService:
    """
    Validate AI-generated Gnojo workflow drafts.
    """

    allowed_node_types = {
        "question",
        "instruction",
        "resolution",
        "transition",
    }

    def validate(self, workflow):
        """
        Return validation results for a workflow draft.
        """

        errors = []
        warnings = []

        if not isinstance(workflow, dict):
            return {
                "is_valid": False,
                "errors": [
                    "Workflow must be a JSON object."
                ],
                "warnings": [],
                "reachable_nodes": [],
                "unreachable_nodes": [],
            }

        workflow_id = workflow.get("workflow_id")
        name = workflow.get("name")
        start_node = workflow.get("start_node")
        nodes = workflow.get("nodes")

        if not isinstance(workflow_id, str) or not workflow_id.strip():
            errors.append(
                "workflow_id is required."
            )

        if not isinstance(name, str) or not name.strip():
            errors.append(
                "name is required."
            )

        if not isinstance(start_node, str) or not start_node.strip():
            errors.append(
                "start_node is required."
            )

        if not isinstance(nodes, dict) or not nodes:
            errors.append(
                "nodes must contain at least one node."
            )

            return {
                "is_valid": False,
                "errors": errors,
                "warnings": warnings,
                "reachable_nodes": [],
                "unreachable_nodes": [],
            }

        if start_node not in nodes:
            errors.append(
                f"Start node '{start_node}' does not exist."
            )

        for node_id, node in nodes.items():
            if not isinstance(node_id, str) or not node_id.strip():
                errors.append(
                    "Every node must have a valid ID."
                )
                continue

            if not isinstance(node, dict):
                errors.append(
                    f"Node '{node_id}' must be an object."
                )
                continue

            node_type = node.get("type")

            self._validate_conditions(node_id, node, nodes, errors)

            if node_type not in self.allowed_node_types:
                errors.append(
                    f"Node '{node_id}' has unsupported type "
                    f"'{node_type}'."
                )
                continue

            if node_type == "question":
                self._validate_question_node(
                    node_id,
                    node,
                    nodes,
                    errors,
                )

            elif node_type == "instruction":
                self._validate_instruction_node(
                    node_id,
                    node,
                    nodes,
                    errors,
                )

            elif node_type == "resolution":
                self._validate_resolution_node(
                    node_id,
                    node,
                    errors,
                )

            elif node_type == "transition":
                self._validate_transition_node(
                    node_id,
                    node,
                    errors,
                )

        self._validate_conditional_cycles(nodes, errors)

        reachable_nodes = self._find_reachable_nodes(
            start_node,
            nodes,
        )

        unreachable_nodes = sorted(
            set(nodes.keys()) - reachable_nodes
        )

        for node_id in unreachable_nodes:
            warnings.append(
                f"Node '{node_id}' is unreachable."
            )

        terminal_nodes = [
            node_id
            for node_id, node in nodes.items()
            if isinstance(node, dict) and node.get("type") in {
                "resolution",
                "transition",
            }
        ]

        if not terminal_nodes:
            errors.append(
                "Workflow must contain at least one "
                "resolution or transition node."
            )

        return {
            "is_valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "reachable_nodes": sorted(
                reachable_nodes
            ),
            "unreachable_nodes": unreachable_nodes,
            "quality": WorkflowQualityValidator().validate(workflow),
        }

    def _validate_conditions(self, node_id, node, nodes, errors):
        from app.services.workflow_condition_service import CONDITION_FIELDS
        conditions = node.get("conditions")
        if conditions is None:
            return
        if not isinstance(conditions, dict) or not conditions:
            errors.append(f"Node '{node_id}' conditions must be a non-empty object.")
            return
        for field, value in conditions.items():
            if field not in CONDITION_FIELDS or value not in CONDITION_FIELDS.get(field, set()):
                errors.append(f"Node '{node_id}' has an invalid condition for '{field}'.")
        skip_to = node.get("skip_to")
        if not isinstance(skip_to, str) or not skip_to:
            errors.append(f"Conditional node '{node_id}' requires skip_to.")
        elif skip_to not in nodes:
            errors.append(f"Conditional node '{node_id}' references missing skip node '{skip_to}'.")
        elif skip_to == node_id:
            errors.append(f"Conditional node '{node_id}' cannot skip to itself.")

    def _validate_conditional_cycles(self, nodes, errors):
        reported = set()
        for start_id in nodes:
            seen = []
            current_id = start_id
            while current_id in nodes:
                node = nodes[current_id]
                if not isinstance(node, dict) or not node.get("conditions"):
                    break
                if current_id in seen:
                    cycle = tuple(seen[seen.index(current_id):])
                    signature = tuple(sorted(cycle))
                    if signature not in reported:
                        reported.add(signature)
                        errors.append(f"Conditional skip route contains a loop involving node '{current_id}'.")
                    break
                seen.append(current_id)
                current_id = node.get("skip_to")

    def _validate_question_node(
        self,
        node_id,
        node,
        nodes,
        errors,
    ):
        question = node.get("question")
        answers = node.get("answers")

        if not isinstance(question, str) or not question.strip():
            errors.append(
                f"Question node '{node_id}' is missing "
                f"question text."
            )

        if not isinstance(answers, dict) or not answers:
            errors.append(
                f"Question node '{node_id}' must have answers."
            )
            return

        for answer_id, answer_data in answers.items():
            if isinstance(answer_data, dict):
                next_node_id = answer_data.get("next")
            else:
                next_node_id = answer_data

            if not isinstance(next_node_id, str):
                errors.append(
                    f"Answer '{answer_id}' in node '{node_id}' "
                    f"is missing a next node."
                )
                continue

            if next_node_id not in nodes:
                errors.append(
                    f"Answer '{answer_id}' in node '{node_id}' "
                    f"references missing node '{next_node_id}'."
                )

    def _validate_instruction_node(
        self,
        node_id,
        node,
        nodes,
        errors,
    ):
        title = node.get("title")
        instruction = node.get("instruction")
        next_node_id = node.get("next")

        if not isinstance(title, str) or not title.strip():
            errors.append(
                f"Instruction node '{node_id}' is missing "
                f"a title."
            )

        if (
            not isinstance(instruction, str)
            or not instruction.strip()
        ):
            errors.append(
                f"Instruction node '{node_id}' is missing "
                f"instruction text."
            )

        if not isinstance(next_node_id, str):
            errors.append(
                f"Instruction node '{node_id}' is missing "
                f"a next node."
            )
        elif next_node_id not in nodes:
            errors.append(
                f"Instruction node '{node_id}' references "
                f"missing node '{next_node_id}'."
            )

    def _validate_resolution_node(
        self,
        node_id,
        node,
        errors,
    ):
        title = node.get("title")
        message = node.get("message")

        if not isinstance(title, str) or not title.strip():
            errors.append(
                f"Resolution node '{node_id}' is missing "
                f"a title."
            )

        if not isinstance(message, str) or not message.strip():
            errors.append(
                f"Resolution node '{node_id}' is missing "
                f"a message."
            )

    def _validate_transition_node(
        self,
        node_id,
        node,
        errors,
    ):
        title = node.get("title")
        message = node.get("message")
        next_workflow = node.get("next_workflow")

        if not isinstance(title, str) or not title.strip():
            errors.append(
                f"Transition node '{node_id}' is missing "
                f"a title."
            )

        if not isinstance(message, str) or not message.strip():
            errors.append(
                f"Transition node '{node_id}' is missing "
                f"a message."
            )

        if (
            not isinstance(next_workflow, str)
            or not next_workflow.strip()
        ):
            errors.append(
                f"Transition node '{node_id}' is missing "
                f"next_workflow."
            )

    def _find_reachable_nodes(
        self,
        start_node,
        nodes,
    ):
        if start_node not in nodes:
            return set()

        reachable = set()
        pending = [start_node]

        while pending:
            node_id = pending.pop()

            if node_id in reachable:
                continue

            node = nodes.get(node_id)

            if not isinstance(node, dict):
                continue

            reachable.add(node_id)

            for next_node_id in self._next_node_ids(node):
                if (
                    next_node_id in nodes
                    and next_node_id not in reachable
                ):
                    pending.append(next_node_id)

        return reachable

    def _next_node_ids(self, node):
        node_type = node.get("type")

        conditional_skip = node.get("skip_to")
        conditional_destinations = (
            [conditional_skip]
            if isinstance(conditional_skip, str) and conditional_skip
            else []
        )

        if node_type == "question":
            next_node_ids = list(conditional_destinations)

            answers = node.get("answers")
            if not isinstance(answers, dict):
                return next_node_ids

            for answer_data in answers.values():
                if isinstance(answer_data, dict):
                    next_node_id = answer_data.get("next")
                else:
                    next_node_id = answer_data

                if isinstance(next_node_id, str):
                    next_node_ids.append(
                        next_node_id
                    )

            return list(dict.fromkeys(next_node_ids))

        if node_type == "instruction":
            next_node_id = node.get("next")

            if isinstance(next_node_id, str):
                return list(dict.fromkeys(
                    [next_node_id, *conditional_destinations]
                ))

            return conditional_destinations

        return conditional_destinations
