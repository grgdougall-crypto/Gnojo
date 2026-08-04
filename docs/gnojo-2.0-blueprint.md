# Gnojo 2.0 Blueprint

**Version:** 1.0  
**Status:** Living Design Document

---

# Vision

Gnojo is a technician-first IT operations platform that unifies troubleshooting knowledge, command references, workflows, decision trees, scripts, policies, and AI-assisted guidance into one connected knowledge ecosystem.

The objective is to reduce resolution time by providing technicians with the right information, tools, and next steps from a single interface.

Gnojo is designed to evolve beyond a traditional knowledge base into an intelligent operations platform.

---

# Core Design Principles

## Technician First

Every feature should reduce the amount of time a technician spends searching for information.

If a feature adds complexity without improving technician efficiency, it should be reconsidered.

---

## Single Responsibility

Each layer of the application has one responsibility.

| Layer | Responsibility |
|--------|----------------|
| Repository | Retrieve and store data |
| Service | Business logic |
| Route | Coordinate requests |
| Template | Present information |

Business logic should never be duplicated across multiple routes.

---

## One Source of Truth

Every knowledge object has exactly one authoritative record.

Relationships should reference existing objects instead of duplicating data.

---

## Relationships Over Duplication

Knowledge should be connected.

Examples include:

- Articles reference Commands
- Commands reference Articles
- Workflows reference Articles
- Decision Trees reference Commands
- Scripts reference Workflows

Gnojo should discover relationships instead of storing duplicate information whenever possible.

---

## Consistency

Naming conventions should remain predictable throughout the application.

Examples:

Repositories

```
knowledge_repository.py
command_repository.py
```

Services

```
search_service.py
relationship_service.py
```

Templates

```
published_article.html
command.html
```

---

# High-Level Architecture

```
User
 │
 ▼
Routes
 │
 ▼
Services
 │
 ▼
Repositories
 │
 ▼
Knowledge Objects (JSON)
```

Routes should remain thin.

Repositories retrieve data.

Services make decisions.

Templates render results.

---

# Project Structure

```
app/

    repositories/
        knowledge_repository.py
        command_repository.py

    services/
        search_service.py
        relationship_service.py

    templates/

    static/

knowledge_base/

    published/

    drafts/

    commands/

docs/

tests/
```

Each folder should have one clearly defined responsibility.

---

# Knowledge Objects

Gnojo organizes information into reusable knowledge objects.

## Knowledge Article

Purpose:

Explain concepts and troubleshooting procedures.

Contains:

- Title
- Overview
- Category
- Difficulty
- Tags
- Estimated Time
- Checklist
- Related Commands
- Related Articles
- Official References

---

## Command

Purpose:

Document command-line utilities.

Contains:

- Name
- Syntax
- Examples
- Summary
- Permissions
- Risk
- Supported Platforms
- Output Fields
- Common Errors
- Related Articles

---

## Workflow

Purpose:

Provide repeatable operational procedures.

Contains:

- Prerequisites
- Steps
- Expected Results
- Escalation Points

---

## Decision Tree

Purpose:

Guide technicians through troubleshooting decisions.

Contains:

- Questions
- Branches
- Outcomes
- Related Commands
- Related Articles

---

## Script

Purpose:

Automate repetitive operational tasks.

Contains:

- Language
- Script
- Parameters
- Requirements
- Risks
- Related Workflows

---

# Repository Layer

Repositories answer one question:

> Where is the data?

Repositories should not:

- Perform searches
- Rank results
- Build relationships
- Apply business rules

Repositories retrieve data only.

---

# Service Layer

Services answer one question:

> What should happen?

Current Services

- SearchService
- RelationshipService

Planned Services

- RankingService
- NavigationService
- ValidationService
- PublishingService
- AIService

Services coordinate repositories and prepare information for presentation.

---

# Search Architecture

Gnojo provides one universal search experience.

Current repositories:

- Knowledge Articles
- Commands

Future repositories:

- Workflows
- Decision Trees
- Scripts
- Policies
- Prompt Library

Search should rank results based on relevance rather than simply returning matching records.

---

# Relationship Graph

```
Knowledge Article
        │
        ├──────── Commands
        │
        ├──────── Workflows
        │
        ├──────── Decision Trees
        │
        ├──────── Scripts
        │
        ├──────── Policies
        │
        └──────── Prompt Library
```

Every object should become discoverable through related objects.

Relationships should be generated through the Relationship Service whenever practical.

---

# User Experience

Navigation should remain simple.

```
Dashboard

↓

Knowledge Center

↓

Universal Search

↓

Article

↓

Command

↓

Workflow

↓

Decision Tree
```

Users should never need to understand where information is stored.

They should simply search once and receive the best available guidance.

---

# Development Standards

Before implementing a new feature, answer three questions:

1. Where is the data stored?
2. Which service owns the business logic?
3. How will users discover it?

If these questions cannot be answered clearly, the design should be reconsidered before implementation.

---

# Long-Term Vision

Gnojo is being designed as a platform rather than a collection of pages.

Future capabilities include:

- AI-assisted troubleshooting
- Automated workflow recommendations
- Relationship visualization
- Script repository
- Prompt library
- Policy management
- Technician learning paths
- Analytics
- REST API
- Desktop client
- Mobile companion
- Microsoft Teams integration

The architecture should support future growth without requiring significant redesign.

---

# Development Roadmap

## Phase 1 — Foundation ✅

- Repository Layer
- Service Layer
- Knowledge Articles
- Command Library
- Universal Search
- Relationship Engine

---

## Phase 2

- Workflow Library
- Decision Trees
- Script Repository
- Publishing Pipeline

---

## Phase 3

- AI Assistant
- Recommendation Engine
- Knowledge Graph
- Analytics

---

## Phase 4

- REST API
- Teams Integration
- Automation
- Enterprise Features

---

# Guiding Principle

Gnojo should always make technicians more effective.

Every feature should either:

- Reduce troubleshooting time
- Improve decision making
- Increase knowledge accessibility
- Automate repetitive work

If it does not accomplish one of these goals, it does not belong in Gnojo.