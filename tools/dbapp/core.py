"""Работа с базой: поиск программ, создание новой базы, подключение.

Модуль не зависит от окон — его можно проверить отдельно, без запуска
графического интерфейса. Всё, что делает окно, сводится к вызовам отсюда.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import webbrowser
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------- программы

SKILL_MARKER = "SKILL.md"

#: Файл в папке skills, где записано, что именно мы ставили и каким был
#: каждый файл. По нему видно, менял ли человек навык руками: если файлы
#: совпадают с записью, навык можно обновить, если нет — нельзя.
SKILL_MANIFEST = ".installed.json"


# Как программа относится к файлам базы. Состояний три, а не два, потому
# что «не читает» и «мы не проверяли» — разные вещи. Обещать первое вместо
# второго нельзя: человек будет искать базу там, где её в принципе нет.
READS_YES = "yes"  # проверено: файлы с диска читает
READS_NO = "no"  # проверено: файлы с диска не читает
READS_UNKNOWN = "unknown"  # не проверяли


@dataclass
class Program:
    """Программа-клиент, в которую можно подключить базу."""

    ident: str
    title: str
    hint: str
    supports_skills: bool = True
    #: Читает ли программа файлы с диска.
    reads_state: str = READS_UNKNOWN
    #: Через что программа получает базу. Видно, откуда берётся память,
    #: а не только «куда положили».
    reads_via: str = ""
    #: куда класть базу, если стандартной папки ещё нет
    fallback: str = ""
    #: Места, по которым программа только опознаётся: файлы туда не кладём.
    #: Это папки установки приложений — писать в них нельзя.
    detect_only: tuple[str, ...] = ()

    def config_dir(self) -> Path:
        """Папка настроек программы. Сначала ищем существующую."""
        for candidate in self._candidates():
            if candidate.is_dir():
                return candidate
        return self._fallback_path()

    def _fallback_path(self) -> Path:
        return expand(self.fallback)

    def _candidates(self) -> list[Path]:
        """Где у программы лежат настройки.

        Пути ищутся от домашней папки и переменных среды, поэтому годится
        на любом компьютере: имя пользователя нигде не вписано. Порядок
        внутри списка — от самого вероятного места к редкому.
        """
        ident = self.ident
        home = Path.home()
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        table: dict[str, list[Path]] = {
            "opencode": [
                home / ".config/opencode",
                appdata / "opencode",
                local / "opencode",
            ],
            "harness": [dsh_home()],
        }
        return table.get(ident, [])

    def _detect_only_places(self) -> list[Path]:
        """Места, по которым программа опознаётся, но куда мы не пишем."""
        return [expand(place) for place in self.detect_only]

    def ability_note(self) -> str:
        """Честное пояснение: увидит ли программа базу и через что."""
        if self.reads_state == READS_YES:
            if self.reads_via:
                return f"Базу увидит: {self.reads_via}."
            return "Базу увидит."
        if self.reads_state == READS_NO:
            return "Базу не увидит: файлы с диска не читает."
        return "Не проверяли, читает ли эта программа файлы с диска."

    def can_attach(self) -> bool:
        """Можно ли вообще подключать базу к этой программе.

        Отказ только один — когда проверено, что файлы программа не читает.
        «Не проверяли» запретом не считается: пусть человек попробует.
        """
        return self.reads_state != READS_NO

    def command(self) -> str | None:
        """Путь к файлу-команде программы, если она есть в системе."""
        names = {
            "opencode": ["opencode.cmd", "opencode.exe"],
            "harness": ["dsh.cmd", "dsh.exe"],
        }.get(self.ident, [])
        for name in names:
            found = shutil.which(name)
            if found:
                return found
        return None

    def is_installed(self) -> bool:
        if self.config_dir().is_dir() or self.command() is not None:
            return True
        return any(place.is_dir() for place in self._detect_only_places())


def dsh_home() -> Path:
    """Домашняя папка Harness: переменная DSH_HOME, иначе ~/.dsh."""
    env = os.environ.get("DSH_HOME", "").strip().strip('"')
    if env:
        return Path(env)
    return Path.home() / ".dsh"


# Список программ, которые программа управления ищет сама. Пути нигде не
# вписаны жёстко: всё считается от домашней папки и переменных среды, поэтому
# база с этим файлом годится для любого компьютера и любого имени пользователя.
#
# Порядок не случаен: сперва те, что базу действительно читают, затем те,
# про которые мы не проверяли, и в конце — те, что не читают вовсе.
# Про каждую сказано честно, что именно она увидит.
# Программа работает только с двумя: OpenCode и Harness (модификация
# пользователя). Сторонних программ в списке нет по решению пользователя
# (сентябрь 2026). Копия прежнего списка — в бэкапе базы.
PROGRAMS: list[Program] = [
    Program(
        "opencode",
        "OpenCode",
        "основная: файлы-навигаторы, плагин памяти и навыки",
        reads_state=READS_YES,
        reads_via="файлы AGENTS.md и opencode.jsonc, плагин памяти и навыки",
        fallback="~/.config/opencode",
        detect_only=(
            "%LOCALAPPDATA%/Programs/@opencode-aidesktop",
            "~/.local/share/opencode",
        ),
    ),
    Program(
        "harness",
        "Harness",
        "твоя модификация: память через AGENTS.md, навыки и мост NCP",
        reads_state=READS_YES,
        reads_via="файл AGENTS.md, папка skills и мост NCP",
        fallback="~/.dsh",
    ),
]

PROGRAMS_BY_ID = {p.ident: p for p in PROGRAMS}


def expand(value: str) -> Path:
    """Разворачивает ~ и переменные среды в путь.

    Тильда разворачивается только в самом начале пути. Внутри пути она
    остаётся как есть: Windows выдаёт короткие имена вида USER_NA~1, и
    заменять там тильду нельзя — путь превратится в мусор.
    """
    text = str(value)
    if text == "~":
        text = str(Path.home())
    elif text.startswith(("~/", "~\\")):
        text = str(Path.home()) + text[1:]
    text = os.path.expandvars(text)
    return Path(text)


# ---------------------------------------------------------------- плагин OpenCode

# Файлы конфигурации, которые нужно положить в папку настроек OpenCode,
# иначе он базу не увидит. Ровно этот набор раскладывает батник База.bat.
CONFIG_FILES = ("opencode.jsonc", "AGENTS.md", "package.json", "package-lock.json")
CONFIG_DIRS = ("plugins", "command")

# Сам плагин памяти — «мозг», который читает базу и подхватывает скиллы.
PLUGIN_REL = ("plugins", "memory-base.js")

# Готовые зависимости плагина. Лежат рядом с конфигом в базе, поэтому
# интернет для установки плагина не нужен.
DEPS_MODULE = ("node_modules", "@opencode-ai", "plugin", "package.json")

# Настройки, в которые вписываются пути к файлам базы.
# Это и есть механизм авто-подключения базы к каждой сессии OpenCode.
#
# Файлы-навигаторы здесь не случайно: без них нейросеть видит только
# профиль, проекты и факты — и не знает ни устройства базы, ни правил
# поведения в ней. Проверено 16.09.2026: правила лежали в базе, но
# в настройках их не было, поэтому до нейросети они не доходили.
INSTRUCTIONS_FILE = "opencode.jsonc"

# Пометка в файлах настроек, вместо которой подставляется настоящий путь
# базы. Благодаря ей файлы в базе годятся для любого компьютера: имя
# пользователя и папка установки в них не прописаны.
BASE_PLACEHOLDER = "{{BASE}}"

# Пометка рабочего стола. Нужна файлам команд: команда создания проекта
# заводит папку проекта рядом с базой, на рабочем столе. Без пометки
# в файле остался бы рабочий стол того, кто писал команду.
DESKTOP_PLACEHOLDER = "{{DESKTOP}}"

INSTRUCTION_TARGETS = (
    "profile.md",
    "projects.md",
    "facts.md",
    "библиотека/АКТИВНАЯ-ПАМЯТЬ.md",
    "КАРТА-БАЗЫ.md",
    "ПРАВИЛА-ИИ.md",
    # Как выбирать скилл и агента и что делать, если результат не устроил.
    # Файл лежит в инструкции/, откуда он и едет в каждую новую базу.
    "инструкции/Скиллы-и-агенты.md",
    # Реестр MCP-серверов: что есть, что нужно поставить, что не работает.
    "инструкции/МCP-серверы.md",
)

#: Где программа хранит файлы, которые копируются в корень новой базы.
#:
#: Раздел 16 плана разбирает корень программы по папкам: документы в
#: `документы/`, реестры в `данные/`. Имена файлов **в созданной базе**
#: при этом прежние — `КАРТА-БАЗЫ.md` лежит в корне базы и должен лежать
#: там же, иначе `config/opencode.jsonc` начнёт указывать в пустоту.
#: Поэтому у программы свой путь, у базы свой, и путать их нельзя.
PROGRAM_FILES = {
    "ОБРАЗЕЦ-БАЗЫ.md": ("документы",),
    "КАРТА-БАЗЫ.md": ("документы",),
    "ПРАВИЛА-ИИ.md": ("документы",),
    "THIRD-PARTY-NOTICES.md": ("документы",),
    "mcp-registry.json": ("данные",),
    "skills-index.json": ("данные",),
}


def program_file(name: str, root: Path | None = None) -> Path:
    """Путь к файлу программы с учётом разобранного корня.

    Сначала новое место, потом корень как запасной. Обратная
    совместимость здесь не для красоты: программа обязана работать и с
    папкой, где перенос ещё не сделан. Иначе проверки в тестах, где
    образец программы собирается на лету, сломались бы молча — а это
    ровно тот класс поломок, который §16.6 называет главной опасностью.

    Если нет ни одного из двух, возвращается новый путь: чтобы ошибка
    называла то место, где файл должен лежать.
    """
    base = Path(root) if root is not None else program_root()
    parts = PROGRAM_FILES.get(name)
    if parts:
        nested = base.joinpath(*parts, name)
        if nested.is_file():
            return nested
    flat = base / name
    if flat.is_file():
        return flat
    return base.joinpath(*parts, name) if parts else flat


def find_config_source(base: Path) -> Path | None:
    """Ищет папку config с плагином. Возвращает None, если её нет."""
    for candidate in (base / "config", base):
        if (candidate / Path(*PLUGIN_REL)).is_file():
            return candidate
    return None


def has_plugin(cfg: Path) -> bool:
    """Есть ли в папке конфигурации сам плагин памяти."""
    return (cfg / Path(*PLUGIN_REL)).is_file()


def build_instructions(base: Path) -> str:
    """Собирает текст opencode.jsonc с путями именно к этой базе.

    Батник берёт готовый файл из базы — и пути там от старого места,
    если база переехала. Здесь файл собирается заново, поэтому пути
    всегда указывают на настоящую папку базы.
    """
    base_posix = str(base).replace("\\", "/")
    lines = [
        "{",
        '  "$schema": "https://opencode.ai/config.json",',
        "  // Локальная база памяти пользователя — автоматически загружается",
        "  // в КАЖДУЮ сессию (все модели, CLI и десктоп), без ручных действий.",
        '  "instructions": [',
    ]
    items = [f"{base_posix}/{name}" for name in INSTRUCTION_TARGETS]
    for index, item in enumerate(items):
        comma = "," if index < len(items) - 1 else ""
        lines.append(f'    "{item}"{comma}')
    lines.append("  ]")
    lines.append("}")
    return "\n".join(lines) + "\n"


def substitute_base(path: Path, base: Path, desktop: Path | None = None) -> bool:
    """Подставляет настоящие пути вместо пометок {{BASE}} и {{DESKTOP}}.

    Нужна для файлов настроек и команд, которые кладутся в программу как
    есть (AGENTS.md, opencode.jsonc, файлы в папке command): в базе они
    лежат с пометкой, поэтому годятся для любого компьютера, а настоящие
    пути появляются только при подключении.

    {{DESKTOP}} — рабочий стол. Он нужен там, где команда создаёт папку
    проекта рядом с базой: без пометки в файле осталось бы имя чужого
    пользователя. Возвращает True, если файл изменён.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    if BASE_PLACEHOLDER not in text and DESKTOP_PLACEHOLDER not in text:
        return False
    fixed = text.replace(BASE_PLACEHOLDER, str(base).replace("\\", "/"))
    if DESKTOP_PLACEHOLDER in fixed:
        table = Path(desktop) if desktop is not None else desktop_dir()
        fixed = fixed.replace(DESKTOP_PLACEHOLDER, str(table).replace("\\", "/"))
    if fixed == text:
        return False
    try:
        path.write_text(fixed, encoding="utf-8")
    except OSError:
        return False
    return True


def install_plugin(base: Path, dest: Path, progress=None) -> tuple[list[str], list[str]]:
    """Кладёт в папку настроек плагин, настройки и команды.

    Возвращает два списка: сообщения и ошибки. Ничего не удаляет —
    прежние файлы уходят в _previous-version.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    cfg = find_config_source(base)
    if cfg is None:
        errors.append(
            "В базе нет папки config с плагином памяти — "
            "OpenCode не сможет её читать."
        )
        return messages, errors

    say(f"Плагин найден: {cfg}")
    dest.mkdir(parents=True, exist_ok=True)

    # Прежние настройки сохраняем, чтобы ничего не потерялось.
    old = [f for f in CONFIG_FILES if (dest / f).is_file()]
    if old:
        backup = dest / "_previous-version"
        backup.mkdir(exist_ok=True)
        for name in old:
            try:
                shutil.copy2(dest / name, backup / name)
            except OSError as exc:
                errors.append(f"Не удалось сохранить копию {name}: {exc}")
        say(f"Прежние настройки сохранены ({len(old)} шт.)")

    # Прежний AGENTS.md запоминаем ещё и текстом: файл настроек будет
    # перезаписан из базы, а правила caveman, поставленные галочкой на
    # вкладке opencode, лежат именно в нём. Без этого блока человек видел
    # бы «включено», а ответы приходили бы обычным текстом.
    agents_before = ""
    if (dest / "AGENTS.md").is_file():
        try:
            agents_before = (dest / "AGENTS.md").read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            agents_before = ""

    # Файлы настроек. opencode.jsonc не перезаписываем поверх живущего
    # в программе: там могут быть провайдеры и мосты. Его разберём
    # отдельно — переведём на эту базу, ничего не стирая.
    copied = 0
    for name in CONFIG_FILES:
        if name == INSTRUCTIONS_FILE:
            continue
        src = cfg / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
                copied += 1
            except OSError as exc:
                errors.append(f"Не удалось скопировать {name}: {exc}")
    say(f"Файлы настроек: {copied} из {len(CONFIG_FILES) - 1}")

    if agents_before:
        try:
            import caveman  # noqa: PLC0415 — рядом лежит, круга нет

            if caveman.rescue(dest, agents_before):
                say("Правила caveman сохранены в AGENTS.md")
        except (OSError, ValueError):
            pass

    # opencode.jsonc: если в программе его ещё нет, кладём образец из базы.
    # Если уже есть — переписываем только пути, оставляя остальные
    # настройки человека в целости.
    cfg_target = dest / INSTRUCTIONS_FILE
    if not cfg_target.is_file():
        src = cfg / INSTRUCTIONS_FILE
        if src.is_file():
            try:
                shutil.copy2(src, cfg_target)
            except OSError as exc:
                errors.append(f"Не удалось скопировать {INSTRUCTIONS_FILE}: {exc}")
        else:
            try:
                cfg_target.write_text(build_instructions(base), encoding="utf-8")
            except OSError as exc:
                errors.append(f"Не удалось записать {INSTRUCTIONS_FILE}: {exc}")
    if cfg_target.is_file():
        if substitute_base(cfg_target, base):
            say(f"Путь к базе подставлен: {INSTRUCTIONS_FILE}")
        try:
            from opencode_caps import set_instructions  # noqa: PLC0415

            text = cfg_target.read_text(encoding="utf-8")
            cfg_target.write_text(set_instructions(text, base), encoding="utf-8")
            say("Пути к базе прописаны в настройках")
        except (OSError, ValueError) as exc:
            try:
                cfg_target.write_text(build_instructions(base), encoding="utf-8")
                say("Пути к базе прописаны заново — прежний файл не читался")
            except OSError as exc2:
                errors.append(f"Не удалось записать {INSTRUCTIONS_FILE}: {exc2}")

    # В остальных файлах настроек путь стоит пометкой — заменяем
    # на настоящий. Так файлы в базе остаются переносимыми.
    for name in CONFIG_FILES:
        if name == INSTRUCTIONS_FILE:
            continue
        target = dest / name
        if target.is_file() and substitute_base(target, base):
            say(f"Путь к базе подставлен: {name}")

    # Папки plugins и command — плагин и готовые команды.
    for folder in CONFIG_DIRS:
        src = cfg / folder
        if src.is_dir():
            try:
                shutil.copytree(
                    src, dest / folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            except OSError as exc:
                errors.append(f"Не удалось скопировать папку {folder}: {exc}")

    # В готовых командах путь тоже стоит пометкой — иначе команда,
    # написанная на одном компьютере, на другом будет искать чужую папку.
    # Проходим и по папке command, и по её подпапкам.
    for folder in CONFIG_DIRS:
        root = dest / folder
        if not root.is_dir():
            continue
        for target in sorted(root.rglob("*")):
            if target.is_file() and substitute_base(target, base):
                say(f"Путь к базе подставлен: {folder}/{target.name}")

    if has_plugin(dest):
        say("Плагин памяти установлен")
    else:
        errors.append("Плагин памяти не установился — база не будет читаться.")

    # Готовые зависимости: без них плагин не запустится.
    deps_src = cfg / "node_modules"
    if (deps_src / "@opencode-ai" / "plugin" / "package.json").is_file():
        try:
            shutil.copytree(
                deps_src, dest / "node_modules", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            say("Зависимости плагина перенесены (интернет не нужен)")
        except OSError as exc:
            errors.append(f"Не удалось перенести зависимости: {exc}")
    elif (dest / Path(*DEPS_MODULE)).is_file():
        say("Зависимости плагина уже на месте")
    else:
        say("Зависимостей нет — понадобится npm install (нужен интернет)")

    return messages, errors


# ---------------------------------------------------------------- Harness

HARNESS_AGENTS_BEGIN = "<!-- == OpenCode_Base: harness == -->"
HARNESS_AGENTS_END = "<!-- == OpenCode_Base: конец harness == -->"


def harness_agents_block(base: Path) -> str:
    """Блок для AGENTS.md в Harness: пути к файлам базы."""
    posix = str(base).replace("\\", "/")
    files = "\n".join(f"- {name}: {posix}/{name}" for name in INSTRUCTION_TARGETS)
    return (
        f"{HARNESS_AGENTS_BEGIN}\n"
        "# База пользователя (подключена программой управления)\n"
        "Память лежит в базе, читай эти файлы:\n"
        f"{files}\n"
        f"Навыки: {posix}/skills/<имя>/SKILL.md\n"
        "Карты подсказок (указатели знаний, открывай нужную):\n"
        + "".join(f"- {posix}/{p}\n" for p in knowledge_index_paths())
        + f"{HARNESS_AGENTS_END}\n"
    )


def install_harness_agents(
    base: Path, dest: Path, progress=None
) -> tuple[list[str], list[str]]:
    """Вписывает базу в AGENTS.md у Harness. Чужое не трогает.

    Прежний AGENTS.md уходит копией в _previous-version. Наш блок живёт
    между метками: повторная установка его заменяет, а не двоит.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    dest.mkdir(parents=True, exist_ok=True)
    target = dest / "AGENTS.md"
    block = harness_agents_block(base)
    try:
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"AGENTS.md не читается: {exc}")
        return messages, errors
    if target.is_file():
        backup_dir = dest / "_previous-version"
        try:
            backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            shutil.copy2(target, backup_dir / f"AGENTS.md-{stamp}")
            say("Копия AGENTS.md сохранена в _previous-version")
        except OSError as exc:
            errors.append(f"Не сохранилась копия AGENTS.md: {exc}")
            return messages, errors
    pattern = re.compile(
        re.escape(HARNESS_AGENTS_BEGIN)
        + r".*?"
        + re.escape(HARNESS_AGENTS_END)
        + r"\n?",
        re.DOTALL,
    )
    if pattern.search(text):
        text = pattern.sub(block, text)
        say("Блок базы в AGENTS.md обновлён")
    else:
        if text and not text.endswith("\n"):
            text += "\n"
        text = text + ("\n" if text else "") + block
        say("Блок базы дописан в AGENTS.md")
    try:
        target.write_text(text, encoding="utf-8")
    except OSError as exc:
        errors.append(f"AGENTS.md не записался: {exc}")
        return messages, errors
    if HARNESS_AGENTS_BEGIN not in target.read_text(encoding="utf-8"):
        errors.append("AGENTS.md не читается после записи — откат вручную из _previous-version")
    return messages, errors


# ---------------------------------------------------------------- создание базы

# Файлы, без которых база не считается базой.
REQUIRED_FILES = ("profile.md", "facts.md", "projects.md", "ОБРАЗЕЦ-БАЗЫ.md")

# Файлы-навигаторы. Они появились в расширенной структуре: без них
# нейросеть видит только profile/facts и не знает про остальные разделы.
NAV_FILES = ("КАРТА-БАЗЫ.md", "ПРАВИЛА-ИИ.md")

# Папки, которые создаются в новой базе.
# Список отражает расширенную структуру: память, личное, журнал решений,
# настройки и знания по областям. Совпадает с базой-образцом в корне.
NEW_DIRS = (
    # служебные разделы базы
    "библиотека",
    "библиотека/записи",
    "библиотека/архив",
    "библиотека/входящие",
    "библиотека/журнал",
    "библиотека/шаблоны",
    # Личная категория внутри библиотеки. Записи отсюда в новые базы не
    # копируются, но сама папка нужна сразу: темы вида «личное/животные»
    # должны иметь куда лечь. Пустая папка с пояснением — не личные данные.
    "библиотека/записи/личное",
    "память",
    "память/архив",
    "журнал-решений",
    "настройки",
    "личное",
    "личное/дневник",
    "знания",
    "projects",
    "sessions",
    "skills",
    "инструкции",
)

# Области знаний. Создаются внутри «знания» — по одной папке на область.
# В каждой области лежит файл «_О-ПАПКЕ.md» с объяснением, что туда кладут.
KNOWLEDGE_AREAS = (
    "Техника",
    "Деньги",
    "Здоровье",
    "Учёба",
    "Дом-и-быт",
    "Документы",
    # личные данные: люди, вещи и то, что нужно в беде
    "Люди",
    "Имущество",
    "Экстренное",
)

# Подпапки внутри областей. Нужны там, где одной папки мало:
# у людей — группы («Семья», «Друзья»), у имущества — виды вещей.
#
# Техника тоже делится: безопасность — отдельная ветка. Раньше её здесь
# не было, и вышло плохо: папка «знания/Техника/Безопасность» с
# одиннадцатью подпапками лежала в конструкторе, а новая база её не
# получала — при том что AGENTS.md велит читать оттуда подсказку по
# безопасности. Инструкция указывала на папку, которой нет. Теперь
# ветка объявлена, и новая база получает её вместе с пояснением.
KNOWLEDGE_SUBDIRS = {
    "Техника": ("Безопасность",),
    "Люди": ("Семья", "Друзья", "Родня", "Коллеги", "Специалисты"),
    "Имущество": ("Техника", "Транспорт", "Недвижимость", "Гарантии-и-чеки"),
}

# Третий уровень: внутри отдельных подпапок свои разделы. Безопасность
# без них не годится — одна папка на десять тем не объясняет ничего.
KNOWLEDGE_SUBSUBDIRS = {
    "Безопасность": (
        "AI",
        "Бинарка-реверс",
        "Веб",
        "Крипта-блокчейн",
        "Linux",
        "Методологии",
        "Мобилки",
        "Приватность",
        "Сеть",
        "Windows",
        "macOS",
    ),
}


def knowledge_folders() -> list[str]:
    """Все папки внутри «знания»: области, подпапки и их подпапки.

    Один список на все случаи: создание базы, пояснения «_О-ПАПКЕ.md»
    и копирование шаблонов из образца.
    """
    places = [f"знания/{area}" for area in KNOWLEDGE_AREAS]
    for area, subs in KNOWLEDGE_SUBDIRS.items():
        for sub in subs:
            places.append(f"знания/{area}/{sub}")
            for deep in KNOWLEDGE_SUBSUBDIRS.get(sub, ()):
                places.append(f"знания/{area}/{sub}/{deep}")
    return places


#: Лимит указателя в байтах на диске. Считается байтами, а не символами:
#: проверка не должна зависеть от кодировки. Для русской прозы байты строже
#: символов, потому что кириллица в UTF-8 — два байта на букву.
#:
#: Расширено 05.10 до 10 МБ на всех трёх уровнях. Что из этого следует,
#: и почему обратно не хочется, записано честно:
#:
#: 1. Ограничителя в коде нет. `_limit` разворачивается в никуда: никто не
#:    обрезает указатель и никто не предупреждает о перевышении. Поэтому
#:    цифра ниже — не запрет, а порог, при котором проверка становится
#:    красной. 10 МБ означает «красная не наступит почти никогда», и это
#:    осознанный выбор: ложная тревога из-за нормально выросшей карты
#:    раздражает сильнее, чем большая карта.
#: 2. Читаемость перестала быть ограничением. Указатель — карта, и смысл
#:    её в том, чтобы модель прочитала её целиком. 10 МБ не прочитает
#:    никто: это примерно в 300 раз больше того, что влезает в контекст.
#:    Если карта вырастет до мегабайтов, полезнее её дробить, а не
#:    разрешать расти.
#: 3. Возврат к килобайтам делается одной правкой трёх строк ниже.
INDEX_LIMIT_ROOT = 10 * 1024 * 1024
INDEX_LIMIT_AREA = 10 * 1024 * 1024
#: Ветка безопасности — 171 папка импорта, её авточасть весит 13 КБ
#: (замерено). 32 КБ держалось как «расчётные 17 плюс запас»; запас
#: оказался не нужен — до 10 МБ он не понадобится ещё очень долго.
INDEX_LIMIT_BRANCH = 10 * 1024 * 1024

#: Имя архива потерянных привязок. Отдельным файлом, а не разделом внутри
#: указателя: архив нельзя отдавать модели как инструкцию — рядом с живыми
#: строками она примет его за указание и пойдёт по несуществующему пути.
ARCHIVE_NAME = "_потеряно-привязок.md"

#: Маркеры машинной части. Между ними перезаписывается всё, вне — текст
#: модели, который программа не трогает.
INDEX_BEGIN = "<!-- == авточасть: дальше не редактировать руками -->"
INDEX_END = "<!-- == конец авточасти -->"


def knowledge_index_targets() -> list[tuple[str, str, int]]:
    """Какие указатели знаний нужны: путь папки, имя файла, лимит в байтах.

    Уровни. Первый — корневой указатель областей, его читают почти всегда,
    поэтому он самый короткий. Второй — указатель области, перечисляет её
    прямые подпапки. Третий — указатель большой импортированной ветки, там
    перечисляется всё поддерево, потому что по-другому не описать 171
    папку в пределах лимита.

    Имя файла всегда «папка указателя + _подсказки-<папка в нижнем регистре>».
    Раньше предполагалось `_ПОДСКАЗКИ-ОБЩАЯ.md` в верхнем регистре —
    отменено 04.10: верхний регистр среди файлов с подчёркиванием выглядит
    чужеродно, а порядок в списке обеспечивает не регистр, а сам указатель.

    Корневой указатель — исключение из правила, и исключение ровно одно:
    его папка называется `знания`, а указатель — «общая», потому что
    перечисляет все области сразу. Имя задаётся здесь строкой, а не
    выводится из `folder.name`, иначе получилось бы «знания».
    """
    out: list[tuple[str, str, int]] = [
        ("знания", "_подсказки-общая.md", INDEX_LIMIT_ROOT),
    ]
    # Уровень 2: своя папка области, её прямые подпапки. «Техника» идёт
    # последней отдельно, потому что её указатель не помещается в лимит
    # области — у неё большая импортированная ветка.
    branch_root = "знания/Техника"
    for area in KNOWLEDGE_AREAS:
        if area == "Техника":
            continue
        out.append((f"знания/{area}", f"_подсказки-{area.lower()}.md",
                    INDEX_LIMIT_AREA))
    out.append((branch_root, "_подсказки-техника.md", INDEX_LIMIT_AREA))
    # Уровень 3: импортированная ветка со своим лимитом.
    for area, subs in KNOWLEDGE_SUBDIRS.items():
        for sub in subs:
            if KNOWLEDGE_SUBSUBDIRS.get(sub):
                out.append((f"знания/{area}/{sub}",
                            f"_подсказки-{sub.lower()}.md",
                            INDEX_LIMIT_BRANCH))
    return out


def knowledge_index_paths() -> list[str]:
    """Пути указателей относительно базы, через прямой слэш.

    Нужен текстам, которые читает модель. Список берётся из
    `knowledge_index_targets`, а не пишется руками: перечисление
    одиннадцати файлов в правилах однажды разошлось бы с кодом, и модель
    пошла бы открывать несуществующий адрес — то самое, из-за чего
    пришлось заводить указатели.
    """
    return [f"{rel}/{fname}" for rel, fname, _ in knowledge_index_targets()]


#: Пояснение папки, сам указатель и архив потерянных привязок — служебные
#: файлы. В счёте «файлов в папке» им не место: иначе пересборка меняла бы
#: цифру сама у себя.
#:
#: Архив сюда добавлен измерением, а не догадкой. Он создаётся как раз
#: пересборкой, но под правило «начинается с `_подсказки`» не подходил, и
#: файл попадал в счёт: папка исчезала — в указателе писало «файлов: 2» при
#: одном файле знаний на диске. Второе следствие хуже: модель, читающая
#: заголовок, приняла бы архив за материал знаний.
def _is_service_markdown(path: Path) -> bool:
    return (path.name == "_О-ПАПКЕ.md"
            or path.name.startswith("_подсказки")
            or path.name == ARCHIVE_NAME)


def _index_scope(scope: str) -> str:
    """Нормализует область обхода.

    Неизвестное значение трактуется как «прямые подпапки»: это самый
    узкий и предсказуемый режим, а ошибочный широкий обход молча утопил
    бы указатель в лимит байтов.
    """
    return "tree" if scope == "tree" else "children"


def _index_paths(folder: Path, scope: str) -> list[tuple[str, int]]:
    """Папки указателя и число содержащихся в них файлов.

    `scope` различает уровни: `areas` и `children` — прямые подпапки
    (корневой указатель и указатель области), `tree` — всё поддерево,
    как у ветки с импортом на 171 папку. Разница нужна, иначе проверка
    «набор путей равен тому, что на диске» падала бы сама по себе.

    Порядок — по путям, а не по порядку обхода файловой системы: иначе
    две сборки подряд отличались бы построчно и пересборка без изменений
    выглядела бы как правка.

    Путь относителен к папке самого указателя: полный путь получается
    сложением, и именно так его читает модель.
    """
    if _index_scope(scope) == "tree":
        folders = [p for p in folder.rglob("*") if p.is_dir()]
    else:
        folders = [p for p in folder.iterdir() if p.is_dir()]
    folders.sort(key=lambda p: p.relative_to(folder).as_posix())
    out: list[tuple[str, int]] = []
    for sub in folders:
        rel = sub.relative_to(folder).as_posix()
        n = sum(1 for p in sub.rglob("*.md")
                if not _is_service_markdown(p))
        out.append((rel, n))
    return out


def _count_markdown(folder: Path) -> int:
    """Сколько обычных файлов в папке, без служебных."""
    return sum(1 for p in folder.rglob("*.md")
               if not _is_service_markdown(p))


def _plural_files(n: int) -> str:
    """«1 файл», «2 файла», «5 файлов», «11 файлов».

    Первая версия различала только единицу и писала «2 файлов» — это
    заметно и бросается в глаза в тексте, который читает модель.
    """
    if n % 100 in (11, 12, 13, 14):
        return "файлов"
    last = n % 10
    if last == 1:
        return "файл"
    if last in (2, 3, 4):
        return "файла"
    return "файлов"


def build_knowledge_index(folder: Path, scope: str, stamp: str) -> str:
    """Готовый текст указателя: машинная часть и заготовка смысловой.

    Машинная часть лежит между маркерами и перезаписывается целиком.
    Вне маркеров — текст модели, который программа не трогает.

    Смысловая часть здесь пустая и затравленная: заполняет её нейросеть.
    """
    title = folder.name
    paths = _index_paths(folder, scope)
    total_files = _count_markdown(folder)
    lines = [
        f"# Указатель: {title}",
        "",
        INDEX_BEGIN,
        f"Собрано: {stamp}. Папок: {len(paths)}, файлов: {total_files}.",
        "",
        "Пути ниже — относительно папки этого указателя.",
        "Полный путь = папка указателя + строка из раздела «Пути».",
        "В строке — число файлов папки вместе со вложенными.",
        # Фраза одной строкой: проверка ищет её целиком, а перенос внутри
        # неё превращает поиск в гадание, и проверка гаснет не пойми почему.
        "Складывать строки нельзя: вложенная папка посчитана дважды.",
        "",
        "## Пути",
    ]
    for rel, n in paths:
        lines.append(f"- `{rel}/` — {n} {_plural_files(n)}")
    lines += [
        INDEX_END,
        "",
        "## Когда заходить",
        "Пока пусто. Этот раздел пишет нейросеть, программа его не трогает.",
        "",
    ]
    return "\n".join(lines)


def _split_index(text: str) -> tuple[str, str, str]:
    """Текст указателя на три части: до маркеров, авточасть, после."""
    b = text.find(INDEX_BEGIN)
    e = text.find(INDEX_END)
    if b < 0 or e < 0 or e < b:
        return text, "", ""
    return text[:b], text[b:e + len(INDEX_END)], text[e + len(INDEX_END):]


def _referenced_paths(after: str) -> set[str]:
    """Пути, упомянутые вне маркеров: полные и короткие имена.

    Короткое имя ищем тоже. Нейросеть вправе написать «смотри `Сеть/`», и
    по одному полному пути такая строка выпала бы из поиска и потерялась.
    Совпадение по короткому имени с чужой папкой допускаем: лишний раз
    перенести в архив лучше, чем потерять написанное.
    """
    found: set[str] = set()
    for raw in re.findall(r"`([^`\n]+)`", after):
        token = raw.strip().rstrip("/")
        if "/" in token:
            found.add(token)
        if token:
            found.add(token.split("/")[-1])
    return found


def _orphaned_lines(old: str, new_paths: set[str]
                    ) -> tuple[list[tuple[str, list[str]]], set[str]]:
    """Строки смысловой части, ссылающиеся на исчезнувшие пути.

    Возвращает тройку:
      * пары (прежний_путь, строки) для архива;
      * множество строк, которые из смысловой части уходят целиком;
      * список (строка, исчезнувший_путь, уцелевшие_пути) для тех строк,
        что остаются на месте с пометкой.

    Порядок — по алфавиту, чтобы архив читался сверху вниз одинаково при
    любом порядке обхода.

    Исчезнувшим считается путь, который был в старой авточасти и которого
    больше нет, — ровно как в спецификации 4.2, п.2: «для каждого пути,
    который был и которого нет». Расширять это на пути, которых в авточасти
    не было, нельзя, и вот почему.

    В смысловой части нейросеть вправе писать не только про папки, но и про
    код: `ALLOW_PUSH`, `signature.py`, `opencode.jsonc`. Всё, что стоит в
    обратных кавычках, выглядит одинаково. Если брать кандидатом любое
    упоминание, которого нет на диске, в архив уедут живые упоминания кода,
    и указатель станет беднее. Измерено на примере с четырьмя такими
    упоминаниями: без фильтра по прошлой авточасти уходят все четыре.

    Поэтому условие «было в авточасти» здесь не ускорение, а граница
    безопасности: она отсекает код от папок.
    """
    _, auto_old, after = _split_index(old)
    known = set(re.findall(r"^- `([^`]+)/`", auto_old, re.M))
    referenced = _referenced_paths(after)
    live = set(new_paths)
    live_tail = {p.split("/")[-1] for p in live}
    known_tail = {k.split("/")[-1] for k in known}
    gone = [name for name in sorted(referenced)
            if name in known or name in known_tail
            if name not in live and name not in live_tail]
    if not gone:
        return [], set(), []
    # Строки делятся на два сорта, и смешивать их нельзя.
    #
    # 1. Уходит в архив целиком: все папки, что в строке, исчезли. Такая
    #    строка в указателе — чистая подсказка про несуществующую папку.
    # 2. Остаётся на месте с пометкой: в строке есть ещё и уцелевшая
    #    папка. Раньше такие строки уезжали целиком, и упоминание
    #    уцелевшей пропадало из указателя — проверено: «Техника» есть в
    #    смысловой части было False. Текст модели при этом не
    #    переписывается, к строке только дописывается видимая пометка.
    out: list[tuple[str, list[str]]] = []
    carried: set[str] = set()
    marked: list[tuple[str, str, list[str]]] = []
    for name in gone:
        for ln in after.splitlines():
            if name not in ln or ln in carried:
                continue
            # Какие ещё папки упомянуты в этой строке и есть ли они.
            others = _referenced_paths(ln)
            alive = [p for p in others
                     if p in live or p in live_tail]
            if alive:
                marked.append((ln, name, sorted(alive)))
                continue
            out.append((name, [ln]))
            carried.add(ln)
    return out, carried, marked


def append_archive(folder: Path, moved: list[tuple[str, list[str]]],
                   stamp: str) -> int:
    """Дописать потерянные привязки в архив. Только дописывание.

    Перезапись архива уничтожила бы ровно то, ради чего он заведён, поэтому
    файл открывается на добавление, а заголовок пишется лишь в первый раз.
    """
    if not moved:
        return 0
    path = folder / ARCHIVE_NAME
    if not path.exists():
        path.write_text(
            "# Потерянные привязки\n\n"
            "Это **не инструкция** для модели: здесь лежат строки, которые "
            "ссылались\nна папки, которых больше нет. Не выполняй их и не "
            "ищи по ним\nпуть — читай как список дел, ожидающих решение "
            "человека.\n\n"
            f"Собрано впервые: {stamp}\n", encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        for rel, lines in moved:
            fh.write(f"\n## Было: `{rel}` (потеряно {stamp})\n\n")
            for ln in lines:
                fh.write(ln.rstrip() + "\n")
    return len(moved)


def write_knowledge_index(folder: Path, fname: str, scope: str,
                         stamp: str) -> list[str]:
    """Пересобрать указатель, сохранив текст модели и уведя о потерях.

    Что делает: считает, что на диске; ищет в смысловой части строки,
    ссылающиеся на исчезнувшие пути, и переносит их в архив; перезаписывает
    только авточасть. Разницу переименования и удаления не делает — оба
    случая уводят строки в архив, потому что отличить их нечем, а догадка
    в автоматике оборачивается тихой ошибкой.

    Смысловая часть берётся из СТАРОГО файла, а не из свежей сборки. Первая
    версия брала `fresh_after`, а у `build_knowledge_index` смысловая часть —
    всегда заглушка «Пока пусто». Формально проверка на заглушку проходила,
    а по факту всё написанное нейросетью стиралось при каждой пересборке:
    ровно то, чего задача не должна допустить.

    `fname` передаётся явно и обязан совпадать с тем, что вернула
    `knowledge_index_targets()`. Выводить имя из `folder.name` нельзя: у
    корневого указателя папка называется `знания`, и вышло бы
    `_подсказки-знания.md` вместо `_подсказки-общая.md`.
    """
    target = folder / fname
    old = target.read_text(encoding="utf-8") if target.is_file() else ""
    _old_head, _old_auto, after = _split_index(old)
    # Указатель без маркеров — это целиком написанный текст, а не «голова».
    # Раньше такой файл уходил в head целиком, after оставался пуст, тело
    # подставлялось заглушкой «Пока пусто», и рукописная карта исчезала
    # молча. Измерено на копии живой базы: карта безопасности на 2289 байт
    # была заменена заглушкой, и никакая проверка этого не заметила, потому
    # что все индексы в селфтесте созданы с маркерами.
    if old and not _old_auto and _old_head == old:
        after = old
    new_paths = {rel for rel, _n in _index_paths(folder, scope)}
    moved, carried, marked = _orphaned_lines(old, new_paths)
    append_archive(folder, moved, stamp)
    # Помеченные строки тоже идут в архив — копией. Они остаются в
    # указателе, потому что в них есть уцелевшая папка, но копия нужна
    # человеку: разбирать потерянное придётся ему, и он должен видеть
    # исходный текст, а не только приписку.
    if marked:
        append_archive(
            folder,
            [("смешанные строки",
              [f"{ln} (в строке пропала папка: {gone})"
               for ln, gone, _alive in marked])],
            stamp)
    fresh = build_knowledge_index(folder, scope, stamp)
    _, _, fresh_after = _split_index(fresh)
    head = fresh[:len(fresh) - len(fresh_after)]
    # Уцелевшие строки смысловой части. Перенесённые уходят: держать их
    # на месте, откуда они перенесены, — значит держать в архиве то, что
    # читается как живое, и архив перестаёт быть списком потерянного.
    keep = [ln for ln in after.splitlines() if ln not in carried]
    # Строки с уцелевшими папками остаются на месте и получают видимую
    # пометку. Текст модели не переписывается: его дословный текст
    # сохраняется, к нему дописывается приписка. Убрать такую строку
    # целиком нельзя — тогда из указателя пропадёт упоминание папки,
    # которая на месте, и модель её не найдёт.
    if marked:
        _note = {
            ln: f" (проверено {stamp}: папки нет — {_gone};"
                 f" на месте — {', '.join(alive)})"
            for ln, _gone, alive in marked
        }
        keep = [ln + _note[ln] if ln in _note else ln for ln in keep]
    body = "\n".join(keep).strip("\n")
    if not body.strip():
        body = fresh_after.strip("\n")
    # newline="\n" обязателен: без него на Windows текст пишется с CRLF, и
    # байтовый лимит начинает зависеть от того, чей файл правили последним —
    # эталон в папке программы оказался на 11 байт меньше копии в живой базе
    # только из-за одиннадцати переводов строк.
    target.write_text(f"{head}\n{body}\n", encoding="utf-8", newline="\n")
    report = [f"указатель: {target.name}"]
    if moved:
        report.append("в архив: " + ", ".join(r for r, _ in moved))
    if marked:
        report.append(f"с пометкой: {len(marked)}")
    return report


# Имена, занятые в Windows. Такую папку создать нельзя.
RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
FORBIDDEN_CHARS = set('<>:"/\\|?*')


class NameError_(ValueError):
    """Имя базы не годится."""


def validate_name(name: str) -> str:
    """Проверяет имя папки и возвращает его без лишних пробелов.

    Бросает NameError_ с понятным объяснением, если имя не годится.
    """
    name = (name or "").strip().strip(".")
    if not name:
        raise NameError_("Имя не может быть пустым.")
    if len(name) > 100:
        raise NameError_("Слишком длинное имя — до 100 знаков.")
    bad = sorted({ch for ch in name if ch in FORBIDDEN_CHARS})
    if bad:
        shown = " ".join(bad)
        raise NameError_(
            f"Эти знаки в имени папки Windows не разрешает: {shown}\n"
            "Уберите их."
        )
    if any(ord(ch) < 32 for ch in name):
        raise NameError_("В имени есть невидимые служебные знаки. Уберите их.")
    if name.upper() in RESERVED:
        raise NameError_(
            f"Имя «{name}» занято самой Windows — под него нельзя создать папку.\n"
            "Возьмите другое, например «{0}-база».".format(name)
        )
    if name in (".", ".."):
        raise NameError_("Такое имя использовать нельзя.")
    return name


@dataclass
class CreationPlan:
    """Что именно сделает создание базы — показывается до записи."""

    target: Path
    name: str
    # Решение человека: можно ли в этой базе хранить пароли и ключи.
    # По умолчанию нет. Выбирается при создании базы и дальше лежит
    # в базе файлом настройки, чтобы правило было видно без программы.
    allow_sensitive: bool = False
    files: list[str] = field(default_factory=list)
    dirs: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    template_from: Path | None = None


def build_plan(parent: Path, name: str, template: Path | None,
              allow_sensitive: bool = False) -> CreationPlan:
    """Готовит план создания базы, ничего не записывая на диск."""
    name = validate_name(name)
    target = Path(parent) / name
    plan = CreationPlan(target=target, name=name,
                        allow_sensitive=allow_sensitive)

    if target.exists():
        if any((target / f).exists() for f in REQUIRED_FILES):
            raise NameError_(
                f"В папке «{name}» уже лежит база — там есть "
                f"{REQUIRED_FILES[0]}.\nВыберите другое имя или другую папку."
            )
        if any(target.iterdir()):
            plan.warnings.append(
                "Папка уже существует и в ней есть посторонние файлы. "
                "Они останутся на месте — новые файлы добавятся к ним."
            )
    else:
        plan.dirs.append(str(target))

    if template and (template / REQUIRED_FILES[0]).is_file():
        plan.template_from = template
        plan.files = [
            "profile.md (из образца)",
            "facts.md (из образца)",
            "projects.md (из образца)",
            "ОБРАЗЕЦ-БАЗЫ.md",
            "библиотека/ (пустая, с активной памятью)",
            "skills/ (пустая — наполните копированием)",
            "заготовки личных данных (_ШАБЛОН-….md)",
        ]
        plan.dirs += [str(target / d) for d in NEW_DIRS]
    else:
        plan.files = [f"{f} (пустой шаблон)" for f in REQUIRED_FILES]
        plan.dirs += [str(target / d) for d in NEW_DIRS]
    return plan


BLANK = {
    "profile.md": "# Профиль пользователя\n\n- Зовут: \n- Язык общения: русский.\n",
    "facts.md": "# Факты и правила (приоритетные)\n\n- [{}] База создана.\n".format(
        datetime.now().strftime("%Y-%m-%d")
    ),
    "projects.md": "# Проекты\n\nСписок появится по мере работы.\n",
    "ОБРАЗЕЦ-БАЗЫ.md": (
        "# Образец базы — как она устроена\n\n"
        "База — это папка с текстовыми файлами. Начинать знакомство\n"
        "нужно с `КАРТА-БАЗЫ.md` — там написано, что где лежит.\n\n"
        "## Корень — самое важное\n\n"
        "- `КАРТА-БАЗЫ.md` — навигатор: что где лежит, куда что писать;\n"
        "- `ПРАВИЛА-ИИ.md` — как ассистент должен себя вести;\n"
        "- `profile.md` — кто пользователь, как с ним общаться;\n"
        "- `facts.md` — правила и решения (самое важное);\n"
        "- `projects.md` — список проектов, по строке на проект.\n\n"
        "## Разделы\n\n"
        "- `память/` — короткая сводка «на чём остановились»;\n"
        "- `библиотека/` — знания с поиском, ведёт ассистент;\n"
        "- `знания/` — материалы по областям: "
        + ", ".join(KNOWLEDGE_AREAS) + ";\n"
        "- `projects/` — по папке на каждый проект;\n"
        "- `личное/` — цели, привычки, идеи, дневник;\n"
        "- `sessions/` — что делали, по месяцам;\n"
        "- `журнал-решений/` — почему так решили;\n"
        "- `настройки/` — стиль общения, запреты, разрешения;\n"
        "- `skills/` — умения ассистента: `skills/<имя>/SKILL.md`;\n"
        "- `инструкции/` — подробные объяснения по разделам.\n\n"
        "## Правила ведения\n\n"
        "- Ничего не удалять — только дополнять.\n"
        "- Одна мысль — одна строка, с датой в квадратных скобках.\n"
        "- Пароли, номера карт и сканы документов сюда не писать.\n\n"
        "В каждой папке лежит файл `_О-ПАПКЕ.md` — объяснение, что туда кладут.\n"
    ),
}

#: Содержимое файлов-навигаторов для пустой базы (без образца).
#: Для базы из образца они копируются как есть.
NAV_BLANK = {
    "КАРТА-БАЗЫ.md": (
        "# Карта базы — что здесь лежит\n\n"
        "Путеводитель по базе. Читается в начале работы.\n\n"
        "## Куда что писать\n\n"
        "| Что случилось | Куда писать |\n"
        "|---|---|\n"
        "| Факт о пользователе | `profile.md` |\n"
        "| Решение, о котором нельзя забыть | `facts.md` |\n"
        "| Новый проект | `projects/<Имя>/` + строка в `projects.md` |\n"
        "| Знание по теме | `знания/<Область>/` |\n"
        "| Задача по любой теме знаний | сначала `"
        + knowledge_index_paths()[0]
        + "`, потом указатель нужной области (список ниже), потом уже файлы |\n"
        "| Ценная мысль | `библиотека/записи/<Тема>/` |\n"
        "| Что сделано | `sessions/<месяц>.md` |\n"
        "| Почему так решили | `журнал-решений/<месяц>.md` |\n\n"
        "## Разделы\n\n"
        "- `память/` — короткая сводка «на чём остановились»;\n"
        "- `библиотека/` — знания с поиском: `записи/`, `входящие/`,\n"
        "  `архив/`, `журнал/`, `шаблоны/`;\n"
        "- `знания/` — области: "
        + ", ".join(KNOWLEDGE_AREAS) + ";\n"
        "- `projects/` — по папке на проект, внутри `_проект.md`;\n"
        "- `личное/` — `цели.md`, `привычки.md`, `идеи.md`, `дневник/`;\n"
        "- `sessions/` — что делали, по месяцам;\n"
        "- `журнал-решений/` — почему так решили;\n"
        "- `настройки/` — `стиль-общения.md`, `запреты.md`, `разрешения.md`;\n"
        "- `skills/` — умения ассистента;\n"
        "- `инструкции/` — подробные объяснения.\n\n"
        "## Указатели знаний\n\n"
        "Открывай нужный, не все подряд. Список заведён кодом — он не\n"
        "протухает.\n"
        + "".join(f"- `{p}`\n" for p in knowledge_index_paths())
        + f"\nВ папке области может лежать `{ARCHIVE_NAME}` — список строк,\n"
          "ссылавшихся на папки, которых уже нет. Инструкцией для модели\n"
          "он не является: разбирает его человек.\n"
          "\nПодробный образец устройства — в `ОБРАЗЕЦ-БАЗЫ.md`.\n"
    ),
    "ПРАВИЛА-ИИ.md": (
        "# Правила для ассистента\n\n"
        "Как вести себя в этой базе. Что где лежит — в `КАРТА-БАЗЫ.md`.\n\n"
        "## Главное\n\n"
        "- Пользователь — не программист: объяснять простым языком,\n"
        "  термины пояснять бытовыми аналогиями.\n"
        "- Отвечать по-русски. Код и имена файлов не переводить.\n\n"
        "## Как вести базу\n\n"
        "- Ничего не удалять — только дополнять. Устаревшее в архив.\n"
        "- Без дубликатов: перед записью проверить, нет ли такого факта.\n"
        "- Только стойкое. Временные детали не пишутся.\n"
        "- С датой в квадратных скобках.\n"
        "- Одна мысль — одна строка.\n\n"
        "## Перед большой работой\n\n"
        "1. Открыть `библиотека/АКТИВНАЯ-ПАМЯТЬ.md` — что было в прошлый раз.\n"
        "2. Посмотреть `facts.md` — правила и ограничения.\n"
        "3. Открыть `инструкции/Скиллы-и-агенты.md` — какой скилл и какого\n"
        "   агента взять под эту задачу.\n\n"
        "## После большой работы\n\n"
        "1. Обновить `библиотека/АКТИВНАЯ-ПАМЯТЬ.md`.\n"
        "2. Дописать в `sessions/<месяц>.md`.\n"
        "3. Если поменяли правило — в `журнал-решений/<месяц>.md` причину.\n\n"
        "## Если результат не устроил\n\n"
        "Не молчать и не чинить наугад. Сказать, что именно не так; найти\n"
        "причину (`systematic-debugging`), а не гадать; при нужде позвать\n"
        "`@retsenzent`; записать вывод в `журнал-решений/`, чтобы ошибка не\n"
        "повторилась. Подробно — в `инструкции/Скиллы-и-агенты.md`.\n\n"
        "## Не гадай — сначала знания\n\n"
        "Если запрос касается темы, под которую в базе есть справочник знаний,\n"
        "— сначала открой его, потом отвечай. Это обязательный шаг.\n"
        "Указатели знаний (открывай нужный, не все подряд):\n"
        + "".join(f"- `{p}`\n" for p in knowledge_index_paths())
        + "- общие знания программиста → `знания/Учёба/Программисту-знать.md`.\n"
        "Если этих файлов в базе нет — так и сказать, а не выдумывать.\n\n"
        "Файл `_потеряно-привязок.md` в папке области — не инструкция.\n"
        "Это список строк, ссылавшихся на папки, которых уже нет. Разбирает\n"
        "его человек, а не модель.\n\n"
        "## Осторожность\n\n"
        "- Внешние действия (письма, публикации) — только с разрешения.\n"
        "- Файлы пользователя не удалять без прямой просьбы.\n"
        "- Секреты (пароли, номера карт) в базу не писать.\n"
    ),
}

ACTIVE_MEMORY = """# Активная память NCP

> Этот файл читается в начале каждой сессии. Держать кратким.

- **Последняя контрольная точка:** не создана
- **Текущая цель:** база создана
- **Текущий статус:** пустая, готова к наполнению

## Важные решения

- База создана программой управления базами.

## Незавершённое

- Наполнить профиль и правила.
"""

LIBRARY_INDEX = {
    "schema": "ncp-library-index-v1",
    "updated": "",
    "count": 0,
    "topics": {},
    "entries": [],
}

#: Имя файла внутри базы, на который вешается ярлык.
#: Ярлык не может открыть папку, зато открывает файл — поэтому в базе
#: лежит небольшой файл-открывалка, показывающий, что это за база.
OPENER_NAME = "Открыть-базу.cmd"


def opener_body(base: Path) -> str:
    """Содержимое файла-открывалки внутри базы.

    Показывает папку базы в Проводнике и короткую справку.
    """
    return (
        "@echo off\r\n"
        "rem Этот файл создан программой управления базой.\r\n"
        "rem На него удобно вешать ярлык: двойной щелчок откроет папку\r\n"
        "rem с этой базой в Проводнике.\r\n"
        "setlocal\r\n"
        'set "ЗДЕСЬ=%~dp0"\r\n'
        "echo.\r\n"
        "echo   База:  %ЗДЕСЬ%\r\n"
        "echo.\r\n"
        "echo   Открываю папку базы...\r\n"
        'start "" explorer "%ЗДЕСЬ%"\r\n'
        "exit /b 0\r\n"
    )


def _rebuild_library_index(base: Path) -> int | None:
    """Пересобирает указатель библиотеки из её файлов. Число или None.

    Зачем отдельной функцией. Указатель - производная величина: он
    обязан отражать то, что лежит в папке записей. Считать это в
    create_base руками нельзя - завтра правила поменяются, и счётчик
    снова разойдётся с диском. Поэтому берём ровно тот код, который
    этим занимается в мосте.

    None означает «не получилось». Это не повод срывать создание базы:
    пустой указатель лучше, чем никакого, а записи в нём появятся при
    первом сохранении.
    """
    import importlib.util
    import sys

    lib = base / "библиотека"
    source = program_root() / "tools" / "ncp-bridge" / "ncp_core.py"
    if not source.is_file() or not lib.is_dir():
        return None
    cache = source.parent / "__pycache__"
    had_cache = cache.is_dir()
    # Запрещаем запись байт-кода на время загрузки. Без этого
    # exec_module создаёт __pycache__ рядом с образцом, и каждая
    # созданная база оставляет мусор в конструкторе. Найдено 29.09
    # прогоном: проверка «в образце моста нет чужого имени» упала
    # на .pyc, который породил мой же код починки.
    was_bytecode = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec = importlib.util.spec_from_file_location(
            "ncp_core_для_новой_базы", source
        )
        if spec is None or spec.loader is None:
            return None
        module = importlib.util.module_from_spec(spec)
        # Модуль кладём в sys.modules: он сам этого требует при
        # наследовании и при повторном вызове в той же сессии.
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        index = module.Library(lib).rebuild_index()
    except Exception:  # noqa: BLE001 — создание базы важнее указателя
        return None
    finally:
        sys.dont_write_bytecode = was_bytecode
        if not had_cache and cache.is_dir():
            shutil.rmtree(cache, ignore_errors=True)
    count = index.get("count") if isinstance(index, dict) else None
    return count if isinstance(count, int) else None


def create_base(plan: CreationPlan, progress=None) -> list[str]:
    """Выполняет план: создаёт папки и файлы. Возвращает список сообщений."""
    log: list[str] = []

    def say(text: str) -> None:
        log.append(text)
        if progress:
            progress(text)

    target = plan.target
    target.mkdir(parents=True, exist_ok=True)
    say(f"Создана папка: {target}")

    for name in NEW_DIRS:
        folder = target / name
        folder.mkdir(parents=True, exist_ok=True)
    say(f"Создано папок: {len(NEW_DIRS)}")

    # Области знаний — по папке на область, чтобы сразу было видно,
    # куда раскладывать материалы. Создаются только если их ещё нет.
    areas_made = 0
    for area in KNOWLEDGE_AREAS:
        folder = target / "знания" / area
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            areas_made += 1
    if areas_made:
        say(f"Областей знаний создано: {areas_made}")

    # Подпапки внутри областей — группы людей, виды имущества и ветка
    # безопасности. Третий уровень создаётся вместе со вторым: иначе
    # папка безопасности была бы пустой, а AGENTS.md отправляет читать
    # подсказку именно оттуда.
    subs_made = 0
    for name in knowledge_folders():
        if name.count("/") < 2:      # только области
            continue
        folder = target / name
        if not folder.is_dir():
            folder.mkdir(parents=True, exist_ok=True)
            subs_made += 1
    if subs_made:
        say(f"Подпапок в областях создано: {subs_made}")

    if plan.template_from:
        copied = _copy_template(plan.template_from, target)
        say(f"Скопировано из образца: {', '.join(copied)}")
    else:
        for name, body in BLANK.items():
            (target / name).write_text(body, encoding="utf-8")
        say(f"Создано файлов: {len(BLANK)}")
        # Наполнение пустой базы из главной базы (где живёт программа):
        # справочники знаний, скиллы, конфиг с плагином и записи библиотеки.
        # Личные файлы (profile/facts/projects) остаются пустыми заготовками.
        filled = (
            _copy_service_files(program_root(), target)
            + _copy_ref_files(program_root(), target)
            + _copy_lib_docs(program_root(), target)
            + _copy_lib_records(program_root(), target)
            + _copy_skills(program_root(), target)
            + _copy_instructions(program_root(), target)
            + _copy_config(program_root(), target)
            + _copy_antiblock(program_root(), target)
        )
        if filled:
            say(f"Наполнено из главной базы: {', '.join(filled)}")

    # Файлы-навигаторы. Нужны в любом случае: у базы из образца они
    # уже скопированы, у пустой — создаются здесь.
    nav_made = 0
    for name, body in NAV_BLANK.items():
        dest = target / name
        if not dest.exists():
            dest.write_text(body, encoding="utf-8")
            nav_made += 1
    if nav_made:
        say(f"Создано файлов-навигаторов: {nav_made}")

    # Пояснения «_О-ПАПКЕ.md» — если их не принёс образец, пишем краткие
    # заготовки, чтобы папки не выглядели пустыми и брошенными.
    # Проверяем не «папка пуста», а «нет ли уже пояснения»: у папки
    # с подпапками внутри файла может не быть.
    hints_made = 0
    folders = list(NEW_DIRS) + knowledge_folders()
    for name in folders:
        folder = target / name
        if not folder.is_dir():
            continue
        hint = folder / "_О-ПАПКЕ.md"
        if hint.exists():
            continue
        note = name.replace("/", " → ")
        hint.write_text(
            f"# Папка «{note}»\n\n"
            "Что сюда кладут и как этим пользоваться — в файле\n"
            "`КАРТА-БАЗЫ.md` в корне базы.\n",
            encoding="utf-8",
        )
        hints_made += 1
    if hints_made:
        say(f"Создано пояснений к папкам: {hints_made}")

    # Мосты NCP и PC живут внутри базы — копируем из конструктора,
    # чтобы при подключении базы они уже были на месте.
    for bridge in ("ncp-bridge", "pc-bridge"):
        src_bridge = program_root() / "tools" / bridge
        dst_bridge = target / "tools" / bridge
        if src_bridge.is_dir() and not dst_bridge.is_dir():
            try:
                shutil.copytree(
                    src_bridge, dst_bridge,
                    # config.json едет вместе с мостом: в образце пути —
                    # пометки {{LIBRARY}} и {{BASE}}, мост разворачивает их
                    # сам. Раньше файл исключался, и мост оставался без
                    # настроек, то есть нерабочим (core.configure_bridges).
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                say(f"Мост {bridge} скопирован в базу")
            except OSError as exc:
                say(f"Мост {bridge}: не удалось скопировать ({exc})")

    # Мосты должны быть рабочими сразу после создания базы, а не только
    # после подключения: достраиваем config.json и вписываем путь к библиотеке.
    for message in refresh_bridge_code(target):
        say(message)
    for message in configure_bridges(target):
        say(message)

    # Скиллы и индекс: новая база рождается с полным набором, и обновляется
    # до актуальной версии, если конструктор новее.
    for message in refresh_skills(target):
        say(message)
    for message in refresh_instructions(target):
        say(message)
    # Указатели знаний — тоже часть новой базы: без них знания лежат, а
    # карты к ним нет, и модель не знает, куда смотреть.
    for message in refresh_knowledge_indexes(target):
        say(message)

    # Агенты едут с базой, как и мосты: без них база на другом компьютере
    # осталась бы без @proektirovschik и остальных одиннадцати.
    agents_src = program_root() / "tools" / "agents"
    agents_dst = target / "tools" / "agents"
    if agents_src.is_dir() and not agents_dst.is_dir():
        try:
            shutil.copytree(
                agents_src, agents_dst,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            say(f"Агенты скопированы в базу ({len(list(agents_dst.glob('*.md')))} штук)")
        except OSError as exc:
            say(f"Агенты: не удалось скопировать ({exc})")

    # AGENTS.md в корень базы. Без него подключение сообщает «AGENTS.md нет
    # в базе — из конфига убран», и нейросеть остаётся вовсе без правил.
    # Образец берём из config/ того же конструктора: он уже копируется
    # в базу, личных данных в нём быть не должно.
    root_agents = target / "AGENTS.md"
    if not root_agents.is_file():
        for candidate in (target / "config" / "AGENTS.md", program_root() / "config" / "AGENTS.md"):
            if candidate.is_file():
                try:
                    shutil.copy2(candidate, root_agents)
                    say("AGENTS.md положен в корень базы (правила для нейросети)")
                except OSError as exc:
                    say(f"AGENTS.md в корень не скопирован ({exc})")
                break

    # Решение человека о чувствительных данных: пишется файлом, чтобы
    # правило жило вместе с базой и было видно без программы.
    pol = target / SENSITIVE_FILE
    if not pol.exists():
        pol.parent.mkdir(parents=True, exist_ok=True)
        pol.write_text(
            sensitive_policy(plan.allow_sensitive), encoding="utf-8"
        )
        say(
            "Чувствительные данные: "
            + ("разрешено" if plan.allow_sensitive else "запрещено")
        )

    memory = target / "библиотека/АКТИВНАЯ-ПАМЯТЬ.md"
    if not memory.exists():
        memory.write_text(ACTIVE_MEMORY, encoding="utf-8")

    # Указатель библиотеки собираем из файлов, а не берём из шаблона.
    # Найдено 29.09: записи библиотеки копируются из конструктора, а
    # индекс писался пустым шаблоном и объявлял count: 0 при двух
    # записях на диске. Каждая новая база рождалась с врущим
    # указателем, и заметить это было некому: спрашивали только
    # «есть ли файл», а он был.
    index = target / "библиотека/index.json"
    if not index.exists():
        rebuilt = _rebuild_library_index(target)
        if rebuilt:
            say(f"Указатель библиотеки собран из файлов: записей {rebuilt}")
        else:
            data = dict(LIBRARY_INDEX)
            data["updated"] = datetime.now().isoformat(timespec="seconds")
            index.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            say("Указатель библиотеки записан пустым: пересборка не удалась, "
                "записи появятся в нём после первого сохранения")

    marker = target / "база.json"
    marker.write_text(
        json.dumps(
            {
                "kind": "opencode-base",
                "name": plan.name,
                "created": datetime.now().isoformat(timespec="seconds"),
                "created_by": "tools/dbapp",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    say("Записана отметка о создании базы")

    opener = target / OPENER_NAME
    opener.write_text(opener_body(target), encoding="utf-8")
    say(f"Создан файл для ярлыка: {OPENER_NAME}")

    # запоминаем базу в списке, чтобы её было легко найти при подключении
    remember_base(target)
    say("База записана в список созданных")
    return log


#: Решение человека о чувствительных данных. Живёт в базе файлом, а не
#: в инструкции: правило должно переезжать вместе с базой и читаться
#: без программы.
SENSITIVE_FILE = "настройки/чувствительные-данные.md"


def sensitive_policy(allowed: bool) -> str:
    """Текст решения: можно ли в базе хранить пароли и ключи."""
    if allowed:
        return (
            "# Чувствительные данные — РАЗРЕШЕНО\n\n"
            "Так решил человек при создании этой базы.\n\n"
            "## Что можно\n\n"
            "- По прямой просьбе человека записывать пароли, ключи, "
            "токены и прочие доступы.\n"
            "- Класть их в тему `личное/доступы`: так их проще найти, и "
            "в новые базы они не уедут.\n\n"
            "## Чего всё равно нельзя\n\n"
            "- Записывать без прямой просьбы. Разрешение — это «можно», "
            "а не «надо».\n"
            "- Выкладывать секреты в ответ, если человек не просил.\n"
            "- Копировать их в другие базы, репозитории или куда-то ещё.\n\n"
            "## Если решение нужно поменять\n\n"
            "Файл переписывается вручную. Нейросеть читает этот файл "
            "и подчиняется ему, а не своей памяти о том, что было "
            "раньше.\n"
        )
    return (
        "# Чувствительные данные — ЗАПРЕЩЕНО\n\n"
        "Так решил человек при создании этой базы.\n\n"
        "## Что это значит\n\n"
        "- Пароли, ключи, токены и прочие доступы в эту базу не "
        "записываются.\n"
        "- Даже если человек попросит записать — не записывать, а "
        "предложить хранилище, где это уместно: менеджер паролей или "
        "зашифрованный файл.\n"
        "- Сами секреты не повторять в ответе.\n\n"
        "## Почему так\n\n"
        "База — это обычные текстовые файлы. Кто-то может её открыть, "
        "скопировать или отправить, а пароль в открытом тексте "
        "разлетается дальше, чем предполагалось.\n\n"
        "## Если решение нужно поменять\n\n"
        "Файл переписывается вручную. Нейросеть читает этот файл "
        "и подчиняется ему.\n"
    )


def _copy_ref_files(source: Path, target: Path) -> list[str]:
    """Справочники знаний: Программисту-знать + вся папка Безопасности.

    Только .md, картинок в образце нет — вес маленький (~14 МБ текста).
    """
    copied: list[str] = []
    ref_files: list = [source / "знания/Учёба/Программисту-знать.md"]
    src_bez = source / "знания/Техника/Безопасность"
    if src_bez.is_dir():
        ref_files += sorted(src_bez.rglob("*.md"))
    # Остальные области знаний тоже едут. Раньше копировались ровно два
    # слоя — Программисту-знать и вся Безопасность, — и новая база
    # получала 1047 файлов из девяти областей. Семь оставались позади
    # навсегда, а указатель для них создавать было нечего: он без
    # содержимого бесполезен.
    src_zn = source / "знания"
    if src_zn.is_dir():
        for area in sorted(p for p in src_zn.iterdir() if p.is_dir()):
            if area.name == "Техника":
                continue
            ref_files += [f for f in sorted(area.rglob("*.md"))
                          if "Безопасность" not in f.parts]
    for src in ref_files:
        try:
            rel = src.relative_to(source)
        except ValueError:
            continue
        if not src.is_file():
            continue
        dest = target / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    if (target / "знания/Учёба/Программисту-знать.md").is_file():
        copied.append("знания/Учёба/Программисту-знать.md")
    if any((target / "знания/Техника/Безопасность").rglob("*.md")):
        copied.append("знания/Техника/Безопасность (текст)")
    extra = sorted(p.name for p in (target / "знания").iterdir()
                   if p.is_dir() and p.name not in {"Техника", "Учёба"}) \
        if (target / "знания").is_dir() else []
    if extra:
        copied.append("знания: " + ", ".join(extra))
    return copied


def _copy_lib_records(source: Path, target: Path) -> list[str]:
    """Справочные записи библиотеки NCP: указатели, чтобы поиск работал сразу.

    Личные записи (папка «личное») в новые базы НЕ копируются: они — часть
    конкретной базы, а не эталон конструктора. Справочные записи — общие
    знания (справочники, конспекты), они едут в каждую новую базу.
    """
    copied: list[str] = []
    src_lib = source / "библиотека/записи"
    if not src_lib.is_dir():
        return copied
    lib_records = sorted(src_lib.rglob("*.md"))
    for src in lib_records:
        rel = src.relative_to(src_lib)
        # Личные записи (подпапка «личное») в эталон не входят.
        if rel.parts and rel.parts[0] == "личное":
            continue
        dest = target / "библиотека/записи" / rel
        if dest.exists():
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
    if lib_records:
        copied.append(f"библиотека/записи ({len(lib_records)} файлов)")
    return copied


def _copy_lib_docs(source: Path, target: Path) -> list[str]:
    """Служебные файлы самой библиотеки: протокол, каталог, память."""
    copied: list[str] = []
    src_lib = source / "библиотека"
    dst_lib = target / "библиотека"
    if not src_lib.is_dir():
        return copied
    for name in ("NCP.md", "README.md", "АКТИВНАЯ-ПАМЯТЬ.md", "КАТАЛОГ.md"):
        src = src_lib / name
        if src.is_file() and not (dst_lib / name).exists():
            shutil.copy2(src, dst_lib / name)
            copied.append(f"библиотека/{name}")
    return copied


def _copy_instructions(source: Path, target: Path) -> list[str]:
    """Человеческие инструкции по устройству базы."""
    copied: list[str] = []
    src_inst = source / "инструкции"
    dst_inst = target / "инструкции"
    if not src_inst.is_dir():
        return copied
    for item in sorted(src_inst.glob("*.md")):
        if not (dst_inst / item.name).exists():
            shutil.copy2(item, dst_inst / item.name)
            copied.append(f"инструкции/{item.name}")
    return copied


def _copy_service_files(source: Path, target: Path) -> list[str]:
    """Служебные файлы папок: пояснения «_О-ПАПКЕ.md» и заготовки
    «_ШАБЛОН-….md». Имя, начинающееся с «_», — признак служебного
    файла: свои записи пользователь так не называет. Поэтому копируем
    именно по этому признаку и ничего лишнего не задеваем.
    """
    copied: list[str] = []
    places = list(NEW_DIRS) + knowledge_folders()
    for name in places:
        folder = source / name
        if not folder.is_dir():
            continue
        for src in sorted(folder.glob("_*.md")):
            dest = target / name / src.name
            if dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            copied.append(f"{name}/{src.name}")
    return copied


def _file_hash(path: Path) -> str:
    """Хэш файла — короткий, хватает для сравнения."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_hashes(folder: Path) -> dict[str, str]:
    """Хэши всех файлов папки по относительному пути."""
    out: dict[str, str] = {}
    for item in sorted(folder.rglob("*")):
        if item.is_file() and "__pycache__" not in item.parts:
            out[str(item.relative_to(folder))] = _file_hash(item)
    return out


def _read_manifest(dst_skills: Path) -> dict:
    """Что мы ставили раньше. Нет файла — значение неизвестно."""
    path = dst_skills / SKILL_MANIFEST
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write_manifest(dst_skills: Path, skills: dict, index_hash: str,
                    files: dict | None = None) -> None:
    """Запоминаем, что было поставлено в эту базу."""
    dst_skills.mkdir(parents=True, exist_ok=True)
    payload = {"skills": skills, "index": index_hash, "files": files or {}}
    (dst_skills / SKILL_MANIFEST).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _index_records(path: Path) -> dict:
    """Записи индекса навыков по имени. Нет файла — пусто."""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    items = data if isinstance(data, list) else data.get("skills", [])
    if not isinstance(items, list):
        return {}
    return {str(x.get("name")): x for x in items if isinstance(x, dict)}


def _skill_folders(folder: Path) -> set[str]:
    """Названия навыков — те папки, где есть SKILL.md."""
    if not folder.is_dir():
        return set()
    return {p.name for p in folder.iterdir()
            if p.is_dir() and (p / SKILL_MARKER).is_file()
            and not p.name.startswith(".")}


def _loose_files(folder: Path) -> set[str]:
    """Файлы рядом с папками навыков: описания и указатели.

    Только имена, начинающиеся с «_»: таким признаком помечены служебные
    файлы папок, свои записи пользователь так не называет. Всё прочее —
    мусор, который в базу не едет.
    """
    if not folder.is_dir():
        return set()
    return {p.name for p in folder.iterdir()
            if p.is_file() and p.name.startswith("_")
            and p.name.endswith(".md") and p.name != SKILL_MANIFEST}


def compare_skills(master: Path, other: Path, expect: str = "full") -> dict:
    """Сравнивает две копии навыков и говорит, чем они расходятся.

    Сравнение идёт по смыслу, а не по байтам: индекс в живой базе может отличаться
    разрядкой, и это не беда. Беда — когда разошлись сами навыки или состав индекса.

    Расхождение бьёт двумя: `stale` — навык отстал, его можно обновить;
    `manual` — человек правил руками, трогать нельзя.

    `expect` — чего ждём от копии. `"full"` — и навыки, и индекс, и файлы рядом.
    `"skills-only"` — только папки навыков: столько копирует плагин opencode в папку настроек,
    а индекс и описания туда не едут вовсе.
    """
    only_skills = expect == "skills-only"
    m_sk, o_sk = master / "skills", other / "skills"
    m_names, o_names = _skill_folders(m_sk), _skill_folders(o_sk)

    shipped = _read_manifest(o_sk).get("skills")
    shipped = shipped if isinstance(shipped, dict) else {}
    stale: list[str] = []
    manual: list[str] = []
    for name in sorted(m_names & o_names):
        want = _tree_hashes(m_sk / name)
        have = _tree_hashes(o_sk / name)
        if want == have:
            continue
        was = shipped.get(name)
        if isinstance(was, dict) and have == was:
            stale.append(name)
        else:
            manual.append(name)

    m_idx = _index_records(master / "skills-index.json")
    o_idx = _index_records(other / "skills-index.json")
    m_idx_file = master / "skills-index.json"
    o_idx_file = other / "skills-index.json"
    both_idx = m_idx_file.is_file() and o_idx_file.is_file()

    return {
        "master": master,
        "other": other,
        "missing": sorted(m_names - o_names),
        "extra": sorted(o_names - m_names),
        "stale": stale,
        "manual": manual,
        "loose_missing": [] if only_skills else sorted(_loose_files(m_sk) - _loose_files(o_sk)),
        "loose_changed": [] if only_skills else sorted(
            n for n in _loose_files(m_sk) & _loose_files(o_sk)
            if _file_hash(m_sk / n) != _file_hash(o_sk / n)),
        "loose_extra": [] if only_skills else sorted(_loose_files(o_sk) - _loose_files(m_sk)),
        "index_absent": (not o_idx_file.is_file()) and not only_skills,
        "index_missing": [] if only_skills else sorted(set(m_idx) - set(o_idx)),
        "index_extra": [] if only_skills else sorted(set(o_idx) - set(m_idx)),
        "index_changed": [] if only_skills else sorted(
            k for k in set(m_idx) & set(o_idx) if m_idx[k] != o_idx[k]),
        "index_bytes_differ": (not only_skills) and both_idx
        and _file_hash(m_idx_file) != _file_hash(o_idx_file),
        "expect": expect,
        "counts": (len(m_names), len(o_names)),
    }


def skills_findings(result: dict) -> list[str]:
    """Расхождения по-человески, по одной строке на каждое. Пусто — копии совпадают."""
    out: list[str] = []
    if result["missing"]:
        out.append(f"навыков нет: {', '.join(result['missing'])}")
    if result["extra"]:
        out.append(f"лишние навыки: {', '.join(result['extra'])}")
    if result["stale"]:
        out.append(f"навыки отстали от мастера: {', '.join(result['stale'])}")
    if result["manual"]:
        out.append(f"навыки изменены руками: {', '.join(result['manual'])}")
    if result["loose_missing"]:
        out.append(f"нет файлов рядом с навыками: {', '.join(result['loose_missing'])}")
    _loose_diff = result.get("loose_changed") or []
    if _loose_diff:
        out.append("файлы рядом с навыками расходятся содержимым: "
                   + ", ".join(_loose_diff))
    if result["loose_extra"]:
        out.append(f"лишние файлы рядом с навыками: {', '.join(result['loose_extra'])}")
    if result["index_absent"]:
        out.append("нет файла skills-index.json")
    if result["index_missing"]:
        out.append(f"в индексе нет: {', '.join(result['index_missing'])}")
    if result["index_extra"]:
        out.append(f"в индексе лишнее: {', '.join(result['index_extra'])}")
    if result["index_changed"]:
        out.append(f"в индексе разошлись записи: {', '.join(result['index_changed'])}")
    return out


def _copy_skills(source: Path, target: Path) -> list[str]:
    """Скиллы: папки с SKILL.md плюс индекс сценариев.

    Навык, который никто не трогал, — обновляется.
    Навык, изменённый руками, — не трогаем, чтобы ничего не пропало.
    """
    copied: list[str] = []
    src_skills = source / "skills"
    if not src_skills.is_dir():
        return copied
    dst_skills = target / "skills"
    manifest = _read_manifest(dst_skills)
    shipped = manifest.get("skills")
    if not isinstance(shipped, dict):
        shipped = {}

    for item in sorted(src_skills.iterdir()):
        if not item.is_dir():
            continue
        if not (item / SKILL_MARKER).is_file():
            continue
        sub = item.name
        if sub.startswith("."):
            continue
        dest = dst_skills / sub
        want = _tree_hashes(item)
        if not dest.exists():
            shutil.copytree(item, dest, ignore=shutil.ignore_patterns("__pycache__"))
            copied.append(f"скилл {sub}")
        else:
            have = _tree_hashes(dest)
            old = shipped.get(sub)
            if have == want:
                pass
            elif isinstance(old, dict) and have == old:
                shutil.rmtree(dest)
                shutil.copytree(item, dest, ignore=shutil.ignore_patterns("__pycache__"))
                copied.append(f"скилл {sub} обновлён")
            else:
                copied.append(f"скилл {sub} изменён в базе — не тронут")
                continue
        shipped[sub] = want

    # Файлы рядом с навыками — часть пакета навыков. У новых баз они есть,
    # а в существующих остались недоделанными: на 16 описанных навыков
    # при 32 навыках. Правило то же, что у самих навыков.
    shipped_files = manifest.get("files")
    shipped_files = shipped_files if isinstance(shipped_files, dict) else {}
    for name in sorted(_loose_files(src_skills)):
        src_f = src_skills / name
        dst_f = dst_skills / name
        want_f = _file_hash(src_f)
        if not dst_f.is_file():
            shutil.copy2(src_f, dst_f)
            copied.append(f"файл {name}")
        elif _file_hash(dst_f) != want_f:
            if shipped_files.get(name) == _file_hash(dst_f):
                shutil.copy2(src_f, dst_f)
                copied.append(f"файл {name} обновлён")
            else:
                copied.append(f"файл {name} изменён в базе — не тронут")
                continue
        shipped_files[name] = want_f

    index_src = program_file("skills-index.json", source)
    index_dst = target / "skills-index.json"
    want_index = ""
    if index_src.is_file():
        want_index = _file_hash(index_src)
        if not index_dst.exists():
            shutil.copy2(index_src, index_dst)
            copied.append("skills-index.json")
        else:
            have_index = _file_hash(index_dst)
            old_index = manifest.get("index")
            if have_index != want_index:
                if old_index and have_index == old_index:
                    shutil.copy2(index_src, index_dst)
                    copied.append("skills-index.json обновлён")
                elif manifest:
                    copied.append("skills-index.json изменён в базе — не тронут")

    _write_manifest(dst_skills, shipped, want_index, shipped_files)
    return copied
    dst_skills = target / "skills"
    for item in sorted(src_skills.iterdir()):
        if not item.is_dir():
            continue
        if not (item / SKILL_MARKER).is_file():
            continue
        sub = item.name
        if sub.startswith("."):
            continue
        dest = dst_skills / sub
        if dest.exists():
            continue
        shutil.copytree(item, dest, ignore=shutil.ignore_patterns("__pycache__"))
        copied.append(f"скилл {sub}")
    index_src = program_file("skills-index.json", source)
    if index_src.is_file():
        index_dst = target / "skills-index.json"
        if not index_dst.exists():
            shutil.copy2(index_src, index_dst)
            copied.append("skills-index.json")
    return copied


#: Набор обхода блокировок, который едет в новую базу как есть.
#: Это файлы из tools/antiblock главной базы: фасад-переводчик,
#: списки, запускалки и команда /обход.
ANTIBLOCK_FILES = (
    "http_facade.py",
    "dns_resolver.py",
    "pool_refresh.py",
    "xray_runner.py",
    "check_nodes.py",
    "refresh_public_nodes.py",
    "start_opencode_proxy.cmd",
    "start_opencode_proxy.ps1",
    "public_socks5.txt",
    "subscriptions.txt",
    "ЧИТАТЬ-МЕНЯ.txt",
)

#: Команда /обход внутри набора.
ANTIBLOCK_COMMAND = ("command", "antiblock.md")

#: Окончания личных рабочих файлов обхода — в новую базу не едут.
#: Живой список (*.local.*) и логи появятся у человека сами после
#: кнопки «Обновить». В новой базе будет чистый стартовый список.
ANTIBLOCK_SKIP_SUFFIXES = (".local.txt", ".local.json", ".bak", ".log")


def _copy_antiblock(source: Path, target: Path) -> list[str]:
    """Набор обхода блокировок: фасад, списки, запускалки, команда.

    Копирует tools/antiblock из главной базы в новую, не задевая уже
    имеющееся. Личные рабочие файлы (*.local.*, логи, кэш) не едут.
    """
    copied: list[str] = []
    src_ab = Path(source) / "tools" / "antiblock"
    if not src_ab.is_dir():
        return copied
    dst_ab = Path(target) / "tools" / "antiblock"
    count = 0
    for name in ANTIBLOCK_FILES:
        src = src_ab / name
        if not src.is_file():
            continue
        dest = dst_ab / name
        if dest.exists():
            continue
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            count += 1
        except OSError:
            pass
    cmd_src = src_ab.joinpath(*ANTIBLOCK_COMMAND)
    cmd_dst = dst_ab.joinpath(*ANTIBLOCK_COMMAND)
    if cmd_src.is_file() and not cmd_dst.exists():
        try:
            cmd_dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(cmd_src, cmd_dst)
            count += 1
        except OSError:
            pass
    if count:
        copied.append(f"обход блокировок ({count} файлов)")
    return copied


def _copy_config(source: Path, target: Path) -> list[str]:
    """Папка config с плагином памяти и зависимостями.

    Пути в настройках переписываем под новое место базы: в источнике
    они указывают на старую папку и после копирования станут ложными.
    """
    copied: list[str] = []
    src_cfg = find_config_source(source)
    if src_cfg is None:
        return copied
    dst_cfg = target / "config"
    for name in CONFIG_FILES + (".gitignore",):
        src = src_cfg / name
        if src.is_file() and not (dst_cfg / name).exists():
            dst_cfg.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst_cfg / name)
            copied.append(f"config/{name}")
    for folder in CONFIG_DIRS:
        src = src_cfg / folder
        if src.is_dir():
            try:
                shutil.copytree(
                    src, dst_cfg / folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
                copied.append(f"config/{folder}")
            except OSError:
                pass
    # Готовые зависимости — чтобы плагин работал без интернета.
    src_deps = src_cfg / "node_modules"
    if (src_deps / "@opencode-ai" / "plugin" / "package.json").is_file():
        try:
            shutil.copytree(
                src_deps, dst_cfg / "node_modules", dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            copied.append("config/node_modules")
        except OSError:
            pass
    if (dst_cfg / INSTRUCTIONS_FILE).is_file():
        try:
            (dst_cfg / INSTRUCTIONS_FILE).write_text(
                build_instructions(target), encoding="utf-8"
            )
            copied.append("config/opencode.jsonc (пути обновлены)")
        except OSError:
            pass
    return copied


def _copy_template(template: Path, target: Path) -> list[str]:
    """Переносит образец базы, не перезаписывая существующее."""
    copied: list[str] = []
    for name in REQUIRED_FILES:
        src = program_file(name, template)
        if src.is_file():
            shutil.copy2(src, target / name)
            copied.append(name)

    # Файлы-навигаторы: карта базы и правила поведения. Без них нейросеть
    # не знает об устройстве базы и не пользуется её разделами.
    for name in NAV_FILES:
        src = program_file(name, template)
        if src.is_file() and not (target / name).exists():
            shutil.copy2(src, target / name)
            copied.append(name)

    copied += _copy_service_files(template, target)
    copied += _copy_ref_files(template, target)
    copied += _copy_lib_records(template, target)

    copied += _copy_skills(template, target)

    copied += _copy_config(template, target)

    copied += _copy_antiblock(template, target)

    copied += _copy_lib_docs(template, target)
    copied += _copy_instructions(template, target)
    return copied


# ---------------------------------------------------------------- скиллы

#: Скиллы, которые лежат в базе, но в папку программы не переносятся.
#: Здесь — имена для будущих исключений.
SKILLS_SKIP = ()


def list_skills(base: Path) -> list[dict[str, str]]:
    """Возвращает скиллы базы: имя, описание, папка.

    Скиллом считается папка с файлом SKILL.md. Описание берётся
    из шапки этого файла — чтобы в окне было видно, что скилл делает,
    а не только его название.
    """
    folder = Path(base) / "skills"
    found: list[dict[str, str]] = []
    if not folder.is_dir():
        return found
    for item in sorted(folder.iterdir(), key=lambda p: p.name.lower()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        if not (item / SKILL_MARKER).is_file():
            continue
        if item.name in SKILLS_SKIP:
            continue
        found.append({
            "name": item.name,
            "title": skill_title(item) or item.name,
            "description": skill_description(item),
            "path": str(item),
        })
    return found


def skill_title(skill_dir: Path) -> str:
    """Читает поле name из шапки SKILL.md."""
    head = _read_skill_head(skill_dir)
    for line in head:
        text = line.strip()
        if text.lower().startswith("name:"):
            return text.split(":", 1)[1].strip().strip('"').strip("'")
    return ""


def skill_item_text(skill: dict[str, str], width: int = 92) -> str:
    """Текст строки списка навыков: название и одна короткая строка описания.

    **Почему одна строка, а не перенос по ширине.** Перенос на 68 знаков
    давал по пять строк на навык, по 58 пикселей высотой, и тридцать два
    навыка растягивались на 4,6 экрана прокрутки. Работало, но листать
    было неудобно. Одна строка — это 24 пикселя на навык, и все 32
    укладываются в полтора экрана.

    **Почему длина фиксирована, а не по ширине окна.** Без горизонтальной
    полосы длинная строка просто обрезается краем списка, и потерянное
    некуда посмотреть. Строка урезается по ширине с многоточием, а полное
    описание остаётся в подсказке по наведению — там оно целиком.

    Переносы строк внутри не делаются: Qt считает высоту по явным переводам
    строк, и строка в две строки даёт ровно две строки. Собственный перенос
    Qt по ширине для этого не нужен и не полезен.
    """
    import textwrap

    title = str(skill.get("title") or skill.get("name") or "")
    desc = str(skill.get("description") or "")
    if not desc:
        return title
    # Мягкий перенос по пробелу, чтобы не разрезать слово.
    lines = textwrap.wrap(desc, width=width, break_long_words=False,
                          break_on_hyphens=False)
    first = lines[0] if lines else desc[:width]
    if len(lines) > 1 or len(desc) > width:
        first = first.rstrip(" ,.;:—-") + "…"
    return title + "\n" + first


def skill_description(skill_dir: Path) -> str:
    """Читает поле description из шапки SKILL.md.

    Описание бывает перенесено на следующую строку (YAML-складка,
    значок «>»), поэтому собираем продолжение, пока идут отступы.
    """
    head = _read_skill_head(skill_dir)
    parts: list[str] = []
    collecting = False
    for line in head:
        text = line.rstrip()
        if text.strip().lower().startswith("description:"):
            parts.append(text.split(":", 1)[1].strip().lstrip(">|-").strip())
            collecting = True
            continue
        if collecting:
            # продолжение — строки с отступом; пустая строка или новое
            # поле вида «license:» означают конец описания
            if not text.strip() or not text.startswith((" ", "\t")):
                break
            parts.append(text.strip())
    text = " ".join(p for p in parts if p).strip()
    return text


def _read_skill_head(skill_dir: Path) -> list[str]:
    """Первые строки SKILL.md до второго разделителя «---»."""
    try:
        raw = (skill_dir / SKILL_MARKER).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    lines = raw.splitlines()
    if not lines or lines[0].strip() != "---":
        return lines[:20]
    head: list[str] = []
    for line in lines[1:]:
        if line.strip() == "---":
            break
        head.append(line)
    return head


# ---------------------------------------------------------------- подключение


@dataclass
class AttachResult:
    ok: bool
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    skills_dir: Path | None = None


def ncp_bridge_dir(base: Path) -> Path:
    """Папка моста NCP внутри базы."""
    return base / "tools" / "ncp-bridge"


#: Мосты, которые живут внутри базы. Образец — в конструкторе, рабочая копия —
#: в базе. Имя папки совпадает с именем в конструкторе.
BASE_BRIDGES = ("ncp-bridge", "pc-bridge")


def bridge_dir(base: Path, name: str) -> Path:
    """Папка моста внутри базы."""
    return base / "tools" / name


def bridge_template_config(name: str) -> Path | None:
    """Образец config.json моста из конструктора."""
    return program_root() / "tools" / name / "config.json"


def _console_python() -> str:
    """Консольный Python для MCP-сервера.

    Мост печатает ответы в поток вывода, поэтому pythonw.exe (без консоли)
    не годится: заменяем его на python.exe рядом, а если нет — ищем python
    в пути.
    """
    exe = sys.executable
    if exe.lower().endswith("pythonw.exe"):
        candidate = exe[:-4] + ".exe"
        if os.path.isfile(candidate):
            return candidate
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    return exe


def ensure_bridge_config(base: Path, name: str) -> list[str]:
    """Достраивает config.json моста в базе, если его нет.

    Раньше копирование моста его исключало, и мост оставался без настроек.
    Здесь недостающее берётся из конструктора: это образец с пометками
    {{LIBRARY}} и {{BASE}}, которые мост разворачивает сам. Никаких личных
    путей в конструкторе не появляется.
    """
    messages: list[str] = []
    config_file = bridge_dir(base, name) / "config.json"
    if config_file.is_file():
        return messages
    template = bridge_template_config(name)
    if not (bridge_dir(base, name) / BRIDGE_SERVER).is_file():
        messages.append(f"Мост {name}: в базе нет папки моста — config.json некуда класть")
        return messages
    if template is None or not template.is_file():
        messages.append(f"Мост {name}: в конструкторе нет образца config.json")
        return messages
    try:
        config_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template, config_file)
    except OSError as exc:
        messages.append(f"Мост {name}: не удалось создать config.json ({exc})")
        return messages
    messages.append(f"Мост {name}: config.json достроен в базу из образца")
    return messages


def configure_ncp_bridge(base: Path) -> list[str]:
    """Вписывает путь к библиотеке в config.json моста NCP внутри базы.

    Образец моста лежит в tools/ncp-bridge и приезжает с пометкой {LIBRARY}
    вместо настоящего пути: без него сервер не откроет библиотеку. Путь уже
    верный — ничего не меняем.
    """
    messages: list[str] = []
    config_file = ncp_bridge_dir(base) / "config.json"
    if not config_file.is_file():
        # Раньше тут был молчаливый return, и поломка оставалась незамеченной:
        # мост без config.json отвечает «не указан путь к библиотеке».
        messages.append("Мост NCP: config.json отсутствует — путь вписать некуда")
        return messages
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        messages.append("Мост NCP: config.json не читается — путь не вписан")
        return messages
    library = (base / "библиотека").as_posix()
    current = str(config.get("library_path", "")).replace("\\", "/")
    if current in ("", "{{LIBRARY}}", "/"):
        config["library_path"] = library
    elif current != library:
        config["library_path"] = library
        messages.append(f"Мост NCP: путь к библиотеке обновлён (было: {current})")
    else:
        return messages
    try:
        config_file.write_text(
            json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        messages.append("Мост NCP: вписан путь к библиотеке")
    except OSError as exc:
        messages.append(f"Мост NCP: не удалось записать путь: {exc}")
    return messages


def configure_bridges(base: Path) -> list[str]:
    """Готовит мосты внутри базы: достраивает config.json, вписывает путь.

    Точка входа одна и для создания базы, и для подключения — мосты должны
    быть рабочими в обоих случаях. Мост ПК сам разворачивает {{BASE}} в свою
    базу, поэтому для него достаточно достроить файл.
    """
    messages: list[str] = []
    for name in BASE_BRIDGES:
        messages.extend(ensure_bridge_config(base, name))
    messages.extend(configure_ncp_bridge(base))
    return messages


def refresh_bridge_code(base: Path) -> list[str]:
    """Обновляет код мостов в базе из конструктора.

    Код моста — часть программы, а не данные базы: он обязан совпадать с
    образцом, иначе мост не умеет то, чему научился. Найдено живьём: в
    существующих базах лежал старый мост ПК без разворачивания пометки
    {{BASE}}, и он не мог открыть ничего, кроме себя.

    config.json НЕ трогаем: там путь к библиотеке этой базы, он живой.
    """
    messages: list[str] = []
    for name in BASE_BRIDGES:
        src = program_root() / "tools" / name
        dst = base / "tools" / name
        if not src.is_dir() or not dst.is_dir():
            continue
        for item in sorted(src.iterdir()):
            if item.name in ("config.json", "__pycache__") or item.is_dir():
                continue
            target = dst / item.name
            was_there = target.is_file()
            try:
                if was_there and target.read_bytes() == item.read_bytes():
                    continue
                shutil.copy2(item, target)
            except OSError as exc:
                messages.append(f"Мост {name}: не удалось обновить {item.name} ({exc})")
                continue
            messages.append(f"Мост {name}: {'обновлён' if was_there else 'добавлен'} {item.name}")
    return messages


def _sync_dir(src: Path, dst: Path) -> list[str]:
    """Догоняет содержимое папки образцом. Ничего не удаляет.

    Возвращает список того, что изменилось. Файлы, которые есть только в
    базе, остаются на месте: пользователь вправе дописать в скилл своё.
    """
    changed: list[str] = []
    for item in sorted(src.iterdir()):
        if item.name in ("__pycache__", ".DS_Store") or item.name.startswith("."):
            continue
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            changed.extend(_sync_dir(item, target))
            continue
        if target.is_file() and target.read_bytes() == item.read_bytes():
            continue
        shutil.copy2(item, target)
        changed.append(f"{item.parent.name}/{item.name}")
    return changed


def refresh_skills(base: Path) -> list[str]:
    """Обновляет скиллы в базе из конструктора.

    Скилл — часть программы, а не данные базы: он обязан совпадать с образцом,
    иначе нейросеть пользуется устаревшей версией. Найдено живьём: в уже
    созданных базах лежали скиллы старой версии, а skills-index.json не
    обновлялся вовсе — из-за `if dest.exists(): continue` в _copy_skills.

    Чужие скиллы не трогаем: папку, которой нет в конструкторе, база
    сохраняет как свою. Файлы внутри знакомого скилла тоже не удаляем.
    """
    messages: list[str] = []
    src_root = program_root() / "skills"
    dst_root = base / "skills"
    if not src_root.is_dir():
        return messages
    dst_root.mkdir(parents=True, exist_ok=True)

    for item in sorted(src_root.iterdir()):
        if not item.is_dir() or not (item / SKILL_MARKER).is_file():
            continue
        if item.name.startswith("."):
            continue
        dst = dst_root / item.name
        if not dst.exists():
            shutil.copytree(item, dst, ignore=shutil.ignore_patterns("__pycache__"))
            messages.append(f"Скилл добавлен: {item.name}")
            continue
        changed = _sync_dir(item, dst)
        if changed:
            messages.append(f"Скилл обновлён: {item.name} ({len(changed)} файл)")

    # Файлы рядом с навыками — часть пакета навыков. Найдено живьём:
    # в уже созданной базе описание навыков осталось на 16 навыках, пока
    # их было 32, и никто этого не замечал.
    for _loose in sorted(_loose_files(src_root)):
        _src_f = src_root / _loose
        _dst_f = dst_root / _loose
        if _dst_f.is_file() and _dst_f.read_bytes() == _src_f.read_bytes():
            continue
        shutil.copy2(_src_f, _dst_f)
        messages.append(f"Описание навыков обновлено: {_loose}")

    # Индекс — часть программы, обновляется всегда: из него нейросеть
    # узнаёт, когда какой скилл применять.
    src_index = program_file("skills-index.json")
    dst_index = base / "skills-index.json"
    if src_index.is_file():
        try:
            data = json.loads(src_index.read_text(encoding="utf-8"))
            count = len(data.get("skills", []))
        except (OSError, ValueError):
            count = 0
        if not dst_index.is_file() or dst_index.read_bytes() != src_index.read_bytes():
            shutil.copy2(src_index, dst_index)
            messages.append(f"Индекс скиллов обновлён ({count} записей)")

    # Агенты — та же история, что и скиллы: часть программы, а не данные.
    # Найдено живьём: в уже созданных базах папки tools/agents не было вовсе,
    # и в папке настроек opencode она была пуста — @iskatel и остальные
    # одиннадцать просто не существовали.
    agents_src = program_root() / "tools" / "agents"
    agents_dst = base / "tools" / "agents"
    if agents_src.is_dir():
        if not agents_dst.is_dir():
            shutil.copytree(agents_src, agents_dst,
                            ignore=shutil.ignore_patterns("__pycache__"))
            count = len(list(agents_dst.glob("*.md")))
            messages.append(f"Агенты скопированы в базу ({count} штук)")
        else:
            changed = _sync_dir(agents_src, agents_dst)
            if changed:
                messages.append(f"Агенты обновлены: {len(changed)} файл")

    # Реестр MCP-серверов — та же история, едет вместе с базой.
    for name in ("mcp-registry.json", "THIRD-PARTY-NOTICES.md"):
        src_file = program_file(name)
        dst_file = base / name
        if not src_file.is_file():
            continue
        if dst_file.is_file() and dst_file.read_bytes() == src_file.read_bytes():
            continue
        try:
            shutil.copy2(src_file, dst_file)
            messages.append(f"Скопировано: {name}")
        except OSError as exc:
            messages.append(f"{name}: не удалось скопировать ({exc})")
    return messages


def refresh_instructions(base: Path) -> list[str]:
    """Обновляет инструкции в базе из конструктора.

    Инструкции — часть программы, а не данные пользователя: в них живут
    таблицы «задача → скилл» и «задача → агент», и они устаревают так же,
    как скиллы. Найдено живьём: в уже созданных базах лежала старая версия
    инструкции при новых скиллах на диске.

    Файлы, которых нет в конструкторе, не трогаем — вдруг это свои.
    """
    messages: list[str] = []
    src_dir = program_root() / "инструкции"
    dst_dir = base / "инструкции"
    if not src_dir.is_dir():
        return messages
    dst_dir.mkdir(parents=True, exist_ok=True)
    for item in sorted(src_dir.iterdir()):
        if not item.is_file() or item.name.startswith("."):
            continue
        target = dst_dir / item.name
        if not target.exists():
            try:
                shutil.copy2(item, target)
            except OSError as exc:
                messages.append(f"Инструкция {item.name}: не удалось скопировать ({exc})")
                continue
            messages.append(f"Инструкция добавлена: {item.name}")
            continue
        if target.read_bytes() == item.read_bytes():
            continue
        try:
            shutil.copy2(item, target)
        except OSError as exc:
            messages.append(f"Инструкция {item.name}: не удалось обновить ({exc})")
            continue
        messages.append(f"Инструкция обновлена: {item.name}")
    return messages


def attach_base(
    base: Path,
    program: Program,
    progress=None,
    skills: list[str] | None = None,
) -> AttachResult:
    """Подключает созданную базу к выбранной программе.

    Копирует файлы и папки базы в папку настроек программы, ничего не удаляя:
    прежние одноимённые файлы уходят в _previous-version.

    `skills` — какие скиллы перенести. None означает «все, что есть в базе».
    Пустой список означает «ни одного». Неотмеченные скиллы, уже лежавшие
    в папке программы, уходят в _previous-version/skills, а не в корзину —
    поэтому выбор всегда можно переиграть.
    """
    result = AttachResult(ok=True)
    messages = result.messages
    errors = result.errors

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    if not (base / REQUIRED_FILES[0]).is_file():
        result.ok = False
        errors.append(f"Папка {base} не похожа на базу: нет {REQUIRED_FILES[0]}.")
        return result

    # Отказ до всякого копирования: защита от записи в чужую папку.
    if not program.can_attach():
        result.ok = False
        errors.append(
            f"{program.title} файлы с диска не читает — база ему не видна. "
            "Это не ошибка настройки и не поломка: приложение показывает "
            "сайт в рамке, а папки на компьютере не читает. "
            "Ничего не скопировано."
        )
        return result

    dest = program.config_dir()
    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        result.ok = False
        errors.append(f"Не удалось создать папку назначения: {exc}")
        return result
    say(f"Папка программы: {dest}")

    # Защита: если выбранная папка — служебная папка пакетов, брать её нельзя.
    if dest.name.lower() in ("npm", "node_modules"):
        result.ok = False
        errors.append(
            "Папка программы определилась как служебная папка пакетов "
            f"({dest.name}). Запись туда запрещена."
        )
        return result

    existing = [f for f in REQUIRED_FILES if (dest / f).is_file()]
    if existing:
        backup = dest / "_previous-version"
        backup.mkdir(exist_ok=True)
        for name in existing:
            try:
                shutil.copy2(dest / name, backup / name)
            except OSError as exc:
                errors.append(f"Не удалось сохранить копию {name}: {exc}")
        say(f"Прежние файлы сохранены в _previous-version ({len(existing)} шт.)")

    copied_any = False
    for name in REQUIRED_FILES:
        src = base / name
        if src.is_file():
            try:
                shutil.copy2(src, dest / name)
                copied_any = True
            except OSError as exc:
                errors.append(f"Не удалось скопировать {name}: {exc}")
    say("Файлы памяти перенесены" if copied_any else "Файлы памяти не найдены")

    for folder in ("знания", "библиотека", "projects", "инструкции", "sessions"):
        src = base / folder
        if src.is_dir():
            try:
                shutil.copytree(
                    src, dest / folder, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns("__pycache__"),
                )
            except OSError as exc:
                errors.append(f"Не удалось скопировать папку {folder}: {exc}")
    say("Папки базы перенесены")

    if program.supports_skills:
        available = list_skills(base)
        names_available = [s["name"] for s in available]
        if skills is None:
            chosen = names_available
        else:
            chosen = [n for n in names_available if n in set(skills)]
            skipped = [n for n in skills if n not in set(names_available)]
            for name in skipped:
                errors.append(
                    f"Скилл «{name}» отмечен, но в базе его нет — пропущен."
                )
        src_skills = base / "skills"
        dest_skills = dest / "skills"
        count = 0
        if not names_available:
            # В базе навыков нет вовсе. Убирать то, что уже стоит
            # в программе, не станем: иначе подключение пустой базы
            # выглядело бы как потеря всех навыков.
            say("В базе навыков нет — в программе ничего не тронуто")
        elif src_skills.is_dir():
            dest_skills.mkdir(parents=True, exist_ok=True)

            # Неотмеченные скиллы не удаляем: убираем в _previous-version,
            # чтобы выбор можно было переиграть в любой момент.
            removed: list[str] = []
            for item in sorted(dest_skills.iterdir()):
                if not item.is_dir() or item.name.startswith("."):
                    continue
                if item.name in chosen:
                    continue
                if not (item / SKILL_MARKER).is_file():
                    continue
                backup = dest / "_previous-version" / "skills" / item.name
                try:
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if backup.exists():
                        shutil.rmtree(backup)
                    shutil.move(str(item), str(backup))
                    removed.append(item.name)
                except OSError as exc:
                    errors.append(f"Скилл {item.name}: не удалось убрать: {exc}")
            if removed:
                say(
                    f"Убрано из программы (сохранено в _previous-version): "
                    f"{len(removed)}"
                )

            for name in chosen:
                item = src_skills / name
                if not (item / SKILL_MARKER).is_file():
                    continue
                try:
                    shutil.copytree(
                        item, dest_skills / name, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("__pycache__"),
                    )
                    count += 1
                except OSError as exc:
                    errors.append(f"Скилл {name}: {exc}")
            result.skills_dir = dest_skills
        say(f"Скиллов перенесено: {count} из {len(names_available)}")
        if not chosen and names_available:
            say("Отмеченных скиллов не было — переносить нечего")
    else:
        say(f"{program.title} скиллы не читает — пропущено")

    marker = dest / "memory-base-path.txt"
    try:
        marker.write_text(str(base).replace("\\", "/"), encoding="utf-8")
        say("Указан путь к базе")
    except OSError as exc:
        errors.append(f"Не удалось записать путь к базе: {exc}")

    # AGENTS.md — правила живут внутри базы. При подключении копируем
    # в конфиг программы, чтобы плагин и opencode их видели.
    agents_src = base / "AGENTS.md"
    agents_dst = dest / "AGENTS.md"
    if agents_src.is_file():
        try:
            shutil.copy2(agents_src, agents_dst)
            say("AGENTS.md скопирован из базы")
        except OSError as exc:
            errors.append(f"Не удалось скопировать AGENTS.md: {exc}")
    else:
        # В базе правил нет — убрать из конфига, чтобы не висели старые.
        try:
            if agents_dst.is_file():
                agents_dst.unlink()
                say("AGENTS.md нет в базе — из конфига убран")
        except OSError as exc:
            errors.append(f"Не удалось убрать AGENTS.md: {exc}")

    # Плагин и настройки — только для OpenCode: именно они заставляют
    # программу читать базу и подхватывать скиллы. Без них файлы базы
    # просто лежат в папке и ни на что не влияют.
    # Для Harness — свой блок в AGENTS.md (install_harness_agents).
    if program.ident == "opencode":
        if find_config_source(base) is None:
            say("Плагин: в базе нет папки config — пропущено")
        else:
            _, plugin_errors = install_plugin(base, dest, progress)
            errors.extend(plugin_errors)
# Мосты живут ВНУТРИ базы: при подключении копируем их из конструктора
        # в базу (если ещё нет), затем config.json моста NCP указывает
        # на библиотеку ЭТОЙ базы, а opencode.jsonc запускает мост из базы.
        for bridge in ("ncp-bridge", "pc-bridge"):
            src_bridge = program_root() / "tools" / bridge
            dst_bridge = base / "tools" / bridge
            if src_bridge.is_dir() and not dst_bridge.is_dir():
                try:
                    shutil.copytree(
                        src_bridge, dst_bridge,
                        # config.json едет вместе с мостом: в образце пути —
                        # пометки {{LIBRARY}} и {{BASE}}, мост разворачивает их
                        # сам (core.configure_bridges вписывает library_path).
                        ignore=shutil.ignore_patterns("__pycache__"),
                    )
                    say(f"Мост {bridge} скопирован в базу")
                except OSError as exc:
                    errors.append(f"Не удалось скопировать мост {bridge}: {exc}")
        for message in refresh_bridge_code(base):
            say(message)
        for message in refresh_skills(base):
            say(message)
        for message in refresh_instructions(base):
            say(message)
        # Существующая база тоже должна получить указатели: их могло не
        # быть вовсе, а знания в ней уже есть. Иначе перенос потерянных
        # привязок в архив так и не включался бы на живой базе.
        for message in refresh_knowledge_indexes(base):
            say(message)
        for message in configure_bridges(base):
            say(message)
        cfg_target = dest / INSTRUCTIONS_FILE
        if cfg_target.is_file():
            try:
                from opencode_caps import rewire_config  # noqa: PLC0415

                for message in rewire_config(dest, base):
                    say(message)
            except (ValueError, OSError) as exc:
                errors.append(f"Мосты opencode не переведены: {exc}")
    elif program.ident == "harness":
        _, harness_errors = install_harness_agents(base, dest, progress)
        errors.extend(harness_errors)

    result.ok = not errors
    # Память о подключении: база появляется в списке на вкладке
    # «Подключить существующую», чтобы её можно было выбрать снова.
    if program.ident in PROGRAMS_BY_ID:
        remember_base(base, program.ident)
    return result


def refresh_knowledge_indexes(base: Path) -> list[str]:
    """Создаёт недостающие указатели знаний и пересобирает имеющиеся.

    Пока этого вызова не было, указатели знаний не появлялись нигде: их
    создавал только селфтест. Новая база рождалась с папкой знаний, в
    которой лежали сотни файлов и ни одного указателя к ним. Измерено до
    правки: у базы из образца не было ни одного из одиннадцати.

    Папки при этом не создаются. Их отсутствие — сведения о базе, а не
    недочёт сборки: у человека может не быть области «Деньги», и молча
    заводить её значило бы врать о структуре. О каждой недостающей папке
    сказано в отчёте, и человек завёл её сам, если она ему нужна.
    """
    messages: list[str] = []
    stamp = datetime.now().strftime("%Y-%m-%d")
    made = 0
    for rel, fname, _limit in knowledge_index_targets():
        folder = base / rel
        if not folder.is_dir():
            messages.append(f"{rel}/{fname}: нет папки, указатель не создан")
            continue
        # Вложенная импортированная ветка описывается целиком: её прямые
        # подпапки — это сотни каталогов, и перечислить их по одной не
        # влезет ни в какой лимит.
        scope = "tree" if len(Path(rel).parts) > 2 else "children"
        was = (folder / fname).is_file()
        try:
            report = write_knowledge_index(folder, fname, scope, stamp)
        except OSError as exc:
            messages.append(f"{rel}/{fname}: не удалось пересобрать — {exc}")
            continue
        made += 1
        for line in report:
            messages.append(f"{rel}/{fname}: {line}")
        if not was:
            messages.append(f"{rel}/{fname}: создан")
        # Лимит проверяется здесь, а не «где-то в коде». Раньше `_limit`
        # разворачивался в никуда: порог был числом в списке, и переполнение
        # проходило молча. Теперь превышение попадает в отчёт — не обрезаем
        # (обрезание выбросило бы ровно то, ради чего указатель и жив) и не
        # молчим.
        size = (folder / fname).stat().st_size
        if size > _limit:
            messages.append(
                f"{rel}/{fname}: ПРЕВЫШЕН ЛИМИТ — {size} байт из {_limit}. "
                f"Указатель стал велик для карты: его нужно дробить, "
                f"а не увеличивать лимит.")
    messages.append(
        f"указателей знаний: пересобрано {made} из "
        f"{len(knowledge_index_targets())}")
    return messages


def sync_selected_skills(
    base: Path,
    dest: Path,
    chosen: list[str],
    progress=None,
) -> tuple[list[str], list[str]]:
    """Ставит в программу только отмеченные скиллы, остальные — в запас.

    Отмеченные копируются из базы целиком. Неотмеченные, уже лежавшие
    в программе, переезжают в _previous-version/skills, а не в корзину:
    выбор всегда можно переиграть. То же правило, что в attach_base,
    только без прочего подключения.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    available = [s["name"] for s in list_skills(base)]
    if not available:
        say("В базе навыков нет — в программе ничего не тронуто")
        return messages, errors
    want = [n for n in chosen if n in available]
    for name in chosen:
        if name not in available:
            errors.append(f"Скилл «{name}» отмечен, но в базе его нет — пропущен.")
    src_skills = base / "skills"
    dest_skills = dest / "skills"
    dest_skills.mkdir(parents=True, exist_ok=True)
    removed = 0
    for item in sorted(dest_skills.iterdir()):
        if not item.is_dir() or item.name.startswith("."):
            continue
        if item.name in want:
            continue
        if not (item / SKILL_MARKER).is_file():
            continue
        backup = dest / "_previous-version" / "skills" / item.name
        try:
            backup.parent.mkdir(parents=True, exist_ok=True)
            if backup.exists():
                shutil.rmtree(backup)
            shutil.move(str(item), str(backup))
            removed += 1
        except OSError as exc:
            errors.append(f"Скилл {item.name}: не удалось убрать: {exc}")
    if removed:
        say(f"Убрано в _previous-version: {removed}")
    count = 0
    for name in want:
        try:
            shutil.copytree(
                src_skills / name, dest_skills / name, dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("__pycache__"),
            )
            count += 1
        except OSError as exc:
            errors.append(f"Скилл {name}: {exc}")
    say(f"Навыков поставлено: {count} из {len(available)}")
    return messages, errors


def count_skills(base: Path) -> int:
    folder = base / "skills"
    if not folder.is_dir():
        return 0
    return sum(
        1
        for item in folder.iterdir()
        if item.is_dir() and (item / SKILL_MARKER).is_file()
    )


def base_info(base: Path) -> dict[str, object]:
    """Короткая сводка о базе — для показа в окне."""
    return {
        "exists": base.is_dir(),
        "is_base": (base / REQUIRED_FILES[0]).is_file(),
        "files": [f for f in REQUIRED_FILES if (base / f).is_file()],
        "skills": count_skills(base),
        "size": dir_size(base) if base.is_dir() else 0,
        "marker": read_marker(base),
    }


def current_base(program: str = "opencode") -> Path | None:
    """Какая база сейчас основная для программы.

    Смотрится по маркеру memory-base-path.txt в папке настроек программы.
    Возвращает None, если маркера нет или он ведёт в несуществующую базу.
    Ничего не меняет.
    """
    prog = PROGRAMS_BY_ID.get(program)
    if prog is None:
        return None
    marker = prog.config_dir() / "memory-base-path.txt"
    try:
        raw = marker.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    folder = Path(raw)
    if not folder.is_dir() or not (folder / REQUIRED_FILES[0]).is_file():
        return None
    return folder


def read_marker(base: Path) -> dict | None:
    marker = base / "база.json"
    if not marker.is_file():
        return None
    try:
        return json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                pass
    return total


def human_size(bytes_: int) -> str:
    value = float(bytes_)
    for unit in ("Б", "КБ", "МБ", "ГБ"):
        if value < 1024 or unit == "ГБ":
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ГБ"


def open_in_explorer(path: Path) -> None:
    """Открывает папку в проводнике Windows."""
    try:
        os.startfile(str(path))  # type: ignore[attr-defined]
    except AttributeError:
        subprocess.run(["explorer", str(path)], check=False)


def open_url(url: str) -> None:
    """Открывает ссылку в браузере по умолчанию.

    Отдельная функция, а не `open_in_explorer`: адрес приходит из реестра,
    и подставлять его в команду проводника было бы подстановкой в
    командную строку. `startfile` и `webbrowser` не строят команду из
    строки, а значит ничего из адреса командой не станет.
    """
    address = str(url or "").strip()
    if not address:
        return
    try:
        webbrowser.open(address)
    except Exception:  # noqa: BLE001 - без браузера просто ничего не делаем
        try:
            os.startfile(address)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001,S110 - молчание здесь уместно
            pass


# ---------------------------------------------------------------- список баз

#: Файл со списком баз, созданных этой программой. Лежит в корне программы,
#: чтобы список не терялся при переносе базы.
BASES_FILE = "созданные-базы.json"

#: Временная подмена файла списка. Нужна проверкам: без неё они пишут
#: в настоящий список и оставляют в окне мусорные базы.
_BASES_OVERRIDE: Path | None = None


def use_bases_file(path: Path | None) -> None:
    """Подменяет файл списка (None — вернуть настоящий)."""
    global _BASES_OVERRIDE
    _BASES_OVERRIDE = Path(path) if path is not None else None


def bases_file() -> Path:
    """Где хранится список созданных баз."""
    if _BASES_OVERRIDE is not None:
        return _BASES_OVERRIDE
    return app_root() / BASES_FILE


def read_bases() -> list[dict]:
    """Читает список баз, созданных программой.

    Возвращает только те записи, которые ещё существуют на диске —
    удалённые базы из списка пропадают сами.
    """
    path = bases_file()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []

    alive: list[dict] = []
    for item in data:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("path", "")).strip()
        if not raw:
            continue
        folder = Path(raw)
        if (folder / REQUIRED_FILES[0]).is_file():
            item["path"] = str(folder)
            alive.append(item)
    return alive


def remember_base(base: Path, program: str = "") -> None:
    """Записывает созданную базу в список. Повтор не создаёт дубликат."""
    base = Path(base)
    entries = read_bases()
    key = str(base).lower()
    for item in entries:
        if str(item.get("path", "")).lower() == key:
            item["when"] = datetime.now().isoformat(timespec="seconds")
            if program:
                item["program"] = program
            break
    else:
        entries.append(
            {
                "name": base.name,
                "path": str(base),
                "when": datetime.now().isoformat(timespec="seconds"),
                "program": program,
            }
        )
    _write_bases(entries)


def forget_base(base: Path) -> None:
    """Убирает базу из списка (сама папка не трогается)."""
    key = str(Path(base)).lower()
    entries = [e for e in read_bases() if str(e.get("path", "")).lower() != key]
    _write_bases(entries)


def delete_base(base: Path, confirm: bool = False) -> tuple[bool, str]:
    """Удаляет папку базы с диска и убирает её из списка.

    Удаляет по-настоящему, как и обещает кнопка: папка исчезает, место
    освобождается. Создал базу — она не понадобилась, держать её рядом
    незачем.

    Два предохранителя, оба незаметные для человека:
    без confirm=True ничего не происходит, а из списка база убирается
    только после успешного удаления — иначе папка осталась бы на месте,
    но потеряла бы связь с программой.

    Возвращает (успех, сообщение).
    """
    base = Path(base)
    if not base.is_dir():
        return False, "Папка базы не найдена"
    if not confirm:
        return False, (
            "Без подтверждения не удаляю. "
            "Вызывай delete_base(base, confirm=True)."
        )

    try:
        shutil.rmtree(base)
    except OSError as exc:
        return False, f"Не удалось удалить папку: {exc}"

    forget_base(base)
    return True, f"База удалена: {base}"


def disconnect_base(base: Path, program: str = "opencode") -> tuple[list[str], list[str]]:
    """Полностью отключает базу от программы.

    Удаляет:
      - память базы из настроек программы (instructions, mcp, permissions)
      - маркер memory-base-path.txt
      - скиллы базы из папки навыков программы
      - (опционально) конфиг NCP-моста, если он ведёт на эту базу

    Возвращает (сообщения, ошибки).
    """
    messages: list[str] = []
    errors: list[str] = []

    prog = PROGRAMS_BY_ID.get(program)
    if prog is None:
        errors.append(f"Программа '{program}' не поддерживается")
        return messages, errors

    config_dir = prog.config_dir()
    base_str = str(base).replace("\\", "/")

    # 1. Убираем из opencode.jsonc: instructions, mcp.ncp/pc, permission.ncp_*/pc_*
    try:
        import opencode_caps
        cfg = config_dir / "opencode.jsonc"
        if cfg.is_file():
            text = cfg.read_text(encoding="utf-8")
            if opencode_caps.check_jsonc(text):
                # Удаляем instructions, связанные с этой базой
                bounds = opencode_caps.find_key_array(text, "instructions")
                if bounds is not None:
                    opening, closing = bounds
                    # Проверяем, есть ли пути этой базы в instructions
                    chunk = text[opening:closing + 1]
                    if base_str in chunk:
                        # Переписываем instructions без путей этой базы
                        lines = []
                        for line in chunk.splitlines():
                            if base_str not in line:
                                lines.append(line)
                        # Если остались пути — оставляем, иначе пустой массив
                        if len(lines) > 2:  # есть содержимое кроме [ и ]
                            fixed = text[:opening] + "\n".join(lines) + text[closing + 1:]
                        else:
                            fixed = text[:opening] + "[]\n" + text[closing + 1:]
                        if opencode_caps.check_jsonc(fixed):
                            text = fixed
                            messages.append("instructions: пути базы удалены")
                        else:
                            errors.append("instructions: не удалось безопасно удалить пути")

                # Удаляем mcp.ncp, mcp.pc, permission.ncp_*, permission.pc_*
                # remove_entry работает только с метками OpenCode_Base.
                # Если записи вставлены без меток — удаляем по имени через
                # find_key_object (он корректно обрабатывает вложенные скобки).
                names = ["ncp", "pc", "ncp_*", "pc_*"]
                for name in names:
                    obj_key = "mcp" if name in ("ncp", "pc") else "permission"
                    if opencode_caps.has_entry(text, name) or name.endswith("*"):
                        try:
                            text = opencode_caps.remove_entry(text, obj_key, name)
                            messages.append(f"Убран блок: {name}")
                        except Exception:
                            pass
                        # Fallback: если меток нет, удалить по имени
                        if opencode_caps.has_entry(text, name):
                            bounds = opencode_caps.find_key_object(text, obj_key)
                            if bounds is not None:
                                chunk = text[bounds[0]:bounds[1] + 1]
                                if opencode_caps.has_entry(chunk, name):
                                    # Ищем начало записи
                                    pattern = re.compile(
                                        r'^\s*"' + re.escape(name) + r'"\s*:\s*',
                                        re.MULTILINE,
                                    )
                                    match = pattern.search(chunk)
                                    if match:
                                        start = bounds[0] + match.start()
                                        # Определяем конец записи: объект {...} или строка "..."
                                        rest = text[start:]
                                        after_colon = rest[rest.find(":") + 1:]
                                        if after_colon.lstrip().startswith('"'):
                                            # Строковое значение: "name": "value"
                                            m2 = re.match(r'^\s*"' + re.escape(name) + r'"\s*:\s*"[^"]*"\s*,?', rest)
                                            if m2:
                                                remove_end = start + m2.end()
                                                text = text[:start] + text[remove_end:]
                                                text = opencode_caps.fix_trailing_commas(text)
                                                messages.append(f"Убран блок (fallback): {name}")
                                                continue
                                        # Объект: ищем закрывающую скобку
                                        brace_start = text.find("{", start)
                                        if brace_start != -1:
                                            depth = 0
                                            end = brace_start
                                            for i in range(brace_start, len(text)):
                                                if text[i] == "{":
                                                    depth += 1
                                                elif text[i] == "}":
                                                    depth -= 1
                                                    if depth == 0:
                                                        end = i
                                                        break
                                            remove_end = end + 1
                                            if remove_end < len(text) and text[remove_end] == ",":
                                                remove_end += 1
                                            text = text[:start] + text[remove_end:]
                                            text = opencode_caps.fix_trailing_commas(text)
                                            messages.append(f"Убран блок (fallback): {name}")
                text = opencode_caps.fix_trailing_commas(text)
                if opencode_caps.check_jsonc(text):
                    cfg.write_text(text, encoding="utf-8")
                    messages.append("opencode.jsonc очищен от настроек базы")
                else:
                    errors.append("opencode.jsonc: после очистки файл не читается — откат")
            else:
                errors.append("opencode.jsonc сломан — правим руками")
    except Exception as exc:
        errors.append(f"Ошибка при очистке opencode.jsonc: {exc}")

    # 2. Удаляем memory-base-path.txt
    marker = config_dir / "memory-base-path.txt"
    try:
        if marker.is_file():
            marker.unlink()
            messages.append("memory-base-path.txt удалён")
    except OSError as exc:
        errors.append(f"Не удалось удалить memory-base-path.txt: {exc}")

    # 2б. Удаляем AGENTS.md из конфига — правила жили внутри базы,
    # вместе с базой они должны уйти. Иначе останутся «общие» правила.
    agents_dst = config_dir / "AGENTS.md"
    try:
        if agents_dst.is_file():
            agents_dst.unlink()
            messages.append("AGENTS.md удалён из конфига (правила были в базе)")
    except OSError as exc:
        errors.append(f"Не удалось удалить AGENTS.md: {exc}")

    # 3. Удаляем скиллы базы из папки навыков программы
    try:
        skills_dir = config_dir / "skills"
        if skills_dir.is_dir():
            base_skills = base / "skills"
            if base_skills.is_dir():
                removed = 0
                for skill in base_skills.iterdir():
                    if skill.is_dir() and (skill / "SKILL.md").is_file():
                        target = skills_dir / skill.name
                        if target.is_dir():
                            try:
                                shutil.rmtree(target)
                                removed += 1
                            except OSError:
                                pass
                if removed:
                    messages.append(f"Убрано скиллов: {removed}")
    except Exception as exc:
        errors.append(f"Ошибка при удалении скиллов: {exc}")

    # 4. Сбрасываем мосты ВНУТРИ базы: отключили базу — память недоступна.
    #    Раньше здесь правился config.json в конструкторе, а он и не был
    #    тем, кем казался: живой мост лежит в базе, и его настройки тоже там.
    #    Конструктор не трогаем совсем — это шаблон, он должен остаться чистым.
    try:
        ncp_config = ncp_bridge_dir(base) / "config.json"
        if ncp_config.is_file():
            data = json.loads(ncp_config.read_text(encoding="utf-8"))
            lib_path = str(data.get("library_path", "")).strip()
            if lib_path and Path(lib_path).resolve() == (base / "библиотека").resolve():
                data["library_path"] = BRIDGE_LIBRARY_PLACEHOLDER
                ncp_config.write_text(
                    json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                messages.append("NCP-мост: конфиг в базе сброшен (путь к библиотеке убран)")
        else:
            messages.append("NCP-мост: в базе нет config.json — сбрасывать нечего")
    except Exception as exc:
        errors.append(f"Ошибка при сбросе NCP-моста: {exc}")

    return messages, errors


def _write_bases(entries: list[dict]) -> None:
    """Сохраняет список баз. Ошибка записи не должна ломать работу."""
    try:
        path = bases_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


# ---------------------------------------------------------------- ярлыки

#: Куда можно положить ярлык. Порядок = порядок в списке окна.
SHORTCUT_PLACES: list[tuple[str, str]] = [
    ("desktop", "Рабочий стол"),
    ("base", "Внутри папки базы"),
    ("project", "Корень программы управления"),
    ("parent", "Рядом с базой (в папке-родителе)"),
    ("custom", "Своя папка…"),
]


def desktop_dir() -> Path:
    """Папка рабочего стола. На разных сборках Windows называется по-разному."""
    for candidate in (
        Path.home() / "Desktop",
        Path.home() / "Рабочий стол",
        Path.home() / "OneDrive" / "Desktop",
        Path.home() / "OneDrive" / "Рабочий стол",
    ):
        if candidate.is_dir():
            return candidate
    return Path.home() / "Desktop"


def program_root() -> Path:
    """Папка программы-конструктора — где лежат tools/, знания/, skills/ и т.д.

    Это эталон для создания новых баз. Программа не хранит базы данных
    внутри себя — она только шаблон.
    """
    return Path(__file__).resolve().parent.parent.parent






def data_bases_folder() -> Path:
    """Папка, где программа рекомендует хранить базы данных."""
    return desktop_dir() / "DataBases"


def ensure_data_bases_folder() -> Path:
    """Создаёт папку DataBases, если её ещё нет. Возвращает путь."""
    folder = data_bases_folder()
    folder.mkdir(parents=True, exist_ok=True)
    return folder


#: Отметка о первом запуске. В корне программы её больше нет: раздел 16
#: отправил служебное в `служебное/`. Папку приходится создавать при
#: записи — иначе отметка молча не сохранится и окно первого запуска
#: покажут второй раз.
FIRST_RUN_MARKER = ("служебное", ".first-run-done")


def first_run_done() -> bool:
    """True, если окно первого запуска уже показывали.

    Второй путь — запасной: у копии программы, где перенос ещё не сделан,
    отметка лежит в корне. Без него окно первого запуска показывалось бы
    заново, и это выглядело бы как ошибка.
    """
    if program_root().joinpath(*FIRST_RUN_MARKER).is_file():
        return True
    return (program_root() / ".first-run-done").is_file()


def mark_first_run_done() -> None:
    """Запоминает, что первый запуск состоялся."""
    marker = program_root().joinpath(*FIRST_RUN_MARKER)
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("done", encoding="utf-8")
    except OSError:
        pass


def apply_theme(theme_path: Path) -> tuple[bool, str]:
    """Применяет тему к OpenCode: копирует JSON в themes/ и обновляет tui.json.

    Возвращает (успех, сообщение).
    """
    import json
    import re

    try:
        data = json.loads(theme_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"Не удалось прочитать тему: {exc}"

    theme_name = theme_path.stem
    opencode_dir = PROGRAMS_BY_ID["opencode"].config_dir()
    themes_dir = opencode_dir / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    dest = themes_dir / f"{theme_name}.json"
    try:
        shutil.copy2(theme_path, dest)
    except OSError as exc:
        return False, f"Не удалось скопировать тему: {exc}"

    # Обновляем tui.json (документированный способ)
    tui_path = opencode_dir / "tui.json"
    tui_content = {
        "$schema": "https://opencode.ai/tui.json",
        "theme": theme_name
    }
    try:
        tui_path.write_text(json.dumps(tui_content, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as exc:
        return False, f"Не удалось обновить tui.json: {exc}"

    # Также обновляем opencode.jsonc для совместимости (простая строка)
    jsonc_path = opencode_dir / "opencode.jsonc"
    try:
        text = jsonc_path.read_text(encoding="utf-8")
    except OSError:
        text = "{}"
    # Заменяем theme в любом формате: объект {name, mode} или строка "name"
    if '"theme"' in text:
        # Сначала пробуем заменить объектный формат
        text = re.sub(r'"theme"\s*:\s*\{[^}]*\}', f'"theme": "{theme_name}"', text, count=1)
        # Затем строковый формат
        text = re.sub(r'"theme"\s*:\s*"[^"]*"', f'"theme": "{theme_name}"', text, count=1)
    else:
        # Добавляем theme в корень JSON
        text = text.replace("{", '{\n  "theme": "' + theme_name + '",', 1)
    try:
        jsonc_path.write_text(text, encoding="utf-8")
    except OSError as exc:
        return False, f"Не удалось обновить opencode.jsonc: {exc}"

    return True, f"Тема «{theme_name}» применена. Перезапустите OpenCode."

def scan_bases() -> list[Path]:
    """Ищет все базы, созданные этой программой, по файлу-идентификатору.

    Сканирует рабочий стол и папку DataBases (первый уровень вложенности).
    Возвращает список найденных баз.
    """
    found: list[Path] = []
    places = [desktop_dir(), desktop_dir() / "DataBases"]
    for place in places:
        if not place.is_dir():
            continue
        for child in place.iterdir():
            if child.is_dir() and (child / ".opencode-base.json").is_file():
                if child not in found:
                    found.append(child)
    return found

def app_root() -> Path:
    """Папка базы данных пользователя.

    Ищет базу в стандартном месте (DataBases/OpenCode_Base). Если не найдена —
    возвращает program_root() (для совместимости).
    """
    default_base = Path.home() / "Desktop" / "DataBases" / "OpenCode_Base"
    if default_base.is_dir() and (default_base / "profile.md").is_file():
        return default_base
    return program_root()


def shortcut_target_folder(base: Path) -> Path:
    """Папка, которую открывает ярлык, — сама база."""
    return Path(base)


def resolve_shortcut_folder(place: str, base: Path, custom: str = "") -> Path:
    """Превращает выбранное место в настоящую папку.

    Бросает NameError_, если папку выбрать нельзя.
    """
    base = Path(base)
    if place == "desktop":
        return desktop_dir()
    if place == "base":
        return base
    if place == "project":
        return app_root()
    if place == "parent":
        return base.parent
    if place == "custom":
        raw = (custom or "").strip()
        if not raw:
            raise NameError_("Не указана своя папка для ярлыка.")
        folder = expand(raw)
        if not folder.is_dir():
            raise NameError_(f"Такой папки нет: {folder}\nВыберите другую.")
        return folder
    raise NameError_(f"Неизвестное место для ярлыка: {place}")


def make_folder_shortcut(link: Path, folder: Path, description: str = "") -> bool:
    """Создаёт ярлык, открывающий папку. Возвращает True при успехе.

    Ярлык делается через сам Windows, но не через его командную строку.
    Командная строка при чужой кодировке портит русские буквы и длинное
    тире в путях — из-за этого ярлык получался битым и вёл в несуществующую
    папку. Здесь путь передаётся напрямую, поэтому остаётся правильным.
    """
    link = Path(link)
    folder = Path(folder)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False

    # основной способ: напрямую, без командной строки
    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(link))
            shortcut.TargetPath = str(folder)
            shortcut.WorkingDirectory = str(folder.parent)
            shortcut.Description = description or folder.name
            shortcut.IconLocation = (
                "%SystemRoot%" + "\\System32\\shell32.dll,3"
            )
            shortcut.Save()
        finally:
            pythoncom.CoUninitialize()
        if link.is_file():
            return True
    except Exception:
        pass

    # запасной способ: через командную строку Windows. Может испортить
    # русские буквы в пути, поэтому применяется только если первый не смог
    return _shortcut_via_windows(link, folder, description)


def make_program_shortcut(
    link: Path,
    target: Path,
    description: str = "",
    icon: str = "",
    workdir: Path | None = None,
) -> bool:
    """Создаёт ярлык на программу/скрипт с заданной иконкой.

    Отличие от make_folder_shortcut: TargetPath указывает на исполняемый
    файл (или .cmd), а не на папку, и иконку можно поставить свою
    (например иконку OpenCode). Возвращает True при успехе.
    """
    link = Path(link)
    target = Path(target)
    try:
        link.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    workdir = Path(workdir) if workdir is not None else target.parent
    icon_line = icon or ("%SystemRoot%" + "\\System32\\shell32.dll,3")

    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(link))
            shortcut.TargetPath = str(target)
            shortcut.WorkingDirectory = str(workdir)
            shortcut.Description = description or target.name
            shortcut.IconLocation = icon_line
            shortcut.Save()
        finally:
            pythoncom.CoUninitialize()
        if link.is_file():
            return True
    except Exception:
        pass

    # запасной способ: через PowerShell
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{target}';"
        "$s.WorkingDirectory='{work}';"
        "$s.Description='{desc}';"
        "$s.IconLocation='{icon}';"
        "$s.Save()"
    ).format(
        link=str(link),
        target=str(target),
        work=str(workdir),
        desc=(description or target.name).replace("'", " "),
        icon=icon_line.replace("'", " "),
    )
    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            done = subprocess.run(
                [
                    exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", script,
                ],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and link.is_file():
            return True
    return False


def _shortcut_via_windows(
    link: Path, folder: Path, description: str = ""
) -> bool:
    """Запасной способ создать ярлык: через программу Windows."""
    script = (
        "$s=(New-Object -ComObject WScript.Shell).CreateShortcut('{link}');"
        "$s.TargetPath='{target}';"
        "$s.WorkingDirectory='{work}';"
        "$s.Description='{desc}';"
        "$s.IconLocation='%SystemRoot%\\System32\\shell32.dll,3';"
        "$s.Save()"
    ).format(
        link=str(link),
        target=str(folder),
        work=str(folder.parent),
        desc=(description or folder.name).replace("'", " "),
    )

    for exe in ("powershell.exe", "pwsh.exe"):
        try:
            done = subprocess.run(
                [
                    exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy", "Bypass",
                    "-Command", script,
                ],
                capture_output=True,
                timeout=60,
            )
        except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
            continue
        if done.returncode == 0 and link.is_file():
            return True
    return False


def shortcut_target(link: Path) -> Path | None:
    """Куда ведёт ярлык — читаем из самого файла, без командной строки.

    Через командную строку путь читается с искажениями (русские буквы и
    длинное тире портятся), поэтому читаем напрямую.
    """
    link = Path(link)
    if not link.is_file():
        return None

    try:
        import pythoncom
        import win32com.client

        pythoncom.CoInitialize()
        try:
            shell = win32com.client.Dispatch("WScript.Shell")
            shortcut = shell.CreateShortcut(str(link))
            path = shortcut.TargetPath
        finally:
            pythoncom.CoUninitialize()
        if path:
            return Path(path)
    except Exception:
        pass

    # запасной способ: через библиотеку разбора ярлыков
    try:
        import pylnk3

        parsed = pylnk3.parse(str(link))
        path = getattr(parsed, "path", None)
        if path:
            return Path(path)
    except Exception:
        pass
    return None


def safe_link_name(name: str) -> str:
    """Имя ярлыка без знаков, которые Windows в именах файлов не разрешает."""
    cleaned = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in name).strip()
    cleaned = cleaned.strip(".").strip()
    if not cleaned:
        cleaned = "база"
    if len(cleaned) > 90:
        cleaned = cleaned[:90]
    return cleaned


@dataclass
class ShortcutResult:
    ok: bool
    link: Path | None = None
    error: str = ""


def create_base_shortcut(
    base: Path,
    place: str,
    custom: str = "",
    name: str = "",
) -> ShortcutResult:
    """Создаёт ярлык на созданную базу в выбранном месте.

    Ярлык ведёт не на папку (двойной щелчок по такой ссылке окно не
    открывает), а на файл-открывалку внутри базы. Если её нет — создаём.
    """
    base = Path(base)
    try:
        folder = resolve_shortcut_folder(place, base, custom)
    except NameError_ as exc:
        return ShortcutResult(False, None, str(exc))

    label = safe_link_name(name or base.name)
    link = folder / f"{label}.lnk"

    if link.exists():
        return ShortcutResult(
            False, link,
            f"Ярлык с таким именем уже есть: {link}\n"
            "Он не тронут — переименуйте существующий или выберите "
            "другое место.",
        )

    # цель ярлыка — файл-открывалка внутри базы
    opener = base / OPENER_NAME
    if not opener.is_file():
        try:
            opener.write_text(opener_body(base), encoding="utf-8")
        except OSError as exc:
            return ShortcutResult(
                False, link, f"Не удалось создать файл для ярлыка: {exc}"
            )

    made = make_folder_shortcut(
        link, opener, description=f"База {label}"
    )
    if not made:
        return ShortcutResult(
            False, link,
            "Ярлык создать не удалось. Его можно сделать вручную: "
            f"правый щелчок по файлу {opener} → «Отправить» → "
            "«Рабочий стол (создать ярлык)».",
        )

    # проверяем, что ярлык ведёт именно на файл этой базы: иначе он
    # откроет не то место, и лучше сказать об этом сразу
    points_to = shortcut_target(link)
    if points_to is not None:
        try:
            same = points_to.resolve() == opener.resolve()
        except OSError:
            same = str(points_to).lower() == str(opener).lower()
        if not same:
            return ShortcutResult(
                False, link,
                "Ярлык создан, но ведёт не на ту базу:\n"
                f"  должно быть {opener}\n  получилось {points_to}\n"
                "Пользуйтесь папкой базы напрямую или сделайте ярлык вручную.",
            )
    return ShortcutResult(True, link, "")


# ---------------------------------------------------------------- мост NCP
#
# Мост — это маленькая программа-переводчик: она даёт нейросети доступ
# к библиотеке NCP по протоколу MCP. Здесь он создаётся, проверяется и
# вписывается в настройки программы-клиента.
#
# Главное правило этого раздела: НИ ОДНОГО ВПИСАННОГО ПУТИ. Всё, что
# зависит от человека и его компьютера, вычисляется на месте. Иначе мост
# не заработает на другом компьютере, а именно за этим он и делается.


BRIDGE_TEMPLATE_DIR = ("tools", "ncp-bridge")
#: Что копируется при создании моста. Список зашит, а не берётся из папки:
#: иначе в мост попадёт мусор от разработки (__pycache__, .pyc, бэкапы),
#: и мост перестанет собираться на чужом компьютере. Поэтому новый модуль
#: моста нельзя забыть: он добавляется здесь.
BRIDGE_FILES = (
    "server.py",
    "ncp_core.py",
    "memory_tools.py",
    "config.json",
    "requirements.txt",
    "install.bat",
    "ЧИТАТЬ-МЕНЯ.txt",
)
BRIDGE_SERVER = "server.py"
BRIDGE_CORE = "ncp_core.py"
BRIDGE_LIBRARY_PLACEHOLDER = "{{LIBRARY}}"
BRIDGE_LOOK_NAMES = ("NCP-мост", "ncp-bridge")
BRIDGE_DEFAULT_NAME = "NCP-мост"


def bridge_template_dir() -> Path:
    """Образец моста внутри программы.

    Лежит в самой базе, поэтому едет вместе с ней на любой компьютер —
    ничего скачивать из интернета не нужно.
    """
    return program_root().joinpath(*BRIDGE_TEMPLATE_DIR)


def bridge_default_dir() -> Path:
    """Куда предлагать положить мост. Человек может выбрать другое."""
    return desktop_dir() / BRIDGE_DEFAULT_NAME


def library_dir(base: Path) -> Path:
    """Папка библиотеки NCP внутри базы."""
    return Path(base) / "библиотека"


def library_ready(base: Path) -> bool:
    """Развёрнута ли библиотека NCP в этой базе."""
    lib = library_dir(base)
    return (lib / "index.json").is_file() or (lib / "NCP.md").is_file()


def resolve_library(path: Path) -> Path:
    """Приводит выбранный путь к папке библиотеки.

    Принимает и базу целиком, и саму папку библиотеки: человек может
    выбрать любое из двух, и оба варианта должны работать.
    """
    path = Path(path)
    inside = path / "библиотека"
    if (inside / "index.json").is_file() or (inside / "NCP.md").is_file():
        return inside
    return path


def looks_like_bridge(folder: Path) -> bool:
    """Наша ли это папка моста. Узнаём по двум файлам, а не по имени."""
    folder = Path(folder)
    return (folder / BRIDGE_SERVER).is_file() and (folder / BRIDGE_CORE).is_file()


def find_bridges() -> list[Path]:
    """Все папки моста, которые видны рядом с базой и на рабочем столе."""
    roots = [desktop_dir(), program_root(), program_root().parent]
    found: list[Path] = []
    for root in roots:
        if not root.is_dir():
            continue
        for name in BRIDGE_LOOK_NAMES:
            folder = root / name
            if looks_like_bridge(folder) and folder not in found:
                found.append(folder)
        try:
            for folder in sorted(root.iterdir()):
                if folder.is_dir() and looks_like_bridge(folder):
                    if folder not in found:
                        found.append(folder)
        except OSError:
            pass
    return found


# ---- поиск Python


def python_candidates() -> list[Path]:
    """Где искать Python. Вписанных путей нет — всё считается от дома.

    Первым идёт тот, на котором работает сама эта программа: он заведомо
    есть и заведомо запускается. Дальше — обычные установки, потом PATH.
    """
    home = Path.home()
    patterns = (
        ".workbuddy-ai/binaries/python/versions/*/python.exe",
        ".workbuddy-ai/binaries/python/envs/*/Scripts/python.exe",
        "AppData/Local/Programs/Python/Python*/python.exe",
        "AppData/Local/Python/bin/python.exe",
    )
    seen: list[str] = []
    out: list[Path] = []

    def add(path) -> None:
        if not path:
            return
        text = str(path)
        if text.lower() in seen:
            return
        seen.append(text.lower())
        out.append(Path(path))

    add(sys.executable)
    for pattern in patterns:
        for path in sorted(home.glob(pattern)):
            add(path)
    for name in ("python.exe", "py.exe"):
        which = shutil.which(name)
        if which:
            add(which)
    return out


def python_works(path: Path) -> bool:
    """Настоящий ли это Python.

    Заглушка из Microsoft Store отвечает на запрос версии и делает вид,
    что всё хорошо, но мост запустить не может. Поэтому её отсеиваем по
    слову WindowsApps в пути.
    """
    text = str(path)
    if "WindowsApps" in text:
        return False
    try:
        done = subprocess.run(
            [text, "-c", "import sys; print(sys.version_info[:2])"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def find_python() -> Path | None:
    """Первый Python, который действительно запускается."""
    for candidate in python_candidates():
        if python_works(candidate):
            return candidate
    return None


# ---- состояние моста


@dataclass
class BridgeStatus:
    """Что сейчас в папке моста."""

    folder: Path
    exists: bool = False
    missing: list[str] = field(default_factory=list)
    library: str = ""
    allow_save: bool = True
    problem: str = ""

    @property
    def server_py(self) -> Path:
        return Path(self.folder) / BRIDGE_SERVER


def bridge_status(folder: Path) -> BridgeStatus:
    """Смотрит, что уже лежит в выбранной папке."""
    folder = Path(folder)
    status = BridgeStatus(folder=folder)
    if not folder.is_dir():
        return status
    if not looks_like_bridge(folder):
        try:
            inside = [p.name for p in folder.iterdir()]
        except OSError:
            inside = []
        if inside:
            status.problem = (
                "Папка занята посторонними файлами: "
                + ", ".join(sorted(inside)[:6])
            )
        return status

    status.exists = True
    status.missing = [name for name in BRIDGE_FILES if not (folder / name).is_file()]

    config_file = folder / "config.json"
    if config_file.is_file():
        try:
            config = json.loads(config_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            status.problem = f"config.json не читается: {exc}"
            return status
        status.library = str(config.get("library_path") or "")
        status.allow_save = bool(config.get("allow_save", True))
        if status.library == BRIDGE_LIBRARY_PLACEHOLDER:
            status.problem = (
                "Путь к библиотеке не подставлен: в config.json осталась "
                f"пометка {BRIDGE_LIBRARY_PLACEHOLDER}."
            )
    else:
        status.problem = "Нет файла config.json — путь к библиотеке неизвестен."
    return status


# ---- создание


@dataclass
class BridgeResult:
    """Итог создания моста."""

    ok: bool
    folder: Path | None = None
    python: Path | None = None
    messages: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    checked: bool = False

    @property
    def server_py(self) -> Path | None:
        return (Path(self.folder) / BRIDGE_SERVER) if self.folder else None


def create_bridge(
    folder: Path,
    library: Path,
    python_exe: Path | None = None,
    progress=None,
) -> BridgeResult:
    """Создаёт мост в выбранной папке.

    Копирует образец из программы и подставляет путь к библиотеке.
    В чужую непустую папку не пишет: там могут быть нужные файлы.
    """

    def say(text: str) -> None:
        if progress:
            progress(text)

    folder = Path(folder)
    library = resolve_library(library)
    result = BridgeResult(ok=False, folder=folder)

    template = bridge_template_dir()
    if not template.is_dir():
        result.errors.append(
            f"В программе нет образца моста: {template}\n"
            "Похоже, папка tools\\ncp-bridge потерялась."
        )
        return result

    if not library_ready(library.parent):
        result.errors.append(
            f"В этой базе нет библиотеки NCP: {library}\n"
            "Сначала создайте базу заново или проверьте путь."
        )
        return result

    # --- папка
    if folder.exists():
        if not folder.is_dir():
            result.errors.append(f"По этому пути лежит файл, а не папка: {folder}")
            return result
        try:
            inside = [p.name for p in folder.iterdir()]
        except OSError as exc:
            result.errors.append(f"Не удалось заглянуть в папку: {exc}")
            return result
        if inside and not looks_like_bridge(folder):
            result.errors.append(
                f"Папка не пустая, и моста в ней нет:\n{folder}\n"
                f"Внутри: {', '.join(sorted(inside)[:6])}\n\n"
                "Выберите пустую папку или новую — чужое не тронем."
            )
            return result
        if looks_like_bridge(folder):
            say("В папке уже есть мост — обновляем файлы.")
    else:
        try:
            folder.mkdir(parents=True)
        except OSError as exc:
            result.errors.append(f"Не удалось создать папку:\n{folder}\n{exc}")
            return result
        say(f"Папка создана: {folder}")

    # --- файлы
    copied = 0
    for name in BRIDGE_FILES:
        source = template / name
        if not source.is_file():
            continue
        try:
            shutil.copy2(source, folder / name)
            copied += 1
        except OSError as exc:
            result.errors.append(f"Не удалось скопировать {name}: {exc}")
    say(f"Файлов скопировано: {copied}")

    if not looks_like_bridge(folder):
        result.errors.append(
            "Мост скопирован не полностью: нет server.py или ncp_core.py."
        )
        return result

    # --- путь к библиотеке
    config_file = folder / "config.json"
    try:
        config = json.loads(config_file.read_text(encoding="utf-8"))
        config["library_path"] = str(library).replace("\\", "/")
        config.pop("installed_at", None)
        config["installed_at"] = datetime.now().isoformat(timespec="seconds")
        config_file.write_text(
            json.dumps(config, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, json.JSONDecodeError) as exc:
        result.errors.append(f"Не удалось записать путь в config.json: {exc}")
        return result
    say(f"Путь к библиотеке записан: {library}")

    # --- python
    python = Path(python_exe) if python_exe else find_python()
    if python is None:
        result.errors.append(
            "Python не найден. Мост создан, но запускать его нечем.\n"
            "Поставьте Python 3.8 или новее с сайта python.org — при "
            "установке отметьте галочку «Add Python to PATH» — и создайте "
            "мост заново."
        )
        result.ok = False
        return result
    result.python = python
    say(f"Python найден: {python}")

    # --- проверка
    say("Проверка моста на временной копии библиотеки…")
    ok, output = bridge_selftest(folder, python)
    result.checked = ok
    if ok:
        say("Проверка пройдена: все 18 проверок.")
    else:
        result.errors.append(
            "Мост создан, но проверка не прошла. Вот что он ответил:\n" + output
        )

    result.ok = looks_like_bridge(folder) and not result.errors
    return result


def bridge_selftest(folder: Path, python_exe: Path | None = None) -> tuple[bool, str]:
    """Прогоняет проверку моста. Настоящую библиотеку не трогает."""
    python = Path(python_exe) if python_exe else find_python()
    if python is None:
        return False, "Python не найден — проверить нечем."
    server = Path(folder) / BRIDGE_SERVER
    if not server.is_file():
        return False, f"В этой папке нет {BRIDGE_SERVER}: {folder}"
    try:
        done = subprocess.run(
            [str(python), str(server), "--selftest"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=300,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"Запустить проверку не удалось: {exc}"
    output = (done.stdout or "") + (done.stderr or "")
    return done.returncode == 0, output.strip()


# ---- подпись настроек моста (показать человеку)


def mcp_entry(python_exe: Path, server_py: Path) -> dict:
    """Одна запись сервера MCP — стандартный вид для stdio-сервера."""
    return {
        "command": str(python_exe),
        "args": [str(server_py)],
        "transportType": "stdio",
    }


def mcp_snippet(python_exe: Path, server_py: Path) -> str:
    """Тот же набор настроек, но текстом — показать человеку."""
    return json.dumps(
        {"ncp": mcp_entry(python_exe, server_py)},
        ensure_ascii=False,
        indent=2,
    )


def harness_overlay_snippet(python_exe: Path, server_py: Path) -> str:
    """Оверлей моста NCP для Harness: применить как dsh --patch <файл>.

    Формат — из примеров Harness (apps/cli/config/examples/mcp-memory):
    MCP-клиент dsh-mcp-client, транспорт stdio, запуск нашим server.py.
    """
    exe = str(python_exe).replace("\\", "/")
    srv = str(server_py).replace("\\", "/")
    return (
        "# Мост NCP для Harness: применить как dsh --patch <этот-файл>\n"
        "# или вмержить в $DSH_HOME/cordis.patch.yml (копию прежнего — в _previous-version).\n"
        "- insert:\n"
        "  - id: memory-ncp\n"
        "    name: '@deepseek-ai/dsh-mcp-client'\n"
        "    config:\n"
        "      serverName: ncp\n"
        "      transport: stdio\n"
        f'      command: ["{exe}", "{srv}"]\n'
    )

