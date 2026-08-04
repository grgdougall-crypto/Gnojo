import json
import os
import re
import subprocess
import tempfile
from pathlib import Path


class ScriptAuthoringError(ValueError):
    pass


class ScriptAuthoringService:
    MAX_SOURCE_LENGTH = 100_000
    LANGUAGE_EXTENSIONS = {"PowerShell": ".ps1", "Bash": ".sh", "Zsh": ".zsh", "Batch": ".bat"}
    PLATFORM_LANGUAGES = {
        "Windows": {"PowerShell", "Batch"},
        "Linux": {"Bash", "PowerShell"},
        "macOS": {"Zsh", "Bash", "PowerShell"},
        "Cross-platform": {"PowerShell"},
    }

    def __init__(self, base_path="knowledge_base/scripts"):
        self.base_path = Path(base_path)
        self.catalog_path = self.base_path / "catalog.json"

    def validate(self, draft, existing_ids=(), check_unique=True):
        errors = []
        required = ("name", "summary", "category", "source", "privacy_note")
        for field in required:
            if not str(draft.get(field, "")).strip():
                errors.append(f"{field.replace('_', ' ').title()} is required.")
        script_id = self.slug(draft.get("id") or draft.get("name"))
        if check_unique and script_id in set(existing_ids):
            errors.append("A script with this ID already exists.")
        source = str(draft.get("source", ""))
        platform = draft.get("platform", "Windows")
        language = draft.get("language", "PowerShell")
        if platform not in self.PLATFORM_LANGUAGES:
            errors.append("Choose a supported platform.")
        elif language not in self.PLATFORM_LANGUAGES[platform]:
            errors.append(f"{language} is not supported for {platform} scripts.")
        if len(source) > self.MAX_SOURCE_LENGTH:
            errors.append("Source is larger than 100,000 characters.")
        errors.extend(self._language_errors(source, language))
        if draft.get("kind") == "Automation":
            if not draft.get("parameters"):
                errors.append("Automation scripts require at least one documented parameter.")
            if not draft.get("changes"):
                errors.append("Automation scripts must explain what they change.")
            if not str(draft.get("dry_run", "")).strip():
                errors.append("Automation scripts must document preview or dry-run behavior.")
            if not str(draft.get("rollback", "")).strip():
                errors.append("Automation scripts must include rollback or recovery guidance.")
            if language == "PowerShell" and ("SupportsShouldProcess=$true" not in source or "ShouldProcess" not in source):
                errors.append("PowerShell automation must use SupportsShouldProcess and ShouldProcess so -WhatIf works.")
            if language in {"Bash", "Zsh"} and "--dry-run" not in source:
                errors.append(f"{language} automation must implement a --dry-run option.")
            if language == "Batch" and not re.search(r"(?i)(/dry-run|DRY_RUN)", source):
                errors.append("Batch automation must implement a /dry-run option.")
        return errors

    def publish(self, draft, existing_ids=()):
        errors = self.validate(draft, existing_ids)
        if errors:
            raise ScriptAuthoringError(errors[0])
        self.base_path.mkdir(parents=True, exist_ok=True)
        record = self.record(draft)
        source_path = self.base_path / record["filename"]
        if source_path.exists():
            raise ScriptAuthoringError("The script source file already exists.")
        catalog = json.loads(self.catalog_path.read_text(encoding="utf-8")) if self.catalog_path.exists() else []
        source_temp = self._write_temp(record["source"], Path(record["filename"]).suffix)
        catalog_temp = None
        try:
            public_record = {key: value for key, value in record.items() if key != "source"}
            catalog.append(public_record)
            catalog_temp = self._write_temp(json.dumps(catalog, indent=2, ensure_ascii=False) + "\n", ".json")
            os.replace(source_temp, source_path)
            os.replace(catalog_temp, self.catalog_path)
        except Exception:
            if source_path.exists() and not any(item.get("id") == record["id"] for item in json.loads(self.catalog_path.read_text(encoding="utf-8"))):
                source_path.unlink()
            raise
        return public_record

    def record(self, draft):
        script_id = self.slug(draft.get("id") or draft.get("name"))
        language = draft.get("language", "PowerShell")
        extension = self.LANGUAGE_EXTENSIONS.get(language, ".txt")
        return {
            "id": script_id, "filename": f"{script_id}{extension}",
            "name": str(draft.get("name", "")).strip(),
            "kind": draft.get("kind") if draft.get("kind") in {"Automation", "Diagnostic Collector"} else "Diagnostic Collector",
            "summary": str(draft.get("summary", "")).strip(),
            "category": str(draft.get("category", "")).strip(),
            "platform": draft.get("platform", "Windows"), "language": language,
            "collects": draft.get("collects", []), "changes": draft.get("changes", []),
            "parameters": draft.get("parameters", []),
            "dry_run": str(draft.get("dry_run", "")).strip(),
            "rollback": str(draft.get("rollback", "")).strip(),
            "permissions": {"requires_elevation": bool(draft.get("requires_elevation")), "notes": str(draft.get("permission_notes", "")).strip()},
            "risk": {"level": "Moderate" if draft.get("kind") == "Automation" else "Low", "changes_system": draft.get("kind") == "Automation"},
            "privacy_note": str(draft.get("privacy_note", "")).strip(),
            "related_commands": draft.get("related_commands", []),
            "related_workflows": draft.get("related_workflows", []),
            "source": str(draft.get("source", "")).rstrip() + "\n",
        }

    @staticmethod
    def slug(value):
        return re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")[:80]

    def _write_temp(self, content, suffix):
        descriptor, name = tempfile.mkstemp(prefix=".script-", suffix=suffix, dir=self.base_path, text=True)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
            file.write(content)
        return name

    @staticmethod
    def _powershell_syntax_errors(source):
        descriptor, name = tempfile.mkstemp(prefix="gnojo-script-", suffix=".ps1", text=True)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as file:
                file.write(source)
            command = (
                "$tokens=$null;$errors=$null;"
                "[System.Management.Automation.Language.Parser]::ParseFile($env:GNOJO_SCRIPT_PARSE_PATH,[ref]$tokens,[ref]$errors)|Out-Null;"
                "$errors|ForEach-Object{$_.Message};if($errors.Count){exit 1}"
            )
            environment = os.environ.copy()
            environment["GNOJO_SCRIPT_PARSE_PATH"] = name
            result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command], capture_output=True, text=True, timeout=10, check=False, env=environment)
            if result.returncode:
                messages = [line.strip() for line in result.stdout.splitlines() if line.strip()]
                return [f"PowerShell syntax: {message}" for message in messages] or ["PowerShell syntax could not be validated."]
            return []
        except (OSError, subprocess.SubprocessError):
            return ["PowerShell syntax validation is unavailable."]
        finally:
            Path(name).unlink(missing_ok=True)

    def _language_errors(self, source, language):
        if language == "PowerShell":
            errors = []
            if not re.search(r"(?im)^\s*(?:\[CmdletBinding[^\]]*\]\s*)?param\s*\(", source):
                errors.append("The script must begin with a PowerShell param block (CmdletBinding may appear first).")
            return errors + self._powershell_syntax_errors(source)
        if language in {"Bash", "Zsh"}:
            expected = "bash" if language == "Bash" else "zsh"
            errors = []
            first_line = source.lstrip().splitlines()[0] if source.strip() else ""
            if not first_line.startswith("#!") or expected not in first_line.lower():
                errors.append(f"{language} scripts must start with a {expected} shebang.")
            if "\x00" in source:
                errors.append(f"{language} source contains an invalid null character.")
            return errors
        if language == "Batch":
            return [] if re.search(r"(?im)^\s*@?echo\s+off\b", source) else ["Batch scripts must begin with @echo off."]
        return ["Choose a supported script language."]
