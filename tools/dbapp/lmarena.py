"""Мост LMArena: программа-мост к моделям сервиса Arena.

Что это. LMArenaBridge — сторонняя программа (форк CloudWaddie, MIT),
которая поднимает на этой машине OpenAI-совместимый сервер: запросы она
отправляет на сайт arena.ai и возвращает ответ модели. Нейросеть в
opencode получает через неё модели арены, в том числе те, что на арене
тестируются анонимно.

Где что лежит:

    tools/thirdparty/lmarena/     код форка (src/, requirements.txt, LICENSE)
    tools/thirdparty/lmarena/.venv    окружение моста (ставится кнопкой)
    lmarena-data/                 база моста: config.json, models.json, журнал
    <папка настроек>/mcp-lmarena-token.txt   токен арены (в логи не выводится)

Порядок установки и запуска — из README форка (на main он удалён коммитом
5e524b1 от 29.08.2026, поэтому взят из последнего коммита с README —
97a547d): установка `pip install -r requirements.txt`, запуск
`python -m src.main`, панель и API на `http://localhost:8000`, адрес API
`http://localhost:8000/api/v1`, токен — кука `arena-auth-prod-v1` с сайта
арены (добывается человеком в браузере, начинается с `base64-`).
Схема с форума: API-ключ моста можно не заполнять.

Чего у форка нет, и о чём честно говорит эта программа:

* Стелс-модели арены закрыты самим форком: `/api/v1/models` их не
  показывает, а запрос к ним отвечает 403 «You do not have access to
  stealth models». Обещать их нельзя.
* Мост слушает все сетевые интерфейсы (`uvicorn.run(..., host="0.0.0.0")`
  в src/main.py) и по умолчанию пускает панель по паролю `admin`. Это
  выбор автора форка, а не наш: программа о нём предупреждает словами и
  пароль не меняет.
* Работа идёт через веб-сервис, а не через официальный API арены. Условия
  сервиса могут это не разрешать, доступ может отвалиться в любой момент.
  Поэтому мост по умолчанию выключен, включается после предупреждения, а
  приватный код и секреты через него отправлять нельзя.

Секретов в настройках opencode нет: токен арены лежит рядом с настройками
(`mcp-lmarena-token.txt`), из него попадает в `config.json` самого моста
(папка `lmarena-data`, закрыта в .gitignore), в логи и в окно не выводится.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

#: Адрес и версия форка, по которому сверялись команды и адреса API.
SOURCE = "https://github.com/CloudWaddie/LMArenaBridge"
SOURCE_COMMIT = "e9655ea6d74cddabdfdd651da285aa4ca60091ad"
SOURCE_COMMIT_NOTE = "коммит от 29.08.2026, на нём удалён README (5e524b1)"
LICENSE = "MIT"
FORK_CHOICE = (
    "Из трёх форков живой один: CloudWaddie/LMArenaBridge (406 звёзд, "
    "последний коммит 29.08.2026, MIT). lianues/lmarenabridge и "
    "hung319/larena2api не существуют (404); ближайший к третьему "
    "deanxv/lmarena2api снят в архив 01.06.2025."
)
#: Чего в форке нет и чего программа поэтому не делает.
#:
#: 1. Отключателя хоста. В `src/main.py` форка запуск зашит как
#:    `uvicorn.run(app, host="0.0.0.0", port=...)`, и переменной окружения
#:    для этого нет. Через `LMARENA_HOST` мы обращаемся к мосту только
#:    сами — сам он всё равно слушает все интерфейсы. Программа об этом
#:    предупреждает словами и ничего не подменяет: это настройка чужого
#:    кода.
#: 2. Пароля панели. Он лежит в `config.json` моста значением по умолчанию
#:    `admin` и меняется только в панели (`/dashboard`). Программа пароль
#:    не трогает по той же причине.
#: 3. Стелс-моделей: `/api/v1/models` их не показывает, запрос отвечает
#:    403.
#: 4. MCP-сервера: его пишем мы (`launchers/lmarena_bridge_launcher.py`).

SERVER_DIR = Path(__file__).resolve().parent.parent / "thirdparty" / "lmarena"
ENTRY = SERVER_DIR / "src" / "main.py"
REQUIREMENTS = SERVER_DIR / "requirements.txt"
VENV_DIR = SERVER_DIR / ".venv"
LAUNCHER = Path(__file__).resolve().parent / "launchers" / "lmarena_bridge_launcher.py"

#: Папка базы и журнала моста — рядом с настройками opencode.
DATA_NAME = "lmarena-data"
#: Файл с токеном арены — рядом с настройками, не в репозитории.
TOKEN_NAME = "mcp-lmarena-token.txt"

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000

#: Сколько ждать ответа API после запуска. Первый запуск тянет список
#: моделей с арены и может поднимать браузер — запас на медленную машину.
READY_TIMEOUT = 240
PROBE_TIMEOUT = 20
#: Установка окружения: pip качает fastapi, uvicorn, camoufox, playwright,
#: cloudscraper — это десятки мегабайт.
INSTALL_TIMEOUT = 1800
#: Ответ модели через арену небыстрый: на арене очередь и проверки.
CHAT_TIMEOUT = 180

HEALTH_PATH = "/api/v1/health"
MODELS_PATH = "/api/v1/models"
CHAT_PATH = "/api/v1/chat/completions"
DASHBOARD_PATH = "/dashboard"

#: Обязательные предупреждения. Показываются в окне до первого запуска
#: (и в командной строке перед запуском руками), текст держится здесь,
#: чтобы окно, скилл и самопроверка говорили одно и то же.
WARNINGS: tuple[str, ...] = (
    "Мост работает через веб-сервис Arena, а не через официальный API. "
    "Такое использование может не соответствовать условиям сервиса, и "
    "доступ может перестать работать без предупреждения.",
    "Запросы уходят на сервис Arena, и на этой площадке их могут "
    "использовать для оценки и публикации результатов. Приватный код и "
    "секреты через этот мост отправлять нельзя.",
    "Список моделей на арене меняется, стелс-модели появляются и "
    "исчезают. Этот форк стелс-модели не отдаёт: список их не показывает, "
    "а запрос отвечает отказом 403.",
    "Бесплатный доступ ограничен лимитами и защитой сайта (проверки на "
    "робота, reCAPTCHA), поэтому нестабильность ожидаема.",
    "Панель и API моста слушают все сетевые интерфейсы этой машины "
    "(так написано в коде форка), а панель по умолчанию пускает по паролю "
    "admin. На общей сети смени пароль в панели или не запускай мост.",
    "Токен арены хранится рядом с настройками opencode и в репозиторий не "
    "попадает; программа его никому не показывает и в журнал не пишет.",
)


# ------------------------------------------------------------------ пути и окружение

def find_settings_dir() -> Path:
    """Папка настроек opencode — та же логика, что у остальной программы.

    Нужна и лаунчеру, и командной строке моста: иначе они писали бы
    данные в разные места и говорили о разных мостах.
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
    """Адрес панели и API моста. Один на всю программу, чтобы не разошёлся.

    Проверка и нестандартный порт: `LMARENA_HOST` и `LMARENA_PORT`. Свои
    переменные, а не чужие: у форка порт зашит в constants.py (8000).
    """
    host = os.environ.get("LMARENA_HOST") or DEFAULT_HOST
    port = os.environ.get("LMARENA_PORT") or str(DEFAULT_PORT)
    return f"http://{host}:{port}"


def api_url() -> str:
    """Адрес API для провайдера opencode: тот, что назван в README форка."""
    return base_url() + "/api/v1"


def data_dir(dest: Path) -> Path:
    """Папка базы LMArenaBridge — рядом с настройками opencode."""
    return dest / DATA_NAME


def token_path(dest: Path) -> Path:
    """Файл с токеном арены — рядом с настройками, не в репозитории."""
    return dest / TOKEN_NAME


def venv_python() -> Path:
    """Python окружения моста. Windows и Linux раскладывают его по-разному."""
    if os.name == "nt":
        return VENV_DIR / "Scripts" / "python.exe"
    return VENV_DIR / "bin" / "python"


def deployed() -> bool:
    """Развёрнут ли мост: есть код и своё окружение с зависимостями."""
    return ENTRY.is_file() and venv_python().is_file()


def missing_files() -> list[str]:
    """Чего не хватает для запуска — словами, а не «ошибка»."""
    missing: list[str] = []
    if not ENTRY.is_file():
        missing.append("код форка (src/main.py)")
    if not REQUIREMENTS.is_file():
        missing.append("список зависимостей (requirements.txt)")
    if not venv_python().is_file():
        missing.append("окружение моста (.venv)")
    return missing


# ------------------------------------------------------------------ мелочи

def _tail(text: str, limit: int = 400) -> str:
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    return "…" + text[-limit:]


def _run(argv: list[str], timeout: int = INSTALL_TIMEOUT, cwd: Path | None = None,
         progress=None) -> tuple[int, str, str]:
    """Запускает внешнюю команду и возвращает (код, stdout, stderr)."""
    try:
        proc = subprocess.run(
            argv, cwd=str(cwd or SERVER_DIR), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _http_json(url: str, timeout: int = PROBE_TIMEOUT, payload: dict | None = None,
               method: str | None = None) -> tuple[bool, object, str]:
    """Запрос к API моста. Возвращает (получилось, разобранный ответ, причина).

    Ключ моста не передаётся намеренно: в README форка написано, что поле
    можно не заполнять, а сам мост при пустом ключе берёт свой первый
    ключ из config.json. Чужой ключ, наоборот, получил бы 401.
    """
    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:
            text = answer.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", "replace")
        except Exception:  # pragma: no cover - тело уже недоступно
            body = ""
        return False, None, f"HTTP {exc.code}: {_tail(body, 200)}"
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return False, None, f"мост не ответил: {exc}"
    try:
        return True, json.loads(text), ""
    except json.JSONDecodeError:
        return False, None, f"ответ не разобрался: {_tail(text, 200)}"


# ------------------------------------------------------------------ установка

def deploy(progress=None) -> tuple[list[str], list[str]]:
    """Ставит окружение моста и зависимости из его requirements.txt.

    Отдельно ставить нечего: код форка лежит в tools/thirdparty/lmarena, а
    всё, что ему нужно из Python, ставится в собственное окружение .venv
    этого моста — служебные папки чужих пакетов не трогаются, права
    администратора не нужны.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not ENTRY.is_file() or not REQUIREMENTS.is_file():
        return [], [
            "Код форка не найден: нет "
            + ", ".join(missing_files())
            + ". Он лежит в репозитории — проверь папку tools/thirdparty/lmarena.",
            "В настройки opencode ничего не вписано.",
        ]

    if not venv_python().is_file():
        say("Создаю окружение моста: .venv рядом с кодом форка.")
        code, _out, err = _run(
            [sys.executable, "-m", "venv", str(VENV_DIR)], timeout=600)
        if code != 0 or not venv_python().is_file():
            errors.append(
                "Окружение не создалось (python -m venv): " + _tail(err, 200))
            return messages, errors

    say("Ставлю зависимости моста в его окружение: pip install -r requirements.txt.")
    code, _out, err = _run(
        [str(venv_python()), "-m", "pip", "install", "--disable-pip-version-check",
         "-r", str(REQUIREMENTS)],
        timeout=INSTALL_TIMEOUT,
    )
    if code != 0:
        errors.append("Зависимости не поставились: " + _tail(err, 300))
        errors.append("Частая причина — нет сети, а она нужна на этом шаге.")
        return messages, errors

    if not deployed():
        errors.append("После установки окружение не на месте — проверь папку моста.")
        return messages, errors
    say("Мост развёрнут: код форка и его окружение готовы.")
    return messages, errors


# ------------------------------------------------------------------ токен

def read_token(dest: Path) -> str:
    """Токен арены из файла рядом с настройками. Пустой строки достаточно."""
    path = token_path(dest)
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return ""


def token_present(dest: Path) -> bool:
    """Есть ли токен. Значение наружу не отдаётся — только «да/нет»."""
    return bool(read_token(dest))


def save_token(dest: Path, token: str) -> tuple[list[str], list[str]]:
    """Кладёт токен арены в файл рядом с настройками.

    Файл закрыт в .gitignore по образцу остальных секретов этой машины;
    права сужаются до владельца (на Windows это делает система сама).
    В сообщениях самого токена нет — только «сохранён» и длина файла.
    """
    token = (token or "").strip()
    if not token:
        return [], ["Токен пустой: вставь куку arena-auth-prod-v1 целиком."]
    dest.mkdir(parents=True, exist_ok=True)
    path = token_path(dest)
    try:
        path.write_text(token + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(path, 0o600)
    except OSError as exc:
        return [], [f"Токен не сохранился: {exc}"]
    return [f"Токен сохранён: {TOKEN_NAME} рядом с настройками opencode."], []


def apply_token(dest: Path) -> tuple[list[str], list[str]]:
    """Переносит токен из файла рядом с настройками в config.json моста.

    Мост читает токен только из своего config.json (кука
    `arena-auth-prod-v1`), переменных окружения у него для этого нет —
    поэтому файл рядом с настройками и база моста связаны здесь.
    Токен заменяется целиком: старый мог протухнуть.
    """
    token = read_token(dest)
    if not token:
        return [], []
    folder = data_dir(dest)
    folder.mkdir(parents=True, exist_ok=True)
    config_path = folder / "config.json"
    config: dict = {}
    if config_path.is_file():
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [], [
                f"config.json моста не читается ({config_path}) — "
                "правим вручную, не автоматом.",
            ]
        if not isinstance(config, dict):
            return [], ["config.json моста — не объект JSON: правь вручную."]
    config["auth_tokens"] = [token]
    config["auth_token"] = token
    try:
        tmp = config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(config, ensure_ascii=False, indent=4),
                       encoding="utf-8")
        os.replace(tmp, config_path)
    except OSError as exc:
        return [], [f"Токен не попал в config.json моста: {exc}"]
    return ["Токен арены перенесён в config.json моста (папка lmarena-data)."], []


# ------------------------------------------------------------------ запуск и остановка

def _port_busy(timeout: float = 1.0) -> bool:
    """Слушает ли кто-нибудь порт моста."""
    host = os.environ.get("LMARENA_HOST") or DEFAULT_HOST
    port = int(os.environ.get("LMARENA_PORT") or DEFAULT_PORT)
    with socket.socket() as probe:
        probe.settimeout(timeout)
        return probe.connect_ex((host, port)) == 0


def health(dest: Path, timeout: int = PROBE_TIMEOUT) -> tuple[bool, dict, str]:
    """Живой запрос к API моста: `GET /api/v1/health`.

    Порт может слушаться кем угодно — чужой процесс ответит чем-то
    другим. Поэтому проверяется тело ответа: там должно быть поле
    `status` (`healthy` или `degraded`). «healthy» означает, что мост уже
    получил с арены куку и список моделей; «degraded» — что сервер жив, а
    токена или моделей пока нет. И то и другое — ответ моста.
    """
    ok, data, note = _http_json(base_url() + HEALTH_PATH, timeout=timeout)
    if not ok:
        return False, {}, f"API LMArenaBridge не ответил: {note}"
    if not isinstance(data, dict) or not str(data.get("status") or ""):
        return False, {}, "API ответил, но это не ответ моста: " + _tail(str(data), 200)
    return True, data, ""


def models(dest: Path, timeout: int = PROBE_TIMEOUT) -> tuple[list[dict], str]:
    """Список моделей моста: `GET /api/v1/models`.

    Пустой список — тоже ответ: мост ещё не получил модели с арены (нет
    токена или арена не ответила). Это не ошибка чтения.
    """
    ok, data, note = _http_json(base_url() + MODELS_PATH, timeout=timeout)
    if not ok:
        return [], note
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return [], "Ответ моста без списка моделей: " + _tail(str(data), 200)
    items = [item for item in data["data"] if isinstance(item, dict)]
    return items, ""


def wait_ready(dest: Path, progress=None, timeout: int = READY_TIMEOUT) -> tuple[list[str], list[str]]:
    """Ждёт, пока API начнёт отвечать. Ждём ответ, а не «процесс жив»."""
    messages: list[str] = []
    errors: list[str] = []
    deadline = time.monotonic() + timeout
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        ok, data, _note = health(dest, timeout=5)
        if ok:
            status = str(data.get("status") or "")
            messages.append(
                f"API моста ответил на живой запрос: {HEALTH_PATH} — «{status}»."
            )
            return messages, []
        time.sleep(2)
    errors.append(
        f"Мост не ответил за {timeout} с (запросов: {attempt}). Первый "
        "запуск тянет список моделей с арены — если это он, нажми кнопку "
        "ещё раз и посмотри журнал моста."
    )
    return messages, errors


def _spawn(dest: Path) -> tuple[bool, str]:
    """Поднимает мост отдельным процессом и пишет его журнал в файл."""
    folder = data_dir(dest)
    folder.mkdir(parents=True, exist_ok=True)
    log_path = folder / "bridge.log"
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(SERVER_DIR), env.get("PYTHONPATH", "")]
    ).rstrip(os.pathsep)
    env["PYTHONUNBUFFERED"] = "1"
    argv = [str(venv_python()), "-m", "src.main"]
    try:
        log = open(log_path, "a", encoding="utf-8", errors="replace")
    except OSError as exc:
        return False, f"Журнал моста не открылся ({log_path}): {exc}"
    kwargs: dict = {"cwd": str(folder), "env": env, "stdout": log, "stderr": log}
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(argv, **kwargs)
    except OSError as exc:
        log.close()
        return False, f"Мост не запустился: {exc}"
    try:
        (folder / "bridge.pid").write_text(str(proc.pid), encoding="utf-8")
    except OSError:
        pass
    return True, f"Мост запущен отдельным процессом, журнал: {log_path}."


def start(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Поднимает мост и ждёт ответа API. В opencode ничего не пишет."""
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not ENTRY.is_file():
        return [], [
            "Код моста не найден: " + ", ".join(missing_files()) + ".",
            "В настройки opencode ничего не вписано.",
        ]
    if not deployed():
        return [], [
            "Мост не развёрнут: нажми «Развернуть» — окружение и "
            "зависимости ставит она. Без окружения запускать нечего.",
            "В настройки opencode ничего не вписано.",
        ]

    ok, data, _note = health(dest, timeout=3)
    if ok:
        say(f"Мост уже запущен и отвечает: {HEALTH_PATH} — "
            f"«{data.get('status')}».")
        return messages, errors
    if _port_busy():
        return [], [
            f"Порт {DEFAULT_PORT} занят чем-то другим: мост на него не "
            "сядет, а чужой процесс программа не трогает.",
            "В настройки opencode ничего не вписано.",
        ]

    got, bad = apply_token(dest)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        return messages, errors

    ok, note = _spawn(dest)
    if not ok:
        errors.append(note)
        return messages, errors
    say(note)

    got, bad = wait_ready(dest, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        return messages, errors
    return messages, errors


def _pid(dest: Path) -> int | None:
    """Номер процесса моста, если мы его запускали."""
    try:
        raw = (data_dir(dest) / "bridge.pid").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return int(raw) if raw.isdigit() else None


def stop(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Останавливает мост, который запустила программа.

    Чужой процесс на порту не трогается: без своего файла с номером
    процесса программа только говорит, что мост не её.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    pid = _pid(dest)
    if pid is None:
        if _port_busy():
            return [], [
                f"На порту {DEFAULT_PORT} кто-то есть, но это не наш мост: "
                "программа чужие процессы не останавливает. Закрой его сам "
                "или укажи другой мост.",
            ]
        say("Мост и так не запущен.")
        return messages, errors

    if os.name == "nt":
        _run(["taskkill", "/PID", str(pid), "/T", "/F"], timeout=60)
    else:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as exc:
            return [], [f"Мост не остановился: {exc}"]
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not _port_busy():
            break
        time.sleep(1)
    else:
        if os.name != "nt":
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            time.sleep(2)
        if _port_busy():
            errors.append(
                f"Процесс {pid} не отпустил порт {DEFAULT_PORT}. Проверь "
                "диспетчер задач."
            )
            return messages, errors
    try:
        (data_dir(dest) / "bridge.pid").unlink()
    except OSError:
        pass
    say("Мост остановлен.")
    return messages, errors


def bring_up(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """То, что нужно мосту, чтобы работать: токен и запущенный сервер.

    Порядок жёсткий: сначала токен из файла рядом с настройками попадает
    в config.json моста, потом поднимается сам мост. Запросы уходят на
    арену уже с токеном, поэтому список моделей подтягивается сразу, а не
    через полчаса до планового обновления.
    """
    return start(dest, progress=progress)


def restart(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Перезапускает мост: остановка, запуск, живая проверка.

    Токен при этом переносится заново: если человек его обновил, свежий
    токен уедет в мост при перезапуске. Застрявший мост лечится так же.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    got, bad = stop(dest, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        return messages, errors
    got, bad = bring_up(dest, progress=progress)
    messages.extend(got)
    errors.extend(bad)
    return messages, errors


# ------------------------------------------------------------------ состояние и проверка

def status_text(dest: Path) -> str:
    """Строка состояния для строки реестра и кнопки «Показать статус».

    Только факты: развёрнут ли, отвечает ли мост, есть ли токен, сколько
    моделей он знает. Токена самого здесь нет и быть не может.
    """
    missing = missing_files()
    if ENTRY.is_file() and not venv_python().is_file():
        return ("Код моста на месте, окружение не поставлено: нажми "
                "«Развернуть».")
    if not ENTRY.is_file():
        return "Код моста не найден: проверь папку tools/thirdparty/lmarena."
    if not deployed():
        return f"Мост не развёрнут: не хватает {', '.join(missing)}."
    ok, data, note = health(dest, timeout=5)
    if not ok:
        return f"Мост развёрнут, но API не отвечает: {note}"
    status = str(data.get("status") or "")
    checks = data.get("checks") if isinstance(data.get("checks"), dict) else {}
    count = checks.get("model_count")
    token = "токен есть" if token_present(dest) else "токена нет — нужен «Обновить токен»"
    models_text = (f"моделей: {count}" if isinstance(count, int) and count
                   else "моделей нет")
    return (f"Мост отвечает: {HEALTH_PATH} — «{status}» ({token}, "
            f"{models_text}).")


def check_connection(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Живая проверка моста: настоящий запрос к API и ответ модели.

    Три шага, и все три — настоящие запросы: сервер отвечает о себе,
    отдаёт список моделей, а потом отвечает модель. Последнее и есть
    доказательство, что мост работает: без токена арены или без сети
    ответа модели не будет, и это честный отказ, а не зелёная отметка.
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
    say(f"Живая проверка: GET {HEALTH_PATH} — «{data.get('status')}».")

    items, note = models(dest)
    if note:
        errors.append(note)
        return messages, errors
    if not items:
        errors.append(
            "Мост не знает ни одной модели: без токена арены (или пока "
            "арена не ответила) список пуст. Нажми «Обновить токен»."
        )
        return messages, errors
    say(f"Мост отдал список моделей: {len(items)}.")

    model = str(items[0].get("id") or "")
    if not model:
        errors.append("В списке моделей нет имени первой — проверь мост.")
        return messages, errors
    answer, note = chat(dest, model, "Ответь одним словом: работает.")
    if note:
        errors.append(note)
        return messages, errors
    say(f"Модель «{model}» ответила через мост: {_tail(answer, 120)}")
    return messages, errors


# ------------------------------------------------------------------ запрос к модели

def chat(dest: Path, model: str, prompt: str,
         timeout: int = CHAT_TIMEOUT) -> tuple[str, str]:
    """Спрашивает модель через мост. Возвращает (ответ, причина отказа).

    Формат запроса — OpenAI-совместимый (`/api/v1/chat/completions`), как
    в README форка. Ответ берётся из первого выбора; пустой ответ
    считается отказом, а не успехом.
    """
    if not model or not prompt:
        return "", "Нужны и модель, и вопрос."
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
    }
    ok, data, note = _http_json(base_url() + CHAT_PATH, timeout=timeout,
                                payload=payload)
    if not ok:
        return "", f"Мост не ответил на запрос к модели: {note}"
    if not isinstance(data, dict):
        return "", "Ответ моста не разобрался."
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return "", "В ответе моста нет выбора: " + _tail(str(data), 200)
    first = choices[0] if isinstance(choices[0], dict) else {}
    message = first.get("message") if isinstance(first.get("message"), dict) else {}
    text = str(message.get("content") or "").strip()
    if not text:
        return "", "Модель ответила пустым текстом."
    return text, ""


def chat_models(dest: Path) -> tuple[list[str], str]:
    """Имена моделей моста — то, что можно вписать в провайдера opencode."""
    items, note = models(dest)
    if note:
        return [], note
    names = [str(item.get("id")) for item in items if item.get("id")]
    return names, ""


# ------------------------------------------------------------------ провайдер в opencode

def provider_block(names: list[str]) -> str:
    """Блок провайдера для opencode.jsonc по живым моделям моста.

    Имена моделей не выдумываются: их называет сам мост (`/api/v1/models`
    — это `publicName` с арены). Ключ не вписывается вовсе: в README
    форка сказано, что поле можно оставить пустым, а сам мост при пустом
    ключе берёт свой первый ключ. Подставленное «любое значение» дало бы
    401 — проверено на живом мосте.
    """
    lines = [
        '"lmarena": {',
        '      "npm": "@ai-sdk/openai-compatible",',
        '      "name": "LMArena (мост)",',
        '      "options": {',
        f'        "baseURL": "{api_url()}"',
        "      },",
        '      "models": {',
    ]
    for index, name in enumerate(names):
        comma = "" if index == len(names) - 1 else ","
        label = name.replace('"', "'")
        lines.append(f'        "{name}": {{"name": "{label}"}}{comma}')
    lines.append("      }")
    lines.append("    },")
    return "\n".join(lines)


def install_provider(dest: Path, names: list[str] | None = None,
                     progress=None) -> tuple[list[str], list[str]]:
    """Дописывает провайдера LMArena в настройки opencode.

    Тем же механизмом, что Ollama, LM Studio и OmniRoute
    (`opencode_caps.install_providers`): слияние с mcp и permission, копия
    прежнего файла и отказ править сломанный. Список моделей берётся у
    живого моста — выдумать его нельзя, он у каждой машины свой.
    """
    import opencode_caps

    if names is None:
        names, note = chat_models(dest)
        if note:
            return [], [
                "Модели моста не прочитались: " + note,
                "В настройки opencode ничего не вписано.",
            ]
    if not names:
        return [], [
            "Мост не назвал ни одной модели: нечего вписывать в провайдера.",
            "В настройки opencode ничего не вписано.",
        ]
    return opencode_caps.install_providers(
        dest, {"lmarena"},
        progress=progress,
        blocks={"lmarena": provider_block(names)},
    )


# ------------------------------------------------------------------ автонастройка

def auto_setup(dest: Path, server, progress=None) -> tuple[list[str], list[str]]:
    """Поднимает мост и только потом вписывает его в opencode.

    Порядок тот же, что у остальных мостов: живая проверка, затем запись.
    Живая проверка здесь — ответ модели через арену, а не «порт
    слушается»: без токена арены или без сети блок в настройках был бы
    нерабочим. При отказе в настройки opencode не попадает ничего.
    """
    import mcp_registry

    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not deployed():
        return [], [
            "Мост не развёрнут: сначала нажми «Развернуть» (код форка уже "
            "в репозитории, а зависимости моста ставит эта кнопка).",
            "В настройки opencode ничего не вписано.",
        ]

    got, bad = bring_up(dest, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    got, bad = check_connection(dest, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    names, note = chat_models(dest)
    if note or not names:
        errors.append("Модели моста не прочитались: " + (note or "список пуст"))
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    got, bad = install_provider(dest, names, progress=progress)
    messages.extend(got)
    if bad:
        errors.extend(bad)
        return messages, errors

    wrote, wrote_errors = mcp_registry.enable(dest, server, progress=progress)
    messages.extend(wrote)
    errors.extend(wrote_errors)
    if not wrote_errors:
        say(f"В провайдера opencode вписаны модели моста: {len(names)}.")
        messages.append("Перезапусти opencode, чтобы он увидел мост.")
    return messages, errors


# ------------------------------------------------------------------ мост как MCP-сервер

MCP_PROTOCOL = "2025-06-18"
MCP_VERSION = "1.0"

_TOOL_DEFS: tuple[dict, ...] = (
    {
        "name": "lmarena_status",
        "description": ("Состояние моста LMArena: отвечает ли локальный "
                        "сервер, есть ли токен арены и сколько моделей он "
                        "знает. Токен не показывается."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "lmarena_models",
        "description": ("Список моделей, доступных через мост LMArena "
                        "(имена — как их называет сама арена). Стелс-модели "
                        "форк не отдаёт."),
        "inputSchema": {"type": "object", "properties": {},
                        "additionalProperties": False},
    },
    {
        "name": "lmarena_chat",
        "description": ("Спросить модель через мост LMArena. Запрос уходит "
                        "через веб-сервис Arena; приватный код и секреты "
                        "отправлять нельзя."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "model": {"type": "string",
                          "description": "имя модели из lmarena_models"},
                "prompt": {"type": "string", "description": "вопрос модели"},
            },
            "required": ["model", "prompt"],
            "additionalProperties": False,
        },
    },
)


def _mcp_text(text: str, is_error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": is_error}


def _mcp_ensure(dest: Path, say) -> tuple[bool, str]:
    """Проверяет, что мост отвечает; не отвечает — поднимает его.

    Молчаливый мост означал бы, что инструменты нейросети отвечают
    отказом, хотя всё на месте. Поэтому лаунчер поднимает мост сам — тем
    же кодом, что кнопка «Запустить».
    """
    ok, _data, _note = health(dest, timeout=5)
    if ok:
        return True, ""
    if not deployed():
        return False, ("Мост LMArena не запущен и не развёрнут: открой "
                       "программу, выдели строку «LMArena» в блоке «6. "
                       "Серверы MCP» и нажми «Развернуть», потом "
                       "«Обновить токен».")
    say("Мост не отвечает — поднимаю его сам.")
    _messages, errors = bring_up(dest)
    if errors:
        return False, ("Мост LMArena не поднялся: " + " ".join(errors))
    ok, _data, note = health(dest, timeout=5)
    if not ok:
        return False, ("Мост LMArena поднялся, но API не отвечает: " + note)
    return True, ""


def _mcp_call(name: str, arguments: dict, dest: Path, say) -> dict:
    """Один вызов инструмента. Отказ — тоже текст, а не молчание."""
    ready, trouble = _mcp_ensure(dest, say)
    if not ready:
        return _mcp_text(trouble, is_error=True)

    if name == "lmarena_status":
        return _mcp_text(status_text(dest))

    if name == "lmarena_models":
        names, note = chat_models(dest)
        if note:
            return _mcp_text("Список моделей не прочитался: " + note,
                             is_error=True)
        if not names:
            return _mcp_text(
                "Мост не знает ни одной модели: нужен токен арены. "
                "Открой программу и нажми «Обновить токен».", is_error=True)
        return _mcp_text("\n".join(names))

    if name == "lmarena_chat":
        model = str(arguments.get("model") or "").strip()
        prompt = str(arguments.get("prompt") or "").strip()
        if not model or not prompt:
            return _mcp_text("Нужны и модель, и вопрос.", is_error=True)
        answer, note = chat(dest, model, prompt)
        if note:
            return _mcp_text(note, is_error=True)
        return _mcp_text(answer)

    return _mcp_text(f"Не знаю такого инструмента: {name}", is_error=True)


def mcp_handle(message: dict, dest: Path, say) -> dict | None:
    """Обрабатывает одно сообщение протокола MCP. None — ответа не надо."""
    method = str(message.get("method") or "")
    ident = message.get("id")
    if ident is None and method.startswith("notifications/"):
        return None
    if method == "initialize":
        asked = message.get("params") or {}
        version = asked.get("protocolVersion") if isinstance(asked, dict) else None
        if version not in ("2024-11-05", "2025-03-26", "2025-06-18"):
            version = MCP_PROTOCOL
        return {"jsonrpc": "2.0", "id": ident, "result": {
            "protocolVersion": version,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "lmarena", "version": MCP_VERSION},
        }}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": ident, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": ident,
                "result": {"tools": [dict(tool) for tool in _TOOL_DEFS]}}
    if method == "tools/call":
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        name = str(params.get("name") or "")
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        say(f"вызов инструмента: {name}")
        return {"jsonrpc": "2.0", "id": ident,
                "result": _mcp_call(name, arguments, dest, say)}
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    return {"jsonrpc": "2.0", "id": ident, "error": {
        "code": -32601, "message": f"Метод не поддерживается: {method}",
    }}


def mcp_main() -> int:
    """Мост LMArena как MCP-сервер: stdio, по строке на сообщение.

    Свой сервер, а не чужая команда: у форка MCP нет — он отдаёт
    OpenAI-совместимый HTTP API. Этот слой — тонкий: три инструмента
    поверх API моста. Весь журнал идёт в stderr (stdout — протокол).
    """
    def say(text: str) -> None:
        print(f"[lmarena-bridge] {text}", file=sys.stderr, flush=True)

    dest = find_settings_dir()
    say(f"LMArena: код {SERVER_DIR}, настройки opencode {dest}")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            say(f"строка не разобралась: {exc}")
            print(json.dumps({"jsonrpc": "2.0", "id": None, "error": {
                "code": -32700, "message": "Разбор JSON не удался"}}),
                flush=True)
            continue
        if not isinstance(message, dict):
            continue
        try:
            answer = mcp_handle(message, dest, say)
        except Exception as exc:  # инструмент не должен ронять мост
            say(f"сбой в инструменте: {exc}")
            answer = {"jsonrpc": "2.0", "id": message.get("id"), "error": {
                "code": -32603, "message": f"Сбой: {exc}"}}
        if answer is not None:
            print(json.dumps(answer, ensure_ascii=False), flush=True)
    return 0


# ------------------------------------------------------------------ из окна и не только

def _say(text: str, kind: str = "") -> None:
    """Строка в консоль для запуска руками: журнал, а не окно."""
    print(text, flush=True)


def _main(argv: list[str]) -> int:
    """Командная строка моста: то же, что кнопки окна.

    Зачем она нужна. Скилл должен уметь подключиться к мосту, обновить
    список моделей в провайдере opencode и перезапустить мост, когда тот
    завис, — а нажимать кнопки в окне программы он не может. Команды
    ровно те же, что у кнопок, и зовут тот же код: разойтись они не могут.

    Токена в командах нет: он лежит файлом рядом с настройками.
    """
    action = (argv[0] if argv else "status").lower()
    dest = find_settings_dir()
    if action in ("-h", "--help", "help"):
        _say("lmarena.py status | start | stop | restart | deploy | "
             "models | sync | token")
        _say("Мост LMArena: то же, что кнопки в окне программы.")
        _say("token — файл с токеном не читается и не печатается.")
        return 0
    if action == "status":
        _say(status_text(dest))
        ok, _data, _note = health(dest, timeout=5)
        return 0 if ok else 1
    if action == "models":
        names, note = chat_models(dest)
        if note:
            _say("Ошибка: " + note)
            return 1
        for name in names:
            _say(name)
        if not names:
            _say("Мост не назвал ни одной модели: нужен токен арены.")
            return 1
        return 0
    if action == "sync":
        messages, errors = install_provider(dest, progress=_say)
    elif action == "deploy":
        messages, errors = deploy(progress=_say)
    elif action == "start":
        # Предупреждения — перед запуском, а не после: мост выключен по
        # умолчанию и включается только после явного согласия.
        for warning in WARNINGS:
            _say("Внимание: " + warning)
        messages, errors = bring_up(dest, progress=_say)
    elif action == "stop":
        messages, errors = stop(dest, progress=_say)
    elif action == "restart":
        messages, errors = restart(dest, progress=_say)
    elif action == "token":
        messages = ["Токен лежит файлом " + TOKEN_NAME + " рядом с настройками."]
        messages.append("Есть токен: " + ("да" if token_present(dest) else "нет")
                        + ". Значение не показывается.")
        errors = []
    else:
        _say(f"Не знаю такой команды: {action}")
        _say("lmarena.py status | start | stop | restart | deploy | "
             "models | sync | token")
        return 2
    for line in messages:
        _say(line)
    for line in errors:
        _say("Ошибка: " + line)
    return 1 if errors else 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
