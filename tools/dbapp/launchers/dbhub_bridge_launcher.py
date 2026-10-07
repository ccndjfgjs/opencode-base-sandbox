"""Лаунчер моста DBHub.

Зачем он нужен. DBHub читает подключения из файла `mcp-dbhub.toml`, а
пароль каждой базы подставляет из переменной окружения: в файле на месте
пароля стоит `${MCP_DBHUB_PASSWORD_<ИМЯ>}`. В opencode.jsonc пароль
попасть не может — конфиг открыт, переносится между компьютерами и ездит
в репозиторий программы. Поэтому мост запускается не напрямую, а через
этот файл: пароли читаются здесь и передаются дальше окружением процесса.

Что делает:
    1. Находит папку настроек opencode (та же логика, что у окна).
    2. Читает `mcp-dbhub.toml` оттуда.
    3. Читает пароли из файлов `mcp-dbhub-<имя>-password.txt` и кладёт их
       в переменные окружения перед стартом сервера.
    4. Запускает `npx @bytebase/dbhub@latest` и передаёт ему stdio.

Почему npx, а не установка пакета. Так велит README DBHub: пакет
`@bytebase/dbhub` запускается через `npx` и обновляется сам. Ставить его
в программу значило бы держать копию чужого пакета и обновлять её руками.
Кэш npx программа прогревает заранее: холодная скачка занимает секунды
и десятки мегабайт, а opencode даёт серверу на старт 30 секунд.

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
#: Своего кода у DBHub в репозитории нет — сервер скачивает npx. Папка всё
#: равно нужна: это рабочая папка запуска и место для пояснения.
SERVER_DIR = HERE.parent.parent / "thirdparty" / "dbhub"

#: Файл подключений рядом с настройками opencode. Секции `[[sources]]`
#: в нём и есть подключения к базам.
CONFIG_NAME = "mcp-dbhub.toml"

#: Как называются файлы с паролями: mcp-dbhub-<имя источника>-password.txt.
PASSWORD_PREFIX = "mcp-dbhub-"
PASSWORD_SUFFIX = "-password.txt"

#: Сколько ждать прогрев кэша npx. Холодная скачка пакета со всеми
#: драйверами баз занимает около семнадцати секунд на быстрой сети.
WARM_TIMEOUT = 150


def say(text: str) -> None:
    """Журнал только в stderr. stdout занят протоколом MCP."""
    print(f"[dbhub-bridge] {text}", file=sys.stderr, flush=True)


def find_settings_dir() -> Path:
    """Папка настроек opencode.

    Порядок тот же, что и у остальной программы: переменная окружения,
    затем стандартное место пользователя. Ничего не выдумываем — если
    папку найти не удалось, говорим об этом, а не читаем чужую.
    """
    raw = os.environ.get("OPENCODE_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME")
    if raw:
        candidate = Path(raw)
        if candidate.name == ".config":
            candidate = candidate / "opencode"
        if candidate.is_dir():
            return candidate
    return Path.home() / ".config" / "opencode"


def password_env(source_id: str) -> str:
    """Имя переменной окружения для пароля источника.

    Правило одно и то же здесь и в окне: имя источника в верхнем регистре,
    дефис становится подчёркиванием. Так источник `my-db` ждёт пароль в
    `MCP_DBHUB_PASSWORD_MY_DB`, и это видно прямо в файле подключений.
    """
    return "MCP_DBHUB_PASSWORD_" + source_id.upper().replace("-", "_")


def read_passwords(settings: Path) -> dict[str, str]:
    """Читает все пароли DBHub из файлов рядом с настройками.

    Файл на источник, а не один общий: у каждой базы свой пароль, и
    перепутать их нельзя. Формат тот же, что у пароля OBS: первая
    непустая строка, которая не является пояснением.
    """
    found: dict[str, str] = {}
    for path in sorted(settings.glob(f"{PASSWORD_PREFIX}*{PASSWORD_SUFFIX}")):
        name = path.name[len(PASSWORD_PREFIX):-len(PASSWORD_SUFFIX)]
        if not name:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            say(f"пароль {path.name} не прочитан: {exc}")
            continue
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith(("Пароль", "Нужен", "opencode", "База")):
                found[name] = line
                break
    return found


def wrap_cmd(argv: list[str]) -> list[str]:
    """Обёртка для .cmd/.bat на Windows.

    CreateProcess не умеет запускать пакетные файлы напрямую, а npx на
    Windows — это npx.cmd. Тот же приём, что в прогреве кэша реестра.
    """
    if argv and argv[0].lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *argv]
    return argv


def warm_cache(npx: str) -> None:
    """Скачивает пакет заранее, чтобы первый запуск не ждал минуту.

    Запускаем сервер без подключений: DBHub выведет справку и выйдет,
    а пакет останется в кэше npx. Неудача прогрева — не беда: скачается
    при первом настоящем запуске.
    """
    say("прогреваю кэш npx: скачиваю пакет DBHub, это до минуты…")
    argv = wrap_cmd([npx, "-y", "@bytebase/dbhub@latest"])
    try:
        subprocess.run(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=WARM_TIMEOUT, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        say(f"прогрев не удался: {exc}. Пакет скачается при первом запуске")
    else:
        say("пакет скачан, кэш прогрет")


def main() -> int:
    settings = find_settings_dir()
    config = settings / CONFIG_NAME

    npx = shutil.which("npx") or shutil.which("npx.cmd")
    if not npx:
        say("npx не найден. Он нужен DBHub: сервер запускается через npx, "
            "а npx ставится вместе с Node.js.")
        return 1

    if not config.is_file():
        say(f"Подключений нет: не найден файл {config}")
        say("Открой программу, вкладка opencode, блок «6. Серверы MCP», "
            "выбери DBHub и нажми «Добавить подключение».")
        # Заодно прогреваем кэш: ждать скачивания у кнопки лучше, чем
        # при первом запуске opencode, которому на старт дано 30 секунд.
        warm_cache(npx)
        return 1

    env = dict(os.environ)
    secrets = read_passwords(settings)
    for source_id, password in secrets.items():
        env[password_env(source_id)] = password
    if secrets:
        # Имена источников, без паролей: это безопасно и помогает в отладке.
        say("пароли подставлены для источников: " + ", ".join(sorted(secrets)))
    else:
        say("файлов с паролями нет — это не поломка, подключения бывают без пароля")

    argv = wrap_cmd([npx, "-y", "@bytebase/dbhub@latest",
                     "--transport", "stdio", "--config", str(config)])
    say(f"запускаю мост: npx -y @bytebase/dbhub@latest ({len(secrets)} паролей)")
    cwd = SERVER_DIR if SERVER_DIR.is_dir() else settings
    try:
        proc = subprocess.Popen(
            argv, cwd=str(cwd), env=env,
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
