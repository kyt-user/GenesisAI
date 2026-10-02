"""当前 Run 的可验证 Evidence Bundle。"""

from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

from genesisai.state.store import uid


class EvidenceBundle:
    def __init__(self, store):
        self.store = store

    @property
    def state(self) -> dict:
        return self.store.data["run_runtime"]

    def add(self, *, source_ref: str, canonical_url: str, title: str, snippet: str, published_at=None, content_hash=None) -> dict:
        digest = content_hash or hashlib.sha256(snippet.encode("utf-8")).hexdigest()
        for item in self.state["evidence"].values():
            if item["content_hash"] == digest:
                return item
        item = {
            "evidence_id": uid("evidence"),
            "source_ref": source_ref,
            "canonical_url": canonical_url,
            "title": title,
            "domain": (urlsplit(canonical_url).hostname or "").lower(),
            "fetched_at": time.time(),
            "published_at": published_at,
            "text_span": snippet[:4000],
            "content_hash": digest,
            "status": "ready",
        }
        self.state["evidence"][item["evidence_id"]] = item
        if source_ref not in self.state["evidence_refs"]:
            self.state["evidence_refs"].append(source_ref)
        if digest not in self.state["content_hashes"]:
            self.state["content_hashes"].append(digest)
        self.store.save()
        return item

    def items(self) -> list[dict]:
        return list(self.state.get("evidence", {}).values())

    def source_refs(self) -> list[str]:
        return list(self.state.get("evidence_refs", []))

    def verify(self) -> None:
        for item in self.items():
            source = self.store.data["sources"].get(item["source_ref"])
            if not source or source.get("kind") != "web":
                raise ValueError("Evidence 指向不存在或未成功读取的来源")
            if source.get("url") != item["canonical_url"]:
                raise ValueError("Evidence URL 与 Source Store 不一致")

