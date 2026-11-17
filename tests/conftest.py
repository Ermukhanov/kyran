import os
import sys
from pathlib import Path

# Переменные окружения — до импорта приложения
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ["DEMO_MODE"] = "true"
os.environ["SECRET_KEY"] = "test-secret"
os.environ["TELEGRAM_BOT_TOKEN"] = ""
os.environ["ANTHROPIC_API_KEY"] = ""
os.environ["GOSZAKUP_TOKEN"] = ""
os.environ["ZAKUP_ENABLED"] = "false"  # в тестах не ходим в интернет
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
