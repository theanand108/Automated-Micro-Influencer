import os

import requests
from dotenv import load_dotenv

load_dotenv()

API_KEY = os.getenv("YOUTUBE_API_KEY")

if not API_KEY:
    raise RuntimeError(
        "YOUTUBE_API_KEY is missing. Check your local .env file."
    )

response = requests.get(
    "https://www.googleapis.com/youtube/v3/search",
    params={
        "part": "snippet",
        "type": "channel",
        "q": "technology",
        "maxResults": 5,
        "key": API_KEY,
    },
    timeout=30,
)

if not response.ok:
    print(f"HTTP status: {response.status_code}")
    print(response.text)
    response.raise_for_status()

data = response.json()

print("\nYouTube API connection successful!\n")

for item in data.get("items", []):
    snippet = item["snippet"]

    print(f"Channel: {snippet['title']}")
    print(f"Channel ID: {snippet['channelId']}")
    print(f"Description: {snippet.get('description', '')[:150]}")
    print("-" * 50)