import json
import re
import textwrap
from io import BytesIO


class WorkflowExportError(ValueError):
    """Raised when a workflow cannot be exported in the requested format."""


class WorkflowExportService:
    FORMATS = {
        "json": "application/json",
        "markdown": "text/markdown",
        "pdf": "application/pdf",
    }

    def export(self, workflow, export_format):
        export_format = str(export_format or "").lower()
        if export_format not in self.FORMATS:
            raise WorkflowExportError("Choose JSON, Markdown, or PDF.")

        filename = f"{self._slug(workflow.get('name') or workflow.get('workflow_id'))}.{self._extension(export_format)}"
        if export_format == "json":
            content = json.dumps(workflow, indent=2, ensure_ascii=False).encode("utf-8")
        elif export_format == "markdown":
            content = self._markdown(workflow).encode("utf-8")
        else:
            content = self._pdf(workflow)
        return content, filename, self.FORMATS[export_format]

    @staticmethod
    def _extension(export_format):
        return "md" if export_format == "markdown" else export_format

    @staticmethod
    def _slug(value):
        slug = re.sub(r"[^a-z0-9]+", "-", str(value or "workflow").lower()).strip("-")
        return slug or "workflow"

    def _markdown(self, workflow):
        lines = [
            f"# {workflow.get('name') or 'Untitled workflow'}",
            "",
            workflow.get("description") or "",
            "",
            f"- Workflow ID: `{workflow.get('workflow_id', '')}`",
            f"- Start node: `{workflow.get('start_node', '')}`",
            f"- Estimated steps: {workflow.get('estimated_steps', '')}",
            "",
            "## Nodes",
            "",
        ]
        for node_id, node in (workflow.get("nodes") or {}).items():
            lines.extend([f"### {node.get('title') or node_id}", "", f"**Type:** {node.get('type', 'node')}  ", f"**Node ID:** `{node_id}`", ""])
            for label, key in (("Question", "question"), ("Instruction", "instruction"), ("Message", "message"), ("Help", "help_text")):
                if node.get(key):
                    lines.extend([f"**{label}:** {node[key]}", ""])
            if node.get("answers"):
                lines.append("**Answers:**")
                for answer in node["answers"].values():
                    lines.append(f"- {answer.get('label', 'Untitled answer')} → `{answer.get('next', '')}`")
                lines.append("")
            elif node.get("next"):
                lines.extend([f"**Next node:** `{node['next']}`", ""])
        return "\n".join(lines).rstrip() + "\n"

    def _pdf(self, workflow):
        text = self._markdown(workflow)
        plain = re.sub(r"[`#*]", "", text).replace("→", "->")
        lines = []
        for line in plain.splitlines():
            lines.extend(textwrap.wrap(line, width=92, replace_whitespace=False) or [""])
        pages = [lines[index:index + 48] for index in range(0, len(lines), 48)] or [["Untitled workflow"]]
        return self._build_pdf(pages)

    @staticmethod
    def _build_pdf(pages):
        objects = []
        def add(value):
            objects.append(value)
            return len(objects)

        catalog = add(b"")
        pages_object = add(b"")
        font = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        page_ids = []
        for page_number, page_lines in enumerate(pages, 1):
            commands = ["BT", "/F1 11 Tf", "54 756 Td", "14 TL"]
            for line in page_lines:
                safe = line.encode("latin-1", "replace").decode("latin-1").replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
                commands.extend([f"({safe}) Tj", "T*"])
            commands.extend(["T*", f"(Page {page_number} of {len(pages)}) Tj", "ET"])
            stream = "\n".join(commands).encode("latin-1")
            content = add(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
            page_ids.append(add(f"<< /Type /Page /Parent {pages_object} 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R >> >> /Contents {content} 0 R >>".encode()))
        objects[pages_object - 1] = f"<< /Type /Pages /Kids [{' '.join(f'{item} 0 R' for item in page_ids)}] /Count {len(page_ids)} >>".encode()
        objects[catalog - 1] = f"<< /Type /Catalog /Pages {pages_object} 0 R >>".encode()

        output = BytesIO()
        output.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for number, obj in enumerate(objects, 1):
            offsets.append(output.tell())
            output.write(f"{number} 0 obj\n".encode() + obj + b"\nendobj\n")
        xref = output.tell()
        output.write(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
        for offset in offsets[1:]:
            output.write(f"{offset:010d} 00000 n \n".encode())
        output.write(f"trailer\n<< /Size {len(objects) + 1} /Root {catalog} 0 R >>\nstartxref\n{xref}\n%%EOF".encode())
        return output.getvalue()
