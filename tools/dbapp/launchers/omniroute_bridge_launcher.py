"""Лаунчер моста OmniRoute.

Зачем он нужен. opencode запускает MCP-сервер моста одной командой, и эта
команда должна быть одинаковой на любой машине — без настоящих путей и без
секретов. Лаунчер сам находит установленный OmniRoute в папке моста, ставит
ему рабочую папку данных рядом с настройками opencode и передаёт stdio.

Что делает:

    1. Проверяет, что пакет развёрнут и что есть Node.js.
    2. Ставит окружение: `DATA_DIR` (папка данных рядом с настройками
       opencode), `PORT`, `OMNIROUTE_SERVER_HOST` — имена переменных из
       `.env` самого OmniRoute.
    3. Запускает `omniroute --mcp` — сервер MCP по stdio, 110 инструментов.
    4. Передаёт ему stdio как есть.

Почему отказ, а не пустой запуск. Если пакета нет, `node` упадёт сам, и
человек увидит в opencode «сервер не запустился» без причины. Лаунчер
вместо этого пишет в stderr, что именно сделать.

Про секреты. Ключи провайдеров лежат в папке данных OmniRoute, и
вписывает их человек в панели — в `opencode.jsonc` они не попадают.
Лаунчер поэтому ничего не читает и не подставляет: паролей и токенов
у него нет.

Ничего не пишет в stdout: stdout у MCP-сервера — это протокол, и лишний
текст в нём ломает связь. Весь журнал идёт в stderr.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Папка моста: там свой package.json и поставленный пакет omniroute.
SERVER_DIR = HERE.parent.parent / "thirdparty" / "omniroute"

#: Точка входа пакета — bin/omniroute из его package.json.
ENTRY = SERVER_DIR / "node_modules" / "omniroute" / "bin" / "omniroute.mjs"

#: Соседний модуль: там же живут имена папки данных и порта. Один
#: источник правды на программу и лаунчер, чтобы они не разошлись.
sys.path.insert(0, str(HERE.parent))


def say(text: str) -> None:
    """Журнал только в stderr. stdout занят протоколом MCP."""
    print(f"[omniroute-bridge] {text}", file=sys.stderr, flush=True)


def main() -> int:
    try:
        import omniroute  # noqa: PLC0415 — рядом лежит
    except ImportError as exc:  # pragma: no cover — ломается только у сломанной сборки
        say(f"не нашёл свой модуль omniroute.py: {exc}")
        return 1

    if not ENTRY.is_file():
        say("OmniRoute не развёрнут: нет папки моста с пакетом.")
        say("Открой программу, вкладка opencode, блок «6. Серверы MCP», "
            "выбери OmniRoute и нажми «Развернуть».")
        return 1

    node = omniroute.node_exe()
    if not node:
        say("Node.js не найден. OmniRoute запускается на Node.js, "
            "он ставится с https://nodejs.org/ (нужна версия из README).")
        return 1

    dest = omniroute.find_settings_dir()
    env = omniroute.config_env(dest)

    say(f"запускаю мост: OmniRoute {omniroute.installed_version() or 'неизвестной версии'}")
    try:
        proc = subprocess.Popen(
            [node, str(ENTRY), "--mcp"], cwd=str(SERVER_DIR), env=env,
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
