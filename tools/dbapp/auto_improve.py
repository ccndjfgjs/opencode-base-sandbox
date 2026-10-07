"""auto-improve — улучшение текста по рубрике чужой программой автора crimeacs.

Что это. Сторонний Python-скрипт (автор crimeacs, лицензия MIT): он берёт
текстовый файл, предлагает точечные правки, оценивает их отдельной
моделью-судьёй по рубрике и оставляет только те, что выиграли попарное
сравнение с текущей версией. Остальное откатывается, а каждый оставленный
шаг становится отдельным git-коммитом — история коммитов и есть журнал
улучшений. Выигрыш даёт чужая программа, а наша работа — поставить её
копию в базу, включать и выключать галочкой, вести ключ судьи и запускать
цикл из окна с понятными предупреждениями.

Где что лежит:

    tools/thirdparty/auto-improve/improve.py     скрипт автора (копия целиком)
    tools/thirdparty/auto-improve/criteria/      рубрики автора (примеры)
    tools/thirdparty/auto-improve/LICENSE        лицензия автора
    <папка настроек opencode>/auto-improve-key.txt    ключ судьи (в .gitignore)
    <папка настроек opencode>/auto-improve-data/      журнал хода и вывод

Ключ судьи. Автор читает его из `GEMINI_API_KEY` или `GOOGLE_API_KEY`.
Программа берёт ключ из файла рядом с настройками, а если файла нет — из
переменной окружения. В командную строку ключ не попадает никогда: он
уезжает в окружение процесса. В журнал и в сообщения окна — тоже.

Запуск только в отдельной ветке. Скрипт сам делает `git add -A`, создаёт
ветку `improve/<тег>` и коммитит в неё. Основную ветку он не трогает, но
незакоммиченные изменения рабочего дерева уедут в новую ветку — поэтому
и в окне, и в скилле про это сказано прямым текстом.

Чего программа не делает: не ставит `requests` и Python (берёт тот, чем
запущена, и честно говорит, если библиотеки нет), не зовёт `plot/` (нужен
Rust), не запускает озвучку из `voice/`, не чистит за человеком файл ключа
и папку данных и не добавляет себя в `mcp-registry.json` — это не MCP.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
sys.stdout.reconfigure(encoding='utf-8', errors='replace')
sys.stderr.reconfigure(encoding='utf-8', errors='replace')

#: Откуда взят скрипт и по какому коммиту сверялись его байты.
SOURCE = "https://github.com/crimeacs/auto-improve"
SOURCE_AUTHOR = "crimeacs"
SOURCE_COMMIT = "03aa5a9464b867bd8ce273841b07bcbf6890d7e7"
SOURCE_NOTE = "коммит от 03.08.2026"
LICENSE = "MIT"

#: Слепки скопированных файлов: ими самопроверка сверяет копии с автором.
SCRIPT_BLOBS = {
    "improve.py": "af8bcdd44e2aed1e813a26e3700da255a0701e1e",
    "LICENSE": "dab1cab1a419b4862176c301837eec18a32b9956",
    "criteria/README.md": "976f2fdfca8081f4cf93b71dfbfa41a5f3e7b541",
    "criteria/cold-email-quality.md": "a2b59ee6badc78cac177660f2a0e67f680cd5ae6",
    "criteria/blog-post-quality.md": "4d22b40864e844ebce482508be117a7266cb7fe9",
    "criteria/prompt-quality.md": "89ee79290639a16adab538bb4f0b2c1ee3413af6",
    "criteria/api-design-quality.md": "891160cf11fb3e923e0ad0f7866a0de8dcf898ee",
}

#: Место копии в базе и модели по умолчанию — из README автора.
SCRIPT_REL = ("tools", "thirdparty", "auto-improve", "improve.py")
CRITERIA_REL = ("tools", "thirdparty", "auto-improve", "criteria")
DEFAULT_MUTATOR = "gemini-flash-latest"
DEFAULT_EVALUATOR = "gemini-flash-latest"

#: Файл ключа и папка данных — рядом с настройками opencode, как у остальных
#: секретов программы. Ключ закрыт в .gitignore, данные чистятся человеком.
KEY_NAME = "auto-improve-key.txt"
DATA_NAME = "auto-improve-data"
MANIFEST_KEY = "auto-improve"

#: Ограничения запуска: больше — дольше и дороже. Значения по умолчанию
#: взяты из README автора и остаются человеку на выбор в окне.
DEFAULT_ITERATIONS = 10
DEFAULT_CANDIDATES = 3
DEFAULT_THRESHOLD = 90
DEFAULT_EVAL_RUNS = 2
MAX_ITERATIONS = 100

CHECK_TIMEOUT = 60
STATUS_TIMEOUT = 120

#: Тег запуска становится именем ветки `improve/<тег>`, поэтому проверяется
#: по правилам git: без пробелов и знаков `~ ^ : ? * [ \`, без «..», без
#: «@{», без точки и дефиса в начале и без точки в конце. Буквы могут быть
#: любые — файлы у человека по-русски называются по-русски.
TAG_BAD_CHARS = " ~^:?*[\\\t\r\n"
TAG_MAX = 60

#: Предупреждение о ветке: показывается в окне перед запуском и лежит в
#: README и скилле. Скрипт автора коммитит сам, и это надо понимать заранее.
BRANCH_WARNING = (
    "Скрипт сам сделает `git add -A`, создаст в репозитории файла ветку "
    "improve/<тег> и будет коммитить в неё. Основную ветку он не тронет, но "
    "незакоммиченные изменения рабочего дерева уедут в новую ветку. "
    "Запускай на копии проекта или в отдельной ветке."
)

#: Оговорка про деньги: цикл тратит токены и упирается в лимиты ключа.
COST_WARNING = (
    "Цикл тратит токены: каждая правка — запрос к модели, каждый раунд — "
    "ещё и оценки судьи. На бесплатном ключе это упирается в лимиты. "
    "Числа экономии и примеры роста из README автора — его заявления, "
    "независимо они не проверены."
)


# ------------------------------------------------------------------ пути


def program_root() -> Path:
    """Папка программы (в ней же лежит tools/thirdparty)."""
    import core  # noqa: PLC0415 — рядом лежит, круга нет

    return core.program_root()


def script_path() -> Path:
    """Копия скрипта автора в базе."""
    return program_root().joinpath(*SCRIPT_REL)


def criteria_dir() -> Path:
    """Папка с рубриками автора."""
    return program_root().joinpath(*CRITERIA_REL)


def criteria_files() -> list[Path]:
    """Рубрики, которые можно выбрать в окне: файлы .md кроме README."""
    folder = criteria_dir()
    if not folder.is_dir():
        return []
    return sorted(p for p in folder.glob("*.md") if p.name.lower() != "readme.md")


def find_settings_dir() -> Path:
    """Папка настроек opencode — та же логика, что у остальной программы.

    Переменные `OPENCODE_CONFIG_DIR` и `XDG_CONFIG_HOME` уважаются: иначе
    проверки писали бы в настоящие настройки человека.
    """
    raw = os.environ.get("OPENCODE_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME")
    if raw:
        candidate = Path(raw)
        if candidate.name == ".config":
            candidate = candidate / "opencode"
        if candidate.is_dir():
            return candidate
    return Path.home() / ".config" / "opencode"


def data_dir(dest: Path) -> Path:
    """Наша папка данных рядом с настройками: журнал хода и вывод циклов."""
    return Path(dest) / DATA_NAME


def results_dir(dest: Path) -> Path:
    """Куда скрипт автора пишет `<тег>.tsv` — ход улучшения.

    Автор по умолчанию кладёт результаты рядом с самим собой, то есть в
    папку программы. Так делать нельзя: программа не пишет в свои же
    служебные папки. Поэтому `RESULTS_DIR` всегда задаётся нами.
    """
    return data_dir(dest) / "results"


def events_file(dest: Path) -> Path:
    """Журнал событий автора (JSONL) — необязательный, но полезный."""
    return data_dir(dest) / "events.jsonl"


def log_file(dest: Path) -> Path:
    """Вывод циклов: то же, что видно в окне, но остаётся на диске."""
    return data_dir(dest) / "auto-improve.log"


def key_file(dest: Path) -> Path:
    """Файл ключа судьи рядом с настройками."""
    return Path(dest) / KEY_NAME


# ------------------------------------------------------------------ окружение


def find_python() -> str:
    """Python, которым запускать скрипт автора: тот же, чем запущена программа."""
    return sys.executable or "python"


def requests_available(python: str | None = None) -> tuple[bool, str]:
    """Есть ли у этого Python библиотека requests (единственная у автора)."""
    exe = python or find_python()
    try:
        done = subprocess.run(
            [exe, "-c", "import requests, sys; print(requests.__version__)"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=CHECK_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, str(exc)
    if done.returncode != 0:
        tail = (done.stderr or done.stdout or "").strip().splitlines()
        return False, tail[-1] if tail else f"код {done.returncode}"
    return True, (done.stdout or "").strip()


def find_git() -> str | None:
    """Путь к git. None — git в PATH нет, а без него цикл не работает."""
    return shutil.which("git")


def read_key(dest: Path) -> tuple[str, str]:
    """Ключ судьи и откуда он взялся: «файл», «окружение» или пусто.

    Значение ключа наружу отдаётся только тому коду, который кладёт его в
    окружение процесса: в сообщения, журнал и окно он не попадает.
    """
    try:
        text = key_file(dest).read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        text = ""
    if text:
        return text, "файл"
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
        value = (os.environ.get(name) or "").strip()
        if value:
            return value, "окружение"
    return "", ""


def key_status(dest: Path) -> str:
    """Строка о ключе без самого ключа — для окна и командной строки."""
    _value, where = read_key(dest)
    if where == "файл":
        return f"ключ судьи вписан файлом ({KEY_NAME})"
    if where == "окружение":
        return "ключ судьи взят из переменной окружения (GEMINI_API_KEY)"
    return "ключ судьи не вписан"


def save_key(dest: Path, text: str) -> tuple[list[str], list[str]]:
    """Кладёт ключ судьи в файл рядом с настройками.

    Файл закрыт в .gitignore по образцу остальных секретов этой машины,
    права сужаются до владельца (на Windows это делает система сама).
    В сообщениях самого ключа нет — только «сохранён» и длина.
    """
    text = (text or "").strip()
    if not text:
        return [], ["Ключ пустой: вставь ключ Gemini из aistudio.google.com/apikey."]
    if any(ch.isspace() for ch in text):
        return [], ["В ключе пробел или перенос строки — скопируй его одним куском."]
    if len(text) < 20:
        return [], ["Ключ слишком короткий — похоже, скопился не весь."]
    dest = Path(dest)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        key_file(dest).write_text(text + "\n", encoding="utf-8")
        if os.name != "nt":
            os.chmod(key_file(dest), 0o600)
    except OSError as exc:
        return [], [f"Ключ не сохранился: {exc}"]
    return [
        f"Ключ судьи сохранён: {KEY_NAME} рядом с настройками opencode "
        f"({len(text)} знаков).",
        "Проверкой ключа будет первый запуск: программа в интернет за этим "
        "не ходит.",
    ], []


def check(dest: Path) -> dict:
    """Что готово для запуска цикла, а чего не хватает. Ничего не меняет."""
    script = script_path()
    script_ok = script.is_file()
    python = find_python()
    req_ok, req_note = requests_available(python)
    git = find_git()
    _value, key_where = read_key(dest)

    missing: list[str] = []
    if not script_ok:
        missing.append("в базе нет копии скрипта tools/thirdparty/auto-improve/improve.py")
    if not req_ok:
        missing.append(
            f"у Python не хватает библиотеки requests ({req_note}) — "
            f"она ставится командой: \"{python}\" -m pip install requests"
        )
    if not git:
        missing.append("в PATH нет git — без него скрипт не делает ни ветки, ни коммитов")
    if not key_where:
        missing.append("не вписан ключ судьи Gemini (кнопка «Вписать ключ судьи»)")
    return {
        "script": script,
        "script_ok": script_ok,
        "python": python,
        "requests_ok": req_ok,
        "requests_note": req_note,
        "git": git or "",
        "key": key_where,
        "ready": not missing,
        "missing": missing,
    }


def status_text(dest: Path) -> str:
    """Строка состояния словами — для окна и для командной строки."""
    data = check(dest)
    parts: list[str] = []
    if data["script_ok"]:
        parts.append("копия скрипта на месте")
    else:
        parts.append("копии скрипта нет")
    parts.append(key_status(dest))
    if data["requests_ok"]:
        parts.append(f"requests {data['requests_note']}")
    else:
        parts.append("requests не найден")
    parts.append("git найден" if data["git"] else "git не найден")
    text = ", ".join(parts) + "."
    if data["ready"]:
        return "Готово к запуску: " + text
    return "Не готово: " + "; ".join(data["missing"]) + ". (" + text + ")"


# ------------------------------------------------------------------ установка


def _manifest(dest: Path) -> tuple[dict, str]:
    """Наш манифест установки. Второе — текст ошибки или пусто."""
    try:
        import opencode_caps  # noqa: PLC0415 — рядом лежит, круга нет

        return opencode_caps.read_manifest(Path(dest)), ""
    except Exception as exc:  # noqa: BLE001 — сломанный манифест не должен ронять окно
        return {}, f"Манифест не прочитался: {exc}"


def _write_manifest(dest: Path, data: dict) -> str:
    """Пишет манифест. Возвращает текст ошибки или пусто."""
    try:
        import opencode_caps  # noqa: PLC0415 — рядом лежит, круга нет

        opencode_caps.write_manifest(Path(dest), data)
        return ""
    except Exception as exc:  # noqa: BLE001
        return f"Манифест не записался: {exc}"


def install(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Включает auto-improve: проверяет окружение и пишет отметку в манифест.

    Живая проверка идёт ДО записи: нет скрипта, нет git, у Python нет
    requests или не вписан ключ судьи — в настройки не пишется ничего.
    В сами настройки opencode писать нечего: инструмент не MCP-сервер и не
    провайдер, он живёт своими файлами рядом с ними.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest = Path(dest)
    data = check(dest)
    if not data["ready"]:
        for line in data["missing"]:
            errors.append(line)
        errors.append("В настройки opencode ничего не вписано.")
        return messages, errors

    say(f"Проверка окружения прошла: {status_text(dest)}")
    manifest, bad_manifest = _manifest(dest)
    if bad_manifest:
        return messages, [bad_manifest]
    manifest.setdefault("files", {})
    manifest.setdefault("jsonc", [])
    manifest[MANIFEST_KEY] = {
        "script_blob": SCRIPT_BLOBS["improve.py"][:12],
        "source": SOURCE_AUTHOR,
        "key": str(data["key"]),
        "evaluator": DEFAULT_EVALUATOR,
    }
    bad = _write_manifest(dest, manifest)
    if bad:
        return messages, [bad]
    say("auto-improve включён: отметка в манифесте. Сам файл ключа и данные — рядом с настройками.")
    say(
        "Запускай цикл кнопкой «Улучшить файл…»: только она открывает "
        "репозиторий файла, а не настройки opencode."
    )
    return messages, errors


def remove(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Выключает auto-improve: снимает только нашу отметку в манифесте.

    Файл ключа, рубрики и журнал хода остаются на диске: программа не
    удаляет чужие файлы и чужие результаты.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest = Path(dest)
    manifest, bad_manifest = _manifest(dest)
    if bad_manifest:
        return messages, [bad_manifest]
    if MANIFEST_KEY not in manifest:
        say("auto-improve и так выключен — отметки в манифесте нет.")
        return messages, errors
    manifest.pop(MANIFEST_KEY)
    bad = _write_manifest(dest, manifest)
    if bad:
        return messages, [bad]
    say("auto-improve выключен: отметка снята.")
    say(f"Ключ ({KEY_NAME}) и папка данных ({DATA_NAME}) остались на диске — убрать их может только человек.")
    return messages, errors


def installed(dest: Path) -> bool:
    """Стоит ли отметка программы — для состояния на вкладке."""
    manifest, _bad = _manifest(dest)
    return bool(manifest.get(MANIFEST_KEY))


# ------------------------------------------------------------------ запуск


def find_repo(artifact: Path) -> Path | None:
    """Корень git-репозитория для файла. None — файл не в репозитории."""
    folder = Path(artifact).resolve().parent
    for candidate in (folder, *folder.parents):
        if (candidate / ".git").exists():
            return candidate
    return None


def tag_problem(tag: str) -> str:
    """Пусто — тег годится. Иначе причина, почему нет."""
    tag = (tag or "").strip()
    if not tag:
        return "не указан тег запуска: из него делается имя ветки improve/<тег>"
    if len(tag) > TAG_MAX:
        return f"тег длиннее {TAG_MAX} знаков: из него делается имя ветки git"
    bad = [ch for ch in tag if ch in TAG_BAD_CHARS]
    if bad:
        return (
            "в теге нельзя пробелы и знаки "
            + " ".join(repr(ch) for ch in sorted(set(bad)))
            + ": из тега делается имя ветки git"
        )
    if ".." in tag or "@{" in tag:
        return "в теге нельзя «..» и «@{»: так git ветку не назовёт"
    if tag.startswith((".", "-", "/")) or tag.endswith((".", "/")) or "//" in tag:
        return "тег не может начинаться с точки, дефиса или слеша и кончаться точкой или слешем"
    return ""


def build_command(
    python: str,
    artifact: Path,
    tag: str,
    criteria: Path | str = "",
    goal: str = "",
    max_iterations: int = DEFAULT_ITERATIONS,
    candidates: int = DEFAULT_CANDIDATES,
    threshold: int = DEFAULT_THRESHOLD,
    eval_runs: int = DEFAULT_EVAL_RUNS,
) -> list[str]:
    """Команда запуска по README автора. Ключ в команду не попадает."""
    cmd = [
        python, str(script_path()),
        "--artifact", str(artifact),
        "--tag", str(tag),
        "--max-iterations", str(int(max_iterations)),
        "--candidates", str(int(candidates)),
        "--threshold", str(int(threshold)),
        "--eval-runs", str(int(eval_runs)),
    ]
    if criteria:
        cmd += ["--criteria", str(criteria)]
    if goal:
        cmd += ["--goal", str(goal)]
    return cmd


def _environment(dest: Path) -> tuple[dict, str]:
    """Окружение процесса скрипта: ключ и папки — внутрь, не в команду."""
    value, _where = read_key(dest)
    env = dict(os.environ)
    if value:
        env["GEMINI_API_KEY"] = value
        env.pop("GOOGLE_API_KEY", None)
    env["RESULTS_DIR"] = str(results_dir(dest))
    env["IMPROVE_EVENTS_LOG"] = str(events_file(dest))
    env["IMPROVE_MUTATOR"] = os.environ.get("IMPROVE_MUTATOR", DEFAULT_MUTATOR)
    env["IMPROVE_EVALUATOR"] = os.environ.get("IMPROVE_EVALUATOR", DEFAULT_EVALUATOR)
    # Дочерний Python обязан печатать в UTF-8: иначе на Windows его stdout
    # в пайпе идёт в cp1251, и русские теги превращаются в «???» и у нас,
    # и в окне программы. Найдено живьём: history("нет-такого") возвращал
    # "No results for ..." с кракозябрами вместо тега.
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env, value


def launch(
    dest: Path,
    artifact: Path,
    tag: str,
    criteria: Path | str = "",
    goal: str = "",
    max_iterations: int = DEFAULT_ITERATIONS,
    candidates: int = DEFAULT_CANDIDATES,
    threshold: int = DEFAULT_THRESHOLD,
    eval_runs: int = DEFAULT_EVAL_RUNS,
    progress=None,
    command: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Запускает цикл улучшения и показывает его вывод построчно.

    Все проверки идут ДО запуска: нет скрипта, git, requests, ключа или
    файла — цикл не начинается, и ничего не запускается «наполовину».
    `command` — для самопроверки: подменить команду заглушкой. Ключ в неё
    не попадает ни при каком случае.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest = Path(dest)
    artifact = Path(artifact)
    data = check(dest)
    if not data["ready"]:
        for line in data["missing"]:
            errors.append(line)
        return messages, errors
    if not artifact.is_file():
        return [], [f"Файл для улучшения не найден: {artifact}"]
    repo = find_repo(artifact)
    if repo is None:
        return [], [
            "Файл не лежит в git-репозитории, а скрипт автора хранит улучшения "
            "коммитами в ветке improve/<тег>. Найди файл в репозитории или "
            "сделай в его папке `git init` и первый коммит.",
        ]
    problem = tag_problem(tag)
    if problem:
        return [], [problem]
    if not 1 <= int(max_iterations) <= MAX_ITERATIONS:
        return [], [f"Итераций должно быть от 1 до {MAX_ITERATIONS}."]

    criteria_path = Path(criteria) if criteria else ""
    if criteria_path and not criteria_path.is_file():
        return [], [f"Файл рубрики не найден: {criteria_path}"]

    env, key_value = _environment(dest)
    if not key_value:
        return [], ["Ключ судьи пропал между проверкой и запуском — повтори."]

    cmd = command or build_command(
        data["python"], artifact, tag, criteria=criteria_path, goal=goal,
        max_iterations=max_iterations, candidates=candidates,
        threshold=threshold, eval_runs=eval_runs,
    )
    folder = data_dir(dest)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return [], [f"Папка данных не создалась ({folder}): {exc}"]

    # Ключ никогда не попадает ни в команду, ни в журнал: если он вдруг
    # оказался в тексте команды, лучше честно отказать, чем засветить его.
    if key_value and any(key_value in part for part in cmd):
        return [], ["Команда содержит ключ — так нельзя, запуск отменён."]

    say(f"Репозиторий файла: {repo}")
    say(f"Ветка запуска: improve/{tag} (её создаёт сам скрипт).")
    say("Запускаю: " + " ".join(str(part) for part in cmd[:2]) + " …")

    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        log = open(log_file(dest), "a", encoding="utf-8", errors="replace")
    except OSError as exc:
        return [], [f"Журнал не открылся ({log_file(dest)}): {exc}"]
    with log:
        log.write(f"\n===== {stamp} | файл {artifact} | тег {tag} =====\n")
        log.flush()
        try:
            proc = subprocess.Popen(
                cmd, cwd=str(repo), env=env, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                errors="replace", bufsize=1,
            )
        except OSError as exc:
            return [], [f"Скрипт не запустился: {exc}"]
        assert proc.stdout is not None
        tail: list[str] = []
        for line in proc.stdout:
            clean = line.rstrip("\n")
            if key_value and key_value in clean:
                clean = clean.replace(key_value, "<ключ>")
            tail.append(clean)
            tail = tail[-40:]
            log.write(clean + "\n")
            log.flush()
            say(clean)
        code = proc.wait()
        log.write(f"===== конец: код {code} =====\n")

    if code == 0:
        say(f"Цикл завершён. Ход — кнопкой «Показать ход» (тег {tag}).")
        return messages, errors
    errors.append(f"Цикл прервался, код {code}. Последние строки:")
    errors += [f"  {line}" for line in tail[-6:]]
    errors.append(f"Полный вывод — в файле {log_file(dest)}.")
    return messages, errors


def history(dest: Path, tag: str, progress=None) -> tuple[list[str], list[str]]:
    """Показывает таблицу хода запуска — её печатает сам скрипт автора."""
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest = Path(dest)
    if not script_path().is_file():
        return [], ["Копии скрипта нет — показывать нечего."]
    problem = tag_problem(tag)
    if problem:
        return [], [problem]
    env, _key = _environment(dest)
    try:
        done = subprocess.run(
            [find_python(), str(script_path()), "--status", "--tag", str(tag)],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=STATUS_TIMEOUT, env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return [], [f"Ход не прочитался: {exc}"]
    out = (done.stdout or "").strip()
    if out:
        for line in out.splitlines():
            say(line)
    else:
        errors.append(f"Скрипт ничего не напечатал (код {done.returncode}): "
                      f"{(done.stderr or '').strip()[:200]}")
    return messages, errors


def seen_tags(dest: Path) -> list[str]:
    """Теги, у которых уже есть ход: по файлам `<тег>.tsv` в наших данных."""
    folder = results_dir(dest)
    if not folder.is_dir():
        return []
    files = sorted(folder.glob("*.tsv"), key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.stem for p in files]


# ------------------------------------------------------------------ командная строка


def _say(text: str) -> None:
    print(text, flush=True)


def _main(argv: list[str]) -> int:
    """Командная строка auto-improve: то же, что галочка и кнопки окна."""
    action = (argv[0] if argv else "status").lower()
    dest = find_settings_dir()
    if action in ("-h", "--help", "help"):
        _say("auto_improve.py status | check | install | remove | tags | history <тег>")
        _say("auto_improve.py run <файл> [тег] [рубрика] [цель]")
        _say("Переключатель — на вкладке opencode; здесь то же самое словами.")
        return 0
    if action == "status":
        _say(status_text(dest))
        _say("Отметка установки: " + ("стоит" if installed(dest) else "не стоит") + ".")
        return 0
    if action == "check":
        data = check(dest)
        _say(status_text(dest))
        return 0 if data["ready"] else 1
    if action == "install":
        messages, errors = install(dest)
    elif action == "remove":
        messages, errors = remove(dest)
    elif action == "tags":
        tags = seen_tags(dest)
        _say("Теги с ходом: " + (", ".join(tags) if tags else "нет"))
        return 0
    elif action == "history":
        if len(argv) < 2:
            _say("Нужен тег: auto_improve.py history <тег>")
            return 2
        messages, errors = history(dest, argv[1])
    elif action == "run":
        if len(argv) < 2:
            _say("Нужен файл: auto_improve.py run <файл> [тег] [рубрика] [цель]")
            return 2
        artifact = Path(argv[1])
        tag = argv[2] if len(argv) > 2 else artifact.stem
        criteria = argv[3] if len(argv) > 3 else ""
        goal = argv[4] if len(argv) > 4 else ""
        messages, errors = launch(dest, artifact, tag, criteria=criteria, goal=goal)
    else:
        _say(f"Не знаю такой команды: {action}")
        _say("auto_improve.py status | check | install | remove | tags | history | run")
        return 2
    for line in messages:
        _say(line)
    for line in errors:
        _say("Ошибка: " + line)
    return 1 if errors else 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
