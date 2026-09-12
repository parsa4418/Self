"""Advanced, opt-in account tools for the Telegram self-bot.

The module is intentionally isolated from betting and wallet code.  Every
setting is stored in the historical per-account SQLite database so upgrading
an existing installation does not replace sessions, balances, or old options.
"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import re
import time
import requests
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from telethon import events, functions, types, utils
from telethon.errors import ChatAdminRequiredError, FloodWaitError

from control_store import (
    claim_due_schedule_jobs,
    create_profile_backup,
    create_scheduled_once,
    get_chatgpt_daily_usage,
    get_helper_config,
    get_latest_profile_backup,
    get_message_version,
    get_runtime_metrics,
    list_due_scheduled_once,
    list_private_allowlist,
    list_scheduled_once,
    mark_private_user_blocked,
    private_user_is_allowed,
    record_message_edit,
    register_private_lock_attempt,
    remember_message_version,
    set_private_allowlist_user,
    set_runtime_metric,
    finish_schedule_job_run,
    update_scheduled_once_status,
)

try:
    import qrcode
except ImportError:  # pragma: no cover - reported at runtime
    qrcode = None

try:
    from PIL import Image, ImageDraw
    Image.MAX_IMAGE_PIXELS = 40_000_000
except ImportError:  # pragma: no cover - reported at runtime
    Image = ImageDraw = None


PERSIAN_DIGITS = str.maketrans(
    "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
    "01234567890123456789",
)

ACTION_ALIASES = {
    "تایپ": "typing",
    "typing": "typing",
    "ویس": "record-audio",
    "voice": "record-audio",
    "ویدیو": "record-video",
    "ویدئو": "record-video",
    "video": "record-video",
    "عکس": "photo",
    "photo": "photo",
    "فایل": "document",
    "file": "document",
    "استیکر": "sticker",
    "sticker": "sticker",
    "بازی": "game",
    "game": "game",
}


class AdvancedFeatureEngine:
    """Private lock, anti-edit, profile, group, and utility feature pack."""

    def __init__(self, feature_engine: Any):
        self.feature_engine = feature_engine
        self.account = feature_engine.account
        self.client = feature_engine.client
        self._active_action_tasks: dict[int, asyncio.Task] = {}
        self.phone = feature_engine.phone
        self.owner_id = feature_engine.owner_id
        self.data_dir = feature_engine.data_dir
        self.users_db = feature_engine.users_db
        self.max_in_memory_media_bytes = max(
            1, min(int(os.getenv("MAX_IN_MEMORY_MEDIA_MB", "50") or 50), 100)
        ) * 1024 * 1024
        self.background_tasks: list[asyncio.Task] = []
        self.last_analog_clock_update = 0.0
        self.last_analog_clock_enabled = False

    def settings(self) -> dict[str, str]:
        return self.feature_engine.settings()

    def save_settings(self, values: dict[str, Any]) -> None:
        self.feature_engine.save_settings(values)

    async def safe_edit(self, event, text: str, **kwargs) -> None:
        await self.feature_engine.safe_edit(event, text, **kwargs)

    async def queued_send_message(self, entity, message, **kwargs):
        priority = kwargs.pop("priority", 50)
        sender = getattr(self.account, "queued_send_message", None)
        if sender is not None:
            return await sender(
                entity,
                message,
                priority=priority,
                **kwargs,
            )
        return await self.client.send_message(entity, message, **kwargs)

    async def queued_send_file(self, entity, file, **kwargs):
        priority = kwargs.pop("priority", 50)
        sender = getattr(self.account, "queued_send_file", None)
        if sender is not None:
            return await sender(
                entity,
                file,
                priority=priority,
                **kwargs,
            )
        return await self.client.send_file(entity, file, **kwargs)

    def start_background_tasks(self) -> list[asyncio.Task]:
        if any(not task.done() for task in self.background_tasks):
            return self.background_tasks
        self.background_tasks = [
            asyncio.create_task(
                self.scheduled_once_loop(), name=f"scheduled-once:{self.phone}"
            ),
            asyncio.create_task(
                self.professional_schedule_loop(), name=f"schedule-jobs:{self.phone}"
            ),
            asyncio.create_task(
                self.analog_clock_loop(), name=f"analog-clock:{self.phone}"
            ),
        ]
        return self.background_tasks

    async def stop_background_tasks(self) -> None:
        current = asyncio.current_task()
        tasks = [
            task for task in self.background_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.background_tasks.clear()

    @staticmethod
    def _saved_message_id(reference: str) -> int:
        value = str(reference or "")
        if not value.startswith("tg:"):
            return 0
        try:
            return int(value.split(":", 1)[1])
        except (TypeError, ValueError):
            return 0

    async def _buffer_from_media_reference(
        self, reference: str, *, default_name: str = "media.bin"
    ) -> io.BytesIO:
        ref = str(reference or "").strip()
        payload = b""
        filename = default_name
        saved_id = self._saved_message_id(ref)
        if saved_id:
            message = await self.client.get_messages("me", ids=saved_id)
            if not message:
                raise FileNotFoundError("رسانه Saved Messages پیدا نشد.")
            file_info = getattr(message, "file", None)
            declared = int(getattr(file_info, "size", 0) or 0)
            if declared and declared > self.max_in_memory_media_bytes:
                raise ValueError("حجم رسانه بیشتر از سقف حافظه است.")
            payload = await self.client.download_media(message, file=bytes)
            filename = str(getattr(file_info, "name", "") or filename)
        elif ref.startswith("botfile:"):
            file_id = ref.split(":", 1)[1]
            helper_token = str(get_helper_config(self.users_db).get("token") or "")
            if not helper_token:
                raise RuntimeError("توکن هلپر برای دریافت رسانه ثبت نشده است.")
            def fetch_bot_file() -> tuple[bytes, str]:
                metadata = requests.get(
                    f"https://api.telegram.org/bot{helper_token}/getFile",
                    params={"file_id": file_id}, timeout=20,
                )
                metadata.raise_for_status()
                result = metadata.json().get("result") or {}
                file_path = str(result.get("file_path") or "")
                size = int(result.get("file_size") or 0)
                if not file_path:
                    raise FileNotFoundError("مسیر فایل هلپر پیدا نشد.")
                if size and size > self.max_in_memory_media_bytes:
                    raise ValueError("حجم رسانه بیشتر از سقف حافظه است.")
                with requests.get(
                    f"https://api.telegram.org/file/bot{helper_token}/{file_path}",
                    timeout=60, stream=True,
                ) as response:
                    response.raise_for_status()
                    declared = int(response.headers.get("content-length") or 0)
                    if declared and declared > self.max_in_memory_media_bytes:
                        raise ValueError("حجم رسانه بیشتر از سقف حافظه است.")
                    chunks = bytearray()
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        if not chunk:
                            continue
                        chunks.extend(chunk)
                        if len(chunks) > self.max_in_memory_media_bytes:
                            raise ValueError("حجم رسانه بیشتر از سقف حافظه است.")
                return bytes(chunks), Path(file_path).name or default_name
            payload, filename = await asyncio.to_thread(fetch_bot_file)
        else:
            legacy = Path(ref)
            if not legacy.is_file():
                raise FileNotFoundError("مرجع رسانه پیدا نشد.")
            if legacy.stat().st_size > self.max_in_memory_media_bytes:
                raise ValueError("حجم رسانه بیشتر از سقف حافظه است.")
            payload = await asyncio.to_thread(legacy.read_bytes)
            filename = legacy.name or default_name
            try:
                legacy.unlink()
            except OSError:
                pass
        if not payload:
            raise ValueError("رسانه خالی است.")
        buffer = io.BytesIO(payload)
        buffer.name = filename
        buffer.seek(0)
        return buffer

    async def _save_bytes_to_saved_messages(
        self, payload: bytes, *, filename: str, caption: str
    ) -> str:
        if not payload or len(payload) > self.max_in_memory_media_bytes:
            return ""
        buffer = io.BytesIO(payload)
        buffer.name = filename
        saved = await self.queued_send_file(
            "me", buffer, priority=70, caption=caption[:1024], silent=True
        )
        return f"tg:{int(getattr(saved, 'id', 0) or 0)}"

    async def register_handlers(self) -> None:
        @self.client.on(events.NewMessage(outgoing=True))
        async def advanced_outgoing_router(event):
            try:
                if not self.feature_engine.is_owner_command_event(event):
                    return
                if await self.handle_command(event):
                    self.account.last_activity = time.time()
                    self.metric("last_activity", datetime.now().isoformat())
                    raise events.StopPropagation
            except events.StopPropagation:
                raise
            except FloodWaitError as exc:
                seconds = max(1, int(getattr(exc, "seconds", 60)))
                self.record_error(f"FloodWait {seconds}s")
            except Exception as exc:
                self.record_error(f"{type(exc).__name__}: {exc}")
                await self.safe_edit(
                    event,
                    f"❌ اجرای ابزار پیشرفته ناموفق بود: "
                    f"{type(exc).__name__}",
                )

        @self.client.on(events.NewMessage(incoming=True))
        async def advanced_incoming_router(event):
            try:
                await self.remember_incoming_message(event)
                if await self.enforce_private_lock(event):
                    raise events.StopPropagation
            except events.StopPropagation:
                raise
            except FloodWaitError:
                return
            except Exception as exc:
                self.record_error(f"incoming {type(exc).__name__}: {exc}")

        @self.client.on(events.MessageEdited(incoming=True))
        async def anti_edit_router(event):
            try:
                await self.handle_message_edited(event)
            except FloodWaitError as exc:
                seconds = max(1, int(getattr(exc, "seconds", 60)))
                self.record_error(f"anti-edit FloodWait {seconds}s")
            except Exception as exc:
                self.record_error(f"edit {type(exc).__name__}: {exc}")

        @self.client.on(events.ChatAction())
        async def group_greeting_router(event):
            try:
                await self.handle_chat_action(event)
            except Exception as exc:
                self.record_error(f"chat-action {type(exc).__name__}: {exc}")

    def metric(self, key: str, value: Any) -> None:
        try:
            set_runtime_metric(
                self.data_dir,
                self.phone,
                key,
                value,
            )
        except Exception:
            pass

    def record_error(self, message: str) -> None:
        print(f"خطای ابزار پیشرفته برای {self.phone}: {message}")
        self.metric("last_error", str(message)[:500])
        self.metric("last_error_at", datetime.now().isoformat())

    @staticmethod
    def normalize(value: str) -> str:
        text = str(value or "").translate(PERSIAN_DIGITS).strip()
        text = text.replace("ي", "ی").replace("ك", "ک")
        # دستورات فارسی بدون نقطه هستند؛ نقطهٔ ابتدای دستور هم برای
        # سازگاری پذیرفته می‌شود و اسلش همچنان دستور محسوب نمی‌شود.
        if text.startswith("/"):
            return ""
        if text.startswith("."):
            text = text[1:].strip()
        return text

    @staticmethod
    def on_off(value: str) -> str | None:
        normalized = str(value or "").strip().lower()
        if normalized in {"on", "روشن", "فعال"}:
            return "on"
        if normalized in {"off", "خاموش", "غیرفعال"}:
            return "off"
        return None

    async def replied_message(self, event):
        if not getattr(event, "is_reply", False):
            return None
        return await event.get_reply_message()

    async def target_from_reply_or_text(
        self,
        event,
        raw_target: str = "",
    ) -> tuple[int, Any]:
        reply = await self.replied_message(event)
        if reply and getattr(reply, "sender_id", None):
            sender = await reply.get_sender()
            return int(reply.sender_id), sender
        target = self.normalize(raw_target)
        if not target:
            raise ValueError("روی پیام کاربر ریپلای کنید یا آیدی او را بنویسید.")
        ref: int | str = int(target) if target.lstrip("-").isdigit() else target
        entity = await self.client.get_entity(ref)
        return int(entity.id), entity

    @staticmethod
    def display_name(entity: Any, fallback: str = "کاربر") -> str:
        parts = [
            str(getattr(entity, "first_name", "") or "").strip(),
            str(getattr(entity, "last_name", "") or "").strip(),
        ]
        name = " ".join(item for item in parts if item).strip()
        username = str(getattr(entity, "username", "") or "").strip()
        return name or (f"@{username}" if username else fallback)

    async def remember_incoming_message(self, event) -> None:
        if self.feature_engine.sensitive_account_message(event):
            return
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        message_id = int(getattr(event, "id", 0) or 0)
        if not chat_id or not message_id:
            return
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        sender_name = str(sender_id or "نامشخص")
        chat_title = str(chat_id)
        try:
            sender = await event.get_sender()
            sender_name = self.display_name(sender, sender_name)
        except Exception:
            pass
        try:
            chat = await event.get_chat()
            chat_title = (
                str(getattr(chat, "title", "") or "").strip()
                or self.display_name(chat, chat_title)
            )
        except Exception:
            pass
        remember_message_version(
            self.data_dir,
            self.phone,
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_title=chat_title,
            message_text=str(getattr(event, "raw_text", "") or ""),
        )

    async def enforce_private_lock(self, event) -> bool:
        settings = self.settings()
        if (
            not getattr(event, "is_private", False)
            or settings.get("private_lock_enabled") != "on"
            or int(getattr(event, "sender_id", 0) or 0) == self.owner_id
        ):
            return False
        sender_id = int(getattr(event, "sender_id", 0) or 0)
        if not sender_id:
            return False
        try:
            sender = await event.get_sender()
            if getattr(sender, "bot", False):
                return False
        except Exception:
            sender = None
        if private_user_is_allowed(
            self.data_dir,
            self.phone,
            sender_id,
        ):
            return False

        warning_limit = self._bounded_int(
            settings.get("private_lock_warning_limit", "1"),
            default=1,
            minimum=0,
            maximum=10,
        )
        attempt = register_private_lock_attempt(
            self.data_dir,
            self.phone,
            sender_id,
        )
        warn_first = settings.get("private_lock_warn_before_block", "on") == "on"
        should_block = not warn_first or attempt > warning_limit
        if should_block:
            try:
                await self.client(
                    functions.contacts.BlockRequest(id=sender_id)
                )
                mark_private_user_blocked(
                    self.data_dir,
                    self.phone,
                    sender_id,
                )
            except Exception as exc:
                self.record_error(
                    f"private-block {sender_id} {type(exc).__name__}"
                )
        else:
            warning = str(
                settings.get("private_lock_warning_text", "")
                or "⛔ پیام خصوصی این حساب بسته است."
            )
            try:
                if getattr(self.account, "queued_send_message", None) is None:
                    await event.reply(warning[:4000])
                else:
                    await self.queued_send_message(
                        event.chat_id,
                        warning[:4000],
                        reply_to=int(event.id),
                        priority=40,
                    )
            except Exception:
                pass

        if settings.get("private_lock_delete_unknown", "on") == "on":
            try:
                await event.delete()
            except Exception:
                pass
        return True

    async def handle_message_edited(self, event) -> None:
        if int(getattr(event, "sender_id", 0) or 0) == self.owner_id:
            return
        settings = self.settings()
        is_private = bool(getattr(event, "is_private", False))
        is_group = bool(getattr(event, "is_group", False))
        if (
            is_private
            and settings.get("anti_edit_private") != "on"
        ) or (
            is_group
            and settings.get("anti_edit_groups") != "on"
        ) or (not is_private and not is_group):
            return
        chat_id = int(getattr(event, "chat_id", 0) or 0)
        message_id = int(getattr(event, "id", 0) or 0)
        previous = get_message_version(
            self.data_dir,
            self.phone,
            chat_id=chat_id,
            message_id=message_id,
        )
        after = str(getattr(event, "raw_text", "") or "")
        if not previous:
            await self.remember_incoming_message(event)
            return
        before = str(previous.get("message_text") or "")
        if before == after:
            return
        sender_name = str(previous.get("sender_name") or "نامشخص")
        chat_title = str(previous.get("chat_title") or chat_id)
        sender_id = int(previous.get("sender_id") or 0)
        scope = "private" if is_private else "group"
        record_message_edit(
            self.data_dir,
            self.phone,
            chat_id=chat_id,
            message_id=message_id,
            sender_id=sender_id,
            sender_name=sender_name,
            chat_title=chat_title,
            before_text=before,
            after_text=after,
            scope=scope,
        )
        await self.remember_incoming_message(event)
        if settings.get("anti_edit_notify_saved", "on") != "on":
            return
        report = (
            "✏️ پیام ویرایش شد\n"
            f"👤 فرستنده: {sender_name}\n"
            f"🆔 کاربر: {sender_id or 'نامشخص'}\n"
            f"💬 چت: {chat_title}\n"
            f"🆔 پیام: {message_id}\n\n"
            f"قبل:\n{before or '—'}\n\n"
            f"بعد:\n{after or '—'}"
        )
        await self.queued_send_message(
            "me",
            report[:4000],
            parse_mode=None,
            silent=True,
            priority=70,
        )

    async def handle_chat_action(self, event) -> None:
        if not getattr(event, "is_group", False):
            return
        settings = self.settings()
        joined = bool(
            getattr(event, "user_joined", False)
            or getattr(event, "user_added", False)
        )
        left = bool(
            getattr(event, "user_left", False)
            or getattr(event, "user_kicked", False)
        )
        if (
            joined
            and settings.get("welcome_enabled") != "on"
        ) or (
            left
            and settings.get("goodbye_enabled") != "on"
        ) or (not joined and not left):
            return
        users = await event.get_users()
        if not isinstance(users, (list, tuple)):
            users = [users]
        chat = await event.get_chat()
        chat_name = str(getattr(chat, "title", "") or event.chat_id)
        template = str(
            settings.get(
                "welcome_text" if joined else "goodbye_text",
                "",
            )
        )
        for user in users[:10]:
            if not user:
                continue
            text = template.format(
                name=self.display_name(user),
                id=int(getattr(user, "id", 0) or 0),
                username=(
                    f"@{user.username}"
                    if getattr(user, "username", None)
                    else ""
                ),
                chat=chat_name,
            )
            await self.queued_send_message(
                event.chat_id,
                text[:4000],
                priority=50,
            )

    async def handle_command(self, event) -> bool:
        raw = self.normalize(getattr(event, "raw_text", ""))
        if not raw:
            return False
        lower = raw.lower()

        if lower in {"قفل پیوی"}:
            settings = self.settings()
            allowed = list_private_allowlist(
                self.data_dir,
                self.phone,
                limit=500,
            )
            await self.safe_edit(
                event,
                "🔐 قفل پیوی\n\n"
                f"وضعیت: {self._state(settings, 'private_lock_enabled')}\n"
                f"حذف ناشناس: "
                f"{self._state(settings, 'private_lock_delete_unknown')}\n"
                f"هشدار قبل بلاک: "
                f"{self._state(settings, 'private_lock_warn_before_block')}\n"
                f"تعداد هشدار: "
                f"{settings.get('private_lock_warning_limit', '1')}\n"
                f"افراد مجاز: {len(allowed)} نفر",
            )
            return True

        match = re.fullmatch(
            r"قفل پیوی\s+(روشن|خاموش)",
            lower,
        )
        if match:
            state = self.on_off(match.group(1))
            self.save_settings({"private_lock_enabled": state})
            await self.safe_edit(
                event,
                f"✅ قفل پیوی "
                f"{'فعال' if state == 'on' else 'غیرفعال'} شد.",
            )
            return True

        match = re.fullmatch(
            r"مجاز افزودن\s*(.*)",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            try:
                user_id, entity = await self.target_from_reply_or_text(
                    event,
                    match.group(1),
                )
            except ValueError as exc:
                await self.safe_edit(event, f"❌ {exc}")
                return True
            set_private_allowlist_user(
                self.data_dir,
                self.phone,
                user_id,
                allowed=True,
                label=self.display_name(entity),
            )
            await self.safe_edit(event, f"✅ کاربر `{user_id}` مجاز شد.")
            return True

        match = re.fullmatch(
            r"مجاز حذف\s*(.*)",
            raw,
            re.IGNORECASE | re.DOTALL,
        )
        if match:
            try:
                user_id, _ = await self.target_from_reply_or_text(
                    event,
                    match.group(1),
                )
            except ValueError as exc:
                await self.safe_edit(event, f"❌ {exc}")
                return True
            set_private_allowlist_user(
                self.data_dir,
                self.phone,
                user_id,
                allowed=False,
            )
            await self.safe_edit(event, f"✅ کاربر `{user_id}` از مجازها حذف شد.")
            return True

        if lower in {"مجازها"}:
            rows = list_private_allowlist(
                self.data_dir,
                self.phone,
                limit=100,
            )
            lines = ["✅ فهرست افراد مجاز پیوی:"]
            lines.extend(
                f"• `{row['user_id']}` — {row['label'] or 'بدون نام'}"
                for row in rows
            )
            if not rows:
                lines.append("• فهرست خالی است.")
            await self.safe_edit(event, "\n".join(lines))
            return True

        match = re.fullmatch(
            r"متن هشدار پیوی\s+([\s\S]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            warning = match.group(1).strip()
            if not 1 <= len(warning) <= 1000:
                await self.safe_edit(
                    event,
                    "❌ متن هشدار باید بین ۱ تا ۱۰۰۰ نویسه باشد.",
                )
            else:
                self.save_settings({"private_lock_warning_text": warning})
                await self.safe_edit(event, "✅ متن هشدار پیوی ذخیره شد.")
            return True

        match = re.fullmatch(
            r"ضد ویرایش\s+(پیوی|گروه)\s+"
            r"(روشن|خاموش)",
            lower,
        )
        if match:
            key = (
                "anti_edit_private"
                if match.group(1) in {"پیوی", "private"}
                else "anti_edit_groups"
            )
            state = self.on_off(match.group(2))
            self.save_settings({key: state})
            await self.safe_edit(
                event,
                "✅ ضد ویرایش "
                f"{'پیوی' if key.endswith('private') else 'گروه'} "
                f"{'فعال' if state == 'on' else 'غیرفعال'} شد.",
            )
            return True

        match = re.fullmatch(
            r"جستجو\s+([\s\S]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            await self.search_messages(event, match.group(1).strip())
            return True

        if lower in {"شناسه پیام"}:
            await self.show_message_info(event)
            return True

        if lower in {"ذخیره پیام"}:
            await self.save_message(event)
            return True

        match = re.fullmatch(
            r"(?:ارسال یکباره|ارسال یک‌باره)\s+"
            r"(.+?)\s*\|\s*([\s\S]+)",
            raw,
            re.IGNORECASE,
        )
        if match:
            await self.schedule_once(
                event,
                match.group(1).strip(),
                match.group(2).strip(),
            )
            return True

        if lower in {"ارسال های یکباره"}:
            await self.show_scheduled_once(event)
            return True

        match = re.fullmatch(
            r"لغو ارسال یکباره\s+(\d+)",
            lower,
        )
        if match:
            update_scheduled_once_status(
                self.data_dir,
                self.phone,
                int(match.group(1)),
                status="cancelled",
            )
            await self.safe_edit(event, "✅ ارسال یک‌باره لغو شد.")
            return True

        match = re.fullmatch(
            r"دانلود پیام(?:\s+([\s\S]+))?",
            raw,
            re.IGNORECASE,
        )
        if match:
            await self.download_message(event, (match.group(1) or "").strip())
            return True

        match = re.fullmatch(r"نام\s+([\s\S]+)", raw, re.I)
        if match:
            await self.update_profile_text(
                event,
                "first_name",
                match.group(1).strip(),
            )
            return True

        match = re.fullmatch(
            r"نام خانوادگی\s+([\s\S]*)",
            raw,
            re.IGNORECASE,
        )
        if match:
            await self.update_profile_text(
                event,
                "last_name",
                match.group(1).strip(),
            )
            return True

        match = re.fullmatch(r"بیو\s+([\s\S]*)", raw, re.I)
        if match:
            await self.update_profile_text(
                event,
                "about",
                match.group(1).strip(),
            )
            return True

        if lower in {"عکس پروفایل"}:
            await self.update_profile_photo(event)
            return True

        if lower in {"کپی پروفایل"}:
            await self.copy_profile(event)
            return True

        if lower in {"بازیابی پروفایل"}:
            await self.restore_profile(event)
            return True

        # اکشن روشن: در همان چت تا زمان «اکشن خاموش» ادامه دارد.
        match = re.fullmatch(r"اکشن\s+(\S+)\s+(روشن|خاموش)", lower)
        if match and match.group(1) in ACTION_ALIASES:
            action_name = match.group(1)
            action = ACTION_ALIASES[action_name]
            state = self.on_off(match.group(2))
            settings = self.settings()
            settings[f"action_{action_name}"] = state or "off"
            self.save_settings({f"action_{action_name}": state or "off"})
            if state == "on":
                await self.start_persistent_action(event, action)
                await self.safe_edit(event, f"✅ اکشن {action_name} فعال شد.")
            else:
                await self.stop_persistent_action(event)
                await self.safe_edit(event, f"✅ اکشن {action_name} غیرفعال شد.")
            return True

        if lower == "اکشن خاموش":
            await self.stop_persistent_action(event)
            keys = {f"action_{name}": "off" for name in ACTION_ALIASES if name not in {"typing", "voice", "video", "photo", "file", "sticker", "game"}}
            # Keep only the Persian action keys used by the feature.
            keys = {f"action_{name}": "off" for name in ("تایپ", "ویس", "ویدیو", "عکس", "فایل", "استیکر", "بازی")}
            self.save_settings(keys)
            await self.safe_edit(event, "✅ همه اکشن‌ها خاموش شدند.")
            return True

        # Legacy one-shot action remains available only when a numeric duration
        # is explicitly supplied.
        match = re.fullmatch(r"اکشن\s+(\S+)\s+(\d+)", lower)
        if match and match.group(1) in ACTION_ALIASES:
            await self.show_action(event, ACTION_ALIASES[match.group(1)], match.group(2))
            return True

        # «اسپم عدد متن» — bounded to a maximum of 100 messages.
        match = re.fullmatch(r"اسپم\\s+(\\d+)\\s+([\\s\\S]+)", lower)
        if match:
            count = min(int(match.group(1)), 100)
            spam_text = match.group(2).strip()
            if count < 1 or not spam_text:
                await self.safe_edit(event, "❌ تعداد یا متن معتبر نیست.")
                return True
            await self.safe_edit(event, f"✅ ارسال {count} پ�