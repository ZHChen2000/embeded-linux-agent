"""Milvus vector store for kernel / datasheet RAG."""

from __future__ import annotations

import hashlib
import logging
from typing import Any

from app.config import settings
from app.models.providers import BailianEmbeddingProvider

logger = logging.getLogger(__name__)

COLLECTIONS = {
    "kernel_drivers": "elda_kernel_drivers",
    "dt_bindings": "elda_dt_bindings",
    "hardware_docs": "elda_hardware_docs",
    "vendor_drivers": "elda_vendor_drivers",
}

DIM = 1024


def _pymilvus():
    """Import pymilvus on first use (top-level import can SIGSEGV on Docker 18.09 / old hosts)."""
    from pymilvus import (
        Collection,
        CollectionSchema,
        DataType,
        FieldSchema,
        connections,
        utility,
    )

    return Collection, CollectionSchema, DataType, FieldSchema, connections, utility


class MilvusStore:
    def __init__(self) -> None:
        self._connected = False

    def connect(self) -> None:
        if self._connected:
            return
        _, _, _, _, connections, _utility = _pymilvus()
        connections.connect(
            alias="default",
            host=settings.milvus_host,
            port=str(settings.milvus_port),
        )
        self._connected = True
        for name in COLLECTIONS.values():
            self._ensure_collection(name)

    def _ensure_collection(self, name: str) -> Any:
        Collection, CollectionSchema, DataType, FieldSchema, _, utility = _pymilvus()
        if utility.has_collection(name):
            return Collection(name)
        fields = [
            FieldSchema(name="id", dtype=DataType.VARCHAR, is_primary=True, max_length=64),
            FieldSchema(name="doc_id", dtype=DataType.VARCHAR, max_length=512),
            FieldSchema(name="path", dtype=DataType.VARCHAR, max_length=1024),
            FieldSchema(name="chunk_index", dtype=DataType.INT64),
            FieldSchema(name="text", dtype=DataType.VARCHAR, max_length=8192),
            FieldSchema(name="embedding", dtype=DataType.FLOAT_VECTOR, dim=DIM),
        ]
        schema = CollectionSchema(fields, description=f"ELDA {name}")
        col = Collection(name, schema)
        col.create_index(
            "embedding",
            {"index_type": "IVF_FLAT", "metric_type": "COSINE", "params": {"nlist": 128}},
        )
        col.load()
        return col

    async def index_chunks(
        self,
        collection_key: str,
        doc_id: str,
        path: str,
        chunks: list[str],
        embedder: BailianEmbeddingProvider,
    ) -> int:
        self.connect()
        Collection, _, _, _, _, _ = _pymilvus()
        name = COLLECTIONS[collection_key]
        col = Collection(name)
        if not chunks:
            return 0
        vectors = await embedder.embed(chunks)
        ids = []
        doc_ids = []
        paths = []
        indices = []
        texts = []
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            chunk_id = hashlib.sha256(f"{doc_id}:{i}:{chunk[:64]}".encode()).hexdigest()[:32]
            ids.append(chunk_id)
            doc_ids.append(doc_id[:500])
            paths.append(path[:1000])
            indices.append(i)
            texts.append(chunk[:8000])
        col.insert([ids, doc_ids, paths, indices, texts, vectors])
        col.flush()
        return len(chunks)

    def search(
        self,
        collection_key: str,
        query_vector: list[float],
        top_k: int = 8,
        expr: str | None = None,
    ) -> list[dict[str, Any]]:
        self.connect()
        Collection, _, _, _, _, _ = _pymilvus()
        name = COLLECTIONS[collection_key]
        col = Collection(name)
        col.load()
        results = col.search(
            data=[query_vector],
            anns_field="embedding",
            param={"metric_type": "COSINE", "params": {"nprobe": 16}},
            limit=top_k,
            expr=expr,
            output_fields=["doc_id", "path", "chunk_index", "text"],
        )
        hits: list[dict[str, Any]] = []
        for hit in results[0]:
            hits.append(
                {
                    "id": hit.id,
                    "score": hit.score,
                    "doc_id": hit.entity.get("doc_id"),
                    "path": hit.entity.get("path"),
                    "chunk_index": hit.entity.get("chunk_index"),
                    "text": hit.entity.get("text"),
                }
            )
        return hits


milvus_store = MilvusStore()
