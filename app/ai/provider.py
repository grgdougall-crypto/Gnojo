from abc import ABC, abstractmethod


class AIProvider(ABC):
    """
    Base interface for all AI providers used by SupportPilot.
    """

    @abstractmethod
    def generate_command(self, command_name, description=""):
        """
        Generate structured command content.
        """
        pass