# SupportPilot Architecture

SupportPilot is built around five primary systems.

## 1. User Interface

Provides the graphical interface for users.

Responsibilities:

- Device Profiles
- Guided troubleshooting
- Ticket management
- Dashboard

---

## 2. Knowledge Base

Stores structured troubleshooting information.

Includes:

- Networking
- Windows
- macOS
- Linux
- Servers
- Active Directory
- Security

---

## 3. Troubleshooting Engine

The core of SupportPilot.

Responsibilities:

- Match user issues
- Calculate confidence
- Select troubleshooting workflow
- Track progress
- Recommend next steps

---

## 4. AI Assistant

Enhances troubleshooting by:

- Explaining concepts
- Improving ticket summaries
- Recommending additional troubleshooting
- Teaching users

AI does not replace the troubleshooting engine.

---

## 5. Data Storage

Stores:

- Device profiles
- Tickets
- User settings
- Knowledge base

---

# Architectural Principles

SupportPilot is designed as a layered troubleshooting platform. Each layer has a single responsibility and communicates with the layer directly above or below it. This separation makes the application easier to maintain, expand, and test.

```
User
   │
   ▼
User Interface
   │
   ▼
Session
   │
   ▼
Troubleshooting Engine
   │
   ├──────────────┐
   ▼              ▼
Decision Trees   Knowledge Base
        │              │
        └──────┬───────┘
               ▼
          AI Assistant
               │
               ▼
          Resolution
```

---

## User Interface

The interface should present one clear question at a time.

Responsibilities:

- Keep language simple.
- Guide users through each step.
- Avoid unnecessary technical jargon.
- Display progress without overwhelming the user.

The interface should never contain troubleshooting logic.

---

## Session

The session represents SupportPilot's memory during a troubleshooting session.

Examples of information stored:

- Problem
- Device
- Connection type
- Previous answers
- Current workflow
- Current step

This allows the application to make informed decisions without asking the same questions repeatedly.

---

## Troubleshooting Engine

The Troubleshooting Engine controls the workflow.

It is responsible for:

- Reading the current session
- Loading the appropriate decision tree
- Determining the next question
- Recording answers
- Determining when a workflow is complete

The Troubleshooting Engine contains workflow logic but does not contain technical knowledge.

---

## Decision Trees

Decision Trees define the path through a troubleshooting workflow.

Each tree is made up of:

- Questions
- Possible answers
- Next steps

The engine follows these trees to guide users toward a solution.

Adding a new troubleshooting topic should primarily involve creating a new decision tree rather than modifying application code.

---

## Knowledge Base

The Knowledge Base contains SupportPilot's technical expertise.

Each topic may include:

- Description
- Symptoms
- Likely causes
- Troubleshooting procedures
- Commands
- Safety information
- Related topics

The Knowledge Base explains **what** SupportPilot knows.

---

## AI Assistant

The AI Assistant enhances the experience by providing context-aware explanations.

It may:

- Explain terminology
- Clarify instructions
- Adapt explanations for beginners or technicians
- Summarize findings

The AI does **not** control the troubleshooting workflow.

The Troubleshooting Engine always determines the next step.

---

# Guiding Principles

## Clarity over cleverness

Every screen should be immediately understandable.

---

## One question at a time

Users should focus only on the current decision.

---

## Knowledge before AI

SupportPilot should rely on curated technical knowledge rather than AI-generated assumptions.

---

## AI assists, never controls

Artificial intelligence enhances troubleshooting but never replaces structured workflows.

---

## Build the framework once

New troubleshooting topics should be added through decision trees and knowledge base articles rather than new application logic.

---

# Long-Term Vision

SupportPilot is being designed as a reusable troubleshooting framework rather than a collection of individual troubleshooting guides.

As the platform grows, new workflows such as printers, VPNs, Active Directory, Microsoft 365, Windows Server, and cybersecurity incidents should integrate into the existing architecture without requiring changes to the core engine.