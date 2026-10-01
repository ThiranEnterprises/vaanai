"""Quick helper: lists every model your Groq account can currently access."""

import os

import requests
from dotenv import load_dotenv

load_dotenv()
api_key = os.getenv("GROQ_API_KEY")

response = requests.get(
    "https://api.groq.com/openai/v1/models",
    headers={"Authorization": f"Bearer {api_key}"},
)

print(f"Status: {response.status_code}\n")

if response.ok:
    models = response.json().get("data", [])
    print(f"Found {len(models)} available model(s):\n")
    for m in models:
        print(f"  - {m['id']}")
else:
    print(response.text)
