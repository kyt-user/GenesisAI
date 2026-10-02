"""安全抓取公开网页正文并登记可引用的网络来源。"""

import json
import time

from genesisai.shared.security import digest
from genesisai.state.store import atomic_json


class Implementation:
    def prepare(self, context, args):
        return {
            "name": "search_fetch",
            "args": args,
            "target": "公开网络请求：" + args["url"],
            "grants": context.store.data["grants"],
            "output": context.store.data["output"],
        }

    def execute(self, context, args):
        cached = next(
            (
                (ref, value)
                for ref, value in context.store.data["sources"].items()
                if value.get("requested_url") == args["url"]
            ),
            None,
        )
        if cached:
            ref, _ = cached
            page = json.loads(
                (context.store.root / "sources" / f"{ref}.json").read_text(encoding="utf-8")
            )
        else:
            page = context.network.fetch(args["url"])
            ref = context.store.source(
                kind="web",
                url=page["url"],
                requested_url=args["url"],
                title=page["title"],
                fetched_at=time.time(),
                sha256=digest(page["text"]),
            )
            atomic_json(context.store.root / "sources" / f"{ref}.json", page)
        offset, limit = args.get("offset", 0), args.get("limit", 4000)
        text = page["text"]
        return {
            "text": text[offset : offset + limit],
            "url": page["url"],
            "title": page["title"],
            "source_refs": [ref],
            "truncated": page["truncated"] or len(text) > offset + limit,
            "next_offset": offset + limit if len(text) > offset + limit else None,
            "cached": bool(cached),
            "links": page.get('links', []),
        }

