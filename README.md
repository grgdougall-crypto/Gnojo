# Gnojo

**Navigate. Diagnose. Resolve.**

Gnojo is an AI-assisted IT troubleshooting and content-authoring platform. It combines guided decision-tree workflows with reviewed knowledge articles, command references, diagnostic scripts, device-aware recommendations, and learning tools.

The name combines **gnosis** (knowledge through understanding) and **dojo** (a place for disciplined practice and improvement). Gnojo is pronounced **NO-joe**.

## Current status

Gnojo is a pre-1.0 application under active development. The current build includes:

- Guided troubleshooting with resumable sessions, history, feedback, and device profiles
- A searchable and filterable workflow catalog
- Workflow authoring, editing, validation, simulation, publishing, versioning, and export
- Built-in workflow foundations that can be copied into protected editable drafts
- Knowledge article drafting, technical review, publication, and workflow linking
- A reviewed command library
- A Windows, Linux, and macOS script library and script builder
- Learning mode, workflow coverage, and content-quality analytics
- Optional Gemini and OpenAI assistance with local fallbacks
- Responsive light and dark themes with accessibility support

The repository currently represents a local development build. A hosted demonstration is planned, but no public demo URL is available yet.

Content marked `pending_review` is intentionally not represented as fully reviewed. Technical content should be validated in a safe test environment before production use.

## Local setup

Requirements: Python 3.11 or newer and Git.

1. Clone the repository and open its folder.
2. Create a virtual environment:

   ```powershell
   py -m venv .venv
   ```

3. Activate it and install dependencies:

   ```powershell
   .\.venv\Scripts\Activate.ps1
   python -m pip install -r requirements.txt
   ```

4. Copy `.env.example` to `.env`. Add API keys only if AI generation is needed.
5. Start Gnojo:

   ```powershell
   python run.py
   ```

6. Open `http://127.0.0.1:5000` in a browser.

`.env` and local runtime data are ignored by Git. Never commit API keys, saved profiles, troubleshooting histories, or unpublished local drafts.

## Configuration

| Variable | Purpose |
| --- | --- |
| `FLASK_SECRET_KEY` | Stable session-signing key; use a strong private value outside local development |
| `GNOJO_DEBUG` | Set to `true` only for local debugging |
| `GEMINI_API_KEY` | Optional Gemini access |
| `GEMINI_MODEL` | Optional Gemini model override |
| `OPENAI_API_KEY` | Optional OpenAI access |
| `OPENAI_MODEL` | Optional OpenAI model override |

## Validation

Run the complete automated test suite before committing:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s tests -p 'test_*.py'
.\.venv\Scripts\python.exe -m pip check
```

The suite covers workflows, publishing, search, device-aware routing, history, knowledge review, commands, scripts, accessibility, responsive behavior, error recovery, and content integrity.

## Content lifecycle

Gnojo keeps generated material separate from trusted published content:

1. Generate or create a draft.
2. Edit and validate the draft.
3. Simulate workflow branches or preview article content.
4. Complete a human technical review.
5. Publish an immutable version.
6. Use feedback and content-quality analytics to guide later improvements.

Built-in workflows remain unchanged. Authors create editable copies, review them, and publish approved versions for the runtime experience.

## Project structure

- `app/` – Flask application, services, templates, static assets, and built-in workflows
- `knowledge_base/` – reviewed commands, scripts, published articles, and local draft storage
- `tests/` – automated application and content-integrity tests
- `docs/` – product, architecture, design, taxonomy, and brand documentation
- `run.py` – local development entry point

## Product principles

- Guide users through evidence-based troubleshooting instead of guessing.
- Explain why a step matters while helping resolve the problem.
- Prefer safe, reversible diagnostics and clearly label risk.
- Keep generated content in review until a person validates it.
- Support end users, technicians, administrators, students, and security professionals.

Start with the [`docs` index](docs/README.md), then see [`docs/architecture.md`](docs/architecture.md), [`docs/brand.md`](docs/brand.md), and [`docs/roadmap.md`](docs/roadmap.md).
