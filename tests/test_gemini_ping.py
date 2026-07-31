from app.knowledge.providers.gemini_provider import (
    GeminiProvider,
)


provider = GeminiProvider()

print()
print("PING TEST")
print(provider.ping())