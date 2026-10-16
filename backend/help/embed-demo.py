"""调用 OpenRouter 嵌入接口的最小示例。

密钥不写在代码里：按「环境变量 → backend/.env」的顺序读取（backend/.env 已被 .gitignore 忽略）。
运行方式：cd backend && uv run python help/demo.py
"""

import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # 让 `import app` 生效
from app.core.openrouter import read_api_key  # noqa: E402

url = "https://openrouter.ai/api/v1/embeddings"

token = read_api_key()

payload = {
    "dimensions": 2048,  # 已查证，该模型支持的维度数是256/512/1024/2048
    "input": "The quick brown fox jumps over the lazy dog",
    "model": "voyageai/voyage-multimodal-3.5",
}
headers = {
    "Authorization": f"Bearer {token}",
    "Content-Type": "application/json",
}

response = requests.post(url, json=payload, headers=headers)

print(response.text)

# 本项目所用嵌入模型的输入长度上限是32768
