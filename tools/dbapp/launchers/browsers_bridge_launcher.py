"""Лаунчер моста браузеров.

Зачем он нужен. Playwright MCP запускается командой `npx @playwright/mcp` с
аргументами, а аргументы зависят от того, что выбрал человек: Chrome, Edge,
Яндекс.Браузер или Firefox; отдельный профиль или свои сессии; с окном или
без. В `opencode.jsonc` уходит только команда этого файла — иначе каждая
смена браузера переписывала бы настройки opencode, а они не наше дело.

Что делает:
    1. Находит папку настроек opencode (та же логика, что у окна).
    2. Читает выбор из `mcp-browsers.json` оттуда.
    3. Собирает команду `npx` с аргументами выбора (одна и та же сборка
       аргументов живёт в `browsers.py` — здесь только запуск).
    4. Запускает сервер и передаёт ему stdio.

Почему npx, а не установка пакета. Так написано в README Playwright MCP:
пакет `@playwright/mcp` запускается через npx и обновляется сам. Ставить
его в программу значило бы держать копию чужого пакета и обновлять её
руками. Кэш npx прогревает сама программа: холодная скачка занимает
секунды, а opencode даёт серверу на старт 30 секунд.

Ничего не пишет в stdout: stdout у MCP-сервера — это протокол, и лишний
текст в нём ломает связь. Весь журнал идёт в stderr.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Своего кода у Playwright MCP в репозитории нет — пакет скачивает npx.
#: Папка всё равно нужна: это рабочая папка запуска и место для пояснения.
SERVER_DIR = HERE.parent.parent / "thirdparty" / "browsers"

#: Соседний модуль: в нём — чтение выбора и сборка аргументов. Один
#: источник правды на программу и лаунчер, чтобы они не разошлись.
sys.path.insert(0, str(HERE.parent))


def say(text: str) -> None:
    """Журнал только в stderr. stdout занят протоколом MCP."""
    print(f"[browsers-bridge] {text}", file=sys.stderr, flush=True)


def main() -> int:
    try:
        import browsers  # noqa: PLC0415 — рядом лежит
    except ImportError as exc:  # pragma: no cover — ломается только у сломанной сборки
        say(f"не нашёл свой модуль browsers.py: {exc}")
        return 1

    settings = browsers.find_settings_dir()
    choice = browsers.read_choice(settings)
    br = browsers.browser(choice.browser)

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        say("npx не найден. Он нужен Playwright MCP: сервер запускается через "
            "npx, а npx ставится вместе с Node.js.")
        return 1

    if br.kind == "path" and not (choice.executable
                                  and Path(choice.executable).is_file()):
        say(f"выбран {br.title}, а путь к нему не указан или неверен: "
            f"«{choice.executable or 'пусто'}».")
        say("Открой программу, вкладка opencode, блок «6. Серверы MCP», "
            "выбери Браузеры и нажми «Выбрать браузер».")
        return 1

    args = [npx, *[str(part) for part in browsers.command(settings, choice)[1:]]]
    argv = browsers._wrap_cmd(args)  # noqa: SLF001 — общая обёртка для .cmd на Windows
    say(f"запускаю мост: {browsers.description(choice)}")
    if choice.profile == "sessions":
        say("режим «мои сессии»: нужен запущенный Chrome или Edge с "
            "расширением Playwright")
    cwd = SERVER_DIR if SERVER_DIR.is_dir() else settings
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd),
            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
        )
    except OSError as exc:
        say(f"мост не запустился: {exc}")
        return 1
    try:
        return proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        return 0


if __name__ == "__main__":
    sys.exit(main())
