class LearningModeService:
    CONCEPTS = {
        "vpn": ("VPN fundamentals", "A VPN creates an encrypted path to a private network. Problems can occur before authentication, during tunnel setup, or after routes are applied."),
        "dns": ("Name resolution", "DNS translates names into IP addresses. Testing it separately distinguishes a naming problem from a broader connectivity problem."),
        "dhcp": ("Automatic addressing", "DHCP supplies IP settings automatically. Missing or incorrect leases can prevent a device from reaching local and remote networks."),
        "wi-fi": ("Wireless connectivity", "Wi-Fi troubleshooting separates signal, association, authentication, and IP configuration problems."),
        "wifi": ("Wireless connectivity", "Wi-Fi troubleshooting separates signal, association, authentication, and IP configuration problems."),
        "ethernet": ("Wired connectivity", "Ethernet checks begin at the physical link, then move through addressing and network reachability."),
        "printer": ("Print path", "Printing depends on the device, connection, queue, driver, and spooler. Isolating each layer prevents unnecessary changes."),
        "adapter": ("Network adapters", "The adapter connects the operating system to the network. Its state, driver, and configuration affect every higher network layer."),
        "credential": ("Authentication", "Authentication verifies identity. Separating credential failures from connectivity failures narrows the investigation safely."),
        "reboot": ("State reset", "A controlled restart clears temporary state and reloads services, drivers, and configuration, but should not replace root-cause analysis."),
        "restart": ("State reset", "A controlled restart clears temporary state and reloads services, drivers, and configuration, but should not replace root-cause analysis."),
        "ip address": ("IP addressing", "IP settings identify the device and determine how it reaches local and remote networks."),
        "performance": ("Performance baselines", "Performance troubleshooting compares the computer's current resource use with its normal behavior, then isolates the busiest resource."),
        "cpu": ("Processor utilization", "Sustained high CPU usage can make every interaction feel delayed. Identifying the responsible process distinguishes expected workload from a stuck application."),
        "memory": ("Memory pressure", "When available memory is low, Windows moves data between RAM and storage. That paging can cause pauses even when the processor is not busy."),
        "disk": ("Storage activity", "High disk activity or very low free space can delay application launches, updates, temporary files, and virtual memory operations."),
        "startup": ("Startup load", "Startup applications compete for processor, memory, disk, and network resources immediately after sign-in. Disabling unnecessary entries reduces that initial load."),
        "malware": ("Security and performance", "Unwanted software may consume resources or change system behavior. Built-in security scans provide evidence before more invasive action is considered."),
        "update": ("System maintenance", "Windows and driver updates can correct known reliability and performance problems, but changes should be installed deliberately and followed by verification."),
    }

    def build(self, node, workflow_name, article=None):
        text = " ".join(str(getattr(node, field, "") or "") for field in ("title", "question", "instruction", "message", "help_text")).lower()
        concepts = []
        used_titles = set()
        for keyword, (title, explanation) in self.CONCEPTS.items():
            if keyword in text and title not in used_titles:
                used_titles.add(title)
                concepts.append({"title": title, "explanation": explanation})
        if not concepts:
            concepts.append({
                "title": "Diagnostic reasoning",
                "explanation": "Good troubleshooting changes one meaningful variable at a time, observes the result, and uses that evidence to choose the next step.",
            })

        specific_content = next((
            str(value).strip()
            for value in (node.question, node.instruction, node.message, node.title)
            if str(value or "").strip()
        ), "")

        if node.type == "question":
            what = specific_content or "This question narrows the problem space. Each answer selects a different evidence-based route through the workflow."
            why = node.help_text or "Clear observations help distinguish symptoms from likely causes before changes are made."
        elif node.type == "instruction":
            what = specific_content or "This action tests or changes one part of the system before Gnojo evaluates the next result."
            why = node.help_text or "Performing a focused action provides evidence while limiting unnecessary changes."
        elif node.type == "resolution":
            what = specific_content or "This outcome records where the diagnostic path ended and what evidence led to the result."
            why = node.help_text or "A documented outcome makes the solution repeatable and helps identify recurring patterns."
        else:
            what = specific_content or "This transition moves the investigation into a more specialized diagnostic phase."
            why = node.help_text or "Separating phases keeps the troubleshooting path understandable and easier to review."

        return {
            "what_it_checks": what,
            "why_it_matters": why,
            "concepts": concepts,
            "article_title": article.get("title") if isinstance(article, dict) else None,
            "workflow_name": workflow_name,
        }
