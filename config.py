import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


class Config:
    KLASSNO_LOGIN: str = os.getenv("KLASSNO_LOGIN", "").strip()
    KLASSNO_PASSWORD: str = os.getenv("KLASSNO_PASSWORD", "").strip()
    KLASSNO_THREAD_IDS_RAW: str = os.getenv("KLASSNO_THREAD_IDS", "28755").strip()

    POLL_INTERVAL_SECONDS: int = int(os.getenv("POLL_INTERVAL_SECONDS", "15"))
    FORWARD_OWN_MESSAGES: bool = os.getenv("FORWARD_OWN_MESSAGES", "false").lower() in ("true", "1", "yes")

    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "").strip()

    DB_PATH: Path = BASE_DIR / "bot.db"

    @classmethod
    def get_thread_ids(cls) -> list[int] | str:
        """Возвращает 'all' или список числовых id тредов"""
        if cls.KLASSNO_THREAD_IDS_RAW.lower() == "all":
            return "all"
        ids = []
        for part in cls.KLASSNO_THREAD_IDS_RAW.split(","):
            part = part.strip()
            if part.isdigit():
                ids.append(int(part))
        return ids or [28755]

