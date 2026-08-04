# Gnojo Knowledge Base

## Purpose

The knowledge base turns reviewed technical guidance into reusable articles that can stand alone or support workflow steps and Learning Mode.

## Article states

- **Draft:** editable content that has not completed review
- **Pending Review:** structurally valid content awaiting human approval
- **Approved:** content whose technical review checklist is complete
- **Published:** trusted content available to the knowledge library and workflow runtime

Structural validation does not prove technical accuracy. Publication requires a human reviewer.

## Required content

A complete article includes:

- Stable ID and clear title
- Category, difficulty, and estimated time
- Plain-language overview
- Actionable checklist
- Common indicators or symptoms
- Related topics
- Commands when they genuinely support the task
- Authoritative sources
- A knowledge-check question, answer choices, and exact correct answer

Commands may be empty when the procedure does not need one.

## Sources

Prefer official vendor documentation. A source is stored as one line using:

```text
Source title | https://example.com/page
```

The linked page should directly support the article's technical guidance. Reviewers must verify that the page is current, authoritative, and specific enough for the claim.

## Workflow links

Workflow nodes reference articles with the stable `knowledge_article` ID. Articles created from a node are linked back to that node automatically. Authors should confirm the ID remains present after publication and test the Learn More panel in the runtime workflow.

## Review checklist

Before approval, a reviewer confirms:

1. Technical steps and claims were verified.
2. Instructions are safe and appropriately scoped.
3. Sources are authoritative and support the guidance.
4. Commands, permissions, and risks were reviewed.

Review notes should identify the authoritative guidance used and any important safety judgment.

## Storage

- Drafts: `knowledge_base/drafts/` (local and ignored by Git)
- Published articles: `knowledge_base/published/` (version controlled)

Published JSON is the canonical content record. The review workspace is the normal authoring interface; direct JSON editing is reserved for maintenance and recovery.
