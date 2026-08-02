from dataclasses import dataclass
from datetime import datetime


@dataclass
class DraftMetadata:
    """
    Metadata describing the lifecycle of a knowledge draft.
    """

    status: str = "Draft"
    version: str | None = None
    last_saved: str = ""
    published_at: str = ""

    def touch(self):
        """
        Update the last saved timestamp.
        """

        self.last_saved = datetime.now().strftime(
            "%b %d, %Y %I:%M %p"
        )