"""单任务编排：整理命名 -> 并行下载（含 SHA1）-> 秒传/OSS 上传 -> 清理。

被 TaskQueue worker 调用。任何异常都回写到跟踪消息，不传播。
任务状态同步到 persistence（历史/统计），账号失败反馈给 AccountManager。
"""
from __future__ import annotations

import logging
import time

from core.app import state
from core.downloader import download
from core.organize import organize
from core.progress import ProgressReporter, human_bytes
from core.queue import Task, TaskCancelled
from core.uploader import upload_to_dir
from persistence.models import (
    STATUS_CANCELLED, STATUS_DONE, STATUS_DOWNLOADING, STATUS_FAILED,
    STATUS_QUEUED, STATUS_UPLOADING, TaskRow,
)

log = logging.getLogger(__name__)


async def _persist(task: Task, status: str, **kw) -> None:
    if state.db is None:
        return
    try:
        await state.db.insert_task(TaskRow(
            task_id=task.task_id, user_id=task.user_id, source=task.source,
            filename=task.filename, size=task.size, target_dir=task.target_dir,
            status=status, chat_id=task.tracking_chat_id,
            message_id=task.tracking_message_id, channel_id=task.channel_id,
            created_at=time.time(),
        ))
    except Exception:  # noqa: BLE001 -- 持久化失败不应阻断主流程
        log.debug("insert_task 失败", exc_info=True)
        try:
            await state.db.update_task(task.task_id, status=status, **kw)
        except Exception:  # noqa: BLE001
            pass


async def _update(task: Task, **kw) -> None:
    if state.db is None:
        return
    try:
        await state.db.update_task(task.task_id, **kw)
    except Exception:  # noqa: BLE001
        log.debug("update_task 失败", exc_info=True)


async def run_task(task: Task) -> None:
    ws = state.workspace
    cfg = state.config
    reporter = ProgressReporter(
        state.pyro_bot, task.tracking_chat_id, task.tracking_message_id,
        task_id=task.task_id, filename=task.filename, source=task.source,
    )
    tmp = ws.path_for(task.filename)

    # 整理：重命名模板 + 按扩展名归类（仅影响上传到 115 时的名/目录，本地临时名不变）
    final_name, final_dir = organize(
        task.filename, task.target_dir,
        rename_template=cfg.organize.rename_template,
        classify_by_ext=cfg.organize.classify_by_ext,
        size=task.size,
    )
    task.filename = final_name
    task.target_dir = final_dir

    await _persist(task, STATUS_QUEUED)
    acct_name_used = None

    try:
        # 提前校验 115 账号可用性，避免下完才发现无法上传
        if state.accounts is None or not state.accounts.names():
            raise RuntimeError("无可用 115 账号（请检查凭据或 /auth 重新授权）")

        if not ws.has_enough_space(task.size):
            state.low_disk_alerted = True
            await reporter.final_text(
                f"⚠️ 磁盘空间不足（需预留 {cfg.storage.min_free_gb}GB），任务已跳过。\n"
                f"当前剩余 {human_bytes(ws.free_bytes())}"
            )
            await _update(task, status=STATUS_FAILED, error="disk full")
            return

        ws.preallocate(tmp, task.size)
        reporter.set_total(task.size)

        await reporter.set_stage("📥 下载中")
        await _update(task, status=STATUS_DOWNLOADING)
        written, sha1 = await download(
            state.download_client(),
            task.message,
            tmp,
            size=task.size,
            workers=cfg.upload.workers,
            on_progress=reporter.on_progress,
            cancel_event=task.cancel_event,
        )
        log.info("下载完成 %s: %d bytes sha1=%s", task.filename, written, sha1)

        await reporter.set_stage("⬆️ 上传到 115")
        await _update(task, status=STATUS_UPLOADING)
        cloud = await state.accounts.get()
        acct_name_used = getattr(cloud, "account", None)
        acct_name_used = getattr(acct_name_used, "name", None)
        try:
            result = await upload_to_dir(
                cloud,
                tmp,
                written,
                sha1,
                task.target_dir,
                task.filename,
                oss_concurrency=cfg.upload.oss_concurrency,
                on_progress=reporter.on_progress,
                cancel_event=task.cancel_event,
            )
        except Exception as up_err:  # noqa: BLE001 -- 上传失败：反馈账号并重抛
            if acct_name_used and state.accounts is not None:
                state.accounts.report_failure(acct_name_used, repr(up_err))
            raise
        if acct_name_used and state.accounts is not None:
            state.accounts.report_success(acct_name_used)

        await _update(task, status=STATUS_DONE, method=result.method)
        await reporter.final_text(
            f"✅ 完成\n📄 {task.filename}\n📦 {human_bytes(written)}\n"
            f"📁 {task.target_dir}\n⚡ {result.method}"
            + (f"\n👤 {acct_name_used}" if acct_name_used else "")
        )
    except TaskCancelled:
        log.info("任务已取消: %s", task.filename)
        await _update(task, status=STATUS_CANCELLED)
        await reporter.final_text(f"🚫 已取消\n📄 {task.filename}")
    except Exception as e:  # noqa: BLE001
        log.exception("任务失败: %s", task.filename)
        await _update(task, status=STATUS_FAILED, error=repr(e)[:500])
        await reporter.final_text(f"❌ 失败: {task.filename}\n原因: {e}")
    finally:
        state.unregister_task(task)
        if cfg.upload.delete_after_upload:
            ws.cleanup(tmp)
