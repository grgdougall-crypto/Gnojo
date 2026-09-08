from pathlib import Path
import json

from app.data_root import resolve_data_path


class CommandRepository:
    """
    Loads command records from the Command Library.
    """

    def __init__(self, base_path=None):
        self.base_path = resolve_data_path(
            "knowledge_base", "commands",
            explicit_path=base_path,
            legacy_path="knowledge_base/commands",
        )

    def get_all(self):
        commands = []

        if not self.base_path.exists():
            return commands

        for file in sorted(self.base_path.glob("*.json")):
            with open(file, encoding="utf-8") as f:
                commands.append(json.load(f))

        return commands

    def get(self, command_id):
        for command in self.get_all():
            if command.get("id") == command_id:
                return command

        return None
