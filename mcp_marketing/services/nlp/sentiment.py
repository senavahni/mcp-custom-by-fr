"""Pipeline sentimen bertingkat (TASK-20, README D9).

normalisasi -> deteksi bahasa -> cache `sentiment_results` (sha256 teks ternormalisasi + model) ->
model lokal ONNX (batch, process pool) -> eskalasi LLM `fast` untuk item ragu/sarkas ->
aspek (keyword-spotting + LLM). Teks duplikat (retweet) hanya dihitung sekali.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import orjson
from sqlalchemy import text

from mcp_marketing.core.errors import UpstreamError
from mcp_marketing.core.logging import get_logger
from mcp_marketing.services.local_ml.classifier_store import sentiment_head
from mcp_marketing.services.nlp import aspects as aspect_mod
from mcp_marketing.services.nlp import sentiment_llm
from mcp_marketing.services.nlp.lang import detect
from mcp_marketing.services.nlp.normalize import normalize
from mcp_marketing.services.nlp.sentiment_model import model_available, model_id, predict_batch
from mcp_marketing.utils.crypto import sha256_hex

log = get_logger(__name__)

Method = Literal["auto", "model", "llm"]
Language = Literal["auto", "id", "en"]
LLM_MODEL_KEY = "llm:fast"
NEUTRAL_FALLBACK = ("neutral", 0.5)
_POSITIVE = re.compile(
    r"\b(bagus|enak|mantap|keren|terbaik|luar biasa|hebat|rekomendasi|suka|senang|emoji_pos)\b"
)
_SARCASM = re.compile(r"\b(katanya|tertawa|yaelah|hebat ya|bagus ya|makasih loh|ya kan)\b")
RunCpu = Callable[..., Awaitable[Any]]


@dataclass
class Item:
    key: str
    text: str
    target_type: str = "text"
    target_id: uuid.UUID | None = None
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Verdict:
    label: str
    score: float
    method: str
    lang: str
    aspects: list[dict[str, str]] = field(default_factory=list)


@dataclass
class Outcome:
    verdicts: dict[str, Verdict]  # item.key -> hasil
    cost: dict[str, int]
    notes: list[str]


def is_sarcastic(normalized: str) -> bool:
    return bool(_SARCASM.search(normalized) and _POSITIVE.search(normalized))


class SentimentAnalyzer:
    def __init__(self, services: Any, *, budget: Any = None, run_cpu: RunCpu | None = None) -> None:
        self.services = services
        self.settings = services.settings.nlp
        self.budget = budget
        self._run_cpu = run_cpu
        self._head: Any = None

    async def _predict(self, texts: list[str]) -> list[tuple[str, dict[str, float]]]:
        if self._head is not None:  # MCP-V1-04: head lokal di atas embed-local
            return await self._head.predict(texts)
        model_dir = str(self.settings.sentiment_model_dir)
        if self._run_cpu is not None:
            return await self._run_cpu(predict_batch, model_dir, texts)  # type: ignore[no-any-return]
        return await asyncio.to_thread(predict_batch, model_dir, texts)

    async def analyze(
        self,
        items: list[Item],
        *,
        method: Method = "auto",
        language: Language = "auto",
        aspects: list[str] | None = None,
    ) -> Outcome:
        notes: list[str] = []
        self._head = await sentiment_head(self.services)
        has_model = self._head is not None or model_available(self.settings.sentiment_model_dir)
        if method == "model" and not has_model:
            raise UpstreamError(
                "Model sentimen lokal belum tersedia; latih head lokal "
                "(`python scripts/manage.py sentiment-train`), unduh ONNX "
                "(`python scripts/manage.py download-models --sentiment-repo …`) atau pakai "
                "method=llm",
                retryable=False,
            )
        effective: Method = method
        if method == "auto" and not has_model:
            effective = "llm"
            notes.append("model_unavailable_fallback_llm")
        if effective != "model" and self.services.llm is None:
            raise UpstreamError("LLM Gateway tidak dikonfigurasi", retryable=False)
        if effective == "llm":
            cache_key = LLM_MODEL_KEY
        elif self._head is not None:
            cache_key = self._head.model_key
        else:
            cache_key = model_id(self.settings.sentiment_model_dir)

        norm = {it.key: normalize(it.text) for it in items}
        digest = {key: sha256_hex(value) for key, value in norm.items()}
        langs = {
            key: (detect(value) if language == "auto" else language) for key, value in norm.items()
        }
        unique: dict[str, Item] = {}
        for it in items:
            unique.setdefault(digest[it.key], it)

        cached = await self._cache_get(cache_key, list(unique))
        missing = [h for h in unique if h not in cached]
        cost = {"llm_tokens": 0, "items_model": 0, "items_llm": 0, "cache_hits": 0}
        cost["cache_hits"] = sum(1 for it in items if digest[it.key] in cached)

        fresh = await self._classify(
            {h: norm[unique[h].key] for h in missing},
            {h: langs[unique[h].key] for h in missing},
            effective,
            cost,
            notes,
        )
        await self._cache_put(cache_key, fresh, unique)
        results = {**cached, **fresh}

        if aspects:
            await self._aspects(results, unique, norm, aspects, cost, notes, cache_key)

        verdicts = {}
        for it in items:
            v = results[digest[it.key]]
            verdicts[it.key] = Verdict(v.label, v.score, v.method, langs[it.key], v.aspects)
        return Outcome(verdicts, cost, sorted(set(notes)))

    # ------------------------------------------------------------------ klasifikasi
    async def _classify(
        self,
        texts: dict[str, str],
        langs: dict[str, str],
        method: Method,
        cost: dict[str, int],
        notes: list[str],
    ) -> dict[str, Verdict]:
        out: dict[str, Verdict] = {}
        if not texts:
            return out
        to_llm: list[str] = []
        if method == "llm":
            to_llm = list(texts)
        else:
            known = [h for h in texts if langs[h] != "other"]
            for h in texts:
                if langs[h] == "other":
                    if method == "model":
                        out[h] = Verdict(*NEUTRAL_FALLBACK, "model", "other")
                        notes.append("other_language_neutral")
                    else:
                        to_llm.append(h)
            predictions = await self._predict([texts[h] for h in known]) if known else []
            cost["items_model"] += len(known)
            threshold = self.settings.sentiment_llm_threshold
            for h, (label, probs) in zip(known, predictions, strict=True):
                score = probs[label]
                out[h] = Verdict(label, round(score, 4), "model", langs[h])
                if method == "auto" and (score < threshold or is_sarcastic(texts[h])):
                    to_llm.append(h)
        if to_llm:
            result = await sentiment_llm.classify(
                self.services.llm, [texts[h] for h in to_llm], self.budget
            )
            cost["llm_tokens"] += result.tokens
            for i, h in enumerate(to_llm):
                if i in result.labels:
                    label, score = result.labels[i]
                    # hybrid = model sudah memberi label lalu dikoreksi LLM
                    out[h] = Verdict(label, score, "hybrid" if h in out else "llm", langs[h])
                    cost["items_llm"] += 1
                elif h not in out:  # baris LLM hilang 2x dan tidak ada label model
                    out[h] = Verdict(*NEUTRAL_FALLBACK, "llm", langs[h])
                    notes.append("llm_line_missing_neutral")
        return out

    # ------------------------------------------------------------------ aspek
    async def _aspects(
        self,
        results: dict[str, Verdict],
        unique: dict[str, Item],
        norm: dict[str, str],
        wanted: list[str],
        cost: dict[str, int],
        notes: list[str],
        cache_key: str,
    ) -> None:
        wanted = [a.strip().lower() for a in wanted if a.strip()]
        pairs: list[tuple[str, str]] = []
        owners: list[str] = []
        for h, item in unique.items():
            have = {a["aspect"] for a in results[h].aspects}
            for aspect, sentence in aspect_mod.spot(norm[item.key], wanted):
                if aspect not in have:
                    pairs.append((aspect, sentence))
                    owners.append(h)
        if not pairs:
            return
        if self.services.llm is None:
            notes.append("aspects_skipped_no_llm")
            return
        labels, tokens, _calls = await sentiment_llm.classify_aspects(
            self.services.llm, pairs, self.budget
        )
        cost["llm_tokens"] += tokens
        changed: set[str] = set()
        for i, (aspect, _sentence) in enumerate(pairs):
            if i in labels:
                results[owners[i]].aspects.append({"aspect": aspect, "label": labels[i]})
                changed.add(owners[i])
        await self._cache_aspects(cache_key, {h: results[h].aspects for h in changed})

    # ------------------------------------------------------------------ cache DB
    async def _cache_get(self, model: str, hashes: list[str]) -> dict[str, Verdict]:
        if not hashes:
            return {}
        async with self.services.db.session() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT text_sha256, label, score, method, lang, aspects "
                        "FROM mcp.sentiment_results WHERE model = :m AND text_sha256 = ANY(:h)"
                    ),
                    {"m": model, "h": hashes},
                )
            ).all()
        return {
            r.text_sha256.strip(): Verdict(
                r.label, float(r.score), r.method, r.lang or "id", list(r.aspects or [])
            )
            for r in rows
        }

    async def _cache_put(
        self, model: str, fresh: dict[str, Verdict], unique: dict[str, Item]
    ) -> None:
        if not fresh:
            return
        async with self.services.db.session() as session:
            for h, v in fresh.items():
                item = unique[h]
                await session.execute(
                    text(
                        "INSERT INTO mcp.sentiment_results (target_type, target_id, text_sha256, "
                        "method, model, label, score, aspects, lang) VALUES (:tt, :tid, :h, "
                        ":method, :model, :label, :score, CAST(:aspects AS jsonb), :lang) "
                        "ON CONFLICT (text_sha256, model) DO NOTHING"
                    ),
                    {
                        "tt": item.target_type,
                        "tid": item.target_id,
                        "h": h,
                        "method": v.method,
                        "model": model,
                        "label": v.label,
                        "score": v.score,
                        "aspects": orjson.dumps(v.aspects).decode(),
                        "lang": v.lang[:8],
                    },
                )

    async def _cache_aspects(self, model: str, aspects: dict[str, list[dict[str, str]]]) -> None:
        if not aspects:
            return
        async with self.services.db.session() as session:
            for h, value in aspects.items():
                await session.execute(
                    text(
                        "UPDATE mcp.sentiment_results SET aspects = CAST(:a AS jsonb) "
                        "WHERE text_sha256 = :h AND model = :m"
                    ),
                    {"a": orjson.dumps(value).decode(), "h": h, "m": model},
                )
