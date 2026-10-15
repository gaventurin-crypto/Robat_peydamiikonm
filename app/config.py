from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

from app.domain import INFO_OPTIONS, LEGACY_INFO_OPTIONS


@dataclass(frozen=True)
class Settings:
    bot_token: str
    admin_telegram_id: int
    database_path: str
    retention_hours: int
    max_upload_bytes: int
    welcome_text: str
    privacy_notice: str
    notice_version: str
    have_question: str
    need_question: str
    description_question: str
    file_help_text: str
    info_options: tuple[str, ...]

    @classmethod
    def from_environment(cls) -> "Settings":
        load_dotenv()
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        if not token:
            raise ValueError("متغیر TELEGRAM_BOT_TOKEN تنظیم نشده است")
        try:
            admin_id = int(os.environ["ADMIN_TELEGRAM_ID"])
        except (KeyError, ValueError) as error:
            raise ValueError("ADMIN_TELEGRAM_ID باید شناسهٔ عددی مدیر باشد") from error
        if admin_id <= 0:
            raise ValueError("ADMIN_TELEGRAM_ID باید عددی مثبت باشد")

        try:
            retention_hours = int(os.getenv("CASE_RETENTION_HOURS", "24"))
            max_upload_mb = int(os.getenv("MAX_UPLOAD_MB", "10"))
        except ValueError as error:
            raise ValueError("مهلت حذف و محدودیت فایل باید عدد صحیح باشند") from error
        if retention_hours < 1:
            raise ValueError("CASE_RETENTION_HOURS باید حداقل ۱ ساعت باشد")
        if not 1 <= max_upload_mb <= 20:
            raise ValueError("MAX_UPLOAD_MB باید بین ۱ تا ۲۰ باشد")

        configured_options = tuple(
            item.strip() for item in os.getenv("INFO_OPTIONS_ORDER", "").split("|") if item.strip()
        )
        if not configured_options:
            options = INFO_OPTIONS
        elif len(configured_options) == len(INFO_OPTIONS) and set(configured_options) == set(INFO_OPTIONS):
            options = configured_options
        elif (
            len(configured_options) == len(LEGACY_INFO_OPTIONS)
            and set(configured_options) == set(LEGACY_INFO_OPTIONS)
        ):
            options = configured_options + tuple(
                option for option in INFO_OPTIONS if option not in configured_options
            )
        else:
            raise ValueError("INFO_OPTIONS_ORDER باید جایگشت دقیق گزینه‌های شناخته‌شده باشد")

        return cls(
            bot_token=token,
            admin_telegram_id=admin_id,
            database_path=os.getenv("DATABASE_PATH", "data/bot.sqlite3"),
            retention_hours=retention_hours,
            max_upload_bytes=max_upload_mb * 1024 * 1024,
            welcome_text=os.getenv(
                "WELCOME_TEXT",
                "پیش‌نویس: به ربات ثبت و پیگیری درخواست خوش آمدید. برای شروع، ثبت درخواست جدید را انتخاب کنید.",
            ),
            privacy_notice=os.getenv(
                "PRIVACY_NOTICE",
                "پیش‌نویس اطلاعیه: فقط اطلاعات ضروری و مجاز را ارسال کنید. رمز عبور، رمز یک‌بارمصرف، PIN، CVV2 یا اطلاعات ورود بانکی را هرگز نفرستید. مبنای قانونی پردازش را پیش از استفاده عمومی بررسی کنید.",
            ),
            notice_version=os.getenv("NOTICE_VERSION", "draft-1"),
            have_question=os.getenv(
                "HAVE_QUESTION",
                "چه اطلاعاتی در اختیار داری؟ موارد مربوط را انتخاب کن. می‌توانی چند گزینه را هم‌زمان انتخاب کنی.",
            ),
            need_question=os.getenv(
                "NEED_QUESTION", "چه اطلاعاتی نیاز داری؟ موارد موردنظر را انتخاب کن."
            ),
            description_question=os.getenv(
                "DESCRIPTION_QUESTION", "هر توضیحی دربارهٔ درخواستت داری، اینجا بنویس."
            ),
            file_help_text=os.getenv(
                "FILE_HELP_TEXT",
                "فقط عکس، ویس و فایل‌های PDF، PNG، JPEG، WebP، متن ساده، OGG یا MP3 تا سقف تعیین‌شده پذیرفته می‌شوند. فایل اجراشدنی یا اطلاعات ورود بانکی نفرست.",
            ),
            info_options=options,
        )

    def prepare_storage(self) -> None:
        if self.database_path != ":memory:":
            Path(self.database_path).parent.mkdir(parents=True, exist_ok=True)