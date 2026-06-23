"""Bailian embeddings for local executor (Milvus indexing)."""

from __future__ import annotations

import httpx

BAILIAN_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


class EmbeddingClient:
    def __init__(
        self,
        api_key: str | None = None,
        model: str = "text-embedding-v3",
        dimensions: int = 1024,
    ) -> None:
        from elda.secrets_loader import load_api_secrets

        secrets = load_api_secrets()
        self.api_key = api_key or secrets.bailian.api_key
        if not self.api_key:
            raise ValueError("Bailian API key required — set secrets/api_keys.yaml")
        self.model = model or secrets.bailian.embedding_model
        self.dimensions = dimensions

    def embed_sync(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = {
            "model": self.model,
            "input": texts,
            "dimensions": self.dimensions,
            "encoding_format": "float",
        }
        with httpx.Client(timeout=120.0) as client:
            r = client.post(
                f"{BAILIAN_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json=body,
            )
            r.raise_for_status()
            data = r.json()["data"]
            return [item["embedding"] for item in sorted(data, key=lambda x: x["index"])]
