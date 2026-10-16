import requests

url = "https://openrouter.ai/api/v1/rerank"

payload = {
    "documents": ["Paris is the capital of France.", "Berlin is the capital of Germany."],
    "model": "cohere/rerank-v3.5",
    "query": "What is the capital of France?",
    "top_n": 3
}
headers = {
    "Authorization": "Bearer <token>",
    "Content-Type": "application/json"
}

response = requests.post(url, json=payload, headers=headers)

print(response.text)

"""
返回值示例：

{
  "id": "gen-rerank-1234567890-abc",
  "model": "cohere/rerank-v3.5",
  "results": [
    {
      "document": {
        "text": "Paris is the capital of France."
      },
      "index": 0,
      "relevance_score": 0.98
    }
  ],
  "usage": {
    "search_units": 1,
    "total_tokens": 150
  }
}
"""