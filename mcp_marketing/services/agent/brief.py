"""Pesan awal agent: system prompt profil + brief task TERPADATKAN (detail via tool bila perlu)."""

from __future__ import annotations

import re
from typing import Any

from mcp_marketing.utils.text import truncate

DESCRIPTION_MAX = 2_000
OUTLINE_SENTENCE_MAX = 160
FORMAT_RULES = (
    "Format hasil: tulis ke dokumen dengan write_blocks berisi markdown (## per bagian, bullet, "
    "checklist, tabel). Bahasa Indonesia, tanpa HTML. Akhiri dengan finish(summary)."
)
_SENTENCE = re.compile(r"(?<=[.!?])\s+")
LIGHT_KEYWORDS = re.compile(
    r"\b(data|riset|research|analisis|analisa|tren|trend|crawl|scrape|survei|statistik)\b", re.I
)


def system_message(system_prompt: str, *, trigger: str) -> dict[str, Any]:
    extra = {
        "mention": "Ini balasan atas mention di komentar: jawab singkat dengan post_comment "
        "di thread yang sama; "
        "ubah dokumen hanya bila diminta.",
        "prompt_block": "Ini permintaan dari AI Prompt Block: tulis hasilnya dengan write_blocks "
        "(otomatis disisip "
        "setelah prompt block), ringkas.",
    }.get(trigger, "")
    content = f"{system_prompt}\n\n{FORMAT_RULES}"
    if extra:
        content += f"\n{extra}"
    return {"role": "system", "content": content}


def outline(context: list[dict[str, Any]], max_items: int = 40) -> list[str]:
    """Heading + 1 kalimat pertama per bagian (bukan seluruh dokumen)."""
    lines: list[str] = []
    need_sentence = True
    for block in context:
        text = (block.get("text") or "").strip()
        if not text:
            continue
        if block.get("type") == "heading":
            lines.append(f"{'#' * int(block.get('level') or 2)} {truncate(text, 120)}")
            need_sentence = True
        elif need_sentence:
            lines.append(
                f"  {truncate(_SENTENCE.split(text, maxsplit=1)[0], OUTLINE_SENTENCE_MAX)}"
            )
            need_sentence = False
        if len(lines) >= max_items:
            lines.append("  …")
            break
    return lines


def _author(item: dict[str, Any]) -> str:
    return str((item.get("author") or {}).get("display_name") or "User")


def _task_lines(task: dict[str, Any] | None) -> list[str]:
    if not task:
        return ["Task: (level project)"]
    props = [f"status={task.get('status')}", f"prioritas={task.get('priority')}"]
    if task.get("start_date") or task.get("end_date"):
        props.append(f"jadwal={task.get('start_date') or '?'}..{task.get('end_date') or '?'}")
    if task.get("tags"):
        props.append("tags=" + ",".join(task["tags"]))
    props.append(f"depth={task.get('depth', 1)}")
    return [f"Task: {task.get('title')} (id {task.get('id')})", "Properti: " + "; ".join(props)]


def user_brief(run_input: dict[str, Any]) -> str:
    trigger = run_input.get("trigger", "assignment")
    task = run_input.get("task")
    lines = _task_lines(task)
    if trigger == "assignment":
        description = truncate(run_input.get("description") or "", DESCRIPTION_MAX)
        if description:
            lines += ["", "Deskripsi:", description]
        subtasks = run_input.get("subtasks") or []
        if subtasks:
            lines += ["", "Subtask yang sudah ada:"]
            lines += [
                f"- {s.get('title')} [{s.get('status')}] (id {s.get('id')})" for s in subtasks[:30]
            ]
    elif trigger == "mention":
        comment = run_input.get("comment") or {}
        thread = run_input.get("thread") or []
        lines += ["", "Thread komentar (lama -> baru):"]
        lines += [
            f"- {_author(t)}: {truncate(t.get('content', ''), 500)}"
            for t in thread[-10:]
            if t.get("id") != comment.get("id")
        ]
        author = (comment.get("author") or {}).get("display_name") or "User"
        lines += ["", f"Mention terbaru dari {author}:", comment.get("content", "")]
    elif trigger == "prompt_block":
        prompt = (run_input.get("prompt_block") or {}).get("prompt", "")
        lines += ["", "Permintaan prompt block:", prompt]
    doc = outline(run_input.get("context") or [])
    if doc:
        lines += ["", "Outline dokumen saat ini:", *doc]
    lines += ["", "Detail lain ambil dengan pm_query_project_context bila perlu."]
    return "\n".join(lines)


def is_light_request(run_input: dict[str, Any]) -> bool:
    """Mode ringan untuk prompt_block & mention sederhana (≤ 500 char, tanpa data/riset)."""
    trigger = run_input.get("trigger")
    if trigger == "prompt_block":
        text = (run_input.get("prompt_block") or {}).get("prompt", "")
    elif trigger == "mention":
        text = (run_input.get("comment") or {}).get("content", "")
    else:
        return False
    return len(text) <= 500 and not LIGHT_KEYWORDS.search(text)
