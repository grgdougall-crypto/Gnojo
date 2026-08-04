# Gnojo Decision Trees

## Purpose

Gnojo uses guided decision trees to help users troubleshoot technical problems one step at a time.

Each workflow should:

- Begin with a problem described in plain language.
- Ask one clear question at a time.
- Always include an "I'm not sure" option when appropriate.
- Avoid technical terms unless they are explained.
- Present only safe steps to general users.
- Adapt instructions to the user's operating system.
- Confirm whether each step worked.
- End with a resolution summary or clear escalation path.

---

# Workflow 1: No Internet Connection

## Entry Point

User selects:

> I can't connect to the Internet.

---

## Step 1: Device Type

Question:

> What kind of device are you using?

Options:

- Windows PC
- Mac
- Linux computer
- Windows Server
- Mobile device
- I'm not sure

Initial supported paths:

- Windows PC
- Mac

Other options will display:

> This workflow is still being developed.

---

## Step 2: Operating System

### Windows PC

Question:

> Which version of Windows are you using?

Options:

- Windows 11
- Windows 10
- I'm not sure

### Mac

Question:

> Which version of macOS are you using?

Options:

- Current or recent macOS
- Older macOS
- I'm not sure

The user should never be blocked because they do not know the exact version.

---

## Step 3: Connection Type

Question:

> How does this device normally connect to the Internet?

Options:

- Wi-Fi
- Ethernet cable
- I'm not sure

---

## Step 4: Scope of the Problem

Question:

> Can other devices connect to the Internet?

Options:

- Yes
- No
- I'm not sure

Branches:

### Yes

The problem is probably limited to the current device.

Continue to device-level troubleshooting.

### No

The problem may involve:

- Router or modem
- Internet service outage
- Wireless access point
- Network infrastructure

Continue to network-level troubleshooting.

### I'm not sure

Guide the user to test another device before continuing.

---

# Device-Level Wi-Fi Path

## Step 5: Confirm Wi-Fi Status

Question:

> Is Wi-Fi turned on?

Buttons:

- Yes
- No
- I'm not sure
- My screen looks different

Guidance must match the selected operating system.

### Windows 11

1. Select the network, sound, and battery area near the clock.
2. Confirm the Wi-Fi button is turned on.
3. Select the arrow beside Wi-Fi.
4. Look for the normal wireless network.

### Windows 10

1. Select the network icon near the clock.
2. Confirm Wi-Fi is turned on.
3. Look for the normal wireless network.

### macOS

1. Select the Wi-Fi icon in the menu bar.
2. Confirm Wi-Fi is turned on.
3. Look for the normal wireless network.

---

## Step 6: Confirm Network Connection

Question:

> Does the correct Wi-Fi network show as connected?

Options:

- Yes
- No
- I do not see the network
- I'm not sure

Branches:

### Yes

Continue to browser and application testing.

### No

Guide the user through connecting to the correct network.

Do not request or store the Wi-Fi password.

### I do not see the network

Possible causes:

- Wi-Fi adapter problem
- Device too far from the access point
- Wireless network unavailable
- Airplane mode enabled
- Router or access point issue

Continue to adapter and network visibility checks.

---

## Step 7: Test Internet Access

Instruction:

> Open a web browser and try to visit a website you normally use.

Question:

> Did the website open?

Options:

- Yes, it works now
- No, it still does not work
- One website fails, but others work
- The browser shows an error

Branches:

### Yes, it works now

Mark the issue as resolved.

### No, it still does not work

Continue to IP configuration and DNS testing.

### One website fails, but others work

Treat as a website-specific or browser-specific issue.

### The browser shows an error

Ask the user to enter or paste the error message without including passwords or personal information.

---

## Step 8: Safe Diagnostic Checks

Gnojo should initially guide the user through graphical checks.

Command-line checks may be offered under:

- Show advanced steps
- Professional Mode
- Explain this step

### Windows diagnostic commands

```text
ipconfig /all
ping <default-gateway>
nslookup example.com