import os
from openai import OpenAI

client = OpenAI(
    base_url="https://inference-api.nvidia.com/v1",
    api_key=os.environ["API_KEY"],
)

completion = client.chat.completions.create(
    model="azure/anthropic/claude-opus-4-6",   # ✅ Claude Opus 4.6 via Azure
    messages=[{"role": "user", "content": "Write a limerick about the wonders of GPU computing."}],
    temperature=0.2,
    max_tokens=1024,
    stream=True
)

for chunk in completion:
    if chunk.choices[0].delta.content is not None:
        print(chunk.choices[0].delta.content, end="")
