# Gnojo Product Architecture

## Product Definition

Gnojo is an AI-assisted IT troubleshooting platform.

It helps teams create, review, validate, publish, and use structured troubleshooting content.

AI generates drafts and suggestions. A human reviews and approves all published content.

## Core Product Areas

### Gnojo Home

The primary user dashboard.

Responsibilities:

- Launch troubleshooting workflows
- Search Gnojo
- View recent activity
- Access device profiles
- Resume troubleshooting sessions

### Troubleshooting

The end-user diagnostic experience.

Responsibilities:

- Run guided workflows
- Track progress
- Preserve navigation history
- Chain related workflows
- Display relevant knowledge articles
- Produce a troubleshooting result

### Content Studio

The internal authoring environment.

Content types:

- Workflows
- Knowledge articles
- Commands
- Scripts

Every content type follows the same lifecycle:

1. Create
2. Generate with AI
3. Review
4. Edit
5. Validate
6. Preview
7. Publish

### Knowledge Center

The published operational knowledge library.

Responsibilities:

- Browse published articles
- Browse command documentation
- Search published content
- Display related articles and commands
- Support troubleshooting workflows

### Device Profiles

Stores optional device context used during troubleshooting.

Examples:

- Operating system
- Device name
- Hardware details
- Network configuration
- Installed applications
- Previous troubleshooting history

## Workflow Module

The workflow module uses the following fixed navigation structure:

### Workflow Studio

Purpose:

- List workflow drafts
- List published workflows
- Search and filter workflows
- Start a new workflow
- Open an existing workflow

### Workflow Builder

Purpose:

- Collect workflow requirements
- Generate a workflow draft with AI
- Validate the generated structure
- Save the workflow as a draft

### Workflow Editor

Purpose:

- Review one workflow
- Edit workflow metadata
- Browse and edit individual nodes
- Add or remove nodes
- Edit branches and destinations
- Review knowledge references

The editor must not expose JSON as the primary interface.

### Workflow Simulator

Purpose:

- Test a workflow as an end user
- Follow each branch
- Identify broken or confusing logic
- Confirm transitions and resolutions

### Workflow Validation

Purpose:

- Check required workflow fields
- Check node types
- Check missing node references
- Check unreachable nodes
- Check missing resolutions
- Check workflow transitions
- Check knowledge article references
- Check command references

### Workflow Publishing

Purpose:

- Confirm validation
- Record version information
- Add reviewer notes
- Publish the workflow to the troubleshooting engine

## Workflow Data Model

Workflow JSON remains the runtime and storage format.

JSON is an implementation detail and is not the main editing interface.

A workflow contains:

- Workflow ID
- Name
- Description
- Platform
- Difficulty
- Estimated steps
- Start node
- Nodes
- Status
- Version
- Generation provider
- Review information

Supported node types:

- Question
- Instruction
- Resolution
- Transition

## AI Provider Architecture

Gnojo uses a provider abstraction layer.

Current provider order:

1. Gemini
2. OpenAI fallback

Both providers must support the same public capabilities.

Examples:

- Generate command
- Generate article
- Generate workflow
- Generate script

AI output must pass validation before it can be published.

## Design Principles

### One page, one responsibility

Each screen must have one clear primary purpose.

### Progressive disclosure

Show summaries first. Show details only when requested.

### Human-readable interfaces

Users work with cards, forms, lists, and simulators rather than raw JSON.

### Human approval

AI-generated content remains a draft until reviewed and published.

### Reusable systems

New content types should reuse the same generation, validation, review, and publishing patterns.

### No manual workflow authoring at scale

Workflow JSON may be written manually for testing or debugging.

Normal workflow creation must use Workflow Builder and Workflow Editor.

## Implementation status

The Workflow Studio, generator, editor, validator, simulator, export, versioning, and publication paths are implemented for local development. Knowledge articles use a human review and publication gate. Commands and scripts have dedicated libraries and authoring experiences.

Production authentication, shared organization data, durable hosted persistence, and operational deployment controls remain future work. See [Roadmap](roadmap.md) for current priorities.
