def workflow_category(workflow):
    value = workflow.get("category")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = " ".join(str(workflow.get(key, "")) for key in ("name", "description", "workflow_id")).lower()
    if any(term in text for term in ("vpn", "network", "dns", "dhcp", "wi-fi", "wifi", "router")):
        return "Networking"
    if "printer" in text or "print" in text:
        return "Printers"
    if any(term in text for term in ("security", "malware", "phishing", "incident")):
        return "Security"
    if any(term in text for term in ("server", "active directory", "identity", "group policy")):
        return "Servers & Identity"
    return "Desktop Support"


def workflow_platform(workflow):
    value = workflow.get("platform")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text = " ".join(str(workflow.get(key, "")) for key in ("name", "description", "workflow_id")).lower()
    if "windows" in text or "win_" in text or text.endswith("win"):
        return "Windows"
    if "macos" in text or "mac os" in text:
        return "macOS"
    if "linux" in text:
        return "Linux"
    return "Cross-platform"
