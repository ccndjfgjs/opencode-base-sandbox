# -*- coding: utf-8 -*-
"""Мост OmniRoute: развёртывание, запуск, живая проверка и бесплатные провайдеры.

Мост — это сам OmniRoute, шлюз к провайдерам моделей за одним адресом.
Программа ставит его в `tools\\thirdparty\\omniroute\\` (свой `package.json`,
пакет из npm), запускает локальный сервер и подключает его к opencode.
Командной строки и адреса API здесь не выдумано: они взяты из README
`diegosouzapw/OmniRoute` и из `omniroute --help`.

Здесь живут пять вещей:

1. **Развёртывание** `deploy()` — `npm install omniroute@latest` в папку
   моста. В служебные папки пакетов программа не пишет: папка своя, с
   собственным `package.json`, как у моста OBS.

2. **Запуск и остановка** `start()` / `stop()` / `restart()` — `omniroute
   serve --daemon --no-open` и `omniroute stop`. Панель и API отвечают по
   адресу `http://127.0.0.1:20128`, адрес из README.

3. **Живая проверка** `check_connection()` — настоящий запрос к API
   OmniRoute: `GET /api/health` обязан вернуть `{"status":"ok"}`. Порт
   может слушаться кем угодно, поэтому проверяется ответ, а не порт.

4. **Бесплатные провайдеры** `sync_free_providers()` — при каждом
   развёртывании и каждом перезапуске список читается заново из каталога
   OmniRoute, и бесплатные провайдеры без ключа подключаются сами. Если
   список в новой версии изменился, добавятся новые, а уже подключённые
   не тронутся.

5. **Автонастройка** `auto_setup()` — сначала развернуть, запустить и
   получить ответ API, и только потом писать что-то в настройки opencode.

Где лежат данные OmniRoute. `DATA_DIR` — папка `omniroute-data` рядом с
настройками opencode. Внутри база SQLite, журналы и созданный самим
OmniRoute файл `.env` с ключом шифрования хранилища и ключами провайдеров.
Это секреты этой машины, поэтому папка закрыта правилом в `.gitignore`.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Пакет из npm. Версию не прибиваем: README советует ставить последнюю,
#: а какая именно встала — видно в статусе.
PACKAGE = "omniroute"

#: Наша папка моста: свой package.json, пакет ставится сюда.
SERVER_DIR = (Path(__file__).resolve().parent.parent / "thirdparty" / "omniroute")

#: Точка входа пакета: bin/omniroute из его package.json.
ENTRY = SERVER_DIR / "node_modules" / "omniroute" / "bin" / "omniroute.mjs"

#: Лаунчер: через него opencode запускает MCP-сервер моста.
LAUNCHER = Path(__file__).resolve().parent / "launchers" / "omniroute_bridge_launcher.py"

#: Папка данных рядом с настройками opencode. Внутри — секреты, поэтому
#: она закрыта правилом в .gitignore.
DATA_NAME = "omniroute-data"

#: Адрес и порт по умолчанию — из README: панель и API на одном порту.
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 20128

#: Сколько ждать ответа API после запуска. Первый старт создаёт базу и
#: ключ шифрования, дальше сервер поднимается за секунды; запас — на
#: медленную машину.
READY_TIMEOUT = 180

#: Сколько ждать ответа живого запроса. Это уже запущенный сервер, ему
#: хватает секунд.
PROBE_TIMEOUT = 30

#: Сколько ждать установки пакета. Распакованный пакет занимает сотни
#: мегабайт, на медленной сети это минуты.
INSTALL_TIMEOUT = 1800

#: Сколько ждать команду CLI (статус, список провайдеров).
CLI_TIMEOUT = 120

#: Бесплатный провайдер, которому ключ не нужен: `noauth` в каталоге
#: OmniRoute. Только такие программа подключает сама — остальным нужен
#: ключ сервиса, а ключей у неё нет.
KEYLESS_CATEGORY = "noauth"

#: Адрес живого запроса. Это тот же адрес, что у панели: отдельного
#: «служебного» порта у проверки нет.
HEALTH_PATH = "/api/health"


def find_settings_dir() -> Path:
    """Папка настроек opencode — та же логика, что у остальной программы.

    Нужна и лаунчеру, и командной строке моста: иначе они писали бы
    данные в разные места и говорили о разных серверах.
    """
    raw = os.environ.get("OPENCODE_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME")
    if raw:
        candidate = Path(raw)
        if candidate.name == ".config":
            candidate = candidate / "opencode"
        if candidate.is_dir():
            return candidate
    return Path.home() / ".config" / "opencode"


def base_url() -> str:
    """Адрес панели и API. Один на всю программу, чтобы он не разошёлся."""
    return f"http://{DEFAULT_HOST}:{DEFAULT_PORT}"


def data_dir(dest: Path) -> Path:
    """Папка данных OmniRoute — рядом с настройками opencode."""
    return dest / DATA_NAME


def config_env(dest: Path, progress=None) -> dict:
    """Окружение запуска: где данные, какой порт, какой интерфейс.

    Имена переменных — из `.env` самого OmniRoute, не выдуманы:
    `DATA_DIR`, `PORT`, `OMNIROUTE_SERVER_HOST`. Интерфейс — 127.0.0.1:
    панель и API этой машины наружу не отдаются, и предупреждение самого
    OmniRoute про открытый порт без ключа тогда не срабатывает.
    """
    env = dict(os.environ)
    folder = data_dir(dest)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        env["DATA_DIR"] = str(folder)
    except OSError as exc:
        if progress:
            progress(f"Папка данных OmniRoute недоступна: {exc}")
    env["PORT"] = str(DEFAULT_PORT)
    env["OMNIROUTE_SERVER_HOST"] = DEFAULT_HOST
    return env


def _wrap_cmd(argv: list[str]) -> list[str]:
    """Обёртка для .cmd/.bat на Windows: CreateProcess их не запускает."""
    if argv and str(argv[0]).lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *argv]
    return argv


def node_exe() -> str | None:
    """Где лежит node. Без него запускать нечего."""
    return shutil.which("node") or shutil.which("node.exe")


def npm_exe() -> str | None:
    """Где лежит npm. На Windows это npm.cmd."""
    return shutil.which("npm") or shutil.which("npm.cmd")


def installed_version() -> str:
    """Какая версия OmniRoute стоит в папке моста. Пусто — не стоит."""
    meta = SERVER_DIR / "node_modules" / PACKAGE / "package.json"
    try:
        data = json.loads(meta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(data.get("version") or "")


def deployed() -> bool:
    """Стоит ли пакет и на месте ли точка входа."""
    return ENTRY.is_file()


def command(args: list[str]) -> list[str]:
    """Команда запуска OmniRoute: node и точка входа из пакета."""
    node = node_exe() or "node"
    return [node, str(ENTRY), *args]


def deploy(progress=None) -> tuple[list[str], list[str]]:
    """Ставит пакет OmniRoute в папку моста.

    `npm install` из README: глобально ставить не даём — программа не
    трогает служебные папки пакетов и общие каталоги Node.js. Папка своя,
    удаляется вместе с базой.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    npm = npm_exe()
    if not npm:
        return [], [
            "npm не найден. Он ставится вместе с Node.js: "
            "https://nodejs.org/ — без Node.js OmniRoute не запускается.",
            "В настройки opencode ничего не вписано.",
        ]

    say("Ставлю OmniRoute: npm install, это сотни мегабайт и несколько минут…")
    argv = _wrap_cmd([
        npm, "install", f"{PACKAGE}@latest",
        "--no-audit", "--no-fund", "--loglevel=warn",
    ])
    try:
        proc = subprocess.run(
            argv, cwd=str(SERVER_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=INSTALL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return messages, [f"Установка OmniRoute не удалась: {exc}"]
    if proc.returncode != 0:
        tail = _tail(proc.stderr or proc.stdout)
        return messages, [f"npm вернул код {proc.returncode}: {tail}"]
    version = installed_version()
    if not version:
        return messages, [
            "npm отработал, но папки omniroute в node_modules нет — "
            "установка не состоялась.",
        ]
    say(f"OmniRoute {version} развёрнут в папке моста.")
    return messages, []


def _tail(text: str, limit: int = 400) -> str:
    """Последние строки вывода — без простыни в окне."""
    lines = [line.strip() for line in (text or "").splitlines() if line.strip()]
    return " / ".join(lines[-3:])[:limit]


def _run(args: list[str], dest: Path, timeout: int = CLI_TIMEOUT,
         progress=None) -> tuple[int, str, str]:
    """Запускает CLI OmniRoute. Возвращает (код, stdout, stderr).

    Журнал самого OmniRoute идёт в stdout — в том числе строки про
    загруженный `.env`. Разбирать их как JSON нельзя, поэтому разбор
    отдельный: `_json_from()` берёт первый объект в тексте.
    """
    argv = command(args)
    env = config_env(dest, progress=progress)
    try:
        proc = subprocess.run(
            argv, cwd=str(SERVER_DIR), env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _json_from(text: str):
    """Первый объект или массив JSON в тексте вывода. Не нашёлся — None.

    Разбор идёт по тексту, а не по строкам: CLI печатает перед JSON свой
    журнал («Loaded env from …»), а иногда и после. Но начинается JSON
    там, где раньше — объект или массив: иначе объект внутри массива был
    бы принят за ответ целиком.
    """
    starts = [(text.find(o), o, c) for o, c in (("{", "}"), ("[", "]"))]
    starts = [item for item in starts if item[0] >= 0]
    if not starts:
        return None
    for _pos, opener, closer in sorted(starts):
        start = text.find(opener)
        if start < 0:
            continue
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == opener:
                depth += 1
            elif char == closer:
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:index + 1])
                    except ValueError:
                        break
    return None


def health(dest: Path, timeout: int = PROBE_TIMEOUT,
           progress=None) -> tuple[bool, dict, str]:
    """Живой запрос к API OmniRoute: `GET /api/health`.

    Порт может слушаться кем угодно — чужой процесс ответит чем-то
    другим. Поэтому проверяется тело ответа: `{"status":"ok"}`. Ответ
    `200` без этого поля живой проверкой не считается.
    """
    url = base_url() + HEALTH_PATH
    try:
        with urllib.request.urlopen(url, timeout=timeout) as answer:
            text = answer.read().decode("utf-8", "replace")
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, {}, f"API OmniRoute не ответил: {exc}"
    data = _json_from(text)
    if not isinstance(data, dict):
        return False, {}, f"Ответ не разобрался: {_tail(text, 200)}"
    if str(data.get("status") or "").lower() != "ok":
        return False, data, f"API ответил, но не «ok»: {_tail(text, 200)}"
    return True, data, ""


def wait_ready(dest: Path, progress=None, timeout: int = READY_TIMEOUT) -> tuple[list[str], list[str]]:
    """Ждёт, пока API начнёт отвечать. Ждём ответ, а не «процесс жив»."""
    messages: list[str] = []
    errors: list[str] = []
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        ok, _data, _note = health(dest, timeout=5)
        if ok:
            messages.append("API OmniRoute ответил на живой запрос: /api/health — «ok».")
            return messages, []
        time.sleep(2)
    errors.append(
        f"OmniRoute не ответил за {timeout} с (запросов: {attempt}). "
        "Первый запуск создаёт базу — если это он, нажми кнопку ещё раз."
    )
    return messages, errors


def start(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Поднимает сервер OmniRoute и ждёт ответа API."""
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not deployed():
        return [], [
            "OmniRoute не развёрнут: нажми «Развернуть» — иначе запускать нечего.",
            "В настройки opencode ничего не вписано.",
        ]
    if not node_exe():
        return [], [
            "Node.js не найден: OmniRoute запускается на нём.",
            "В настройки opencode ничего не вписано.",
        ]

    ok, _data, _note = health(dest, timeout=5)
    if ok:
        say("OmniRoute уже отвечает — второй экземпляр не поднимаю.")
    else:
        say("Запускаю OmniRoute: omniroute serve --daemon --no-open…")
        code, out, err = _run(
            ["serve", "--daemon", "--no-open", "--port", str(DEFAULT_PORT)],
            dest, timeout=CLI_TIMEOUT, progress=progress,
        )
        if code != 0:
            return messages, [
                f"Запуск OmniRoute не удался (код {code}): {_tail(err or out)}",
                "В настройки opencode ничего не вписано.",
            ]
    got, bad = wait_ready(dest, progress=progress)
    messages.extend(got)
    errors.extend(bad)
    return messages, errors


def stop(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Останавливает сервер OmniRoute."""
    messages: list[str] = []
    code, out, err = _run(["stop"], dest, progress=progress)
    if code != 0:
        return messages, [f"Остановить OmniRoute не удалось: {_tail(err or out)}"]
    messages.append("OmniRoute остановлен.")
    return messages, []


def bring_up(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Поднимает мост и подключает бесплатных провайдеров.

    Одним действием, потому что человек не должен помнить про второй шаг.
    Порядок жёсткий: сначала сервер и живая проверка, потом провайдеры.
    Не поднялся сервер — провайдеров не трогаем: подключать их некуда.
    Список бесплатных читается заново, поэтому изменившийся каталог
    подхватывается при каждом запуске.
    """
    messages, errors = start(dest, progress=progress)
    if errors:
        return messages, errors
    got, bad = sync_free_providers(dest, progress=progress)
    return messages + got, errors + bad


def restart(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Перезапускает сервер: остановка, запуск, живая проверка, провайдеры.

    Останавливаем и запускаем по отдельности, а не командой `restart`:
    так видно, на каком шаге беда, и после запуска обязательна живая
    проверка — «перезапустил» без ответа API ничего не значит. Запуск
    идёт через `bring_up()`, поэтому бесплатные провайдеры подключаются
    и при перезапуске — так требует задание.
    """
    messages: list[str] = []
    errors: list[str] = []
    got, bad = stop(dest, progress=progress)
    messages.extend(got)
    if bad:
        # Сервер мог не работать вовсе: это не повод отказывать в запуске.
        messages.extend(bad)
    got, bad = bring_up(dest, progress=progress)
    messages.extend(got)
    errors.extend(bad)
    return messages, errors


def server_version(dest: Path) -> str:
    """Версия работающего сервера — из ответа API, а не из файла.

    `omniroute health --json` идёт на тот же API и печатает версию и
    время работы. Не ответил — пустая строка, и это не отказ запуска.
    """
    code, out, _err = _run(["health", "--json"], dest)
    if code != 0:
        return ""
    data = _json_from(out)
    if isinstance(data, dict):
        return str(data.get("version") or "")
    return ""


def _split_free(items) -> tuple[list[dict], list[dict]]:
    """Делит каталог на «бесплатные без ключа» и «остальные бесплатные».

    Отдельная функция, а не строки внутри `catalog()`: это правило отбора,
    и его надо уметь проверить без запуска OmniRoute. Устаревшие записи
    (`deprecated`) не берём вовсе — подключать снятое с поддержки нельзя.
    """
    keyless: list[dict] = []
    keyed: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("hasFree"):
            continue
        if item.get("deprecated"):
            continue
        if str(item.get("category") or "") == KEYLESS_CATEGORY:
            keyless.append(item)
        else:
            keyed.append(item)
    return keyless, keyed


def catalog(dest: Path) -> tuple[list[dict], list[dict], str]:
    """Каталог провайдеров OmniRoute: (бесплатные без ключа, прочие бесплатные, беда).

    Список читается командой самого OmniRoute `providers available
    --json`. Сервер для неё не нужен — это каталог внутри пакета. Поэтому
    при перезапуске список бесплатных провайдеров виден даже раньше, чем
    поднялся сервер.
    """
    code, out, err = _run(["providers", "available", "--json"], dest)
    data = _json_from(out)
    if code != 0 or not isinstance(data, dict):
        return [], [], f"Каталог провайдеров не прочитался: {_tail(err or out)}"
    keyless, keyed = _split_free(data.get("providers") or [])
    return keyless, keyed, ""


def configured_providers(dest: Path) -> tuple[set[str], str]:
    """Что уже подключено у работающего OmniRoute. Пустое множество — ничего."""
    code, out, err = _run(["providers", "list", "--json"], dest)
    data = _json_from(out)
    if code != 0 or not isinstance(data, dict):
        return set(), f"Список подключённых провайдеров не прочитался: {_tail(err or out)}"
    names: set[str] = set()
    for item in data.get("providers") or []:
        if isinstance(item, dict):
            names.add(str(item.get("provider") or ""))
            names.add(str(item.get("name") or ""))
    names.discard("")
    return names, ""


def sync_free_providers(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Подключает бесплатных провайдеров OmniRoute без ключа.

    Порядок такой же, как у остальных мостов: сначала живая проверка,
    потом действие. Каталог читается заново при каждом развёртывании и
    каждом перезапуске, поэтому изменившийся список подхватывается сам:
    чего нет — добавится, что уже стоит — не тронется.

    Бесплатные провайдеры с ключом программа не подключает: ключа
    сервиса у неё нет, и обещать за человека нечего. Про них честно
    сказано словами — сколько их и где взять ключ.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    ok, _data, note = health(dest, timeout=5)
    if not ok:
        return [], [note or "OmniRoute не отвечает — подключать провайдеров некуда."]

    keyless, keyed, trouble = catalog(dest)
    if trouble:
        return [], [trouble]
    if not keyless:
        say("Бесплатных провайдеров без ключа в каталоге нет — подключать нечего.")
        return messages, errors

    have, trouble = configured_providers(dest)
    if trouble:
        # Список не прочитался — добавляем вслепую нельзя, иначе получим
        # второй экземпляр уже подключённого провайдера.
        return messages, [trouble]

    added: list[str] = []
    failed: list[str] = []
    for item in keyless:
        name = str(item.get("id") or "")
        if not name or name in have:
            continue
        code, out, err = _run(
            ["providers", "add", name, "--allow-no-credential", "--yes", "--json"],
            dest, progress=progress,
        )
        if code == 0:
            added.append(name)
        else:
            failed.append(f"{name} ({_tail(err or out, 120)})")
    if added:
        say(f"Подключены бесплатные провайдеры без ключа: {', '.join(added)}.")
    if not added:
        say("Бесплатные провайдеры без ключа уже подключены — не трогаю.")
    if failed:
        errors.append("Не подключились: " + "; ".join(failed))
    if keyed:
        say(
            f"Ещё {len(keyed)} бесплатных провайдеров требуют ключ сервиса: "
            "программа их не подключает, ключ вписывает человек в панели "
            "OmniRoute (вкладка Providers)."
        )
    return messages, errors


def status_text(dest: Path) -> str:
    """Строка состояния для строки реестра и кнопки «Показать статус».

    Показывает только факты: развёрнут ли, отвечает ли API, какая версия,
    сколько провайдеров подключено. Ни ключей, ни токенов здесь нет и
    быть не может.
    """
    version = installed_version()
    if not version:
        return "OmniRoute не развёрнут: нажми «Развернуть»."
    ok, data, note = health(dest, timeout=5)
    if not ok:
        return f"OmniRoute {version} развёрнут, но API не отвечает: {note}"
    server = server_version(dest) or version
    have, trouble = configured_providers(dest)
    if trouble:
        providers = "провайдеры: список не прочитался"
    elif have:
        providers = f"подключено провайдеров: {len(have)}"
    else:
        providers = "провайдеры: ни один не подключён"
    stamp = str(data.get("timestamp") or "")
    return (
        f"OmniRoute {server} отвечает: {HEALTH_PATH} — «ok» ({stamp}). "
        f"{providers}."
    )


def check_connection(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Живая проверка моста: настоящий запрос к API OmniRoute.

    Порт может слушаться кем угодно, поэтому проверяется ответ API, а
    потом — что сервер знает сам себя (версия и время работы из
    `omniroute health`). Не ответил — отказ, и это «не проверено», а не
    зелёная отметка.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    ok, data, note = health(dest)
    if not ok:
        errors.append(note)
        return messages, errors
    stamp = str(data.get("timestamp") or "")
    say(f"Живая проверка: GET {HEALTH_PATH} вернул «ok» ({stamp}).")
    version = server_version(dest)
    if version:
        say(f"Сервер отвечает о себе: версия {version}.")
    else:
        say("Версию сервер не назвал — на работу это не влияет.")
    return messages, errors


def auto_setup(dest: Path, server, progress=None) -> tuple[list[str], list[str]]:
    """Разворачивает OmniRoute и только потом вписывает его в opencode.

    Порядок тот же, что у остальных мостов: развернуть, запустить,
    получить ответ API, подключить бесплатных провайдеров, и лишь затем
    писать в настройки opencode. При отказе на любом шаге в настройки не
    попадает ничего: неработающий сервер в списке хуже, чем его
    отсутствие.
    """
    import mcp_registry
    import opencode_caps

    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not deployed():
        got, bad = deploy(progress=progress)
        messages.extend(got)
        if bad:
            errors.extend(bad)
            errors.append("В настройки opencode ничего не вписано.")
            return messages, errors
    else:
        say(f"OmniRoute {installed_version()} уже развёрнут.")

    # Запуск и провайдеры — по шагам, а не готовым действием: здесь важно
    # отличить «сервер не поднялся» (тогда отказ и ни строчки в настройки)
    # от «сервер работает, а провайдеры не подключились» (мост годится, но
    # сказать об этом надо вслух).
    got, bad = start(dest, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    got, bad = sync_free_providers(dest, progress=progress)
    messages.extend(got)
    messages.extend(f"Бесплатные провайдеры: {text}" for text in bad)

    got, bad = opencode_caps.install_providers(dest, {"omniroute"}, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        errors.append("Сервер MCP в настройки не вписан: сначала провайдер.")
        return messages, errors

    wrote, wrote_errors = mcp_registry.enable(dest, server, progress=progress)
    messages.extend(wrote)
    errors.extend(wrote_errors)
    if not wrote_errors:
        messages.append("Перезапусти opencode, чтобы он увидел мост.")
    return messages, errors


# ------------------------------------------------------------------ из окна и не только

def _say(text: str, kind: str = "") -> None:
    """Строка в консоль для запуска руками: журнал, а не окно."""
    print(text, flush=True)


def _main(argv: list[str]) -> int:
    """Командная строка моста: то же, что кнопки окна.

    Зачем она нужна. Скилл должен уметь поднять мост и перезапустить его,
    когда тот завис, — а нажимать кнопки в окне программы он не может.
    Команды ровно те же, что у кнопок, и зовут тот же код: разойтись они
    не могут.

    Секретов у команд нет: ключи провайдеров лежат в панели.
    """
    action = (argv[0] if argv else "status").lower()
    dest = find_settings_dir()
    if action in ("-h", "--help", "help"):
        _say("omniroute.py status | start | stop | restart | deploy | sync")
        _say("Мост OmniRoute: то же, что кнопки в окне программы.")
        return 0
    if action == "status":
        _say(status_text(dest))
        ok, _data, _note = health(dest, timeout=5)
        return 0 if ok else 1
    if action == "deploy":
        messages, errors = deploy(progress=_say)
    elif action == "start":
        messages, errors = bring_up(dest, progress=_say)
    elif action == "stop":
        messages, errors = stop(dest, progress=_say)
    elif action == "restart":
        messages, errors = restart(dest, progress=_say)
    elif action == "sync":
        messages, errors = sync_free_providers(dest, progress=_say)
    else:
        _say(f"Не знаю такой команды: {action}")
        _say("omniroute.py status | start | stop | restart | deploy | sync")
        return 2
    for line in messages:
        _say(line)
    for line in errors:
        _say("Ошибка: " + line)
    return 1 if errors else 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
