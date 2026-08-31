import requests

response = requests.post(
    "https://tabitoken.com/v1/messages",
    headers={
        "x-api-key": "sk-IaePkzmteEFueCDgMXkduVIIjL9h2lG1dcYb2snxYSYhkpE4",
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    },
    json={
        "model": "claude-opus-5-thinking",
        "max_tokens": 1024,
        "messages": [
            {
                "role": "user",
                "content": "Explain quantum entanglement in one paragraph."
            }
        ],
    },
)

print(response.json())
