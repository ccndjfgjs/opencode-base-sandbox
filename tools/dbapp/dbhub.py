# -*- coding: utf-8 -*-
"""Мост DBHub: подключения к базам, пароли и живая проверка.

Мост — это сервер `@bytebase/dbhub`: он умеет PostgreSQL, MySQL, MariaDB,
SQL Server, Oracle и SQLite. Программа не кладёт его к себе: пакет ставит
и обновляет npx, так написано в README Bytebase. Наша часть — файл
подключений, пароль и запуск через лаунчер.

Здесь живут четыре вещи:

1. **Файл подключений** `mcp-dbhub.toml` рядом с настройками opencode.
   Секции `[[sources]]` и `[[tools]]` — форма самого DBHub, не наша
   выдумка. Из его документации взято и то, что «только чтение» с
   лимитом строк задаётся отдельной записью `[[tools]]` на источник.

2. **Пароль** — в файле `mcp-dbhub-<имя>-password.txt`, а в строке
   подключения на его месте стоит `${MCP_DBHUB_PASSWORD_<ИМЯ>}`:
   подстановку переменных DBHub умеет сам. В настройках opencode и в
   реестре секретов нет.

3. **Живая проверка** `probe()` — настоящий разговор с сервером по
   протоколу: `initialize`, список инструментов и запрос к схеме.
   DBHub соединяется со всеми источниками сразу при старте и падает,
   если хоть один недоступен, поэтому ответ протокола доказывает живое
   соединение с базой, а не «процесс запустился».

4. **Автонастройка** `auto_setup()` — сначала живая проверка, и только
   потом запись в настройки opencode.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

#: Файл подключений рядом с настройками opencode.
TOML_NAME = "mcp-dbhub.toml"

#: Как называются файлы с паролями: mcp-dbhub-<имя источника>-password.txt.
PASSWORD_PREFIX = "mcp-dbhub-"
PASSWORD_SUFFIX = "-password.txt"

#: Лаунчер: единственное место, где пароль попадает в окружение процесса.
LAUNCHER = Path(__file__).resolve().parent / "launchers" / "dbhub_bridge_launcher.py"

#: Сколько строк отдавать серверу по умолчанию. Лимит не украшение:
#: без него один неосторожный запрос вернёт таблицу целиком.
DEFAULT_MAX_ROWS = 1000

#: Сколько ждать ответа моста. Первый запуск скачивает пакет из npm
#: (около семнадцати секунд на быстрой сети), дальше старт занимает
#: секунды; запас — на медленную сеть.
PROBE_TIMEOUT = 120

#: Виды баз, которые понимает DBHub, и начало строки подключения у каждого.
#: Список и примеры строк — из README и документации DBHub.
DB_TYPES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("postgres", "PostgreSQL", ("postgres", "postgresql")),
    ("mysql", "MySQL", ("mysql",)),
    ("mariadb", "MariaDB", ("mariadb",)),
    ("sqlserver", "SQL Server", ("sqlserver", "mssql")),
    ("oracle", "Oracle", ("oracle",)),
    ("sqlite", "SQLite", ("sqlite",)),
)

#: Пример строки подключения для окна. Это образец формы, а не чужой
#: адрес: человек подставляет свои хост, базу и пользователя.
DSN_EXAMPLES = {
    "postgres": "postgres://пользователь:пароль@localhost:5432/имя_базы",
    "mysql": "mysql://пользователь:пароль@localhost:3306/имя_базы",
    "mariadb": "mariadb://пользователь:пароль@localhost:3306/имя_базы",
    "sqlserver": "sqlserver://пользователь:пароль@localhost:1433/имя_базы",
    "oracle": "oracle://пользователь:пароль@localhost:1521/имя_службы",
    "sqlite": "sqlite:///C:/путь/к/файлу.sqlite",
}

_HEADER = (
    "# Подключения DBHub.\n"
    "# Файл ведёт программа: вкладка opencode, блок «6. Серверы MCP», строка\n"
    "# DBHub, кнопка «Добавить подключение». Он лежит рядом с настройками\n"
    "# opencode, а не в репозитории. На месте пароля стоит ${...}: значение\n"
    "# подставляет лаунчер из файла mcp-dbhub-<имя>-password.txt.\n"
    "# Правки руками возможны, но следующая запись из программы их затрёт.\n"
)

#: Строка подключения — единственное место файла, где заменяем пароль.
_URL_CRED = re.compile(
    r"(?P<s>[A-Za-z][A-Za-z0-9+.\-]*://[^/@\s:]+:)(?P<p>[^/@\s]*)@")


@dataclass
class Source:
    """Одно подключение из файла: имя, строка и ограничения."""

    id: str
    dsn: str = ""
    readonly: bool = True
    max_rows: int = DEFAULT_MAX_ROWS

    @property
    def db_type(self) -> str:
        """Вид базы по началу строки подключения.

        Отдельного поля «тип» в файле нет: DBHub определяет вид базы по
        схеме строки, и второй источник правды здесь был бы лишним —
        они разошлись бы при первой правке руками.
        """
        scheme = self.dsn.split("://", 1)[0].lower() if "://" in self.dsn else ""
        for key, _title, schemes in DB_TYPES:
            if scheme in schemes:
                return key
        return scheme or "неизвестно"


# ------------------------------------------------------------------ пути и имена

def config_path(dest: Path) -> Path:
    """Файл подключений. Рядом с настройками, как пароль OBS."""
    return Path(dest) / TOML_NAME


def password_file(dest: Path, source_id: str) -> Path:
    """Файл с паролем одного источника."""
    return Path(dest) / f"{PASSWORD_PREFIX}{source_id}{PASSWORD_SUFFIX}"


def password_env(source_id: str) -> str:
    """Имя переменной окружения для пароля источника.

    Правило продублировано в лаунчере намеренно: лаунчер запускается сам
    по себе и не может импортировать программу. Совпадение проверяет
    самопроверка — расходиться этим двум местам нельзя.
    """
    return "MCP_DBHUB_PASSWORD_" + source_id.upper().replace("-", "_")


# ------------------------------------------------------------------ чтение файла

def _unquote(raw: str) -> str:
    raw = raw.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        body = raw[1:-1]
        out: list[str] = []
        index = 0
        while index < len(body):
            char = body[index]
            if char == "\\" and index + 1 < len(body):
                nxt = body[index + 1]
                out.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, nxt))
                index += 2
                continue
            out.append(char)
            index += 1
        return "".join(out)
    return raw


def _quote(text: str) -> str:
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _as_bool(raw: str | None, default: bool) -> bool:
    if raw is None:
        return default
    return raw.strip().lower() in ("true", "1", "yes", "да")


def _as_int(raw: str | None, default: int) -> int:
    try:
        return int(str(raw).strip())
    except (TypeError, ValueError):
        return default


def _parse_blocks(text: str) -> list[tuple[str, dict[str, str]]]:
    """Разбирает файл на секции `[[имя]]` с парами «ключ = значение».

    Разбор нарочно простой: файл ведёт программа, и сложных форм TOML
    (вложенные таблицы, массивы) в нём не встречается. Всё, чего разбор
    не понял, остаётся чужим и при записи не переносится — об этом
    предупреждает шапка файла.
    """
    blocks: list[tuple[str, dict[str, str]]] = []
    table = ""
    fields: dict[str, str] | None = None
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[["):
            table = line.strip("[]").strip()
            fields = {}
            blocks.append((table, fields))
            continue
        if fields is None:
            continue
        key, sep, value = line.partition("=")
        if not sep:
            continue
        fields[key.strip()] = _unquote(value)
    return blocks


def read_sources(dest: Path) -> list[Source]:
    """Читает подключения из файла.

    Источники берутся из секций `[[sources]]`, ограничения — из секций
    `[[tools]]` с именем `execute_sql` и ссылкой на источник.
    """
    try:
        text = config_path(dest).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    blocks = _parse_blocks(text)
    sources: list[Source] = []
    index: dict[str, Source] = {}
    for table, fields in blocks:
        if table != "sources":
            continue
        source_id = fields.get("id", "").strip()
        if not source_id or source_id in index:
            continue
        source = Source(id=source_id, dsn=fields.get("dsn", ""))
        index[source_id] = source
        sources.append(source)
    for table, fields in blocks:
        if table != "tools" or fields.get("name") != "execute_sql":
            continue
        source = index.get(fields.get("source", ""))
        if source is None:
            continue
        source.readonly = _as_bool(fields.get("readonly"), True)
        source.max_rows = _as_int(fields.get("max_rows"), DEFAULT_MAX_ROWS)
    return sources


# ------------------------------------------------------------------ запись файла

def _type_schemes(db_type: str) -> tuple[str, ...]:
    for key, _title, schemes in DB_TYPES:
        if key == db_type:
            return schemes
    return ()


def split_secret(dsn: str, source_id: str) -> tuple[str, str]:
    """Отделяет пароль от строки подключения.

    Возвращает (строка для файла, пароль). Пароль из строки убирается и
    заменяется подстановкой `${...}` — её DBHub выполняет сам, и в файле
    подключений секрет не остаётся.

    Пароль возвращается в том же виде, в каком был в строке. Перекодировать
    его нельзя: строка подключения попадёт к серверу ровно такой, какой
    была бы без нашей правки, и разбираться будет так же.
    """
    if "://" not in dsn:
        return dsn, ""
    head, _, tail = dsn.partition("://")
    slash = tail.find("/")
    if slash >= 0:
        authority, rest = tail[:slash], tail[slash:]
    else:
        authority, rest = tail, ""
    if "@" not in authority:
        return dsn, ""
    userinfo, _, host = authority.rpartition("@")
    if ":" not in userinfo:
        return dsn, ""
    user, _, password = userinfo.partition(":")
    if not password:
        return dsn, ""
    placeholder = "${" + password_env(source_id) + "}"
    return f"{head}://{user}:{placeholder}@{host}{rest}", password


def _blocks_text(source: Source, dsn: str) -> str:
    """Запись одного подключения: источник и его инструменты.

    Две секции `[[tools]]` не украшение. Если задать инструменты явно,
    DBHub отдаёт только их: с одной записью `execute_sql` из списка
    пропадает `search_objects`, и нейросеть теряет поиск по схеме —
    проверено живьём 07.10.2026.
    """
    readonly = "true" if source.readonly else "false"
    return (
        f"\n[[sources]]\n"
        f"id = {_quote(source.id)}\n"
        f"dsn = {_quote(dsn)}\n"
        f"\n[[tools]]\n"
        f"name = \"execute_sql\"\n"
        f"source = {_quote(source.id)}\n"
        f"readonly = {readonly}\n"
        f"max_rows = {int(source.max_rows)}\n"
        f"\n[[tools]]\n"
        f"name = \"search_objects\"\n"
        f"source = {_quote(source.id)}\n"
    )


def add_source(dest: Path, source_id: str, db_type: str, dsn: str,
               readonly: bool = True, max_rows: int = DEFAULT_MAX_ROWS
               ) -> tuple[list[str], list[str]]:
    """Добавляет или переписывает подключение.

    Возвращает (сообщения, ошибки). Файл пересобирается целиком из того,
    что программа прочитала: чужие ключи внутри наших секций не
    переносятся, и об этом сказано в шапке файла. Так выбранный в окне
    лимит строк и режим «только чтение» доходят до файла, а не теряются.
    """
    messages: list[str] = []
    errors: list[str] = []

    source_id = source_id.strip()
    dsn = dsn.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", source_id):
        return [], [
            "Имя подключения: латинские буквы, цифры, дефис и подчёркивание, "
            "до 32 знаков. Из имени собирается имя переменной окружения и "
            "имя файла с паролем."
        ]
    schemes = _type_schemes(db_type)
    if not schemes:
        return [], [f"Неизвестный тип базы: {db_type}"]
    if not dsn:
        return [], [
            "Пустая строка подключения — подключаться нечем. Пример: "
            + DSN_EXAMPLES.get(db_type, "postgres://пользователь:пароль@хост/база")
        ]
    if "://" not in dsn:
        return [], [
            "Строка подключения должна начинаться со схемы, например: "
            + DSN_EXAMPLES.get(db_type, "postgres://пользователь:пароль@хост/база")
        ]
    scheme = dsn.split("://", 1)[0].lower()
    if scheme not in schemes:
        return [], [
            f"Выбран тип «{db_type}», а строка начинается с «{scheme}://». "
            "Проверь тип базы или строку подключения."
        ]

    file_dsn, secret = split_secret(dsn, source_id)
    source = Source(id=source_id, readonly=readonly, max_rows=max_rows)

    path = config_path(dest)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        old = read_sources(dest)
        kept = [s for s in old if s.id != source_id]
        replaced = len(kept) != len(old)
        kept.append(source)
        text = _HEADER + "".join(
            _blocks_text(s, file_dsn if s.id == source_id else s.dsn)
            for s in kept
        )
        path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return [], [f"Файл подключений не записался: {exc}"]

    if secret:
        pw_path = password_file(dest, source_id)
        try:
            pw_path.write_text(
                f"Пароль базы для подключения {source_id}\n\n{secret}\n",
                encoding="utf-8",
            )
        except OSError as exc:
            return [], [
                f"Пароль не записался: {exc}. В файле подключений осталась "
                "ссылка на переменную — без файла пароля она не разрешится, "
                "и база не откроется."
            ]
        messages.append(f"Пароль подключения «{source_id}» лежит отдельно: {pw_path}")
    else:
        messages.append("Пароля в строке подключения не было — отдельный файл не понадобился.")

    messages.insert(0, f"Подключение «{source_id}» добавлено: {path}")
    if replaced:
        messages.insert(0, f"Подключение «{source_id}» было — переписано заново.")
    return messages, errors


# ------------------------------------------------------------------ живая проверка

def _wrap_cmd(argv: list[str]) -> list[str]:
    if argv and str(argv[0]).lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *argv]
    return argv


def _launcher_command() -> list[str]:
    return [sys.executable, str(LAUNCHER)]


def probe(dest: Path, timeout: int = PROBE_TIMEOUT,
          command: list[str] | None = None, source_id: str = "") -> tuple[bool, str]:
    """Спрашивает у DBHub протоколом, а не портом.

    Запускает мост (лаунчер, если команда не задана) и разговаривает с
    ним по протоколу: `initialize`, список инструментов и запрос к схеме.
    DBHub соединяется со всеми источниками сразу при старте и падает,
    если хоть один недоступен, поэтому ответ протокола — доказательство
    живого соединения с базой.

    Секреты наружу не выходят: строки подключений и пароли вычищаются
    из сообщения об отказе.
    """
    argv = _wrap_cmd(list(command) if command else _launcher_command())
    env = dict(os.environ)
    env["OPENCODE_CONFIG_DIR"] = str(dest)
    env["PYTHONUNBUFFERED"] = "1"
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, env=env, cwd=str(dest),
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
    except OSError as exc:
        return False, f"Мост не запустился: {exc}"

    answers: dict[object, dict] = {}
    problems: list[str] = []

    def read_stdout() -> None:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                message = json.loads(line)
            except ValueError:
                continue
            if isinstance(message, dict) and "id" in message:
                answers[message["id"]] = message

    def read_stderr() -> None:
        assert proc.stderr is not None
        for line in proc.stderr:
            line = line.strip()
            if line:
                problems.append(line)
                del problems[:-4]

    reader = threading.Thread(target=read_stdout, daemon=True)
    err_reader = threading.Thread(target=read_stderr, daemon=True)
    reader.start()
    err_reader.start()

    try:
        assert proc.stdin is not None
        for payload in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "opencode-base", "version": "1.0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
            except OSError:
                break
        try:
            proc.stdin.flush()
        except OSError:
            pass

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and len(answers) < 2 and proc.poll() is None:
            time.sleep(0.2)

        init = (answers.get(1) or {}).get("result") or {}
        tools = ((answers.get(2) or {}).get("result") or {}).get("tools")
        if init and isinstance(tools, list) and tools:
            info = init.get("serverInfo") or {}
            name = str(info.get("name") or "")
            version = str(info.get("version") or "")
            if "dbhub" not in name.lower():
                return False, (
                    "Ответил не DBHub, а «" + _scrub(name or "без имени", dest)
                    + "» — проверь запись в реестре"
                )
            # Список инструментов — ещё не база. Просим сервер показать
            # таблицы: этот ответ DBHub получает только от живого источника.
            schema = _ask_schema(proc, answers, tools, timeout)
            tail = f", {schema}" if schema else ""
            return True, (f"DBHub ответил: {name} {version}, "
                          f"инструментов {len(tools)}{tail}")
        return False, _failure_note(problems, dest, timeout)
    finally:
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        try:
            proc.kill()
        except OSError:
            pass
        proc.wait()


def _ask_schema(proc: "subprocess.Popen[str]", answers: dict, tools: list,
                timeout: int) -> str:
    """Спрашивает у сервера таблицы. Пустая строка — если спросить не вышло.

    Пустая строка здесь — не отказ: соединение уже доказано списком
    инструментов, а показ таблиц только добавляет к нему слова. Врать
    «таблиц не найдено» вместо «спросить не удалось» нельзя, поэтому
    неудача молчит, а не называет ноль.
    """
    names = [str(tool.get("name") or "") for tool in tools
             if isinstance(tool, dict)]
    pick = next((n for n in names if n == "search_objects"), None)
    if pick is None:
        pick = next((n for n in names if n.startswith("search_objects")), None)
    if not pick or proc.stdin is None:
        return ""
    whole = min(timeout, 30)
    try:
        proc.stdin.write(json.dumps({
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": pick, "arguments": {"object_type": "table"}},
        }) + "\n")
        proc.stdin.flush()
    except OSError:
        return ""
    deadline = time.monotonic() + whole
    while time.monotonic() < deadline and 3 not in answers and proc.poll() is None:
        time.sleep(0.2)
    result = (answers.get(3) or {}).get("result") or {}
    if not result or result.get("isError"):
        return ""
    text = ""
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text += str(item.get("text") or "")
    count = _count_items(text)
    if count is None:
        return "поиск по схеме отвечает"
    return f"таблиц видно {count}"


def _count_items(text: str) -> int | None:
    """Сколько предметов в ответе сервера. None — если сосчитать не вышло."""
    if not text.strip():
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if isinstance(data, list):
        return len(data)
    if isinstance(data, dict):
        for value in data.values():
            if isinstance(value, list):
                return len(value)
    return None


def _scrub(text: str, dest: Path | None = None) -> str:
    """Вычищает секреты из текста, который увидит человек.

    Ошибки DBHub печатают строку подключения целиком — вместе с паролем.
    Показывать её в окне или в журнале нельзя, поэтому адреса вида
    «схема://пользователь:пароль@» теряют пароль, а значения из файлов
    паролей вычищаются как есть.
    """
    if dest is not None:
        for path in sorted(Path(dest).glob(f"{PASSWORD_PREFIX}*{PASSWORD_SUFFIX}")):
            for line in _password_lines(path):
                if line and line in text:
                    text = text.replace(line, "…")
    return _URL_CRED.sub(r"\g<s>…@", text)


def _password_lines(path: Path) -> list[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return [line.strip() for line in text.splitlines()
            if line.strip() and not line.startswith(("Пароль", "Нужен", "opencode", "База"))]


def _failure_note(problems: list[str], dest: Path, timeout: int) -> str:
    if problems:
        tail = " ".join(problems[-2:])
        return "Мост ответил ошибкой: " + _scrub(tail, dest)[:300]
    return (f"Мост не ответил за {timeout} с. Проверь подключение к базе и "
            "Node.js; если пакет ещё не скачан, первый запуск занимает до минуты.")


# ------------------------------------------------------------------ шаги для окна

def check_connection(dest: Path, sources: list[Source],
                     progress=None) -> tuple[list[str], list[str]]:
    """Живая проверка всех подключений. Возвращает (сообщения, ошибки)."""

    def say(text: str) -> None:
        if progress:
            progress(text)

    messages: list[str] = []
    errors: list[str] = []
    names = ", ".join(f"{s.id} ({s.db_type})" for s in sources)
    say(f"Подключений в файле: {len(sources)} — {names}")
    say("Поднимаю мост и спрашиваю его протоколом — это настоящая база, "
        "а не проверка, что порт слушается…")
    ok, note = probe(dest)
    if ok:
        say(note)
        messages.append(note)
        return messages, errors
    errors.append("Живая проверка не прошла: " + note)
    return messages, errors


def auto_setup(dest: Path, server, progress=None) -> tuple[list[str], list[str]]:
    """Проверяет связь и только потом вписывает сервер в настройки.

    Порядок тот же, что у остальных мостов: живая проверка, затем
    запись. При отказе в настройки opencode не попадает ничего —
    неработающий сервер в списке хуже, чем его отсутствие.
    """
    import mcp_registry

    messages: list[str] = []
    errors: list[str] = []

    sources = read_sources(dest)
    if not sources:
        return [], [
            "Подключений нет: нажми «Добавить подключение» и впиши базу — "
            "без неё мосту нечего делать.",
            "В настройки opencode ничего не вписано.",
        ]

    got, bad = check_connection(dest, sources, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    wrote, wrote_errors = mcp_registry.enable(dest, server, progress=progress)
    messages.extend(wrote)
    errors.extend(wrote_errors)
    if not wrote_errors:
        messages.append("Перезапусти opencode, чтобы он увидел сервер.")
    return messages, errors
