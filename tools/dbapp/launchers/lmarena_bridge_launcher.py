"""Лаунчер моста LMArena — он же MCP-сервер моста.

Зачем он нужен. У форка LMArenaBridge нет MCP: он отдаёт
OpenAI-совместимый HTTP API на этой машине. opencode же умеет подключать
серверы только по протоколу MCP. Значит, слой между ними — наш: файл
запускается как обычный stdio-сервер MCP и отвечает нейросети тремя
инструментами поверх HTTP API моста.

Второй повод: сам мост-программа к моменту вызова может быть не поднят.
Лаунчер поднимает его тем же кодом, что кнопка «Запустить» в программе, и
только после этого отвечает инструментам.

Чего лаунчер не делает: не запускает внешнюю программу через Popen с
пробросом stdio (шаблон §4 рассчитан на мост, который сам является чужой
командой) и не трогает чужие процессы. Секретов у него нет вовсе: токен
арены лежит файлом рядом с настройками и в config.json моста, а запросы
к мосту идут без ключа — так написано в README форка.

stdout — это протокол MCP. Весь журнал идёт в stderr.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
DBAPP_DIR = HERE.parent
SERVER_DIR = DBAPP_DIR.parent / "thirdparty" / "lmarena"


def say(text: str) -> None:
    """Всё это в stderr. stdout — это протокол MCP."""
    print(f"[lmarena-bridge] {text}", file=sys.stderr, flush=True)


def main() -> int:
    if not (SERVER_DIR / "src" / "main.py").is_file():
        say(f"Код моста не найден: {SERVER_DIR / 'src' / 'main.py'}")
        say("Открой программу, выдели строку «LMArena» и нажми «Развернуть».")
        return 1
    if str(DBAPP_DIR) not in sys.path:
        sys.path.insert(0, str(DBAPP_DIR))
    try:
        import lmarena
    except ImportError as exc:  # pragma: no cover - сломанная установка
        say(f"Модуль моста не импортируется: {exc}")
        return 1
    say(f"запускаю мост: LMArenaBridge, код {SERVER_DIR}")
    say(f"API моста: {lmarena.base_url()}{lmarena.MODELS_PATH}")
    return lmarena.mcp_main()


if __name__ == "__main__":
    sys.exit(main())
