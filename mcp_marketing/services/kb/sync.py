"""Sinkron Dashboard -> KB koleksi `dashboard:<project_id>` (inkremental).

Dokumen = Project Brief (blok tanpa task) + satu dokumen per task (judul, properti, blok task).
Konten tiap dokumen dibangun dari `document_blocks.plain_text` urut `order_index`; dokumen yang
hash-nya sama dilewati sehingga re-sync tanpa perubahan tidak memanggil embedding sama sekali.
Task/blok terhapus -> dokumen & chunk-nya dihapus.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from sqlalchemy import select

from mcp_marketing.core.errors import NotFound
from mcp_marketing.models.dashboard import DocumentBlock, Project, Task
from mcp_marketing.models.kb import KbCollection, KbDocument
from mcp_marketing.services.kb.embedder import Embedder
from mcp_marketing.services.kb.identity import embed_identity
from mcp_marketing.services.kb.ingest import collection_name_for_project
from mcp_marketing.services.kb.store import DocInput, delete_document, upsert_document
from mcp_marketing.utils.time import now

SYNC_MARK_TTL_S = 3_600
SOURCE_BRIEF = "dashboard_brief"
SOURCE_TASK = "dashboard_task"
_PREFIX = {
    "bulletListItem": "- ",
    "numberedListItem": "1. ",
    "checkListItem": "- [ ] ",
    "toggleListItem": "- ",
    "quote": "> ",
}


def sync_mark_key(project_id: uuid.UUID | str) -> str:
    return f"kb_sync:{project_id}"


async def maybe_schedule_sync(
    services: Any, project_id: uuid.UUID, workspace_id: uuid.UUID | None
) -> bool:
    """Antrekan `kb_sync_dashboard` bila sinkron terakhir > 1 jam (tanpa menunggu hasilnya)."""
    if services.jobs is None or await services.cache.get(sync_mark_key(project_id)) is not None:
        return False
    params = {
        "project_id": str(project_id),
        "workspace_id": str(workspace_id) if workspace_id else None,
        "full": False,
    }
    await services.jobs.enqueue("kb_sync_dashboard", params, client_id=None, priority=7)
    return True


def block_markdown(block: DocumentBlock) -> str:
    text = (block.plain_text or "").strip()
    if not text:
        return ""
    if block.block_type == "heading":
        level = int(((block.content_json or {}).get("props") or {}).get("level") or 2)
        return f"{'#' * max(1, min(level, 3))} {text}"
    if block.block_type == "codeBlock":
        return f"```\n{text}\n```"
    if block.block_type == "checkListItem" and ((block.content_json or {}).get("props") or {}).get(
        "checked"
    ):
        return f"- [x] {text}"
    return f"{_PREFIX.get(block.block_type, '')}{text}"


def task_markdown(task: Task, blocks: list[DocumentBlock]) -> str:
    props = [f"Status: {task.status}", f"Prioritas: {task.priority}"]
    if task.start_date or task.end_date:
        props.append(f"Tanggal: {task.start_date or '?'} – {task.end_date or '?'}")
    if task.tags:
        props.append(f"Tag: {', '.join(task.tags)}")
    body = "\n\n".join(b for b in (block_markdown(x) for x in blocks) if b)
    return f"# {task.title}\n\n{' | '.join(props)}\n\n{body}".strip()


async def project_collection(services: Any, project: Project) -> KbCollection:
    name = collection_name_for_project(project.id)
    async with services.db.session() as session:
        coll = await session.scalar(
            select(KbCollection).where(
                KbCollection.workspace_id == project.workspace_id, KbCollection.name == name
            )
        )
        if coll is None:
            model, dim = embed_identity(services.settings)
            coll = KbCollection(
                name=name,
                description=f"Sinkron Dashboard project {project.title}"[:2_000],
                workspace_id=project.workspace_id,
                project_id=project.id,
                embed_model=model,
                embed_dim=dim,
            )
            session.add(coll)
            await session.flush()
    return coll


async def sync_project(
    services: Any,
    project_id: uuid.UUID,
    make_embedder: Callable[[KbCollection], Embedder],
    *,
    workspace_id: uuid.UUID | None = None,
    full: bool = False,
    progress: Callable[[int, int], Any] | None = None,
) -> dict[str, Any]:
    async with services.db.session() as session:
        conds = [Project.id == project_id, Project.deleted_at.is_(None)]
        if workspace_id is not None:
            conds.append(Project.workspace_id == workspace_id)
        project = await session.scalar(select(Project).where(*conds))
        if project is None:
            raise NotFound(f"Project {project_id} tidak ditemukan")
        tasks = list(
            (
                await session.scalars(
                    select(Task)
                    .where(Task.project_id == project_id, Task.deleted_at.is_(None))
                    .order_by(Task.depth, Task.title)
                )
            ).all()
        )
        blocks = (
            await session.scalars(
                select(DocumentBlock)
                .where(DocumentBlock.project_id == project_id, DocumentBlock.deleted_at.is_(None))
                .order_by(DocumentBlock.order_index)
            )
        ).all()
    coll = await project_collection(services, project)
    embedder = make_embedder(coll)
    by_task: dict[uuid.UUID | None, list[DocumentBlock]] = defaultdict(list)
    for block in blocks:
        by_task[block.task_id].append(block)

    docs: list[DocInput] = []
    brief_blocks = by_task.get(None, [])
    if brief_blocks:
        body = "\n\n".join(b for b in (block_markdown(x) for x in brief_blocks) if b)
        docs.append(
            DocInput(
                SOURCE_BRIEF,
                str(project_id),
                f"Project Brief: {project.title}",
                body,
                {"project_id": str(project_id), "kind": "brief"},
                source_version=max(b.version for b in brief_blocks),
            )
        )
    for task in tasks:
        task_blocks = by_task.get(task.id, [])
        docs.append(
            DocInput(
                SOURCE_TASK,
                str(task.id),
                task.title,
                task_markdown(task, task_blocks),
                {"project_id": str(project_id), "task_id": str(task.id), "kind": "task"},
                source_version=max((b.version for b in task_blocks), default=0),
            )
        )

    stats = {"documents": len(docs), "created": 0, "updated": 0, "skipped": 0, "deleted": 0}
    stats |= {"chunks": 0, "tokens_embedded": 0}
    async with services.db.session() as session:
        existing = (
            await session.execute(
                select(KbDocument.id, KbDocument.source_type, KbDocument.source_ref).where(
                    KbDocument.collection_id == coll.id,
                    KbDocument.source_type.in_((SOURCE_BRIEF, SOURCE_TASK)),
                )
            )
        ).all()
    wanted = {(d.source_type, d.source_ref) for d in docs}
    for row in existing:
        if full or (row.source_type, row.source_ref) not in wanted:
            await delete_document(services.db, row.id)
            stats["deleted"] += 0 if full else 1
    for i, doc in enumerate(docs, 1):
        result = await upsert_document(services.db, coll, doc, embedder)
        stats[result.status] += 1
        stats["chunks"] += result.chunks
        stats["tokens_embedded"] += result.tokens_embedded
        if progress is not None:
            await progress(i, len(docs))
    await services.cache.set(
        sync_mark_key(project_id), "kb_sync", {"at": now().isoformat()}, SYNC_MARK_TTL_S
    )
    return {"collection_id": str(coll.id), **stats}
