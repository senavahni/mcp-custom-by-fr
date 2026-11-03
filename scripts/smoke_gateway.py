"""Smoke test LLM Gateway (TASK-06): /v1/models memuat route wajib, satu chat `fast`, satu embed.

    scripts\\conda-run.bat python scripts\\smoke_gateway.py [--skip-embed]

Prasyarat: Gateway berjalan dan LLM_GATEWAY_API_KEY = key project `mcp-marketing`
(di repo llm-genai: `manage.py create-project --slug mcp-marketing` lalu `create-key`,
scope chat, embeddings, models:read, usage:read).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcp_marketing.core.config import get_settings  # noqa: E402
from mcp_marketing.core.errors import ToolError  # noqa: E402
from mcp_marketing.services.llm.gateway_client import GatewayClient  # noqa: E402


async def main(skip_embed: bool) -> int:
    s = get_settings()
    client = GatewayClient(s)
    ok = True
    try:
        models = await client.list_models()
        required = {s.llm.llm_route_fast, s.llm.llm_route_smart}
        if not skip_embed:
            required.add(s.llm.llm_route_embed)
        missing = sorted(required - set(models))
        print(f"[models] {len(models)} model/route; wajib ada: {sorted(required)}")
        if missing:
            print(f"[models] GAGAL, route hilang: {missing}")
            ok = False
        result = await client.chat(
            "summarize_short",
            [{"role": "user", "content": 'Balas JSON {"status":"ok"} saja.'}],
        )
        print(
            f"[chat]   route={result.route} model={result.model_resolved} "
            f"tokens={result.prompt_tokens}+{result.completion_tokens} {result.latency_ms} ms "
            f"-> {result.content!r}"
        )
        if not skip_embed:
            vectors = await client.embed(["uji embedding mcp marketing"])
            print(f"[embed]  dim={len(vectors[0])}")
    except ToolError as err:
        print(f"GAGAL: {err.code} {err.message} {err.details or ''}")
        ok = False
    finally:
        await client.close()
    print("SMOKE OK" if ok else "SMOKE GAGAL")
    return 0 if ok else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-embed", action="store_true")
    sys.exit(asyncio.run(main(parser.parse_args().skip_embed)))
