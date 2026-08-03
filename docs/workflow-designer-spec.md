# Workflow Designer Specification

## Purpose

The Workflow Designer is the main authoring interface for reviewing and editing one troubleshooting workflow.

It must be clean, intuitive, and human-readable.

Raw JSON must remain hidden unless the user explicitly opens an advanced view.

## Page Layout

The page uses a two-column layout.

### Left Column

The left column contains workflow-level information.

Sections:

- Workflow name
- Status
- Platform
- Difficulty
- Estimated steps
- Validation status
- Node statistics
- Start node
- Workflow actions

Primary actions:

- Save
- Validate
- Simulate
- Publish

### Right Column

The right column is the main working area.

It contains:

- Node search
- Node type filters
- Node list
- Selected node details
- Node editing controls

## Node Browser

Each node appears as a compact card.

Each card shows:

- Node type
- Human-readable title
- Short preview
- Branch count or next step
- Validation warning when applicable

Node IDs are hidden by default.

The entire card is clickable.

Node types use subtle visual differences:

- Question
- Instruction
- Resolution
- Transition

The node browser must support:

- Search by title or text
- Filter by node type
- Show all nodes
- Show only problem nodes
- Show the start node clearly

## Selected Node Panel

When a node is selected, the right side displays its editable details.

Question fields:

- Question text
- Help text
- Answer labels
- Answer destinations
- Knowledge article reference

Instruction fields:

- Title
- Instruction text
- Next node
- Knowledge article reference

Resolution fields:

- Title
- Resolution message

Transition fields:

- Title
- Message
- Next workflow

Actions:

- Save node
- Cancel changes
- Delete node
- Duplicate node

## Validation

Validation must be visible without overwhelming the editor.

Validation states:

- Passed
- Warning
- Error

Examples:

- Missing destination
- Missing knowledge article
- Unreachable node
- Missing resolution
- Invalid transition
- Circular reference

## Simulator

The simulator opens separately from the editor.

It runs the workflow exactly as an end user would experience it.

The simulator must allow:

- Restart
- Previous step
- Follow branches
- View linked knowledge
- Return to editor

## Publishing

A workflow cannot be published unless:

- Validation passes
- At least one resolution exists
- All required references exist
- A reviewer confirms the workflow
- Version information is recorded

## Design Rules

- One page, one responsibility
- No raw JSON in the default interface
- Human-readable labels before internal identifiers
- Progressive disclosure
- Entire node cards are clickable
- Important actions remain visible
- Advanced details stay hidden unless requested
- The interface must remain usable with large workflows