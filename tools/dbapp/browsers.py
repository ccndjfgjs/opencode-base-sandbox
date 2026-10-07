# -*- coding: utf-8 -*-
"""Мост браузеров: отдельный браузер для нейросети через Playwright MCP.

Основа — пакет `@playwright/mcp` от Microsoft (Apache-2.0). Программа не
кладёт его к себе: пакет ставит и обновляет npx, так написано в README
Playwright MCP. Наша часть — выбор: каким браузером пользоваться, с каким
профилем и показывать ли окно.

Выбор лежит файлом `mcp-browsers.json` рядом с настройками opencode, а в
`opencode.jsonc` уходит только команда лаунчера. Иначе смена браузера
требовала бы переписывать настройки opencode, а они — не наше дело.

Что известно про флаги (из README и `--help` пакета, проверено 07.10.2026):

  * `--browser chrome` и `--browser msedge` — уже установленный браузер
    берётся по каналу. Ставить его программе не нужно;
  * `--browser firefox` — **своя сборка Playwright**, а не тот Firefox,
    что стоит у человека: подключиться к обычному нельзя (Playwright
    правит сборку под себя). Сборку скачивает `npx playwright install
    firefox`, около 90 МБ;
  * `--executable-path` — путь к браузеру на диске. Так запускается
    Яндекс.Браузер: он на Chromium, канала для него у Playwright нет;
  * `--user-data-dir` — отдельный профиль на диске, `--isolated` — профиль
    в памяти. По умолчанию Playwright MCP берёт временный профиль, а мы
    задаём свой: чужой профиль нельзя открыть двум браузерам сразу;
  * `--extension` — подключение к своему Chrome или Edge с расширением
    Playwright. Это единственный способ работать в своих сессиях, и он
    требует, чтобы расширение человек поставил сам: программа его не
    ставит и не обещает этого.

Профиль нейросети лежит в `cache/browsers/profile-<браузер>` рядом с
настройками: в личные вкладки и сессии он не залезает, а папка `cache`
и так не публикуется.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

#: Файл выбора рядом с настройками opencode.
CONFIG_NAME = "mcp-browsers.json"

#: Пакет моста. Версию выбирает npx сам: @latest.
PACKAGE = "@playwright/mcp@latest"

#: Лаунчер: единственное место, где собирается команда запуска.
LAUNCHER = Path(__file__).resolve().parent / "launchers" / "browsers_bridge_launcher.py"

#: Куда класть профили нейросети. Папка `cache` рядом с настройками уже
#: закрыта от публикации, поэтому лишних правил в .gitignore не нужно.
CACHE_NAME = "cache"
PROFILE_ROOT = "browsers"

#: Сколько ждать ответа моста. Первый запуск скачивает пакет из npm, а
#: запуск браузера на холодную занимает секунды; запас — на медленную сеть.
PROBE_TIMEOUT = 180

#: Сколько ждать скачивания сборки Firefox для Playwright.
DOWNLOAD_TIMEOUT = 900

#: Заголовок страницы, которую открывает живая проверка. По нему видно,
#: что браузер не просто запустился, а открыл страницу и отдал её текст.
CHECK_TITLE = "Мост браузеров: проверка"


@dataclass(frozen=True)
class Browser:
    """Один браузер: как его запускает Playwright MCP."""

    key: str          # что пишется в файл выбора
    title: str        # как называется человеку
    kind: str         # channel — установленный, playwright — своя сборка, path — путь
    note: str         # честно о том, чего не будет


#: Список из плана: Chrome, Edge, Яндекс.Браузер, Firefox. Порядок не
#: случаен: сначала те, что ставятся одним каналом и работают без
#: скачиваний, потом остальные.
BROWSERS: tuple[Browser, ...] = (
    Browser(
        key="chrome",
        title="Google Chrome",
        kind="channel",
        note="Берётся тот Chrome, что уже стоит. Ставить ничего не нужно.",
    ),
    Browser(
        key="msedge",
        title="Microsoft Edge",
        kind="channel",
        note="Edge есть в Windows из коробки: ставить ничего не нужно.",
    ),
    Browser(
        key="yandex",
        title="Яндекс.Браузер",
        kind="path",
        note="Запускается по пути к browser.exe: канала у Playwright для "
             "него нет. Путь укажи кнопкой «Обзор».",
    ),
    Browser(
        key="firefox",
        title="Firefox",
        kind="playwright",
        note="Не тот Firefox, что стоит у тебя: Playwright водит только свою "
             "сборку, и подключиться к обычному Firefox нельзя. Сборка "
             "скачивается отдельно, около 90 МБ.",
    ),
)

#: Режимы профиля. «Отдельный» — по умолчанию и врозь от личных вкладок;
#: «мои сессии» — только явным выбором и только там, где это возможно.
PROFILE_MODES: tuple[tuple[str, str], ...] = (
    ("separate", "Отдельный профиль — свои вкладки, личные не трогает"),
    ("isolated", "Отдельный, без сохранения на диске"),
    ("sessions", "Мои сессии — через расширение Playwright (Chrome и Edge)"),
)


def find_settings_dir() -> Path:
    """Папка настроек opencode.

    Порядок тот же, что и у остальной программы: переменная окружения,
    затем стандартное место пользователя. Ничего не выдумываем — если
    папку найти не удалось, читаем стандартную и говорим об этом словами,
    а не молча берём чужую.
    """
    raw = os.environ.get("OPENCODE_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME")
    if raw:
        candidate = Path(raw)
        if candidate.name == ".config":
            candidate = candidate / "opencode"
        if candidate.is_dir():
            return candidate
    return Path.home() / ".config" / "opencode"


def browser(key: str) -> Browser:
    """Браузер по ключу. Неизвестный ключ — Chrome, а не падение."""
    for item in BROWSERS:
        if item.key == key:
            return item
    return BROWSERS[0]


def browser_titles() -> list[str]:
    return [item.title for item in BROWSERS]


@dataclass
class Choice:
    """Что выбрал человек. Пишется в файл рядом с настройками."""

    browser: str = "chrome"
    executable: str = ""           # путь к Яндекс.Браузеру
    profile: str = "separate"      # separate | isolated | sessions
    headless: bool = False         # без окна браузера
    download_firefox: bool = False  # согласие скачать сборку Playwright
    checked: str = ""              # дата живой проверки, ставит программа


def config_path(dest: Path) -> Path:
    return Path(dest) / CONFIG_NAME


def read_choice(dest: Path) -> Choice:
    """Читает выбор. Чего нет — берётся по умолчанию, а не падением.

    Файл пишет программа, но его может править и человек, и он может
    остаться от прошлой версии. Поэтому неизвестные значения заменяются
    на понятные, а не ломают окно.
    """
    choice = Choice()
    path = config_path(dest)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return choice
    if not isinstance(raw, dict):
        return choice
    key = str(raw.get("browser") or "")
    if any(item.key == key for item in BROWSERS):
        choice.browser = key
    choice.executable = str(raw.get("executable") or "")
    mode = str(raw.get("profile") or "")
    if any(value == mode for value, _ in PROFILE_MODES):
        choice.profile = mode
    choice.headless = bool(raw.get("headless"))
    choice.download_firefox = bool(raw.get("download_firefox"))
    choice.checked = str(raw.get("checked") or "")
    return choice


def write_choice(dest: Path, choice: Choice) -> tuple[list[str], list[str]]:
    """Записывает выбор. Возвращает (сообщения, ошибки).

    Проверяем только то, что ломает запуск: путь к Яндекс.Браузеру и
    режим «мои сессии» не для Chrome и Edge. Остальное — дело вкуса.
    """
    dest = Path(dest)
    messages: list[str] = []
    errors: list[str] = []
    br = browser(choice.browser)

    if br.kind == "path":
        path = Path(choice.executable)
        if not choice.executable:
            errors.append(
                "Выбран Яндекс.Браузер, а путь к нему не указан: нажми "
                "«Обзор» и выбери browser.exe. Без пути он не запустится."
            )
        elif not path.is_file():
            errors.append(
                f"По пути «{choice.executable}» нет файла. Укажи путь к "
                "browser.exe ещё раз."
            )
    if choice.profile == "sessions" and choice.browser not in ("chrome", "msedge"):
        errors.append(
            "Режим «мои сессии» работает только с Chrome и Edge: расширение "
            "Playwright ставится в них. Выбери другой браузер или другой режим."
        )
    if errors:
        return [], errors

    path = config_path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "browser": choice.browser,
                    "executable": choice.executable,
                    "profile": choice.profile,
                    "headless": choice.headless,
                    "download_firefox": choice.download_firefox,
                    "checked": choice.checked,
                },
                ensure_ascii=False,
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )
    except OSError as exc:
        return [], [f"Файл выбора не записался: {exc}"]

    messages.append(f"Выбор записан: {path}")
    messages.append("Дальше — «Проверить браузер»: живая проверка откроет "
                    "страницу и прочитает заголовок. Только после неё "
                    "«Включить».")
    return messages, errors


def profile_dir(dest: Path, key: str) -> Path:
    """Папка профиля нейросети для этого браузера."""
    return Path(dest) / CACHE_NAME / PROFILE_ROOT / f"profile-{key}"


def description(choice: Choice) -> str:
    """Короткая строка для окна: что именно выбрано."""
    br = browser(choice.browser)
    mode = dict(PROFILE_MODES).get(choice.profile, "")
    mode = mode.split("—")[0].strip().rstrip(",")
    window = "без окна" if choice.headless else "с окном"
    extra = ""
    if br.kind == "path" and choice.executable:
        extra = f" ({Path(choice.executable).name})"
    if choice.profile == "sessions":
        window = "твоё окно"
    return f"{br.title}{extra}, {mode.lower()}, {window}"


def _path_candidates(template: str) -> list[Path]:
    """Пути установки по шаблону с переменными окружения Windows.

    Примеры берём у самих браузеров, а не выдумываем: это обычные места
    установки. Если браузер стоит в другом месте, его укажет человек —
    программа не ищет по всему диску.
    """
    found = os.path.expandvars(template)
    if "%" in found or "$" in found:  # переменная не подставилась
        return []
    return [Path(found)]


#: Обычные места установки. Проверяются, чтобы честно сказать человеку,
#: стоит ли браузер, а не чтобы запускать его: запускает Playwright.
INSTALL_PATHS: dict[str, tuple[str, ...]] = {
    "chrome": (
        r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
        r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
        r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    ),
    "msedge": (
        r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
        r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        r"%LocalAppData%\Microsoft\Edge\Application\msedge.exe",
    ),
    "yandex": (
        r"%LocalAppData%\Yandex\YandexBrowser\Application\browser.exe",
        r"%ProgramFiles%\Yandex\YandexBrowser\Application\browser.exe",
        r"%ProgramFiles(x86)%\Yandex\YandexBrowser\Application\browser.exe",
    ),
    "firefox": (
        r"%ProgramFiles%\Mozilla Firefox\firefox.exe",
        r"%ProgramFiles(x86)%\Mozilla Firefox\firefox.exe",
    ),
}


def installed_path(key: str) -> Path | None:
    """Где стоит браузер, если он в обычном месте. None — не нашли."""
    for template in INSTALL_PATHS.get(key, ()):
        for path in _path_candidates(template):
            if path.is_file():
                return path
    return None


def playwright_cache_dir() -> Path:
    """Папка, куда Playwright кладёт свои сборки браузеров."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local")
        return Path(base) / "ms-playwright"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "ms-playwright"
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "ms-playwright"


def playwright_firefox_dir() -> Path | None:
    """Скачана ли сборка Firefox для Playwright. None — нет."""
    root = playwright_cache_dir()
    try:
        found = sorted(p for p in root.glob("firefox-*") if p.is_dir())
    except OSError:
        return None
    return found[-1] if found else None


def hint(key: str, choice: Choice | None = None) -> str:
    """Что сказать человеку про этот браузер до проверки.

    Это не проверка моста, а справка: молчать про то, чего нет, нельзя —
    иначе первый же отказ будет выглядеть поломкой программы.
    """
    br = browser(key)
    if br.kind == "path":
        if choice is not None and choice.executable and Path(choice.executable).is_file():
            return f"Путь указан: {choice.executable}"
        found = installed_path(key)
        if found:
            return f"Нашёл в обычном месте: {found}. Путь можно не искать."
        return "Не нашёл в обычных местах. Укажи путь к browser.exe кнопкой «Обзор»."
    if br.kind == "playwright":
        found = playwright_firefox_dir()
        if found:
            return f"Сборка Playwright скачана: {found}"
        return ("Сборки Playwright нет. Это не твой Firefox: водить обычный "
                "Playwright не умеет. Галочка ниже скачает свою сборку, "
                "около 90 МБ.")
    found = installed_path(key)
    if found:
        return f"Нашёл в обычном месте: {found}"
    return ("В обычных местах не нашёл. Если браузер стоит в другом месте, "
            "Playwright его не увидит — тогда лучше Яндекс.Браузер с путём.")


def _which(name: str) -> str:
    return shutil.which(name) or shutil.which(name + ".cmd") or name


def _wrap_cmd(argv: list[str]) -> list[str]:
    """Обёртка для .cmd/.bat на Windows: CreateProcess их не запускает."""
    if argv and str(argv[0]).lower().endswith((".cmd", ".bat")):
        return [os.environ.get("COMSPEC") or "cmd.exe", "/c", *argv]
    return argv


def browser_args(dest: Path, choice: Choice) -> list[str]:
    """Аргументы Playwright MCP под этот выбор.

    Порядок и состав — из README пакета: сначала браузер, потом профиль,
    потом мелочи. Режим «мои сессии» свои аргументы заменяет целиком:
    там браузером управляет расширение, а не наш запуск.
    """
    br = browser(choice.browser)
    if choice.profile == "sessions":
        # Расширение работает только со своим запущенным Chrome или Edge,
        # поэтому ни профиля, ни headless здесь быть не может.
        return ["--extension"]
    args: list[str] = []
    if br.kind == "path":
        args += ["--executable-path", str(choice.executable)]
    else:
        args += ["--browser", br.key]
    if choice.profile == "isolated":
        args.append("--isolated")
    else:
        args += ["--user-data-dir", str(profile_dir(dest, br.key))]
    if choice.headless:
        args.append("--headless")
    return args


def command(dest: Path, choice: Choice) -> list[str]:
    """Полная команда запуска моста: npx, пакет и аргументы выбора."""
    return ["npx", "-y", PACKAGE, *browser_args(dest, choice)]


def launcher_command() -> list[str]:
    return [sys.executable, str(LAUNCHER)]


# --------------------------------------------------------------- проверки

def _tool_text(result: dict) -> str:
    """Текст из ответа инструмента MCP."""
    text = ""
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "text":
            text += str(item.get("text") or "")
    return text.strip()


def _clean(text: str, limit: int = 300) -> str:
    """Одна строка без служебных решёток: для сообщения человеку."""
    line = " ".join(str(text).split())
    return line[:limit] + ("…" if len(line) > limit else "")


def _failure_note(problems: list[str], timeout: int) -> str:
    tail = " | ".join(problems[-2:]) if problems else ""
    if tail:
        return (f"Мост не ответил за {timeout} с. Последнее из журнала: "
                f"{_clean(tail)}")
    return (f"Мост не ответил за {timeout} с: ни протокола, ни отказа. "
            "Проверь, что стоят Node.js и npx, а браузер — тот, что выбран.")


def probe(dest: Path, timeout: int = PROBE_TIMEOUT,
          command: list[str] | None = None) -> tuple[bool, str]:
    """Живая проверка: открывает страницу и читает её заголовок.

    Порядок ровно такой, как обещает план: сервер отвечает по протоколу,
    браузер открывает страницу, заголовок читается, снимок экрана
    сохраняется файлом. «Процесс запустился» доказательством не считается:
    без открытой страницы проверка говорит об отказе.

    Возвращает (получилось, сообщение). Сообщение об отказе всегда
    объясняет причину словами сервера, а не молчит.
    """
    choice = read_choice(dest)
    argv = _wrap_cmd(list(command) if command else launcher_command())
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

    threading.Thread(target=read_stdout, daemon=True).start()
    threading.Thread(target=read_stderr, daemon=True).start()

    def send(payload: dict) -> bool:
        assert proc.stdin is not None
        try:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()
            return True
        except OSError:
            return False

    def wait_for(answer_id: object, seconds: float) -> dict:
        deadline = time.monotonic() + seconds
        while (time.monotonic() < deadline and answer_id not in answers
               and proc.poll() is None):
            time.sleep(0.2)
        return (answers.get(answer_id) or {}).get("result") or {}

    def call(tool: str, arguments: dict, answer_id: int,
             seconds: float) -> dict:
        send({"jsonrpc": "2.0", "id": answer_id, "method": "tools/call",
              "params": {"name": tool, "arguments": arguments}})
        return wait_for(answer_id, seconds)

    try:
        if not send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18",
                                "capabilities": {},
                                "clientInfo": {"name": "opencode-base",
                                               "version": "1.0"}}}):
            return False, _failure_note(problems, timeout)
        send({"jsonrpc": "2.0", "method": "notifications/initialized",
              "params": {}})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})

        init = wait_for(1, timeout)
        listed = wait_for(2, timeout)
        tools = listed.get("tools")
        if not init or not isinstance(tools, list) or not tools:
            return False, _failure_note(problems, timeout)

        info = init.get("serverInfo") or {}
        name = str(info.get("name") or "")
        version = str(info.get("version") or "")
        if "playwright" not in name.lower():
            return False, (
                f"Ответил не Playwright MCP, а «{_clean(name or 'без имени')}» — "
                "проверь запись в реестре")

        # Шаг 1: браузер открывает страницу. Данные в адресе, а не в сети:
        # проверка не должна зависеть от того, есть ли интернет.
        html = (f"<html><head><title>{CHECK_TITLE}</title></head>"
                "<body>ok</body></html>")
        url = ("data:text/html;charset=utf-8,"
               + urllib.parse.quote(html))
        opened = call("browser_navigate", {"url": url}, 3, min(timeout, 120))
        if not opened or opened.get("isError"):
            text = _tool_text(opened) or "браузер не ответил"
            return False, (f"Страница не открылась: {_clean(text)} "
                           "(браузер выбран и запускается — проверь, что он "
                           "установлен)")

        # Шаг 2: заголовок страницы. Открывшаяся страница ещё не доказательство:
        # доказательство — что её видно из браузера.
        title = call("browser_evaluate",
                     {"function": "() => document.title"}, 4, min(timeout, 60))
        text = _tool_text(title)
        if title.get("isError") or CHECK_TITLE not in text:
            return False, (f"Страница открылась, а заголовок прочитать не "
                           f"вышло: {_clean(text or 'пустой ответ')}")

        # Шаг 3: снимок экрана файлом — чтобы человек мог посмотреть сам.
        shot = profile_dir(dest, browser(choice.browser).key).parent / "check.png"
        shot.parent.mkdir(parents=True, exist_ok=True)
        call("browser_take_screenshot", {"filename": str(shot)}, 5,
             min(timeout, 60))
        tail = f", снимок: {shot}" if shot.is_file() else " (снимок не вышел)"
        # Число инструментов — как в ответе DBHub: по нему видно, что сервер
        # ответил целиком, а не одним приветствием.
        return True, (f"{name} {version}: инструментов {len(tools)}, "
                      f"страница открыта, заголовок прочитан{tail}")
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


def check_connection(dest: Path, choice: Choice | None = None,
                     progress=None) -> tuple[list[str], list[str]]:
    """Живая проверка выбранного браузера. Возвращает (сообщения, ошибки)."""
    def say(text: str) -> None:
        if progress:
            progress(text)

    choice = choice or read_choice(dest)
    br = browser(choice.browser)
    messages: list[str] = []
    errors: list[str] = []
    say(f"Проверяю {br.title}: {description(choice)}")
    ok, note = probe(dest)
    if ok:
        say(note)
        messages.append(note)
        return messages, errors
    errors.append("Живая проверка не прошла: " + note)
    return messages, errors


def install_firefox(progress=None) -> tuple[list[str], list[str]]:
    """Скачивает сборку Firefox для Playwright.

    Команда из документации Playwright: `npx playwright install firefox`.
    Мы не подменяем её своей загрузкой: чужая сборка обновляется вместе с
    пакетом, а наша копия — нет.
    """
    def say(text: str) -> None:
        if progress:
            progress(text)

    messages: list[str] = []
    errors: list[str] = []
    if playwright_firefox_dir() is not None:
        messages.append("Сборка Firefox для Playwright уже скачана.")
        return messages, errors

    say("Скачиваю сборку Firefox для Playwright — около 90 МБ, "
        "это может занять несколько минут…")
    argv = _wrap_cmd([_which("npx"), "-y", "playwright@latest",
                      "install", "firefox"])
    try:
        proc = subprocess.run(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            timeout=DOWNLOAD_TIMEOUT, check=False,
        )
    except subprocess.TimeoutExpired:
        errors.append(
            f"Скачивание не уложилось в {DOWNLOAD_TIMEOUT} с. Оно идёт "
            "мимо программы: запусти вручную "
            "`npx playwright install firefox` и повтори."
        )
        return messages, errors
    except OSError as exc:
        errors.append(f"Скачивание не запустилось: {exc}")
        return messages, errors

    if playwright_firefox_dir() is None:
        tail = _clean((proc.stderr or "")[-300:] or "неизвестно почему")
        errors.append(
            "Сборка не появилась. Последнее из журнала: " + tail
        )
        return messages, errors
    messages.append("Сборка Firefox для Playwright скачана.")
    say("Сборка скачана.")
    return messages, errors


def auto_setup(dest: Path, server, progress=None) -> tuple[list[str], list[str]]:
    """Проверяет браузер и только потом вписывает сервер в настройки.

    Порядок тот же, что у остальных мостов: сначала живая проверка, потом
    запись. При отказе в настройки opencode не попадает ничего —
    неработающий сервер в списке хуже, чем его отсутствие.
    """
    import mcp_registry

    messages: list[str] = []
    errors: list[str] = []
    choice = read_choice(dest)
    br = browser(choice.browser)

    if br.kind == "path" and not (choice.executable
                                  and Path(choice.executable).is_file()):
        return [], [
            "Яндекс.Браузер выбран, а путь к нему не указан или неверен: "
            "нажми «Выбрать браузер» и укажи browser.exe.",
            "В настройки opencode ничего не вписано.",
        ]

    if choice.profile == "sessions":
        messages.append(
            "Режим «мои сессии»: Playwright подключится к твоему Chrome или "
            "Edge. Расширение Playwright в нём ставится руками — программа "
            "этого не делает и обещать не может."
        )

    if (choice.browser == "firefox" and choice.download_firefox
            and playwright_firefox_dir() is None):
        got, bad = install_firefox(progress)
        messages.extend(got)
        if bad:
            errors.extend(bad)
            errors.append("В настройки opencode ничего не вписано.")
            return messages, errors
        choice.download_firefox = False
        written, write_errors = write_choice(dest, choice)
        messages.extend(written)
        if write_errors:
            errors.extend(write_errors)
            errors.append("В настройки opencode ничего не вписано.")
            return messages, errors

    got, bad = check_connection(dest, choice, progress=progress)
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
