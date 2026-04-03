from anthropic import Anthropic
import os

client = Anthropic(
    api_key=os.environ["ANTHROPIC_API_KEY"],
    base_url=os.getenv("ANTHROPIC_ENDPOINT", "https://api.anthropic.com"),
    default_headers={"anthropic-version": os.getenv("ANTHROPIC_VERSION", "2023-06-01")}
)

resp = client.messages.create(
    model=os.getenv("ANTHROPIC_MODEL", "claude-sonnet-4"),
    max_tokens=256,
    messages=[{"role": "user", "content": "Status ping from my service"}],
    metadata={"workspace": "my-workspace", "service": "my-api"}
)
print(resp.content[0].text)
