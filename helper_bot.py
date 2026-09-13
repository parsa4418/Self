"""Inline helper bot used to display and control every user's self-bot panel."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import psutil
from telegram import (
    CopyTextButton,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InlineQueryResultCachedPhoto,
    InputTextMessageContent,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

from control_store import (
    add_enemy_hostile_replies,
    add_auto_reply_response,
    add_friend_affection_replies,
    add_secretary_reply,
    add_word_filter,
    clear_message_archive,
    count_secretary_replies,
    create_auto_reply_rule,
    create_schedule_job,
    delete_first_comment_channel,
    delete_enemy_hostile_reply,
    delete_auto_reply_rule,
    delete_friend_affection_reply,
    delete_secretary_reply,
    delete_tracked_profile,
    delete_word_filter,
    get_active_user,
    get_chatgpt_daily_usage,
    get_feature_counts,
    get_helper_config,
    get_runtime_metrics,
    get_self_settings,
    list_auto_reply_rules,
    list_enemies,
    list_enemy_hostile_replies,
    list_first_comment_channels,
    list_private_allowlist,
    list_friend_affection_replies,
    list_friends,
    list_secretary_replies,
    list_schedule_jobs,
    list_tracked_profiles,
    list_word_filters,
    set_enemy,
    set_friend,
    set_private_allowlist_user,
    set_app_settings,
    set_self_setting,
    set_schedule_job_status,
    upsert_first_comment_channel,
    upsert_tracked_profile,
)


TOGGLE_LABELS = {
    "online_status": "همیشه آنلاین",
    "presence_emoji_enabled": "ایموجی آنلاین/آفلاین کنار نام",
    "presence_auto_detect": "تشخیص خودکار آنلاین/آفلاین",
    "typing_action": "اکشن تایپینگ",
    "secretary": "پاسخ عمومی منشی",
    "auto_reply": "سؤال‌وجواب‌های ثبت‌شده",
    "offline_reply_enabled": "پاسخ حالت آفلاین",
    "timename": "ساعت در نام",
    "timebio": "ساعت در بیو",
    "save_timed_photos": "ذخیره عکس زمان‌دار",
    "anti_delete_enabled": "ضدحذف پیام‌های عادی",
    "anti_delete_private": "ضدحذف پیوی",
    "anti_delete_groups": "ضدحذف گروه",
    "anti_delete_channels": "ضدحذف کانال",
    "scheduled_message_enabled": "ارسال زمان‌بندی‌شده",
    "force_join_private": "عضویت اجباری پیوی",
    "auto_read_private": "سین خودکار پیوی",
    "auto_read_groups": "سین خودکار گروه",
    "auto_reaction": "ری‌اکت خودکار",
    "relationship_reaction": "واکنش دوست/دشمن",
    "friend_affection_reply": "پاسخ صمیمی به دوست",
    "enemy_hostile_reply": "پاسخ خودکار به دشمن",
    "outgoing_signature_enabled": "امضای خودکار",
    "lock_links": "قفل لینک",
    "lock_forwards": "قفل فوروارد",
    "lock_photos": "قفل عکس",
    "lock_videos": "قفل ویدیو",
    "lock_gifs": "قفل گیف",
    "lock_stickers": "قفل استیکر",
    "lock_voice": "قفل ویس",
    "lock_files": "قفل فایل",
    "lock_polls": "قفل نظرسنجی",
    "word_filter_enabled": "فیلتر کلمات",
    "profile_monitor_enabled": "پایش پروفایل",
    "first_comment_enabled": "کامنت اول",
    "private_lock_enabled": "قفل کامل پیوی",
    "private_lock_delete_unknown": "حذف پیام ناشناس",
    "anti_edit_private": "ضد ویرایش پیوی",
    "anti_edit_groups": "ضد ویرایش گروه",
    "welcome_enabled": "خوش‌آمدگویی",
    "goodbye_enabled": "خداحافظی",
    "analog_clock_enabled": "ساعت عقربه‌ای عکس",
}


def render_panel_html(text: str) -> str:
    """Escape panel text and turn `command` fragments into real code spans."""
    parts = re.split(r"(`[^`\n]+`)", str(text or ""))
    rendered = []
    for part in parts:
        if len(part) >= 2 and part.startswith("`") and part.endswith("`"):
            rendered.append(f"<code>{html.escape(part[1:-1])}</code>")
        else:
            rendered.append(html.escape(part))
    return "".join(rendered)


def fit_photo_caption(text: str, limit: int = 1000) -> str:
    """Keep photo-panel captions within Telegram's 1024 character limit."""
    value = str(text or "")
    if len(value) <= limit:
        return value
    clipped = value[: limit - 62].rstrip()
    if clipped.count("`") % 2:
        clipped = clipped.rsplit("`", 1)[0].rstrip()
    return (
        f"{clipped}\n\n"
        "… ادامه متن در این صفحه خلاصه شد؛ دکمه‌ها همچنان فعال‌اند."
    )


def glass_button(text: str, owner_id: int, action: str, *, style=None):
    api_kwargs = {"style": style} if style else None
    return InlineKeyboardButton(
        text=text,
        callback_data=f"hp:{owner_id}:{action}",
        api_kwargs=api_kwargs,
    )


def link_button(text: str, url: str, *, style=None):
    api_kwargs = {"style": style} if style else None
    return InlineKeyboardButton(
        text=text,
        url=url,
        api_kwargs=api_kwargs,
    )


def copy_button(text: str, value: str):
    return InlineKeyboardButton(
        text=text,
        copy_text=CopyTextButton(text=value),
    )


def write_runtime_status(
    status_file: str | Path | None,
    status: str,
    detail: str | None = None,
    **extra,
) -> None:
    if not status_file:
        return

    status_path = Path(status_file)
    status_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "status": status,
        "pid": os.getpid(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        **extra,
    }
    if detail:
        payload["detail"] = str(detail)

    temporary_path = status_path.with_suffix(status_path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporary_path, status_path)


class HelperPanelBot:
    def __init__(
        self,
        token: str,
        data_dir: str | Path,
        status_file: str | Path | None,
    ):
        self.token = token
        self.data_dir = Path(data_dir)
        self.users_db = self.data_dir / "users.db"
        self.status_file = Path(status_file) if status_file else None
        self.application = (
            Application.builder()
            .token(token)
            .post_init(self.post_init)
            .post_shutdown(self.post_shutdown)
            .build()
        )
        self.application.add_handler(CommandHandler("start", self.start))
        self.application.add_handler(
            CommandHandler("cancel", self.cancel_schedule_input)
        )
        self.application.add_handler(InlineQueryHandler(self.inline_panel))
        self.application.add_handler(
            CallbackQueryHandler(self.panel_callback, pattern=r"^hp:")
        )
        self.application.add_handler(
            MessageHandler(
                filters.ChatType.PRIVATE & ~filters.COMMAND,
                self.receive_schedule_input,
            )
        )

    async def post_init(self, application: Application) -> None:
        helper_user = await application.bot.get_me()
        if not helper_user.username:
            raise RuntimeError("بات هلپر نام کاربری ندارد.")
        if not helper_user.supports_inline_queries:
            raise RuntimeError(
                "Inline Mode بات هلپر در BotFather فعال نشده است."
            )

        set_app_settings(
            self.users_db,
            {
                "helper_username": helper_user.username,
                "helper_bot_id": helper_user.id,
                "helper_pid": os.getpid(),
            },
        )
        write_runtime_status(
            self.status_file,
            "ready",
            username=helper_user.username,
            bot_id=helper_user.id,
        )

    async def post_shutdown(self, application: Application) -> None:
        write_runtime_status(self.status_file, "stopped")

    async def start(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        payload = context.args[0] if context.args else ""
        match = re.fullmatch(
            r"(schedtext|schedtarget|schedinterval|schedcreate|secretaryqa|"
            r"secretaryfallback|offlinetext|offlinecooldown|"
            r"onlineemoji|offlineemoji|"
            r"formcreate|formintro|filteradd|friendadd|frienddel|"
            r"friendtext|enemyadd|enemydel|enemytext|profileadd|profiledel|"
            r"pmwarning|pmallowadd|pmallowdel|welcometext|goodbyetext|"
            r"actionduration|firstcomment|"
            r"firstcommentdel|watermark|signature|reaction)_(\d+)",
            payload,
        )
        if match:
            input_type = {
                "schedtext": "schedule_text",
                "schedtarget": "schedule_target",
                "schedinterval": "schedule_interval",
                "schedcreate": "schedule_create",
                "secretaryqa": "secretary_qa",
                "secretaryfallback": "secretary_fallback",
                "offlinetext": "offline_text",
                "offlinecooldown": "offline_cooldown",
                "onlineemoji": "online_emoji",
                "offlineemoji": "offline_emoji",
                "filteradd": "filter_add",
                "friendadd": "friend_add",
                "frienddel": "friend_del",
                "friendtext": "friend_text",
                "enemyadd": "enemy_add",
                "enemydel": "enemy_del",
                "enemytext": "enemy_text",
                "profileadd": "profile_add",
                "profiledel": "profile_del",
                "pmwarning": "private_warning",
                "pmallowadd": "private_allow_add",
                "pmallowdel": "private_allow_del",
                "welcometext": "welcome_text",
                "goodbyetext": "goodbye_text",
                "actionduration": "action_duration",
                "firstcomment": "first_comment",
                "firstcommentdel": "first_comment_del",
                "watermark": "watermark_text",
                "signature": "signature_text",
                "reaction": "reaction_emoji",
            }[match.group(1)]
            owner_id = int(match.group(2))
            if update.effective_user.id != owner_id:
                await update.effective_message.reply_text(
                    "❌ این لینک تنظیمات متعلق به حساب شما نیست."
                )
                return
            record = self.user_record(owner_id)
            if not record:
                await update.effective_message.reply_text(
                    "❌ سلف فعال این حساب پیدا نشد."
                )
                return
            prompts = {
                "schedule_text": (
                    "📝 متن پیام زمان‌بندی‌شده را بفرستید.\n\n"
                    "نمونه: میو\n"
                    "حداکثر طول متن ۳۵۰۰ نویسه است."
                ),
                "schedule_target": (
                    "👥 آیدی گروه مقصد را بفرستید.\n\n"
                    "نمونه عمومی: @MyGroup\n"
                    "نمونه خصوصی: -1001234567890\n\n"
                    "حساب سلف باید از قبل داخل گروه عضو باشد."
                ),
                "schedule_interval": (
                    "⏱ فاصله ارسال را برحسب دقیقه بفرستید.\n\n"
                    "نمونه: 5\n"
                    "مقدار مجاز از ۱ دقیقه تا ۷ روز است."
                ),
                "schedule_create": (
                    "⏰ مرحله ۱ — مقصد برنامه\n\n"
                    "آیدی مقصد را بفرستید؛ نمونه: @MyGroup یا "
                    "-1001234567890"
                ),
                "secretary_qa": (
                    "💬 مرحله ۱ — کلمات یا سؤال‌های محرک\n\n"
                    "هر عبارت را در یک خط جدا بفرستید؛ سپس می‌توانید چند "
                    "پاسخ متنی، عکس، ویدئو، ویس، استیکر یا فایل ثبت کنید.\n\n"
                    "نمونه:\nقیمت\nقیمت چنده\nهزینه"
                ),
                "secretary_fallback": (
                    "🤖 متن پاسخ عمومی منشی را بفرستید.\n\n"
                    "این متن فقط وقتی ارسال می‌شود که پیام کاربر با هیچ "
                    "سؤال‌وجواب یا فرم فعالی مطابقت نداشته باشد."
                ),
                "offline_text": (
                    "🌙 متنی را بفرستید که در حالت آفلاین به پیام خصوصی "
                    "کاربر پاسخ داده شود.\n\n"
                    "می‌توانید از {time} و {date} داخل متن استفاده کنید."
                ),
                "offline_cooldown": (
                    "⏳ فاصله تکرار پاسخ آفلاین برای هر کاربر را به دقیقه "
                    "بفرستید.\n\n"
                    "مقدار مجاز از ۱ دقیقه تا ۷ روز است."
                ),
                "online_emoji": (
                    "🟢 ایموجی حالت آنلاین را بفرستید.\n"
                    "نمونه: 🟢 یا ✅"
                ),
                "offline_emoji": (
                    "🔴 ایموجی حالت آفلاین را بفرستید.\n"
                    "نمونه: 🔴 یا 🌙"
                ),
                "filter_add": (
                    "🧹 مرحله ۱ از ۲ — عبارت‌های فیلتر\n\n"
                    "یک یا چند عبارت را بفرستید؛ برای افزودن گروهی هر "
                    "عبارت را در یک خط جدا قرار دهید."
                ),
                "friend_add": (
                    "💚 آیدی عددی کاربری را بفرستید که دوست محسوب شود."
                ),
                "friend_del": "💚 آیدی عددی دوست را برای حذف بفرستید.",
                "friend_text": (
                    "💞 متن‌های پاسخ به دوست را بفرستید.\n\n"
                    "برای افزودن گروهی، هر متن را در یک خط جدا بنویسید. "
                    "در هر بار تا ۵۰ متن و برای هر متن حداکثر ۵۰۰ نویسه "
                    "مجاز است."
                ),
                "enemy_add": (
                    "💢 آیدی عددی کاربری را بفرستید که دشمن محسوب شود."
                ),
                "enemy_del": "💢 آیدی عددی دشمن را برای حذف بفرستید.",
                "enemy_text": (
                    "💢 متن‌های پاسخ به دشمن را بفرستید.\n\n"
                    "برای افزودن گروهی، هر متن را در یک خط جدا بنویسید. "
                    "در هر بار تا ۵۰ متن و برای هر متن حداکثر ۵۰۰ نویسه "
                    "مجاز است."
                ),
                "profile_add": (
                    "👁 آیدی عددی کاربر را برای پایش تغییر نام، یوزرنیم، "
                    "بیو و عکس بفرستید."
                ),
                "profile_del": (
                    "👁 آیدی عددی کاربر را برای توقف پایش بفرستید."
                ),
                "private_warning": (
                    "🔐 متن هشدار قفل پیوی را بفرستید.\n\n"
                    "این متن پیش از بلاک برای کاربر ناشناس ارسال می‌شود."
                ),
                "private_allow_add": (
                    "✅ آیدی عددی کاربری را بفرستید که اجازه پیام خصوصی دارد."
                ),
                "private_allow_del": (
                    "➖ آیدی عددی کاربر مجاز را برای حذف بفرستید."
                ),
                "welcome_text": (
                    "👋 متن خوش‌آمد را بفرستید.\n\n"
                    "متغیرها: {name}، {id}، {username} و {chat}"
                ),
                "goodbye_text": (
                    "👋 متن خداحافظی را بفرستید.\n\n"
                    "متغیرها: {name}، {id}، {username} و {chat}"
                ),
                "action_duration": (
                    "🎭 مدت پیش‌فرض اکشن نمایشی را به ثانیه بفرستید.\n"
                    "مقدار مجاز: ۱ تا ۳۰۰ ثانیه"
                ),
                "first_comment": (
                    "💬 مرحله ۱ از ۳ — کانال کامنت اول\n\n"
                    "آیدی کانال را بفرستید؛ نمونه: @MyChannel"
                ),
                "first_comment_del": (
                    "🗑 شناسه کانال ثبت‌شده را دقیقاً مانند @MyChannel "
                    "یا آیدی عددی بفرستید."
                ),
                "watermark_text": (
                    "🖼 متن لوگوی روی عکس را بفرستید؛ حداکثر ۱۰۰ نویسه."
                ),
                "signature_text": (
                    "✍️ متن امضای انتهای پیام‌ها را بفرستید؛ "
                    "حداکثر ۳۰۰ نویسه."
                ),
                "reaction_emoji": (
                    "❤️ ایموجی ری‌اکت خودکار را بفرستید.\n"
                    "نمونه: ❤️ یا 👍"
                ),
            }
            context.user_data["panel_input"] = {
                "owner_id": owner_id,
                "input_type": input_type,
                "stage": 0,
                "values": {},
            }
            await update.effective_message.reply_text(prompts[input_type])
            return

        context.user_data.pop("schedule_input", None)
        context.user_data.pop("panel_input", None)
        await update.effective_message.reply_text(
            "🤖 این بات، هلپر پنل سلف است.\n\n"
            "برای نمایش پنل، با حسابی که سلف آن فعال شده عبارت «پنل» "
            "را در چت موردنظر ارسال کنید."
        )

    async def cancel_schedule_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = (
            context.user_data.pop("panel_input", None)
            or context.user_data.pop("schedule_input", None)
        )
        if pending:
            await update.effective_message.reply_text(
                "✅ تنظیم نیمه‌کاره لغو شد."
            )
        else:
            await update.effective_message.reply_text(
                "تنظیم نیمه‌کاره‌ای وجود ندارد."
            )

    async def receive_schedule_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        pending = (
            context.user_data.get("panel_input")
            or context.user_data.get("schedule_input")
        )
        if not pending:
            return

        owner_id = int(pending.get("owner_id") or 0)
        if update.effective_user.id != owner_id:
            context.user_data.pop("schedule_input", None)
            context.user_data.pop("panel_input", None)
            return
        record = self.user_record(owner_id)
        if not record:
            context.user_data.pop("schedule_input", None)
            context.user_data.pop("panel_input", None)
            await update.effective_message.reply_text(
                "❌ سلف این حساب دیگر فعال نیست."
            )
            return

        raw = (
            update.effective_message.text
            or update.effective_message.caption
            or ""
        ).strip()
        input_type = str(pending.get("input_type") or "")
        stage = int(pending.get("stage") or 0)
        values = pending.setdefault("values", {})
        try:
            if input_type in {"text", "schedule_text"}:
                if not raw or len(raw) > 3500:
                    raise ValueError(
                        "متن باید بین ۱ تا ۳۵۰۰ نویسه باشد."
                    )
                key = "scheduled_message_text"
                normalized = raw
                notice = "✅ متن پیام زمان‌بندی‌شده ذخیره شد."
                target_page = "schedule"
            elif input_type in {"target", "schedule_target"}:
                normalized = self.normalize_schedule_target(raw)
                key = "scheduled_message_target"
                notice = f"✅ مقصد روی {normalized} ذخیره شد."
                target_page = "schedule"
            elif input_type in {"interval", "schedule_interval"}:
                translated = raw.translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                if not translated.isdigit():
                    raise ValueError("فاصله ارسال باید یک عدد صحیح باشد.")
                interval = int(translated)
                if not 1 <= interval <= 10080:
                    raise ValueError(
                        "فاصله ارسال باید بین ۱ تا ۱۰۰۸۰ دقیقه باشد."
                    )
                key = "scheduled_message_interval_minutes"
                normalized = str(interval)
                notice = f"✅ فاصله ارسال روی {interval} دقیقه ذخیره شد."
                target_page = "schedule"
            elif input_type == "schedule_create":
                if stage == 0:
                    values["target"] = self.normalize_schedule_target(raw)
                    pending["stage"] = 1
                    await update.effective_message.reply_text(
                        "⏰ مرحله ۲ — محتوای پیام\n\n"
                        "متن، عکس، ویدئو، ویس، استیکر یا فایل را بفرستید."
                    )
                    return
                if stage == 1:
                    payload = await self.extract_panel_payload(
                        update.effective_message,
                        str(record["phone"]),
                        "schedule",
                    )
                    values.update(payload)
                    pending["stage"] = 2
                    await update.effective_message.reply_text(
                        "⏰ مرحله ۳ — نوع زمان‌بندی\n\n"
                        "یکی از عددهای زیر را بفرستید:\n"
                        "1 — یک‌باره\n"
                        "2 — تکرار با فاصله دقیقه‌ای\n"
                        "3 — هر روز در ساعت مشخص\n"
                        "4 — هر هفته در روز و ساعت مشخص"
                    )
                    return
                if stage == 2:
                    kind_map = {
                        "1": "once",
                        "2": "interval",
                        "3": "daily",
                        "4": "weekly",
                    }
                    recurrence = kind_map.get(
                        raw.translate(
                            str.maketrans(
                                "۰۱۲۳۴۵۶۷۸۹",
                                "0123456789",
                            )
                        )
                    )
                    if not recurrence:
                        raise ValueError("نوع زمان‌بندی باید یکی از ۱ تا ۴ باشد.")
                    values["recurrence_type"] = recurrence
                    pending["stage"] = 3
                    prompt = {
                        "once": (
                            "تاریخ و ساعت را بفرستید.\n"
                            "نمونه: 2026-08-01 18:30"
                        ),
                        "interval": (
                            "فاصله تکرار را به دقیقه بفرستید.\n"
                            "مقدار مجاز: ۱ تا ۱۰۰۸۰"
                        ),
                        "daily": "ساعت روزانه را بفرستید؛ نمونه: 18:30",
                        "weekly": (
                            "شماره روز هفته را بفرستید:\n"
                            "۰ دوشنبه، ۱ سه‌شنبه، ۲ چهارشنبه، "
                            "۳ پنجشنبه، ۴ جمعه، ۵ شنبه، ۶ یکشنبه"
                        ),
                    }[recurrence]
                    await update.effective_message.reply_text(prompt)
                    return
                recurrence = str(values.get("recurrence_type") or "")
                now = datetime.now().astimezone()
                if stage == 3 and recurrence == "weekly":
                    translated = raw.translate(
                        str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
                    )
                    if translated not in {str(item) for item in range(7)}:
                        raise ValueError("شماره روز هفته باید از ۰ تا ۶ باشد.")
                    values["weekday"] = int(translated)
                    pending["stage"] = 4
                    await update.effective_message.reply_text(
                        "ساعت ارسال هفتگی را بفرستید؛ نمونه: 18:30"
                    )
                    return
                if stage in {3, 4}:
                    if recurrence == "once":
                        try:
                            parsed = datetime.strptime(
                                raw.translate(
                                    str.maketrans(
                                        "۰۱۲۳۴۵۶۷۸۹",
                                        "0123456789",
                                    )
                                ),
                                "%Y-%m-%d %H:%M",
                            ).replace(tzinfo=now.tzinfo)
                        except ValueError as exc:
                            raise ValueError(
                                "زمان باید مانند 2026-08-01 18:30 باشد."
                            ) from exc
                        if parsed <= now:
                            raise ValueError("زمان اجرا باید در آینده باشد.")
                        recurrence_value = ""
                        next_run = parsed
                    elif recurrence == "interval":
                        translated = raw.translate(
                            str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
                        )
                        if not translated.isdigit():
                            raise ValueError("فاصله باید عدد صحیح باشد.")
                        minutes = int(translated)
                        if not 1 <= minutes <= 10080:
                            raise ValueError(
                                "فاصله باید بین ۱ تا ۱۰۰۸۰ دقیقه باشد."
                            )
                        recurrence_value = str(minutes)
                        next_run = now + timedelta(minutes=minutes)
                    else:
                        hour, minute = self.parse_clock(raw)
                        candidate = now.replace(
                            hour=hour,
                            minute=minute,
                            second=0,
                            microsecond=0,
                        )
                        if recurrence == "daily":
                            if candidate <= now:
                                candidate += timedelta(days=1)
                            recurrence_value = f"{hour:02d}:{minute:02d}"
                        else:
                            weekday = int(values["weekday"])
                            days_ahead = (weekday - now.weekday()) % 7
                            candidate = (
                                now + timedelta(days=days_ahead)
                            ).replace(
                                hour=hour,
                                minute=minute,
                                second=0,
                                microsecond=0,
                            )
                            if candidate <= now:
                                candidate += timedelta(days=7)
                            recurrence_value = json.dumps(
                                {
                                    "weekday": weekday,
                                    "time": f"{hour:02d}:{minute:02d}",
                                },
                                ensure_ascii=False,
                            )
                        next_run = candidate
                    values["recurrence_value"] = recurrence_value
                    values["next_run_at"] = next_run.isoformat(
                        timespec="seconds"
                    )
                    pending["stage"] = 5
                    await update.effective_message.reply_text(
                        "⏰ مرحله آخر — حذف خودکار\n\n"
                        "اگر پیام بعد از مدتی حذف شود، تعداد دقیقه را بفرستید؛ "
                        "برای غیرفعال‌بودن عدد ۰ را بفرستید."
                    )
                    return
                translated = raw.translate(
                    str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
                )
                if not translated.isdigit():
                    raise ValueError("زمان حذف باید عدد صحیح باشد.")
                delete_after = int(translated)
                if not 0 <= delete_after <= 10080:
                    raise ValueError("زمان حذف باید بین ۰ تا ۱۰۰۸۰ دقیقه باشد.")
                job_id = create_schedule_job(
                    self.data_dir,
                    str(record["phone"]),
                    target=str(values["target"]),
                    message_type=str(values["message_type"]),
                    message_text=str(values.get("message_text") or ""),
                    media_path=str(values.get("media_path") or ""),
                    caption=str(values.get("caption") or ""),
                    recurrence_type=str(values["recurrence_type"]),
                    recurrence_value=str(values["recurrence_value"]),
                    next_run_at=str(values["next_run_at"]),
                    timezone_name=str(now.tzinfo or "local"),
                    delete_after_minutes=delete_after,
                )
                key = None
                normalized = None
                notice = f"✅ برنامه حرفه‌ای #{job_id} ثبت شد."
                target_page = "schedule"
            elif input_type == "secretary_qa":
                if stage == 0:
                    triggers = [
                        line.strip()
                        for line in raw.splitlines()
                        if line.strip()
                    ]
                    if len(triggers) == 1 and "/" in triggers[0]:
                        triggers = [
                            item.strip()
                            for item in triggers[0].split("/")
                            if item.strip()
                        ]
                    unique_triggers = []
                    seen = set()
                    for trigger in triggers:
                        marker = trigger.casefold()
                        if marker not in seen:
                            unique_triggers.append(trigger)
                            seen.add(marker)
                    if not unique_triggers:
                        raise ValueError("حداقل یک کلمه یا سؤال بفرستید.")
                    if len(unique_triggers) > 50:
                        raise ValueError(
                            "در هر بار حداکثر ۵۰ عبارت قابل ثبت است."
                        )
                    if any(len(item) > 100 for item in unique_triggers):
                        raise ValueError(
                            "هر عبارت باید حداکثر ۱۰۰ نویسه باشد."
                        )
                    values["triggers"] = unique_triggers
                    values["trigger_count"] = len(unique_triggers)
                    pending["stage"] = 1
                    await update.effective_message.reply_text(
                        "💬 مرحله ۲ — پاسخ اول\n\n"
                        f"برای {len(unique_triggers)} عبارت، یک متن، عکس، "
                        "ویدئو، ویس، استیکر یا فایل بفرستید. بعد از ثبت، "
                        "می‌توانید پاسخ‌های بیشتری اضافه کنید."
                    )
                    return
                payload = await self.extract_panel_payload(
                    update.effective_message,
                    str(record["phone"]),
                    "auto_reply",
                )
                rule_id = int(values.get("rule_id") or 0)
                if not rule_id:
                    rule_id = create_auto_reply_rule(
                        self.data_dir,
                        str(record["phone"]),
                        values.get("triggers") or [],
                        scope="private",
                        match_mode="contains",
                        cooldown_seconds=30,
                    )
                    values["rule_id"] = rule_id
                add_auto_reply_response(
                    self.data_dir,
                    str(record["phone"]),
                    rule_id,
                    response_type=str(payload["message_type"]),
                    content_text=str(payload.get("message_text") or ""),
                    media_path=str(payload.get("media_path") or ""),
                    caption=str(payload.get("caption") or ""),
                )
                set_self_setting(
                    self.data_dir,
                    str(record["phone"]),
                    "auto_reply",
                    "on",
                )
                pending["stage"] = 2
                values["response_count"] = (
                    int(values.get("response_count") or 0) + 1
                )
                await update.effective_message.reply_text(
                    f"✅ پاسخ شماره {values['response_count']} ذخیره شد.\n\n"
                    "پاسخ بعدی را بفرستید یا روی «پایان» بزنید.",
                    reply_markup=InlineKeyboardMarkup(
                        [
                            [
                                glass_button(
                                    "✅ پایان و فعال‌سازی",
                                    owner_id,
                                    "autoreply.done",
                                    style="success",
                                )
                            ],
                            [
                                glass_button(
                                    "❌ لغو ادامه افزودن",
                                    owner_id,
                                    "autoreply.cancel",
                                    style="danger",
                                )
                            ],
                        ]
                    ),
                )
                return
            elif input_type == "secretary_fallback":
                if not 1 <= len(raw) <= 3500:
                    raise ValueError(
                        "متن منشی باید بین ۱ تا ۳۵۰۰ نویسه باشد."
                    )
                key = "secretary_fallback_text"
                normalized = raw
                notice = "✅ متن پاسخ عمومی منشی ذخیره شد."
                target_page = "secretary"
            elif input_type == "offline_text":
                if not 1 <= len(raw) <= 3500:
                    raise ValueError(
                        "متن آفلاین باید بین ۱ تا ۳۵۰۰ نویسه باشد."
                    )
                key = "offline_reply_text"
                normalized = raw
                notice = "✅ متن پاسخ حالت آفلاین ذخیره شد."
                target_page = "secretary"
            elif input_type == "offline_cooldown":
                translated = raw.translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                if not translated.isdigit():
                    raise ValueError("فاصله تکرار باید یک عدد صحیح باشد.")
                interval = int(translated)
                if not 1 <= interval <= 10080:
                    raise ValueError(
                        "فاصله تکرار باید بین ۱ تا ۱۰۰۸۰ دقیقه باشد."
                    )
                key = "offline_reply_cooldown_minutes"
                normalized = str(interval)
                notice = (
                    f"✅ فاصله تکرار پاسخ آفلاین روی {interval} دقیقه "
                    "ذخیره شد."
                )
                target_page = "secretary"
            elif input_type in {"online_emoji", "offline_emoji"}:
                if not 1 <= len(raw) <= 16 or any(
                    character.isspace() for character in raw
                ):
                    raise ValueError("یک ایموجی کوتاه و بدون فاصله بفرستید.")
                key = (
                    "online_name_emoji"
                    if input_type == "online_emoji"
                    else "offline_name_emoji"
                )
                normalized = raw
                notice = (
                    "✅ ایموجی آنلاین ذخیره شد."
                    if input_type == "online_emoji"
                    else "✅ ایموجی آفلاین ذخیره شد."
                )
                target_page = "profile_management"
            elif input_type in {
                "private_allow_add",
                "private_allow_del",
            }:
                translated = raw.translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                if not translated.isdigit() or int(translated) <= 0:
                    raise ValueError("آیدی کاربر باید یک عدد مثبت باشد.")
                target_id = int(translated)
                set_private_allowlist_user(
                    self.data_dir,
                    str(record["phone"]),
                    target_id,
                    allowed=input_type == "private_allow_add",
                )
                key = None
                normalized = None
                notice = (
                    "✅ کاربر به فهرست مجاز افزوده شد."
                    if input_type == "private_allow_add"
                    else "✅ کاربر از فهرست مجاز حذف شد."
                )
                target_page = "security"
            elif input_type in {"welcome_text", "goodbye_text"}:
                if not 1 <= len(raw) <= 1000:
                    raise ValueError(
                        "متن باید بین ۱ تا ۱۰۰۰ نویسه باشد."
                    )
                try:
                    raw.format(
                        name="نام",
                        id=1,
                        username="@user",
                        chat="گروه",
                    )
                except (KeyError, ValueError) as exc:
                    raise ValueError(
                        "متغیر متن معتبر نیست؛ فقط {name}، {id}، "
                        "{username} و {chat} مجازند."
                    ) from exc
                key = (
                    "welcome_text"
                    if input_type == "welcome_text"
                    else "goodbye_text"
                )
                normalized = raw
                notice = (
                    "✅ متن خوش‌آمد ذخیره شد."
                    if input_type == "welcome_text"
                    else "✅ متن خداحافظی ذخیره شد."
                )
                target_page = "groups_new"
            elif input_type == "action_duration":
                translated = raw.translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                if not translated.isdigit() or not 1 <= int(translated) <= 300:
                    raise ValueError(
                        "مدت اکشن باید عددی بین ۱ تا ۳۰۰ ثانیه باشد."
                    )
                key = "action_default_duration"
                normalized = translated
                notice = "✅ مدت پیش‌فرض اکشن ذخیره شد."
                target_page = "messages"
            elif input_type == "filter_add":
                if stage == 0:
                    phrases = [
                        line.strip().lower()
                        for line in raw.splitlines()
                        if line.strip()
                    ]
                    phrases = list(dict.fromkeys(phrases))
                    if not phrases:
                        raise ValueError("حداقل یک عبارت فیلتر بفرستید.")
                    if len(phrases) > 50:
                        raise ValueError(
                            "در هر بار حداکثر ۵۰ فیلتر قابل ثبت است."
                        )
                    if any(len(item) > 200 for item in phrases):
                        raise ValueError(
                            "هر عبارت فیلتر باید حداکثر ۲۰۰ نویسه باشد."
                        )
                    values["phrases"] = phrases
                    pending["stage"] = 1
                    await update.effective_message.reply_text(
                        "🧹 مرحله ۲ از ۲ — عملیات فیلتر\n\n"
                        "یکی از این موارد را بفرستید:\n"
                        "delete یا حذف\nwarn یا اخطار\nmute یا سکوت\n"
                        "block یا بلاک"
                    )
                    return
                action = raw
                action = {
                    "حذف": "delete",
                    "اخطار": "warn",
                    "سکوت": "mute",
                    "بلاک": "block",
                }.get(action.lower(), action.lower())
                filter_ids = [
                    add_word_filter(
                        self.data_dir,
                        str(record["phone"]),
                        phrase,
                        action,
                    )
                    for phrase in values.get("phrases", [])
                ]
                key = None
                normalized = None
                notice = f"✅ {len(filter_ids)} فیلتر ذخیره شد."
                target_page = "moderation"
            elif input_type == "friend_text":
                inserted, skipped = add_friend_affection_replies(
                    self.data_dir,
                    str(record["phone"]),
                    raw.splitlines(),
                )
                key = None
                normalized = None
                notice = (
                    f"✅ {inserted} متن دوست ذخیره شد."
                    + (
                        f" {skipped} متن تکراری نادیده گرفته شد."
                        if skipped
                        else ""
                    )
                )
                target_page = "friend_replies"
            elif input_type == "enemy_text":
                inserted, skipped = add_enemy_hostile_replies(
                    self.data_dir,
                    str(record["phone"]),
                    raw.splitlines(),
                )
                key = None
                normalized = None
                notice = (
                    f"✅ {inserted} متن دشمن ذخیره شد."
                    + (
                        f" {skipped} متن تکراری نادیده گرفته شد."
                        if skipped
                        else ""
                    )
                )
                target_page = "enemy_replies"
            elif input_type in {
                "friend_add",
                "friend_del",
                "enemy_add",
                "enemy_del",
                "profile_add",
                "profile_del",
            }:
                translated = raw.translate(
                    str.maketrans(
                        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                        "01234567890123456789",
                    )
                )
                raw_ids = [
                    item.strip()
                    for item in re.split(r"[\s,،]+", translated)
                    if item.strip()
                ]
                if (
                    not raw_ids
                    or any(not item.isdigit() or int(item) <= 0 for item in raw_ids)
                ):
                    raise ValueError(
                        "آیدی‌ها باید عدد مثبت و هرکدام در یک خط باشند."
                    )
                if len(raw_ids) > 50:
                    raise ValueError(
                        "در هر بار حداکثر ۵۰ آیدی قابل ثبت است."
                    )
                target_ids = list(dict.fromkeys(map(int, raw_ids)))
                phone = str(record["phone"])
                if input_type == "friend_add":
                    for target_id in target_ids:
                        set_friend(
                            self.data_dir,
                            phone,
                            target_id,
                            enabled=True,
                        )
                    notice = f"✅ {len(target_ids)} کاربر به دوستان افزوده شد."
                    target_page = "relationships"
                elif input_type == "friend_del":
                    for target_id in target_ids:
                        set_friend(
                            self.data_dir,
                            phone,
                            target_id,
                            enabled=False,
                        )
                    notice = f"✅ {len(target_ids)} کاربر از دوستان حذف شد."
                    target_page = "relationships"
                elif input_type == "enemy_add":
                    for target_id in target_ids:
                        set_enemy(
                            self.data_dir,
                            phone,
                            target_id,
                            enabled=True,
                        )
                    notice = f"✅ {len(target_ids)} کاربر به دشمنان افزوده شد."
                    target_page = "relationships"
                elif input_type == "enemy_del":
                    for target_id in target_ids:
                        set_enemy(
                            self.data_dir,
                            phone,
                            target_id,
                            enabled=False,
                        )
                    notice = f"✅ {len(target_ids)} کاربر از دشمنان حذف شد."
                    target_page = "relationships"
                elif input_type == "profile_add":
                    if len(target_ids) != 1:
                        raise ValueError(
                            "برای پایش پروفایل در هر بار فقط یک آیدی بفرستید."
                        )
                    upsert_tracked_profile(
                        self.data_dir,
                        phone,
                        target_ids[0],
                    )
                    notice = "✅ کاربر به فهرست پایش افزوده شد."
                    target_page = "profiles"
                else:
                    if len(target_ids) != 1:
                        raise ValueError(
                            "برای حذف پایش در هر بار فقط یک آیدی بفرستید."
                        )
                    delete_tracked_profile(
                        self.data_dir,
                        phone,
                        target_ids[0],
                    )
                    notice = "✅ کاربر از فهرست پایش حذف شد."
                    target_page = "profiles"
                key = None
                normalized = None
            elif input_type == "first_comment":
                if stage == 0:
                    if not raw or len(raw) > 200:
                        raise ValueError("آیدی کانال معتبر نیست.")
                    values["chat_id"] = raw
                    pending["stage"] = 1
                    await update.effective_message.reply_text(
                        "💬 مرحله ۲ از ۳ — متن کامنت\n\n"
                        "متنی را بفرستید که زیر پست کانال ارسال شود."
                    )
                    return
                if stage == 1:
                    if not 1 <= len(raw) <= 1000:
                        raise ValueError(
                            "متن کامنت باید بین ۱ تا ۱۰۰۰ نویسه باشد."
                        )
                    values["comment_text"] = raw
                    pending["stage"] = 2
                    await update.effective_message.reply_text(
                        "💬 مرحله ۳ از ۳ — تأخیر\n\n"
                        "تأخیر ارسال را به ثانیه و فقط عددی بفرستید؛ "
                        "نمونه: 2"
                    )
                    return
                translated = raw.translate(
                        str.maketrans(
                            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                            "01234567890123456789",
                        )
                    )
                if not translated.isdigit():
                    raise ValueError("تأخیر باید عدد صحیح باشد.")
                delay = int(translated)
                upsert_first_comment_channel(
                    self.data_dir,
                    str(record["phone"]),
                    str(values.get("chat_id") or ""),
                    str(values.get("comment_text") or ""),
                    delay_seconds=delay,
                )
                key = None
                normalized = None
                notice = "✅ کانال کامنت اول ذخیره شد."
                target_page = "automation"
            elif input_type == "first_comment_del":
                delete_first_comment_channel(
                    self.data_dir,
                    str(record["phone"]),
                    raw,
                )
                key = None
                normalized = None
                notice = "✅ کانال از فهرست کامنت اول حذف شد."
                target_page = "automation"
            elif input_type == "watermark_text":
                if not 1 <= len(raw) <= 100:
                    raise ValueError(
                        "متن لوگو باید بین ۱ تا ۱۰۰ نویسه باشد."
                    )
                key = "watermark_text"
                normalized = raw
                notice = "✅ متن لوگوی تصویر ذخیره شد."
                target_page = "appearance"
            elif input_type == "signature_text":
                if not 1 <= len(raw) <= 300:
                    raise ValueError(
                        "متن امضا باید بین ۱ تا ۳۰۰ نویسه باشد."
                    )
                key = "outgoing_signature_text"
                normalized = raw
                notice = "✅ متن امضای خودکار ذخیره شد."
                target_page = "signature"
            elif input_type == "reaction_emoji":
                if not 1 <= len(raw) <= 16 or any(
                    character.isspace() for character in raw
                ):
                    raise ValueError("یک ایموجی معتبر بفرستید.")
                key = "auto_reaction_emoji"
                normalized = raw
                notice = "✅ ایموجی ری‌اکت خودکار ذخیره شد."
                target_page = "automation"
            else:
                raise ValueError("نوع تنظیم ناشناخته است.")
        except ValueError as exc:
            await update.effective_message.reply_text(
                f"❌ {exc}\n\nمقدار درست را دوباره بفرستید."
            )
            return

        if key is not None:
            set_self_setting(
                self.data_dir,
                str(record["phone"]),
                key,
                normalized,
            )
        context.user_data.pop("schedule_input", None)
        context.user_data.pop("panel_input", None)
        text, keyboard = self.build_page(
            owner_id,
            record,
            target_page,
            notice,
        )
        await update.effective_message.reply_text(
            text,
            reply_markup=keyboard,
        )

    @staticmethod
    def normalize_schedule_target(value: str) -> str:
        raw = (value or "").strip()
        raw = re.sub(
            r"^https?://(?:www\.)?t\.me/",
            "",
            raw,
            flags=re.IGNORECASE,
        ).strip("/")
        normalized_digits = raw.translate(
            str.maketrans(
                "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
                "01234567890123456789",
            )
        )
        if re.fullmatch(r"-?\d{5,20}", normalized_digits):
            return normalized_digits
        username = raw.lstrip("@")
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
            raise ValueError(
                "مقصد باید @username یا آیدی عددی معتبر گروه باشد."
            )
        return f"@{username}"

    @staticmethod
    def parse_clock(value: str) -> tuple[int, int]:
        normalized = str(value or "").strip().translate(
            str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789")
        )
        match = re.fullmatch(r"(\d{1,2}):(\d{2})", normalized)
        if not match:
            raise ValueError("ساعت باید مانند 18:30 باشد.")
        hour = int(match.group(1))
        minute = int(match.group(2))
        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("ساعت واردشده معتبر نیست.")
        return hour, minute

    async def extract_panel_payload(
        self,
        message,
        phone: str,
        category: str,
    ) -> dict[str, str]:
        """Persist helper-uploaded media and return a Telethon-ready payload."""
        text = str(message.text or "").strip()
        caption = str(message.caption or "").strip()
        if text:
            if len(text) > 3500:
                raise ValueError("متن حداکثر ۳۵۰۰ نویسه است.")
            return {
                "message_type": "text",
                "message_text": text,
                "media_path": "",
                "caption": "",
            }

        attachment = None
        message_type = ""
        extension = ".bin"
        file_size = 0
        if message.photo:
            attachment = message.photo[-1]
            message_type = "photo"
            extension = ".jpg"
            file_size = int(getattr(attachment, "file_size", 0) or 0)
        elif message.video:
            attachment = message.video
            message_type = "video"
            extension = ".mp4"
            file_size = int(message.video.file_size or 0)
        elif message.voice:
            attachment = message.voice
            message_type = "voice"
            extension = ".ogg"
            file_size = int(message.voice.file_size or 0)
        elif message.sticker:
            attachment = message.sticker
            message_type = "sticker"
            extension = (
                ".webm"
                if getattr(message.sticker, "is_video", False)
                else ".tgs"
                if getattr(message.sticker, "is_animated", False)
                else ".webp"
            )
            file_size = int(message.sticker.file_size or 0)
        elif message.animation:
            attachment = message.animation
            message_type = "animation"
            extension = ".mp4"
            file_size = int(message.animation.file_size or 0)
        elif message.document:
            attachment = message.document
            message_type = "document"
            suffix = Path(message.document.file_name or "").suffix
            extension = suffix[:12] if suffix else ".bin"
            file_size = int(message.document.file_size or 0)
        else:
            raise ValueError(
                "متن، عکس، ویدئو، ویس، استیکر یا فایل بفرستید."
            )
        if file_size > 50 * 1024 * 1024:
            raise ValueError("حجم رسانه حداکثر ۵۰ مگابایت است.")
        # Keep only Telegram's cloud file id.  The self-bot downloads it
        # into memory at send time; no customer media is written to disk.
        file_id = str(getattr(attachment, "file_id", "") or "").strip()
        if not file_id:
            raise ValueError("شناسه ابری فایل از تلگرام دریافت نشد.")
        return {
            "message_type": message_type,
            "message_text": "",
            "media_path": f"botfile:{file_id}",
            "caption": caption[:1000],
        }

    def user_record(self, user_id: int):
        record = get_active_user(self.users_db, user_id)
        if not record or not int(record.get("is_active") or 0):
            return None
        return record

    @staticmethod
    async def panel_profile_photo_id(bot, user_id: int) -> str:
        """Return the newest Bot API profile-photo file id when accessible."""
        try:
            photos = await bot.get_user_profile_photos(user_id, limit=1)
            if not photos.photos:
                return ""
            sizes = photos.photos[0]
            if not sizes:
                return ""
            return sizes[-1].file_id
        except Exception:
            return ""

    @staticmethod
    def process_is_running(
        pid,
        expected_script: str = "self_bot.py",
    ) -> bool:
        if not pid:
            return False
        try:
            process = psutil.Process(int(pid))
            command = " ".join(process.cmdline())
            return (
                process.is_running()
                and process.status() != psutil.STATUS_ZOMBIE
                and expected_script in command
            )
        except (psutil.Error, OSError, TypeError, ValueError):
            return False

    async def inline_panel(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        inline_query = update.inline_query
        requested = (inline_query.query or "").strip().lower()
        if requested not in {"panel", "پنل", "menu", "منو"}:
            await inline_query.answer([], cache_time=0, is_personal=True)
            return

        owner_id = inline_query.from_user.id
        record = self.user_record(owner_id)
        if not record:
            result = InlineQueryResultArticle(
                id=f"inactive-{owner_id}",
                title="سلف فعالی برای این حساب پیدا نشد",
                description="ابتدا سلف را از ربات اصلی فعال کنید.",
                input_message_content=InputTextMessageContent(
                    "❌ سلف فعالی برای این حساب ثبت نشده است."
                ),
            )
            await inline_query.answer(
                [result],
                cache_time=0,
                is_personal=True,
            )
            return

        record = dict(record)
        record["panel_first_name"] = inline_query.from_user.first_name or ""
        record["panel_last_name"] = inline_query.from_user.last_name or ""
        record["panel_username"] = inline_query.from_user.username or ""
        text, keyboard = self.build_page(owner_id, record, "home")
        profile_photo_id = await self.panel_profile_photo_id(
            context.bot,
            owner_id,
        )
        if profile_photo_id:
            result = InlineQueryResultCachedPhoto(
                id=f"panel-photo-{owner_id}-{secrets.token_hex(4)}",
                photo_file_id=profile_photo_id,
                title="🎛 نمایش پنل سلف",
                description="پنل دکمه‌ای مدیریت حساب",
                caption=render_panel_html(fit_photo_caption(text)),
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        else:
            result = InlineQueryResultArticle(
                id=f"panel-{owner_id}-{secrets.token_hex(4)}",
                title="🎛 نمایش پنل سلف",
                description="پنل دکمه‌ای مدیریت حساب",
                input_message_content=InputTextMessageContent(
                    render_panel_html(text),
                    parse_mode="HTML",
                ),
                reply_markup=keyboard,
            )
        await inline_query.answer(
            [result],
            cache_time=0,
            is_personal=True,
        )

    async def panel_callback(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        query = update.callback_query
        try:
            _, owner_text, action = query.data.split(":", 2)
            owner_id = int(owner_text)
        except (AttributeError, TypeError, ValueError):
            await query.answer("داده دکمه معتبر نیست.", show_alert=True)
            return

        if query.from_user.id != owner_id:
            await query.answer(
                "این پنل فقط برای صاحب سلف قابل استفاده است.",
                show_alert=True,
            )
            return

        record = self.user_record(owner_id)
        if not record:
            await query.answer(
                "سلف این حساب فعال نیست.",
                show_alert=True,
            )
            return

        phone = str(record["phone"])
        notice = None
        page = action

        if action == "panel_ping":
            # A lightweight health/response check against the running Self process.
            # This is intentionally local: it measures whether the Self process is
            # alive and responsive to an OS-level health probe, not Render HTTP latency.
            pid = record.get("self_pid")
            started = time.perf_counter()
            alive = False
            if pid:
                try:
                    os.kill(int(pid), 0)
                    alive = True
                except (OSError, ValueError, TypeError):
                    alive = False
            elapsed_ms = (time.perf_counter() - started) * 1000
            if alive:
                notice = f"🏓 پینگ سلف\n\n⚡ {elapsed_ms:.2f} ms\n🟢 پردازش سلف فعال است."
            else:
                notice = "🏓 پینگ سلف\n\n🔴 پردازش سلف پاسخ‌گو نیست."
            page = "panel_page3"

        elif action.startswith("toggle."):
            setting = action.removeprefix("toggle.")
            if setting not in TOGGLE_LABELS:
                await query.answer("تنظیم ناشناخته است.", show_alert=True)
                return
            settings = get_self_settings(self.data_dir, phone)
            new_value = "off" if settings.get(setting) == "on" else "on"
            if (
                setting == "scheduled_message_enabled"
                and new_value == "on"
                and (
                    not settings.get("scheduled_message_target", "").strip()
                    or not settings.get("scheduled_message_text", "").strip()
                )
            ):
                await query.answer(
                    "ابتدا متن پیام و گروه مقصد را تنظیم کنید.",
                    show_alert=True,
                )
                return
            set_self_setting(self.data_dir, phone, setting, new_value)
            state_text = "فعال" if new_value == "on" else "غیرفعال"
            notice = f"✅ {TOGGLE_LABELS[setting]} {state_text} شد."
            if (
                setting in {
                    "presence_emoji_enabled",
                    "presence_auto_detect",
                }
                and new_value == "on"
                and (
                    setting == "presence_auto_detect"
                    or settings.get("presence_auto_detect", "on") == "on"
                )
                and settings.get("online_status") == "on"
            ):
                set_self_setting(
                    self.data_dir,
                    phone,
                    "online_status",
                    "off",
                )
                notice += (
                    "\n✅ «همیشه آنلاین» خاموش شد تا وضعیت واقعی "
                    "آنلاین/آفلاین قابل تشخیص باشد."
                )
            page = self.page_for_setting(setting)

        elif action == "duration":
            settings = get_self_settings(self.data_dir, phone)
            durations = ["5", "10", "20", "30", "60"]
            current = settings.get("typing_duration", "5")
            try:
                next_value = durations[(durations.index(current) + 1) % len(durations)]
            except ValueError:
                next_value = durations[0]
            set_self_setting(
                self.data_dir,
                phone,
                "typing_duration",
                next_value,
            )
            notice = f"✅ مدت تایپینگ روی {next_value} ثانیه قرار گرفت."
            page = "secretary"

        elif action == "font":
            settings = get_self_settings(self.data_dir, phone)
            try:
                current_font = int(settings.get("font", "1"))
            except ValueError:
                current_font = 1
            next_font = 1 if current_font >= 10 else current_font + 1
            set_self_setting(self.data_dir, phone, "font", next_font)
            set_self_setting(
                self.data_dir,
                phone,
                "timename_font",
                next_font,
            )
            set_self_setting(
                self.data_dir,
                phone,
                "timebio_font",
                next_font,
            )
            notice = f"✅ فونت ساعت روی مدل {next_font} قرار گرفت."
            page = "appearance"

        elif action in {"namefont", "biofont"}:
            settings = get_self_settings(self.data_dir, phone)
            key = "timename_font" if action == "namefont" else "timebio_font"
            try:
                current_font = int(
                    settings.get(key, settings.get("font", "1"))
                )
            except ValueError:
                current_font = 1
            next_font = 1 if current_font >= 10 else current_font + 1
            set_self_setting(
                self.data_dir,
                phone,
                key,
                next_font,
            )
            notice = (
                f"✅ فونت ساعت "
                f"{'نام' if action == 'namefont' else 'بیو'} "
                f"روی مدل {next_font} قرار گرفت."
            )
            page = "profile_management"

        elif action == "language":
            settings = get_self_settings(self.data_dir, phone)
            next_language = (
                "en"
                if settings.get("panel_language", "fa") == "fa"
                else "fa"
            )
            set_self_setting(
                self.data_dir,
                phone,
                "panel_language",
                next_language,
            )
            notice = (
                "✅ Panel language changed to English."
                if next_language == "en"
                else "✅ زبان پنل فارسی شد."
            )
            page = "home"

        elif action == "textstyle":
            settings = get_self_settings(self.data_dir, phone)
            styles = [
                "none",
                "bold",
                "italic",
                "code",
                "strike",
                "underline",
                "spoiler",
            ]
            current = settings.get("outgoing_text_style", "none")
            try:
                next_value = styles[
                    (styles.index(current) + 1) % len(styles)
                ]
            except ValueError:
                next_value = styles[0]
            set_self_setting(
                self.data_dir,
                phone,
                "outgoing_text_style",
                next_value,
            )
            notice = f"✅ حالت متن روی {next_value} قرار گرفت."
            page = "appearance"

        elif action == "filteraction":
            settings = get_self_settings(self.data_dir, phone)
            actions = ["delete", "warn", "mute", "block"]
            current = settings.get("word_filter_action", "delete")
            try:
                next_value = actions[
                    (actions.index(current) + 1) % len(actions)
                ]
            except ValueError:
                next_value = actions[0]
            set_self_setting(
                self.data_dir,
                phone,
                "word_filter_action",
                next_value,
            )
            notice = f"✅ عملیات پیش‌فرض فیلتر روی {next_value} قرار گرفت."
            page = "moderation"

        elif action == "profileinterval":
            settings = get_self_settings(self.data_dir, phone)
            intervals = ["5", "10", "30", "60", "180", "360"]
            current = settings.get(
                "profile_monitor_interval_minutes",
                "10",
            )
            try:
                next_value = intervals[
                    (intervals.index(current) + 1) % len(intervals)
                ]
            except ValueError:
                next_value = intervals[0]
            set_self_setting(
                self.data_dir,
                phone,
                "profile_monitor_interval_minutes",
                next_value,
            )
            notice = f"✅ فاصله پایش روی {next_value} دقیقه قرار گرفت."
            page = "profiles"

        elif action == "ttsvoice":
            settings = get_self_settings(self.data_dir, phone)
            next_value = (
                "male"
                if settings.get("tts_voice", "female") == "female"
                else "female"
            )
            set_self_setting(
                self.data_dir,
                phone,
                "tts_voice",
                next_value,
            )
            notice = (
                "✅ صدای پیش‌فرض متن‌به‌ویس روی "
                f"{'مرد' if next_value == 'male' else 'زن'} قرار گرفت."
            )
            page = "tools"

        elif action == "antidelete.max":
            settings = get_self_settings(self.data_dir, phone)
            limits = ["10", "25", "50", "100", "200"]
            current = settings.get("anti_delete_max_mb", "50")
            try:
                next_value = limits[
                    (limits.index(current) + 1) % len(limits)
                ]
            except ValueError:
                next_value = limits[0]
            set_self_setting(
                self.data_dir,
                phone,
                "anti_delete_max_mb",
                next_value,
            )
            notice = f"✅ سقف هر رسانه روی {next_value} مگابایت قرار گرفت."
            page = "general"

        elif action == "antidelete.retention":
            settings = get_self_settings(self.data_dir, phone)
            periods = ["1", "3", "7", "14", "30"]
            current = settings.get("anti_delete_retention_days", "7")
            try:
                next_value = periods[
                    (periods.index(current) + 1) % len(periods)
                ]
            except ValueError:
                next_value = periods[0]
            set_self_setting(
                self.data_dir,
                phone,
                "anti_delete_retention_days",
                next_value,
            )
            notice = f"✅ نگهداری موقت روی {next_value} روز قرار گرفت."
            page = "general"

        elif action == "antidelete.clear":
            page = "antidelete_confirm"

        elif action == "antidelete.clear.confirm":
            deleted = clear_message_archive(self.data_dir, phone)
            notice = f"✅ آرشیو موقت پاک شد؛ {deleted} پیام حذف شد."
            page = "general"

        elif action.startswith("reply.delete."):
            try:
                reply_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "شناسه پاسخ منشی معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_secretary_reply(
                self.data_dir,
                phone,
                reply_id,
            )
            notice = (
                "✅ سؤال و پاسخ منشی حذف شد."
                if deleted
                else "این سؤال و پاسخ قبلاً حذف شده است."
            )
            page = "secretary"

        elif action.startswith("friendreply.delete."):
            try:
                reply_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "شناسه متن دوست معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_friend_affection_reply(
                self.data_dir,
                phone,
                reply_id,
            )
            notice = (
                "✅ متن پاسخ دوست حذف شد."
                if deleted
                else "این متن قبلاً حذف شده است."
            )
            page = "friend_replies"

        elif action.startswith("enemyreply.delete."):
            try:
                reply_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "شناسه متن دشمن معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_enemy_hostile_reply(
                self.data_dir,
                phone,
                reply_id,
            )
            notice = (
                "✅ متن پاسخ دشمن حذف شد."
                if deleted
                else "این متن قبلاً حذف شده است."
            )
            page = "enemy_replies"

        elif action.startswith("filter.delete."):
            try:
                filter_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "شناسه فیلتر معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_word_filter(
                self.data_dir,
                phone,
                filter_id,
            )
            notice = (
                "✅ فیلتر حذف شد."
                if deleted
                else "این فیلتر قبلاً حذف شده است."
            )
            page = "moderation"

        elif action.startswith("profile.delete."):
            try:
                target_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "آیدی کاربر معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_tracked_profile(
                self.data_dir,
                phone,
                target_id,
            )
            notice = (
                "✅ پایش کاربر حذف شد."
                if deleted
                else "این کاربر قبلاً حذف شده است."
            )
            page = "profiles"

        elif action in {"autoreply.done", "autoreply.cancel"}:
            pending = context.user_data.get("panel_input") or {}
            response_count = int(
                (pending.get("values") or {}).get("response_count") or 0
            )
            if (
                pending.get("input_type") == "secretary_qa"
                and response_count > 0
            ):
                context.user_data.pop("panel_input", None)
                notice = (
                    f"✅ قانون چندپاسخی با {response_count} پاسخ فعال شد."
                    if action == "autoreply.done"
                    else "✅ افزودن پاسخ بیشتر متوقف شد؛ پاسخ‌های ذخیره‌شده فعال‌اند."
                )
            else:
                notice = "تنظیم پاسخ نیمه‌کاره‌ای وجود ندارد."
            page = "secretary"

        elif action.startswith("autoreply.delete."):
            try:
                rule_id = int(action.rsplit(".", 1)[1])
            except ValueError:
                await query.answer(
                    "شناسه پاسخ خودکار معتبر نیست.",
                    show_alert=True,
                )
                return
            deleted = delete_auto_reply_rule(
                self.data_dir,
                phone,
                rule_id,
            )
            notice = (
                "✅ قانون پاسخ چندپاسخی حذف شد."
                if deleted
                else "این قانون قبلاً حذف شده است."
            )
            page = "secretary"

        elif action.startswith("schedule."):
            parts = action.split(".")
            if len(parts) != 3:
                await query.answer("دستور برنامه معتبر نیست.", show_alert=True)
                return
            command, job_text = parts[1], parts[2]
            try:
                job_id = int(job_text)
            except ValueError:
                await query.answer("شناسه برنامه معتبر نیست.", show_alert=True)
                return
            status_map = {
                "pause": "paused",
                "resume": "active",
                "cancel": "cancelled",
            }
            if command not in status_map:
                await query.answer("عملیات برنامه معتبر نیست.", show_alert=True)
                return
            changed = set_schedule_job_status(
                self.data_dir,
                phone,
                job_id,
                status_map[command],
            )
            labels = {
                "pause": "متوقف",
                "resume": "فعال",
                "cancel": "لغو",
            }
            notice = (
                f"✅ برنامه #{job_id} {labels[command]} شد."
                if changed
                else "وضعیت این برنامه قابل تغییر نیست."
            )
            page = "schedule"

        if action == "autoreply":
            page = "autoreply"
        elif action == "online":
            page = "online"
        elif action == "textstyle":
            page = "textstyle"
        elif action == "clock":
            page = "clock"
        elif action == "signature":
            page = "signature"
        elif action == "first_comment":
            page = "first_comment"
        elif action == "security":
            page = "security"
        elif action == "antidelete":
            page = "antidelete"
        elif action == "auto_reaction":
            page = "auto_reaction"
        elif action == "actions":
            page = "actions"
        elif action == "currency":
            page = "currency"
        elif action == "tts":
            page = "tts"
        elif action == "calculator":
            page = "calculator"
        elif action == "animation":
            page = "animation"
        elif action == "presence":
            page = "presence"
        elif action == "reaction_setting":
            page = "auto_reaction"
        elif action == "forcejoin_channel" or action == "forcejoin_group":
            await query.answer("تنظیم جوین اجباری فعلاً از پنل مدیریت ربات اصلی انجام می‌شود.", show_alert=True)
            return
        elif action == "security_edit":
            page = "security_edit"

        valid_pages = {
            "home",
            "panel_page2",
            "panel_page3",
            "panel_ping",
            "autoreply", "online", "clock", "signature", "first_comment",
            "antidelete", "auto_reaction", "actions", "currency", "tts",
            "calculator", "animation", "presence", "security_edit",
            "general",
            "secretary",
            "appearance",
            "schedule",
            "moderation",
            "automation",
            "relationships",
            "friend_replies",
            "enemy_replies",
            "profiles",
            "tools",
            "status",
            "security",
            "messages",
            "profile_management",
            "groups_new",
            "stats_new",
            "extras",
            "legacy",
            "guide",
            "guide_people",
            "guide_groups",
            "guide_tools",
            "guide_automation",
            "option_guide",
            "option_security",
            "option_presence",
            "option_messages",
            "option_groups",
            "option_storage",
            "antidelete_confirm",
        }
        if page not in valid_pages:
            await query.answer("صفحه پنل معتبر نیست.", show_alert=True)
            return

        text, keyboard = self.build_page(owner_id, record, page, notice)
        await query.answer()
        rendered_text = render_panel_html(text)
        try:
            await query.edit_message_text(
                text=rendered_text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except BadRequest as exc:
            error_text = str(exc).lower()
            if "message is not modified" in error_text:
                return
            try:
                await query.edit_message_caption(
                    caption=render_panel_html(fit_photo_caption(text)),
                    parse_mode="HTML",
                    reply_markup=keyboard,
                )
            except BadRequest as caption_exc:
                if "message is not modified" not in str(caption_exc).lower():
                    raise

    @staticmethod
    def page_for_setting(setting: str) -> str:
        if setting == "online_status":
            return "online"
        if setting in {
            "save_timed_photos",
            "anti_delete_enabled",
            "anti_delete_private",
            "anti_delete_groups",
            "anti_delete_channels",
            "force_join_private",
            "auto_read_private",
            "auto_read_groups",
        }:
            return "antidelete"
        if setting == "force_join_private" or setting.startswith("private_lock_") or setting.startswith("anti_edit_"):
            return "security"
        if setting in {"timename", "timebio", "analog_clock_enabled"}:
            return "clock"
        if setting == "outgoing_signature_enabled":
            return "signature"
        if setting in {"welcome_enabled", "goodbye_enabled"}:
            return "groups_new"
        if setting in {"presence_emoji_enabled", "presence_auto_detect"}:
            return "presence"
        if setting == "scheduled_message_enabled":
            return "schedule"
        if setting == "auto_reply":
            return "autoreply"
        if setting in {"typing_action", "secretary", "offline_reply_enabled"}:
            return "secretary"
        if setting.startswith("lock_") or setting == "word_filter_enabled":
            return "moderation"
        if setting == "auto_reaction":
            return "auto_reaction"
        if setting in {"relationship_reaction", "first_comment_enabled"}:
            return "first_comment"
        if setting in {
            "friend_affection_reply",
            "enemy_hostile_reply",
        }:
            return "relationships"
        if setting == "profile_monitor_enabled":
            return "profiles"
        return "appearance"


    def build_page(
        self,
        owner_id: int,
        record,
        page: str,
        notice: str | None = None,
    ):
        phone = str(record["phone"])
        settings = get_self_settings(self.data_dir, phone)
        notice_text = f"{notice}\n\n" if notice else ""

        def pb(label, action, style="primary"):
            return glass_button(f"⌬ {label} ⌬", owner_id, action, style=style)

        helper_username = get_helper_config(self.users_db).get("username", "")
        setup_base = f"https://t.me/{helper_username}?start=" if helper_username else ""

        if page == "home":
            text = f"{notice_text}✨ پنل مدیریت سلف\n\nوضعیت سلف: {'🟢 فعال' if self.process_is_running(record.get('self_pid')) else '🔴 متوقف'}"
            keyboard = InlineKeyboardMarkup([
                [pb("منشی", "secretary"), pb("جواب خودکار", "autoreply")],
                [pb("حالت آنلاین", "online"), pb("حالت متن", "textstyle")],
                [pb("کامنت اول", "first_comment"), pb("ساعت", "clock")],
                [pb("قفل پیوی", "security", "danger"), pb("امضا پیام", "signature")],
                [pb("صفحه بعد", "panel_page2")],
            ])
            return text, keyboard

        if page == "panel_page2":
            text = f"{notice_text}🧩 پنل مدیریت سلف — صفحه ۲"
            keyboard = InlineKeyboardMarkup([
                [pb("مدیریت گروه", "groups_new"), pb("ایموجی آنلاین/آفلاین", "presence")],
                [pb("محافظ ویرایش", "security_edit"), pb("دوست/دشمن", "relationships")],
                [pb("محافظ حذف", "antidelete"), pb("ری اکشن خودکار", "auto_reaction")],
                [pb("اکشن ها", "actions"), pb("قیمت ارز", "currency")],
                [pb("صفحه بعد", "panel_page3"), pb("صفحه قبل", "home")],
            ])
            return text, keyboard

        if page == "panel_page3":
            text = f"{notice_text}🧰 پنل ابزارها — صفحه ۳"
            keyboard = InlineKeyboardMarkup([
                [pb("متن به ویس", "tts"), pb("چک کردن پروفایل", "profiles")],
                [pb("ماشین حساب", "calculator"), pb("انیمیشن ها", "animation")],
                [pb("وضعیت", "status"), pb("پینگ", "panel_ping")],
                [pb("صفحه قبل", "panel_page2")],
            ])
            return text, keyboard

        if page == "secretary":
            text = (
                f"{notice_text}⌬ منشی ⌬\n\n"
                f"منشی عمومی: {self.state(settings, 'secretary')}\n"
                f"پاسخ حالت آفلاین: {self.state(settings, 'offline_reply_enabled')}"
            )
            rows = [
                [self.toggle_button(owner_id, "secretary", settings)],
                [self.toggle_button(owner_id, "offline_reply_enabled", settings)],
            ]
            if setup_base:
                rows += [
                    [link_button("⌬ تنظیم پاسخ عمومی ⌬", f"{setup_base}secretaryfallback_{owner_id}")],
                    [link_button("⌬ تنظیم متن آفلاین ⌬", f"{setup_base}offlinetext_{owner_id}")],
                    [link_button("⌬ فاصله تکرار آفلاین ⌬", f"{setup_base}offlinecooldown_{owner_id}")],
                ]
            rows.append([pb("بازگشت", "home")])
            return text, InlineKeyboardMarkup(rows)

        if page == "autoreply":
            rules = list_auto_reply_rules(self.data_dir, phone, limit=20)
            replies = list_secretary_replies(self.data_dir, phone, limit=20)
            text = (
                f"{notice_text}⌬ جواب خودکار ⌬\n\n"
                f"سؤال و جواب های ثبت شده: {self.state(settings, 'auto_reply')}\n"
                f"تعداد پاسخ های ثبت شده: {len(replies)}\n"
                f"تعداد پاسخ های چندگانه: {len(rules)}"
            )
            rows = [
                [self.toggle_button(owner_id, "auto_reply", settings)],
            ]
            if setup_base:
                rows.append([link_button("⌬ افزودن پاسخ چندگانه/رسانه ای ⌬", f"{setup_base}secretaryqa_{owner_id}")])
            for item in rules:
                label = str(item.get("triggers") or "").replace(",", "، ")[:36]
                rows.append([pb(f"حذف چند پاسخی: {label}", f"autoreply.delete.{int(item['id'])}", "danger")])
            rows.append([pb("بازگشت", "home")])
            return text, InlineKeyboardMarkup(rows)

        if page == "online":
            text = f"{notice_text}⌬ حالت آنلاین ⌬\n\nهمیشه آنلاین: {self.state(settings, 'online_status')}"
            return text, InlineKeyboardMarkup([
                [self.toggle_button(owner_id, "online_status", settings)],
                [pb("بازگشت", "home")],
            ])

        if page == "textstyle":
            style = settings.get("outgoing_text_style", "none")
            text = f"{notice_text}⌬ حالت متن ⌬\n\nحالت متن فعلی: {style}"
            return text, InlineKeyboardMarkup([
                [glass_button("⌬ تغییر حالت متن ⌬", owner_id, "textstyle")],
                [pb("بازگشت", "home")],
            ])

        if page == "clock":
            text = (
                f"{notice_text}⌬ ساعت ⌬\n\n"
                f"ساعت در بیو: {self.state(settings, 'timebio')}\n"
                f"ساعت در اسم: {self.state(settings, 'timename')}"
            )
            return text, InlineKeyboardMarkup([
                [self.toggle_button(owner_id, "timebio", settings)],
                [self.toggle_button(owner_id, "timename", settings)],
                [pb("تغییر فونت ساعت", "namefont")],
                [pb("بازگشت", "home")],
            ])

        if page == "signature":
            text = (
                f"{notice_text}⌬ امضای پیام ⌬\n\n"
                f"امضای خودکار: {self.state(settings, 'outgoing_signature_enabled')}\n"
                f"متن امضا: {settings.get('outgoing_signature_text', '')[:100] or 'ثبت نشده'}"
            )
            rows = [
                [self.toggle_button(owner_id, "outgoing_signature_enabled", settings)],
            ]
            if setup_base:
                rows.append([link_button("⌬ تنظیم متن امضا ⌬", f"{setup_base}signaturetext_{owner_id}")])
            rows.append([pb("بازگشت", "home")])
            return text, InlineKeyboardMarkup(rows)

        if page == "first_comment":
            text = f"{notice_text}⌬ کامنت اول ⌬"
            return text, InlineKeyboardMarkup([
                [self.toggle_button(owner_id, "relationship_reaction", settings)],
                [self.toggle_button(owner_id, "auto_reaction", settings)],
                [pb("تنظیم ری اکت", "reaction_setting")],
                [pb("بازگشت", "home")],
            ])

        if page == "security":
            text = (
                f"{notice_text}⌬ قفل پیوی ⌬\n\n"
                f"قفل کامل پیوی: {self.state(settings, 'private_lock_enabled')}\n"
                f"حذف پیام ناشناس: {self.state(settings, 'private_lock_delete_unknown')}\n"
                f"ضد ویرایش پیوی: {self.state(settings, 'anti_edit_private')}\n"
                f"ضد ویرایش گروه: {self.state(settings, 'anti_edit_groups')}\n"
                f"جوین اجباری پیوی: {self.state(settings, 'force_join_private')}"
            )
            rows = [
                [self.toggle_button(owner_id, "private_lock_enabled", settings)],
                [self.toggle_button(owner_id, "private_lock_delete_unknown", settings)],
                [self.toggle_button(owner_id, "force_join_private", settings)],
                [pb("افزودن کانال جوین اجباری", "forcejoin_channel")],
                [pb("افزودن گروه جوین اجباری", "forcejoin_group")],
                [pb("بازگشت", "home")],
            ]
            return text, InlineKeyboardMarkup(rows)

        if page == "security_edit":
            return self._security_edit_page(owner_id, phone, settings, notice_text, pb)

        if page == "antidelete":
            text = (
                f"{notice_text}⌬ محافظ حذف ⌬\n\n"
                f"ضدحذف پیام های عادی: {self.state(settings, 'anti_delete_enabled')}\n"
                f"ضدحذف گروه: {self.state(settings, 'anti_delete_groups')}\n"
                f"ضدحذف پیوی: {self.state(settings, 'anti_delete_private')}\n"
                f"ضدحذف کانال: {self.state(settings, 'anti_delete_channels')}"
            )
            return text, InlineKeyboardMarkup([
                [self.toggle_button(owner_id, "anti_delete_enabled", settings)],
                [self.toggle_button(owner_id, "anti_delete_groups", settings)],
                [self.toggle_button(owner_id, "anti_delete_private", settings)],
                [self.toggle_button(owner_id, "anti_delete_channels", settings)],
                [pb("بازگشت", "panel_page2")],
            ])

        if page == "auto_reaction":
            emoji = settings.get("auto_reaction_emoji", "❤️")
            text = f"{notice_text}⌬ ری اکت خودکار ⌬\n\nری اکت خودکار: {self.state(settings, 'auto_reaction')}\nایموجی: {emoji}"
            rows = [[self.toggle_button(owner_id, "auto_reaction", settings)]]
            if setup_base:
                rows.append([link_button("⌬ تنظیم ری اکت ⌬", f"{setup_base}reaction_{owner_id}")])
            rows.append([pb("بازگشت", "panel_page2")])
            return text, InlineKeyboardMarkup(rows)

        if page == "actions":
            text = f"{notice_text}⌬ اکشن ها ⌬\n\nاکشن های نمایشی: تایپ، بازی، ویدیو و سایر اکشن های پشتیبانی شده."
            return text, InlineKeyboardMarkup([[pb("بازگشت", "panel_page2")]])

        if page == "currency":
            return "قیمت «btc» / ارز «USD» «EUR» — قیمت آنلاین", InlineKeyboardMarkup([[pb("بازگشت", "panel_page2")]])

        if page == "tts":
            return "• ویس زن «متن» / ویس مرد «متن» — متن‌به‌ویس", InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "calculator":
            return "• حساب «2+2*3» — ماشین‌حساب", InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "animation":
            return "• تایپ متن / شمارش 10 — انیمیشن", InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "presence":
            return f"{notice_text}⌬ ایموجی آنلاین/آفلاین ⌬\n\nایموجی آنلاین/آفلاین: {self.state(settings, 'presence_emoji_enabled')}", InlineKeyboardMarkup([
                [self.toggle_button(owner_id, "presence_emoji_enabled", settings)],
                [pb("بازگشت", "panel_page2")],
            ])

        if page == "groups_new":
            return f"{notice_text}⌬ مدیریت گروه ⌬", InlineKeyboardMarkup([[pb("بازگشت", "panel_page2")]])

        if page == "relationships":
            return self._relationships_page(owner_id, phone, settings, notice_text, pb)

        if page == "profiles":
            tracked = list_tracked_profiles(self.data_dir, phone, limit=20)
            text = f"{notice_text}⌬ چک کردن پروفایل ⌬\n\nکاربران تحت پایش: {len(tracked)}"
            return text, InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "status":
            running = self.process_is_running(record.get("self_pid"))
            return f"{notice_text}⌬ وضعیت ⌬\n\nوضعیت سلف: {'🟢 فعال' if running else '🔴 متوقف'}", InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "panel_ping":
            return "⌬ پینگ سلف ⌬\n\nبرای مشاهده مقدار پینگ، دکمه پینگ را بزنید.", InlineKeyboardMarkup([[pb("بازگشت", "panel_page3")]])

        if page == "moderation":
            return f"{notice_text}⌬ مدیریت گروه ⌬", InlineKeyboardMarkup([[pb("بازگشت", "panel_page2")]])

        if page == "schedule":
            return f"{notice_text}⌬ ارسال زمان بندی شده ⌬", InlineKeyboardMarkup([[pb("بازگشت", "home")]])

        if page == "appearance":
            return f"{notice_text}⌬ ظاهر ⌬", InlineKeyboardMarkup([[pb("بازگشت", "home")]])

        return f"{notice_text}صفحه موردنظر پیدا نشد.", InlineKeyboardMarkup([[pb("بازگشت", "home")]])

    def _security_edit_page(self, owner_id, phone, settings, notice_text, pb):
        text = (
            f"{notice_text}⌬ محافظ ویرایش ⌬\n\n"
            f"ضد ویرایش پیوی: {self.state(settings, 'anti_edit_private')}\n"
            f"ضد ویرایش گروه: {self.state(settings, 'anti_edit_groups')}"
        )
        return text, InlineKeyboardMarkup([
            [self.toggle_button(owner_id, "anti_edit_private", settings)],
            [self.toggle_button(owner_id, "anti_edit_groups", settings)],
            [pb("بازگشت", "panel_page2")],
        ])

    def _relationships_page(self, owner_id, phone, settings, notice_text, pb):
        return f"{notice_text}⌬ دوست/دشمن ⌬", InlineKeyboardMarkup([[pb("بازگشت", "panel_page2")]])


    @staticmethod
    def state(settings, key: str) -> str:
        return "✅ فعال" if settings.get(key) == "on" else "❌ غیرفعال"

    @staticmethod
    def state_en(settings, key: str) -> str:
        return "✅ Enabled" if settings.get(key) == "on" else "❌ Disabled"

    def toggle_button_locale(
        self,
        owner_id: int,
        key: str,
        settings,
        english: bool,
    ):
        if not english:
            return self.toggle_button(owner_id, key, settings)
        labels = {
            "private_lock_enabled": "Private lock",
            "private_lock_delete_unknown": "Delete unknown",
            "private_lock_warn_before_block": "Warn before block",
            "anti_edit_private": "Anti-edit PM",
            "anti_edit_groups": "Anti-edit groups",
            "welcome_enabled": "Welcome",
            "goodbye_enabled": "Goodbye",
            "timename": "Clock in name",
            "timebio": "Clock in bio",
            "analog_clock_enabled": "Analog photo clock",
            "presence_emoji_enabled": "Presence emoji",
            "presence_auto_detect": "Auto presence detection",
        }
        enabled = settings.get(key) == "on"
        icon = "✅" if enabled else "❌"
        action = "Disable" if enabled else "Enable"
        style = "danger" if enabled else "success"
        return glass_button(
            f"{icon} {action} {labels.get(key, key)}",
            owner_id,
            f"toggle.{key}",
            style=style,
        )

    def toggle_button(self, owner_id: int, key: str, settings):
        enabled = settings.get(key) == "on"
        icon = "✅" if enabled else "❌"
        style = "danger" if enabled else "success"
        action = "غیرفعال‌کردن" if enabled else "فعال‌کردن"
        return glass_button(
            f"{icon} {action} {TOGGLE_LABELS[key]}",
            owner_id,
            f"toggle.{key}",
            style=style,
        )

    def run(self) -> None:
        self.application.run_polling(allowed_updates=Update.ALL_TYPES)


def parse_arguments():
    parser = argparse.ArgumentParser(description="Inline self-bot helper")
    parser.add_argument(
        "--data-dir",
        default=os.getenv("BOT_DATA_DIR"),
        help="مسیر پوشه داده مشترک",
    )
    parser.add_argument("--status-file", help="مسیر فایل وضعیت هلپر")
    return parser.parse_args()


def main() -> None:
    args = parse_arguments()
    data_dir = Path(args.data_dir) if args.data_dir else Path(__file__).parent / "data"
    users_db = data_dir / "users.db"
    config = get_helper_config(users_db)
    token = config.get("token", "")

    if not config.get("enabled"):
        raise RuntimeError("بات هلپر در تنظیمات مرکزی غیرفعال است.")
    if not token:
        raise RuntimeError("توکن بات هلپر در تنظیمات مرکزی ثبت نشده است.")

    bot = HelperPanelBot(token, data_dir, args.status_file)
    bot.run()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        status_arg = None
        try:
            status_arg = parse_arguments().status_file
        except SystemExit:
            pass
        write_runtime_status(status_arg, "failed", str(exc))
        print(f"❌ خطای بات هلپر: {exc}", file=sys.stderr)
        raise
