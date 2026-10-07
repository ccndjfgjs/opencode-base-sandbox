"""greenlight — проверка iOS-приложения перед App Store (чужая программа Revyl).

Что это. Сторонний консольный сканер (автор — Revyl, лицензия MIT): он читает
исходники, файлы приватности, Info.plist и собранный IPA и сверяет их с
правилами Apple App Store Review Guidelines. Каждая находка ссылается на
правило и предлагает исправление. Уровни: CRITICAL, HIGH, WARN, INFO. По
README автора основной сканер работает офлайн, без аккаунта и ничего никуда
не отправляет. Неофлайновые команды у автора две, и обе честно названы:
`verify` без `--dry-run` (облако Revyl) и `scan --app-id` (App Store Connect).

Где что лежит:

    tools/thirdparty/greenlight/            исходники автора (67 файлов, копия
                                            байт в байт, сверка — СВЕРКА-БАЙТОВ.txt)
    tools/thirdparty/greenlight/build/      собранный бинарник (в .gitignore:
                                            его собирает человек, у которого есть Go)
    <папка настроек opencode>/greenlight-data/   журнал и машинные отчёты

Это не MCP и не экономия токенов: greenlight — обычная программа на Go, в
`mcp-registry.json` её нет и быть не должно, моделей внутри нет вовсе. Сам
opencode-base (PyQt6, Windows) в ней не нуждается — опция для тех, кто делает
приложения для iOS.

Как запускается (ключи — из README автора и `--help` самой программы):

    greenlight preflight <папка проекта>            основная проверка, офлайн
    greenlight verify <папка> --dry-run             список проверок, офлайн
    greenlight verify <папка> --build-name <имя>    облако Revyl, по подтверждению

Чего программа не делает: не ставит Go и не собирает greenlight за человека
без его команды (кнопка «Собрать greenlight» — отдельное действие), не
запускает `scan --app-id` и `auth` (это App Store Connect с аккаунтом), не
подставляет имя сборки для облака и не отправляет в облако Revyl ничего без
явного подтверждения.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
# Под pythonw (оконный Python, без консоли) sys.stdout равен None:
# .reconfigure() на нём роняет импорт модуля, а модуль импортирует
# main.py при старте — программа не открывалась совсем. Найдено живьём.
for _stream in (sys.stdout, sys.stderr):
    if _stream is not None:
        try:
            _stream.reconfigure(encoding='utf-8', errors='replace')
        except (AttributeError, ValueError, OSError):
            pass

#: Откуда взята программа и по какому коммиту сверялись её байты.
SOURCE = "https://github.com/RevylAI/greenlight"
SOURCE_AUTHOR = "Revyl"
SOURCE_COMMIT = "fcb36e395bd4ae20448f2535382baecba9396fd9"
SOURCE_NOTE = "коммит от 05.08.2026, версия 0.2.0"
LICENSE = "MIT"

#: Где лежит копия автора, как называются собранный бинарник и наши данные.
VENDOR_REL = ("tools", "thirdparty", "greenlight")
BLOBS_NAME = "СВЕРКА-БАЙТОВ.txt"
BINARY_NAME = "greenlight"
BUILD_DIR = "build"
DATA_NAME = "greenlight-data"
MANIFEST_KEY = "greenlight"

#: Установка словами автора (README), на случай, когда Go нет вовсе.
INSTALL_HINT = (
    "по README автора greenlight ставится так: `brew install revylai/tap/greenlight` "
    "(только macOS), `go install github.com/RevylAI/greenlight/cmd/greenlight@latest` "
    "или сборкой из исходников — `make build`, бинарник появится в build/greenlight"
)

#: Пояснения, которые видно и в окне, и в журнале. Ничего не выдумано: это
#: прямые слова README автора и его же документации к командам.
OFFLINE_NOTE = (
    "Основная проверка (preflight, а также codescan, privacy, playscan, ipa и "
    "guidelines) идёт офлайн: сканер читает файлы проекта и наружу ничего не "
    "отправляет. Сеть нужны только двум командам самого автора: verify без "
    "«--dry-run» (облако Revyl) и scan --app-id (App Store Connect)."
)
CLOUD_WARNING = (
    "«Проверка в облаке Revyl» — единственная часть greenlight, которая выходит "
    "наружу: команда verify отдаёт проверяемые сценарии во внешний сервис Revyl, "
    "для неё нужны установленный CLI revyl и бесплатный аккаунт Revyl. Всё "
    "остальное в greenlight работает офлайн. Продолжать?"
)
NO_GO_NOTE = (
    "Go не установлен — собрать greenlight из исходников нечем. Нужен Go 1.24 "
    "или новее (как в go.mod автора). Правило из инструкции: проверить Go и make "
    "и сказать прямо, если их нет, — вот это оно и есть."
)

#: Возврат «есть находки уровня CRITICAL/HIGH» при --exit-code: у самого
#: сканера это код 1 без сообщения, поэтому --exit-code мы не передаём, а
#: судим по отчёту.
EXIT_CODE_NOTE = "--exit-code в программе не включается: она показывает отчёт как есть."


# ------------------------------------------------------------------ пути

def program_root() -> Path:
    """Папка программы (в ней же лежит tools/thirdparty)."""
    import core  # noqa: PLC0415 — рядом лежит, круга нет

    return core.program_root()


def vendor_dir() -> Path:
    """Копия исходников автора в базе."""
    return program_root().joinpath(*VENDOR_REL)


def binary_name() -> str:
    """Имя собранного бинарника на этой системе."""
    return BINARY_NAME + ".exe" if os.name == "nt" else BINARY_NAME


def binary_path() -> Path:
    """Куда ложится наша сборка."""
    return vendor_dir() / BUILD_DIR / binary_name()


def find_binary() -> tuple[Path | None, str]:
    """Готовый greenlight: сперва из PATH, потом своя сборка.

    Первое значение — путь (или None), второе — откуда он: «PATH» или «сборка».
    """
    for name in (BINARY_NAME, BINARY_NAME + ".exe"):
        found = shutil.which(name)
        if found:
            return Path(found), "PATH"
    ours = binary_path()
    if ours.is_file():
        return ours, "сборка"
    return None, ""


def find_go() -> str | None:
    """Go в PATH — без него сборка из исходников невозможна."""
    return shutil.which("go")


def find_make() -> str | None:
    """make в PATH — по README автора им собирается цель build."""
    return shutil.which("make")


def go_version(go: str | None = None) -> str:
    """Строка версии Go — или пусто, если его нет."""
    go = go or find_go()
    if not go:
        return ""
    try:
        done = subprocess.run(
            [go, "version"], capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or done.stderr).strip().splitlines()[0] if (done.stdout or done.stderr) else ""


def make_version(make: str | None = None) -> str:
    """Первая строка версии make — или пусто, если его нет."""
    make = make or find_make()
    if not make:
        return ""
    try:
        done = subprocess.run(
            [make, "--version"], capture_output=True, text=True, timeout=60,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    text = (done.stdout or done.stderr).strip()
    return text.splitlines()[0] if text else ""


def binary_works(binary: Path | None = None) -> tuple[bool, str]:
    """Отвечает ли бинарник на `--version`. Живая проверка до записи."""
    binary = binary or find_binary()[0]
    if binary is None:
        return False, "бинарник не найден"
    if not Path(binary).is_file():
        return False, f"файла нет: {binary}"
    try:
        done = subprocess.run(
            [str(binary), "--version"], capture_output=True, text=True, timeout=60,
        )
    except OSError as exc:
        return False, f"не запустился: {exc}"
    text = (done.stdout or done.stderr).strip().splitlines()
    if done.returncode != 0 or not text:
        return False, f"ответил кодом {done.returncode}"
    return True, text[0]


def data_dir(dest: Path) -> Path:
    """Наши данные рядом с настройками: журнал и машинные отчёты."""
    return Path(dest) / DATA_NAME


def reports_dir(dest: Path) -> Path:
    """Машинные отчёты (JSON) — их пишет сам сканер, мы только задаём путь."""
    return data_dir(dest) / "отчёты"


def log_file(dest: Path) -> Path:
    """Вывод прогонов: то же, что видно в окне, но остаётся на диске."""
    return data_dir(dest) / "greenlight.log"


def reports_seen(dest: Path) -> list[Path]:
    """Сколько машинных отчётов уже лежит. Не заглядывая внутрь."""
    folder = reports_dir(dest)
    if not folder.is_dir():
        return []
    return sorted(folder.glob("*.json"))


# ------------------------------------------------------------------ сверка копии

def vendor_blobs() -> dict[str, str]:
    """Слепки файлов автора из нашего списка сверки. Пусто — списка нет."""
    path = vendor_dir() / BLOBS_NAME
    if not path.is_file():
        return {}
    marks: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        rel, _, sha = line.partition(" ")
        if rel and sha:
            marks[rel] = sha
    return marks


def sources_check() -> tuple[bool, int, list[str]]:
    """Копия автора на месте: (всё сходится, сколько файлов, расхождения)."""
    marks = vendor_blobs()
    if not marks:
        return False, 0, [f"нет файла сверки {BLOBS_NAME} — копия не проверена"]
    bad: list[str] = []
    good = 0
    for rel, want in marks.items():
        path = vendor_dir() / rel
        if not path.is_file():
            bad.append(f"нет файла {rel}")
            continue
        data = path.read_bytes().replace(b"\r\n", b"\n")
        sha = hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()
        if sha == want:
            good += 1
        else:
            bad.append(f"{rel} отличается от автора")
    return (not bad), good, bad


# ------------------------------------------------------------------ состояние

def check(dest: Path) -> dict:
    """Что есть для проверки iOS-приложения. Ничего не меняет."""
    dest = Path(dest)
    binary, where = find_binary()
    works, version = binary_works(binary) if binary else (False, "")
    sources_ok, sources_good, sources_bad = sources_check()
    go = find_go()
    make = find_make()
    manifest, _bad = _manifest(dest)
    marked = bool(manifest.get(MANIFEST_KEY))

    missing: list[str] = []
    if binary is None:
        if go:
            missing.append(
                "greenlight не собран — нажми «Собрать greenlight» (Go найден, "
                "сборка пройдёт), или " + INSTALL_HINT
            )
        else:
            missing.append(
                "greenlight не собран, и Go нет: " + NO_GO_NOTE + " (" + INSTALL_HINT + ")"
            )
    elif not works:
        missing.append(f"greenlight нашёлся ({binary}), но не отвечает: {version}")
    if not sources_ok:
        missing.append("копия исходников автора не сходится: " + "; ".join(sources_bad[:5]))

    return {
        "binary": binary,
        "binary_where": where,
        "binary_ok": works,
        "version": version,
        "sources_ok": sources_ok,
        "sources_files": sources_good,
        "sources_bad": sources_bad,
        "go": go or "",
        "go_version": go_version(go),
        "make": make or "",
        "make_version": make_version(make),
        "marked": marked,
        "reports": reports_seen(dest),
        "ready": not missing,
        "missing": missing,
    }


def status_text(dest: Path) -> str:
    """Строка состояния словами — для окна и командной строки.

    Про Go и make говорится прямо: инструкция требует не молчать, если их нет.
    """
    dest = Path(dest)
    data = check(dest)
    parts: list[str] = []
    if data["binary"] and data["binary_ok"]:
        parts.append(f"greenlight есть ({data['binary_where']}: {data['binary']}), {data['version']}")
    elif data["binary"]:
        parts.append(f"greenlight есть, но не отвечает: {data['version']}")
    else:
        parts.append("greenlight не собран")
    parts.append(f"Go: {data['go_version']}" if data["go"] else "Go не найден")
    parts.append(f"make: {data['make_version']}" if data["make"] else "make не найден")
    if data["sources_ok"]:
        parts.append(f"копия автора сходится ({data['sources_files']} файлов)")
    else:
        parts.append("копия автора не проверена")
    if data["reports"]:
        parts.append(f"отчётов на диске: {len(data['reports'])}")
    text = "; ".join(parts) + "."
    if data["ready"]:
        return "Готово: " + text
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
    """Включает greenlight: живая проверка до записи, потом отметка.

    В настройки opencode писать нечего: greenlight ничего в них не добавляет
    (он не MCP-сервер и не провайдер). Поэтому установка — это рабочая копия
    плюс наша отметка; при отказе отметки не появляется вовсе.
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
        return messages, list(data["missing"])

    manifest, bad_manifest = _manifest(dest)
    if bad_manifest:
        return messages, [bad_manifest]
    manifest[MANIFEST_KEY] = {
        "source": "RevylAI",
        "commit": SOURCE_COMMIT,
        "binary": data["binary_where"],
        "version": data["version"],
    }
    bad = _write_manifest(dest, manifest)
    if bad:
        return messages, [bad]
    say(f"greenlight включён: {data['version']} ({data['binary']}).")
    say("В настройки opencode программа ничего не пишет: greenlight — не MCP-сервер и не провайдер.")
    say(OFFLINE_NOTE)
    return messages, errors


def remove(dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Выключает greenlight: снимается только наша отметка в манифесте."""
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
        say("greenlight и так выключен — отметки в манифесте нет.")
        return messages, errors
    manifest.pop(MANIFEST_KEY)
    bad = _write_manifest(dest, manifest)
    if bad:
        return messages, [bad]
    say("greenlight выключен: отметка снята.")
    say(
        f"Журнал и отчёты ({DATA_NAME}) остались на диске, собранный бинарник — тоже: "
        "их убирает человек, если захочет."
    )
    return messages, errors


def installed(dest: Path) -> bool:
    """Стоит ли отметка программы — для состояния на вкладке."""
    manifest, _bad = _manifest(dest)
    return bool(manifest.get(MANIFEST_KEY))


# ------------------------------------------------------------------ запуск

def build_command(dest: Path | None = None) -> list[str]:
    """Команда сборки: make build, а без make — то же самое напрямую.

    `make build` — команда из README автора; прямая `go build -o build/greenlight
    ./cmd/greenlight` — ровно то, что делает цель build в его Makefile.
    """
    folder = vendor_dir()
    target = binary_path()
    make = find_make()
    if make:
        return [make, "build"]
    go = find_go()
    if not go:
        return []
    return [go, "build", "-o", str(target), "./cmd/greenlight"]


def build(dest: Path, progress=None, command: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Собирает greenlight из исходников автора в tools/thirdparty/greenlight/build.

    Сборка — действие человека: Go или make программа не ставит. Если их нет,
    отказ говорит об этом прямым текстом (инструкция требует говорить прямо).
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest = Path(dest)
    sources_ok, good, bad = sources_check()
    if not vendor_dir().is_dir():
        return messages, [f"Нет копии исходников автора: {vendor_dir()}"]
    if not sources_ok:
        return messages, ["Копия исходников не сходится с автором: " + "; ".join(bad[:5])]

    # Без Go сборка бессмысленна даже при живом make: `make build` — это
    # обёртка над `go build`, и без Go она падает «go: not found». Поэтому
    # отказываем прямо и до запуска, а не показываем человеку чужую ошибку.
    go = find_go()
    make = find_make()
    if not go:
        return messages, [NO_GO_NOTE]
    cmd = command or build_command()
    if not cmd:
        return messages, [NO_GO_NOTE]
    say(f"Собираю из исходников автора ({good} файлов, сверка сошлась).")
    if command is None:
        if make:
            say(f"make найден ({make_version(make)}), команда — `make build` из README автора.")
        else:
            say(
                "make не найден, поэтому собираю напрямую: `go build -o build/greenlight "
                "./cmd/greenlight` — это ровно то, что делает цель build в Makefile автора."
            )

    log = _open_log(dest)
    tail: list[str] = []
    code = -1
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(vendor_dir()),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\n")
            tail.append(clean)
            tail = tail[-40:]
            if log:
                log.write(clean + "\n")
                log.flush()
            say(clean)
        code = proc.wait()
    except OSError as exc:
        return messages, [f"Сборка не запустилась: {exc}"]
    finally:
        if log:
            log.write(f"===== сборка: код {code} =====\n")
            log.close()

    if code != 0:
        errors.append(f"Сборка прервалась, код {code}. Последние строки:")
        errors += [f"  {line}" for line in tail[-6:]]
        errors.append(f"Полный вывод — в файле {log_file(dest)}.")
        return messages, errors

    ok, version = binary_works()
    if not ok:
        return messages, [f"Сборка прошла, но бинарник не отвечает: {version}"]
    say(f"Собрано: {version} ({binary_path()}).")
    return messages, errors


def scan(
    dest: Path,
    project: Path | str,
    ipa: Path | str = "",
    progress=None,
    command: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Основная проверка: `greenlight preflight <папка проекта>` (офлайн).

    Дополнительно сохраняется машинный отчёт (JSON) — его пишет сам сканер,
    а мы только задаём путь рядом с нашими данными. В окно идёт вывод сканера
    как есть, без переписывания.
    """
    dest = Path(dest)
    project = Path(project)
    if not project.is_dir():
        return [], [f"Папки проекта нет: {project}"]
    ipa_path = Path(ipa) if ipa else None
    if ipa_path and not ipa_path.is_file():
        return [], [f"Файла .ipa нет: {ipa_path}"]

    cmd = command or build_scan_command(project, ipa_path)
    lines, errors, _tail = _run(dest, cmd, f"проверка {project}", project, progress)
    if errors:
        return lines, errors

    report = _write_report(dest, project, ipa_path)
    if report:
        lines.append(f"Машинный отчёт: {report}")
    else:
        lines.append("Машинный отчёт не сохранён — смотри журнал.")
    return lines, errors


def build_scan_command(project: Path, ipa: Path | None = None) -> list[str]:
    """Команда основного прогона — ключи из README автора."""
    binary = _require_binary()
    cmd = [str(binary), "preflight", str(project)]
    if ipa:
        cmd += ["--ipa", str(ipa)]
    return cmd


def verify_dry(dest: Path, project: Path | str, progress=None,
               command: list[str] | None = None) -> tuple[list[str], list[str]]:
    """Список проверок без облака: `verify --dry-run` (офлайн, без устройства)."""
    project = Path(project)
    if not project.is_dir():
        return [], [f"Папки проекта нет: {project}"]
    cmd = command or (_require_cmd("verify", str(project), "--dry-run"))
    lines, errors, _tail = _run(
        Path(dest), cmd, f"список проверок {project}", project, progress,
    )
    if not errors:
        lines.append("Это был сухой прогон: ни устройства, ни облака, ни аккаунта.")
    return lines, errors


def verify_cloud(
    dest: Path,
    project: Path | str,
    build_name: str = "",
    confirm: bool = False,
    progress=None,
    command: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Проверка в облаке Revyl: только по явному подтверждению (confirm=True).

    Без подтверждения не запускается вовсе — это единственная часть greenlight,
    которая отправляет данные наружу.
    """
    if not confirm:
        return [], [CLOUD_WARNING + " (Программа не запускает облако без подтверждения.)"]
    project = Path(project)
    if not project.is_dir():
        return [], [f"Папки проекта нет: {project}"]
    name = (build_name or "").strip()
    if not name:
        return [], [
            "Не указано имя сборки: команда verify у автора запускается с "
            "`--build-name <имя>` (имя приложения или сборки в Revyl).",
        ]
    cmd = command or _require_cmd("verify", str(project), "--build-name", name)
    lines, errors, _tail = _run(
        Path(dest), cmd, f"облачная проверка {project}", project, progress,
    )
    lines.insert(0, CLOUD_WARNING)
    return lines, errors


def _require_binary() -> Path:
    """Бинарник или исключение: без него запускать нечего."""
    binary, _where = find_binary()
    if binary is None:
        raise FileNotFoundError("greenlight не собран и не найден в PATH")
    ok, note = binary_works(binary)
    if not ok:
        raise FileNotFoundError(f"greenlight не отвечает: {note}")
    return binary


def _require_cmd(*args: str) -> list[str]:
    """Команда запуска: [бинарник, аргументы…]. Без бинарника — исключение."""
    return [str(_require_binary()), *args]


def _write_report(dest: Path, project: Path, ipa: Path | None = None) -> Path | None:
    """Машинный отчёт: тихий прогон `--format json --output <файл>`.

    Сканер уже отработал для человека; этот прогон — для машины (миллисекунды).
    Неудача отчёта не отменяет проверку: она просто не сохраняется.
    """
    try:
        binary = _require_binary()
    except FileNotFoundError:
        return None
    folder = reports_dir(dest)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    stamp = time.strftime("%Y-%m-%d_%H-%M-%S")
    name = project.resolve().name or "проект"
    target = folder / f"{stamp}_{name}.json"
    cmd = [str(binary), "preflight", str(project), "--format", "json", "--output", str(target)]
    if ipa:
        cmd += ["--ipa", str(ipa)]
    try:
        done = subprocess.run(
            cmd, cwd=str(project), capture_output=True, text=True, timeout=600,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return target if target.is_file() else None


def _open_log(dest: Path, title: str = ""):
    """Журнал прогонов. None — открыть не удалось (это не смертельно)."""
    folder = data_dir(dest)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        log = open(log_file(dest), "a", encoding="utf-8", errors="replace")
    except OSError:
        return None
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    log.write(f"\n===== {stamp}{' | ' + title if title else ''} =====\n")
    log.flush()
    return log


def _run(dest: Path, cmd: list[str], title: str, cwd: Path, progress=None):
    """Общий запуск бинарника: вывод идёт в окно и в журнал построчно.

    Возвращаются (строки, ошибки, хвост). Код возврата сканера для preflight
    без --exit-code — 0; всё остальное (кроме явно несуществующего пути,
    который мы проверяем заранее) — настоящая ошибка.
    """
    lines: list[str] = []
    errors: list[str] = []
    tail: list[str] = []

    def say(text: str) -> None:
        lines.append(text)
        if progress:
            progress(text)

    if not cmd:
        return lines, ["Команда не собралась: нет бинарника greenlight."], tail
    say("Запускаю: " + " ".join(str(part) for part in cmd[:3]) + " …")
    log = _open_log(dest, title)
    if log:
        log.write("$ " + " ".join(str(part) for part in cmd) + "\n")
    code = -1
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", bufsize=1,
        )
        assert proc.stdout is not None
        for line in proc.stdout:
            clean = line.rstrip("\n")
            tail.append(clean)
            tail = tail[-40:]
            if log:
                log.write(clean + "\n")
                log.flush()
            say(clean)
        code = proc.wait()
    except OSError as exc:
        if log:
            log.close()
        return lines, [f"greenlight не запустился: {exc}"], tail
    if log:
        log.write(f"===== код {code} =====\n")
        log.close()
    if code != 0:
        errors.append(f"greenlight ответил кодом {code}. Последние строки:")
        errors += [f"  {line}" for line in tail[-6:]]
        errors.append(f"Полный вывод — в файле {log_file(dest)}.")
    return lines, errors, tail


# ------------------------------------------------------------------ командная строка

def _say(text: str) -> None:
    print(text)


def _main(argv: list[str]) -> int:
    """Командная строка — то же, что кнопки в окне."""
    import argparse  # noqa: PLC0415 — нужен только здесь

    parser = argparse.ArgumentParser(
        description="greenlight — проверка iOS-приложения перед App Store "
                    "(чужая программа Revyl, копия в tools/thirdparty/greenlight).",
    )
    sub = parser.add_subparsers(dest="what", required=True)

    sub.add_parser("status", help="что готово и чего не хватает")
    sub.add_parser("check", help="то же, что status")
    sub.add_parser("install", help="включить отметку в манифесте")
    sub.add_parser("remove", help="снять отметку")
    sub.add_parser("build", help="собрать из исходников автора")

    p_scan = sub.add_parser("scan", help="проверить iOS-приложение (офлайн)")
    p_scan.add_argument("project", help="папка проекта")
    p_scan.add_argument("--ipa", default="", help="файл .ipa (необязательно)")

    p_dry = sub.add_parser("dry", help="список проверок без облака")
    p_dry.add_argument("project", help="папка проекта")

    p_ver = sub.add_parser("verify", help="проверка в облаке Revyl (нужно подтверждение)")
    p_ver.add_argument("project", help="папка проекта")
    p_ver.add_argument("--build-name", default="", help="имя сборки в Revyl")
    p_ver.add_argument(
        "--yes", action="store_true",
        help="подтверждение: данные уйдут во внешний сервис Revyl",
    )

    args = parser.parse_args(argv)
    dest = None
    for env in ("OPENCODE_CONFIG_DIR", "XDG_CONFIG_HOME"):
        raw = os.environ.get(env)
        if raw:
            candidate = Path(raw)
            if candidate.name == ".config":
                candidate = candidate / "opencode"
            dest = candidate
            break
    if dest is None:
        dest = Path.home() / ".config" / "opencode"

    if args.what in ("status", "check"):
        _say(status_text(dest))
        return 0 if check(dest)["ready"] else 1
    if args.what == "install":
        messages, errors = install(dest, progress=_say)
        return 1 if errors else 0
    if args.what == "remove":
        messages, errors = remove(dest, progress=_say)
        return 1 if errors else 0
    if args.what == "build":
        messages, errors = build(dest, progress=_say)
        return 1 if errors else 0
    if args.what == "scan":
        messages, errors = scan(dest, args.project, ipa=args.ipa, progress=_say)
        return 1 if errors else 0
    if args.what == "dry":
        messages, errors = verify_dry(dest, args.project, progress=_say)
        return 1 if errors else 0
    if args.what == "verify":
        messages, errors = verify_cloud(
            dest, args.project, args.build_name, confirm=args.yes, progress=_say,
        )
        return 1 if errors else 0
    return 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
