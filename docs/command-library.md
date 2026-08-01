# SupportPilot Command Library

## Purpose

The Command Library provides a curated collection of command-line tools and commands used throughout SupportPilot.

Commands should be stored once, documented clearly, and reused across:

- Knowledge articles
- Troubleshooting workflows
- Learning Mode
- Decision trees
- Future script and automation features

The goal is to prevent duplicate command explanations and create one trusted source of truth for each command.

---

# Core Principles

## 1. One command, one canonical record

Each command should have one primary JSON file.

Examples:

- `ipconfig.json`
- `ping.json`
- `tracert.json`
- `nslookup.json`

Articles and workflows should reference these records rather than duplicating the command documentation.

## 2. Explain before instructing

Every command record should explain:

- What the command does
- When to use it
- What information it returns
- Whether elevated permissions are required
- Any risks or side effects

Commands should never be presented without context.

## 3. Safety is required

Commands that modify system state must clearly identify:

- Required permissions
- Potential impact
- Restart requirements
- Reversal or recovery steps
- Whether the command is safe for beginners

Read-only commands should be distinguished from commands that make changes.

## 4. Official references validate the content

Each command record should include at least one authoritative reference whenever practical.

Preferred sources include:

- Microsoft Learn
- Apple documentation
- Red Hat documentation
- Ubuntu documentation
- Cisco documentation
- Python documentation
- GNU documentation
- RFCs and standards organizations

References support the Command Library. They do not replace the explanation provided by SupportPilot.

## 5. Commands should support learning

Command records should help users understand the output instead of only telling them what to type.

Where appropriate, include:

- Example output
- Important fields
- Common errors
- Related commands
- Practice questions
- Troubleshooting examples

---

# Initial Command Categories

## Windows Command Prompt

Examples:

- `ipconfig`
- `ping`
- `tracert`
- `nslookup`
- `netstat`
- `arp`
- `route`
- `netsh`
- `systeminfo`
- `tasklist`

## PowerShell

Examples:

- `Get-NetIPAddress`
- `Test-NetConnection`
- `Get-Service`
- `Get-Process`
- `Get-WinEvent`
- `Get-ComputerInfo`

## Linux and Bash

Examples:

- `ip`
- `ping`
- `traceroute`
- `dig`
- `ss`
- `grep`
- `find`
- `systemctl`
- `journalctl`

## macOS Terminal

Examples:

- `ifconfig`
- `networksetup`
- `scutil`
- `ping`
- `traceroute`
- `dig`

---

# Proposed Command Schema

Each command record should use the following structure:

```json
{
  "schema_version": "1.0",
  "id": "ipconfig",
  "name": "ipconfig",
  "title": "Inspect Windows network configuration with ipconfig",
  "shell": "Command Prompt",
  "platforms": [
    "Windows 10",
    "Windows 11",
    "Windows Server"
  ],
  "category": "Networking",
  "difficulty": "Beginner",
  "summary": "Displays TCP/IP configuration details for Windows network adapters.",
  "syntax": "ipconfig [options]",
  "examples": [
    {
      "command": "ipconfig",
      "description": "Displays a basic summary of active network adapters."
    },
    {
      "command": "ipconfig /all",
      "description": "Displays detailed configuration, including DNS and DHCP information."
    }
  ],
  "permissions": {
    "requires_elevation": false,
    "notes": "Some repair options may require administrator access."
  },
  "risk": {
    "level": "Low",
    "changes_system": false,
    "warning": null
  },
  "output_fields": [
    {
      "name": "IPv4 Address",
      "description": "The current IPv4 address assigned to the adapter."
    },
    {
      "name": "Default Gateway",
      "description": "The device used to reach other networks."
    }
  ],
  "common_errors": [],
  "related_commands": [
    "ping",
    "tracert",
    "nslookup"
  ],
  "related_articles": [
    "inspect-windows-network-ipconfig"
  ],
  "tags": [
    "networking",
    "windows",
    "tcp/ip",
    "dns",
    "dhcp"
  ],
  "sources": [
    {
      "title": "ipconfig",
      "organization": "Microsoft Learn",
      "url": "",
      "type": "Official Documentation",
      "verified": null
    }
  ],
  "review_status": "draft",
  "reviewed_by": null,
  "last_reviewed": null,
  "version": "1.0"
}