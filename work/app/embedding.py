"""V0.4 Embedding Provider 抽象。

关键决策：
- Mock 使用 blake2b 做确定性哈希（Python 内置 hash() 按进程加盐，写入与查询会落在不同空间）。
- Mock 是"哈希词袋"，只能做词面相似，不能做真正的语义近义；语义验收需用真实模型（fastembed）单独跑。
- 真实模型惰性加载，未安装/未下载模型时不影响默认流程。
"""
import hashlib
import os
from typing import Optional

import numpy as np

from . import db


class EmbeddingProvider:
    name = "base"
    dim = 0

    def embed(self, texts: list[str]) -> np.ndarray:
        """返回 (n, dim) 的 float32 数组，已 L2 归一化。"""
        raise NotImplementedError


def _normalize(m: np.ndarray) -> np.ndarray:
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return (m / n).astype(np.float32)


class MockEmbedding(EmbeddingProvider):
    name = "mock-hash-256"
    dim = 256

    def embed(self, texts: list[str]) -> np.ndarray:
        out = np.zeros((len(texts), self.dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in db.tokenize(t):
                h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")
                # 词频累加到哈希桶；不使用随机符号，保证同词必然同桶同值（确定性）
                out[i, h % self.dim] += 1.0
        return _normalize(out)


class FastEmbedProvider(EmbeddingProvider):
    """基于 ONNX 的本地模型（无需 PyTorch）。首次使用需联网下载模型。"""

    def __init__(self, model: str = "BAAI/bge-small-zh-v1.5"):
        self.name = model
        self._model_name = model
        self._m = None

    def _load(self):
        if self._m is None:
            from fastembed import TextEmbedding
            self._m = TextEmbedding(self._model_name)
            self.dim = len(next(iter(self._m.embed(["测试"]))))

    def embed(self, texts: list[str]) -> np.ndarray:
        self._load()
        return _normalize(np.array(list(self._m.embed(texts)), dtype=np.float32))


_provider: Optional[EmbeddingProvider] = None


def get_embedder() -> EmbeddingProvider:
    global _provider
    if _provider is None:
        mode = os.environ.get("EMBEDDING_MODE", "mock").lower()
        if mode == "mock":
            _provider = MockEmbedding()
        elif mode == "local":
            _provider = FastEmbedProvider(os.environ.get("EMBEDDING_MODEL", "BAAI/bge-small-zh-v1.5"))
        else:
            raise RuntimeError(f"EMBEDDING_MODE={mode} 未实现")
    return _provider


def set_embedder(p: Optional[EmbeddingProvider]) -> None:
    global _provider
    _provider = p
