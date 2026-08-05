import re


class ArticleTagService:
    """Create concise, searchable tags from article content."""

    TERM_TAGS = (
        (r"\bbluetooth\b", "bluetooth"),
        (r"\bwindows update\b", "windows update"),
        (r"\bdevice manager\b", "device manager"),
        (r"\bdrivers?\b", "drivers"),
        (r"\bethernet\b", "ethernet"),
        (r"\bwi[ -]?fi\b", "wi-fi"),
        (r"\bipconfig\b", "ipconfig"),
        (r"\bip(?:v4)? address\b", "ip address"),
        (r"\bdefault gateway\b", "default gateway"),
        (r"\bdns\b", "dns"),
        (r"\bdhcp\b", "dhcp"),
        (r"\brouters?\b", "router"),
        (r"\bmodems?\b", "modem"),
        (r"\bnetwork(?:ing)?\b", "networking"),
        (r"\bprinters?\b", "printer"),
        (r"\bprint queue\b", "print queue"),
        (r"\bmonitors?\b", "monitor"),
        (r"\bdisplay\b", "display"),
        (r"\bcameras?|webcams?\b", "camera"),
        (r"\btask manager\b", "task manager"),
        (r"\bperformance\b", "performance"),
        (r"\bstorage\b", "storage"),
        (r"\bdisk space\b", "disk space"),
        (r"\bvpn\b", "vpn"),
        (r"\bpowershell\b", "powershell"),
        (r"\bwindows\b", "windows"),
    )
    STOP_WORDS = {
        "a", "an", "and", "approved", "how", "install", "inspect", "of",
        "the", "to", "use", "with", "your", "this", "that", "from",
    }

    @classmethod
    def generate(cls, article):
        existing = cls.normalize(article.get("tags", []))
        if existing:
            return existing[:8]

        content = " ".join(
            str(value or "") for value in (
                article.get("title"), article.get("overview"),
                " ".join(article.get("checklist", []) or []),
                " ".join(article.get("common_indicators", []) or []),
                " ".join(article.get("related_topics", []) or []),
            )
        ).lower()
        tags = []
        for pattern, tag in cls.TERM_TAGS:
            if re.search(pattern, content) and tag not in tags:
                tags.append(tag)

        title_words = re.findall(r"[a-z0-9]+", str(article.get("title", "")).lower())
        for word in title_words:
            if len(word) >= 4 and word not in cls.STOP_WORDS and word not in tags:
                tags.append(word)

        category = str(article.get("category", "")).strip().lower()
        if category and category not in {"troubleshooting", "uncategorized"} and category not in tags:
            tags.append(category)
        if len(tags) < 3:
            tags.append("troubleshooting")
        return cls.normalize(tags)[:8]

    @staticmethod
    def normalize(tags):
        if isinstance(tags, str):
            tags = re.split(r"[,\n]", tags)
        result = []
        for tag in tags or []:
            value = re.sub(r"\s+", " ", str(tag).strip().lower())
            if value and value not in result:
                result.append(value[:40])
        return result
