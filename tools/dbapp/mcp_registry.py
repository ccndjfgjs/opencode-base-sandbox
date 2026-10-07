# -*- coding: utf-8 -*-
"""Реестр MCP-серверов: что известно, что готово, что можно включить.

Модуль отдельный от core.py намеренно. Здесь три вещи, которые не должны
размазываться по программе: чтение реестра, живая проверка требований и
включение/выключение сервера в настройках opencode.

Про проверку требований. Реестр честно предупреждает: поле ready_here —
снимок на дату checked, а не обещание, и верить ему нельзя. Поэтому
состояние сервера считается здесь, на живой машине, а снимок из реестра
показывается рядом и помечен как устаревший. Иначе программа будет
врать пользователю про свою же готовность.

Программа ничего не устанавливает из интернета и не покупает подписки.
Она только проверяет, вписывает блок в настройки и честно говорит, чего
не хватает.
"""

from __future__ import annotations

import glob
import json
import datetime
import os
import re
import shutil
import subprocess
import winreg
from dataclasses import dataclass, field
from pathlib import Path

# Общие правила для версий. Импорт двойной: при плоском запуске папка лежит
# в sys.path, при запуске как пакет нужен относительный.
try:
    import versions  # type: ignore[import-not-found]
except ImportError:  # pragma: no cover - запуск как пакет
    from . import versions  # type: ignore[no-redef]

#: Имя файла реестра. Лежит в корне базы и едет с ней.
REGISTRY_NAME = "mcp-registry.json"

#: Куда искать программу, если её нет в PATH. Ключ — имя файла,
#: значение — ветка реестра Windows, где записан полный путь.
PROGRAMS_IN_REGISTRY = {
    "excel.exe": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\excel.exe",
    "blender.exe": r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\blender.exe",
}

#: Сколько ждать ответа команды проверки, секунд. Проверка должна быть
#: мгновенной; если зависла — значит это не проверка версии.
CHECK_TIMEOUT = 8

#: Сколько ждать прогрева кэша npx. Холодная скачка пакета занимает до
#: минуты; если за это время не уложились, кэш считаем прогретым и
#: продолжаем: opencode всё равно попробует сам.
WARM_UP_TIMEOUT = 180


@dataclass
class Requirement:
    """Одно требование сервера и результат его проверки."""

    what: str
    kind: str = "manual"          # command | program | manual
    value: str = ""               # команда или имя программы
    args: list[str] = field(default_factory=list)
    min_version: int = 0
    max_version: int = 0          # 0 — сверху ограничения нет
    until: str = ""               # строковая граница «до этой включительно»
    note: str = ""
    blocks: bool = False         # ручное требование, без которого сервер бессмыслен
    ok: bool = False
    detail: str = ""              # что именно найдено, для показа


@dataclass
class PluginSpec:
    """Плагин чужой программы, который вкладка умеет докачать сама.

    Отдельный блок, а не поле в `ProgramInstall`, потому что это не про
    установку самой программы: OBS стоит, а не хватает одной её части.
    Смешивать их значило бы получить две разные кнопки из одного описания.

    `finder` — короткое имя, по которому код находит папку программы.
    Не имя программы и не путь: путь меняется при переносе, а программу
    вкладке называет реестр. На «obs» код знает, где папка OBS; на
    незнакомое имя кнопки не будет, и карточка это скажет.
    """

    finder: str = ""
    name: str = ""
    button: str = ""
    hint: str = ""
    unavailable: str = ""
    fallback: str = ""      # запасная дорога разрешена в реестре?


@dataclass
class ProgramInstall:
    """Как поставить программу, нужную серверу, и где вкладка должна остановиться.

    Живёт в реестре под именем `program_install`, а не `install`. Имя
    выбрано не по вкусу: поле `install` в реестре уже занято человеческим
    текстом («uvx mcp-for-blender + аддон в Blender») — это пояснение для
    читателя, а не машиночитаемые данные. Переписать его в объект значило бы
    выбросить то, что читают люди, и сломать старые копии реестра. Поэтому
    структура лежит рядом, под своим именем, а `install` остаётся текстом.

    Метод `method` — то, что вкладка умеет делать сама:

    | method | что получится |
    |---|---|
    | `winget` | кнопка «установить» ставит через winget |
    | `official-download` | кнопка качает с официального адреса и сверяет подпись |
    | `manual` | кнопки нет: официальная страница и команда в буфер |
    | `none` | программа не нужна, ставить нечего |

    `expected_signer` — имя, которому обязана совпасть цифровая подпись
    установщика, а для winget — издатель из каталога. Проверка подписи
    появляется на этапе 8, до него это записанное намерение, а не
    действующая проверка.
    """

    program: str = ""
    method: str = "manual"        # winget | official-download | manual | none
    winget_id: str = ""
    catalog_version: str = ""     # версия в каталоге, не установленная
    expected_publisher: str = ""  # издатель из МАНИФЕСТА winget
    expected_signer: str = ""     # кто подписал сам файл; иное поле, см. ниже
    signer_checked: str = ""      # когда CN подтверждён на настоящем файле
    signer_proof: str = ""        # чем подтверждён: путь и что сказала Windows
    needs_admin: bool = False
    needs_admin_verified: bool = False
    official_url: str = ""
    hand_over: bool = False       # дальше нужен человек: вход, оплата, галочка
    publisher_trusted: bool = True
    bridge: str = ""              # bundled — мост уже лежит внутри программы
    instructions: str = ""
    checked: str = ""             # когда сверяли с winget, а не когда ставили
    # Что известно о версии НА ЭТОЙ МАШИНЕ. Поле catalog_version — версия
    # в каталоге winget, это другое; здесь то, что реально стоит.
    installed_version: str = ""
    known_good: list[str] = field(default_factory=list)
    version_measured: str = ""    # когда сняли версию
    bridge_checked: bool = False  # мост проверен живым запросом
    version_note: str = ""        # что именно измерено и чем
    #: Какие версии стояли ДО последнего обновления. Откат строит команду
    #: `winget install --version` из этого списка, и winget сам эти версии
    #: не помнит — он знает только текущую. Поэтому список пополняется
    #: ДО запуска обновления, а не после: после обновления прежняя версия
    #: уже никому не известна. Пустой список означает, что откатываться
    #: нечем, и кнопка обязана быть выключена с этой причиной, а не
    #: спрятана: человек, только что обновившийся и сломавший мост, не
    #: должен гадать, появится ли возврат.
    previous_versions: list[str] = field(default_factory=list)
    alternatives: list[dict] = field(default_factory=list)
    plugin: PluginSpec | None = None

    @property
    def can_install(self) -> bool:
        """Есть ли кнопка, которая действительно поставит."""
        return self.method in ("winget", "official-download")

    @property
    def stops_for_human(self) -> bool:
        """Где кнопка обязана остановиться и позвать человека."""
        return self.hand_over


@dataclass
class Server:
    """Один сервер из реестра вместе с живым состоянием."""

    id: str
    name: str
    raw: dict
    requirements: list[Requirement] = field(default_factory=list)
    installed: bool = False       # вписан ли в настройки opencode
    has_connection: bool = False  # есть ли что вписывать

    @property
    def missing(self) -> list[Requirement]:
        """Чего объективно не хватает: то, что программа умеет проверить."""
        return [r for r in self.requirements if not r.ok and r.kind != "manual"]

    @property
    def blocking_manual(self) -> list[Requirement]:
        """Ручные требования, без которых сервер работать не будет.

        Пример — приложения Adobe: сервер формально подключается, но
        отвечает отказом на всё. Отличать их надо, иначе программа
        предложит включить заведомо бесполезное.
        """
        return [r for r in self.requirements if r.kind == "manual" and r.blocks]

    @property
    def manual(self) -> list[Requirement]:
        """Ручные требования, которые человек проверяет сам."""
        return [r for r in self.requirements if r.kind == "manual" and not r.blocks]

    @property
    def ready(self) -> bool:
        """Можно ли включить.

        Обычные ручные требования НЕ мешают. Например, права
        администратора: без них сервер запускается, просто часть
        инструментов откажет. Если запретить включение из-за
        недоказанного, пользователь не сможет включить вполне рабочий
        сервер. А вот помеченные как blocks — мешают: без них сервер
        бесполезен, и честнее сказать об этом, чем вписать его зря.
        """
        if self.manual_setup:
            # Ручные требования здесь не блокируют: человек их уже
            # выполнил, если вставил конфигурацию. Блокировали бы - он
            # не смог бы дойти до окна, где эту конфигурацию вставлять.
            return not self.missing and self.has_connection
        return not self.missing and not self.blocking_manual and self.has_connection

    @property
    def license(self) -> str:
        return str(self.raw.get("license") or "—")

    @property
    def tools_count(self) -> str:
        value = self.raw.get("tools_count")
        return f"{value} инстр." if value else "—"

    @property
    def source(self) -> str:
        return str(self.raw.get("source") or "")

    # ------------------------------------------------ сервер с ручной настройкой

    @property
    def manual_setup(self) -> bool:
        """Настраивается ли сервер руками, изнутри своей программы.

        Такой сервер нельзя включить отсюда: его сначала надо включить
        там. Программа говорит об этом прямо и просит конфигурацию.
        """
        conn = self.raw.get("connection")
        return isinstance(conn, dict) and bool(conn.get("manual_config"))

    @property
    def ready_here(self) -> bool:
        """Честный ответ: можно ли этим сервером пользоваться на этой машине."""
        return bool(self.raw.get("ready_here"))

    @property
    def setup_steps(self) -> list[str]:
        """Пошаговая инструкция, если она есть в реестре."""
        steps = self.raw.get("setup_steps")
        if not isinstance(steps, list):
            return []
        return [str(s).strip() for s in steps if str(s).strip()]

    @property
    def auth(self) -> str:
        return str(self.raw.get("auth") or "")

    @property
    def only_while_running(self) -> str:
        return str(self.raw.get("only_while_running") or "")

    @property
    def why(self) -> str:
        return str(self.raw.get("why") or "")

    @property
    def program_install(self) -> ProgramInstall | None:
        """Как поставить программу для этого сервера.

        None в трёх случаях, и все три нормальны: блока нет вовсе (старый
        реестр), блок не объект (человек дописал строку) или программа
        серверу не нужна и метод равен `none`. Молча превращать это в
        «поставить нечем» нельзя — тогда потерялось бы «программа не нужна».
        """
        raw = self.raw.get("program_install")
        if not isinstance(raw, dict):
            return None
        return ProgramInstall(
            program=str(raw.get("program") or ""),
            method=str(raw.get("method") or "manual").strip().lower(),
            winget_id=str(raw.get("winget_id") or ""),
            catalog_version=str(raw.get("catalog_version") or ""),
            expected_publisher=str(raw.get("expected_publisher") or ""),
            expected_signer=str(raw.get("expected_signer") or ""),
            signer_checked=str(raw.get("signer_checked") or ""),
            signer_proof=str(raw.get("signer_proof") or ""),
            needs_admin=bool(raw.get("needs_admin")),
            needs_admin_verified=bool(raw.get("needs_admin_verified")),
            official_url=str(raw.get("official_url") or ""),
            hand_over=bool(raw.get("hand_over")),
            publisher_trusted=bool(raw.get("publisher_trusted", True)),
            bridge=str(raw.get("bridge") or ""),
            instructions=str(raw.get("instructions") or ""),
            checked=str(raw.get("checked") or ""),
            installed_version=str(raw.get("installed_version") or ""),
            known_good=[str(v).strip() for v in (raw.get("known_good") or [])
                        if str(v).strip()],
            version_measured=str(raw.get("version_measured") or ""),
            bridge_checked=bool(raw.get("bridge_checked")),
            version_note=str(raw.get("version_note") or ""),
            previous_versions=[str(v).strip()
                                for v in (raw.get("previous_versions") or [])
                                if str(v).strip()],
            alternatives=[a for a in (raw.get("alternatives") or []) if isinstance(a, dict)],
            plugin=_plugin_spec(raw.get("plugin")),
        )

    @property
    def verdict(self) -> str:
        return str(self.raw.get("verdict") or "")


# ------------------------------------------------------------------ чтение


#: Реестр уехал в `данные/` — это раздел 16 плана. Но в созданной базе он
#: остался в корне: оттуда его читают `programs.py` и окно управления, и
#: переносить его в базе незачем. Поэтому мест два, и порядок задан явно:
#: сначала новое, потом старое. Пустой элемент означает «в корне».
REGISTRY_FOLDERS = ("данные", "")


def registry_path(base: Path) -> Path:
    """Где лежит реестр: в новой папке программы или в корне базы."""
    base = Path(base)
    for part in REGISTRY_FOLDERS:
        candidate = base.joinpath(part, REGISTRY_NAME)
        if candidate.is_file():
            return candidate
    # Файла нет ни там, ни там. Возвращаем новое место, чтобы ошибка
    # называла то место, где файл должен лежать, а не то, где он был.
    return base.joinpath(REGISTRY_FOLDERS[0], REGISTRY_NAME)


def load_registry(base: Path) -> dict:
    """Читает реестр. При беде возвращает пустой — программа не падает."""
    try:
        data = json.loads(registry_path(base).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}

#: Сколько прежних версий держим. Пять — с запасом на несколько обновлений
#: подряд, но без бесконечного роста файла: список не архив, а точки отката.
#: Сама константа забыта была при первой записи версии, и `NameError`
#: вылез на первой же записи — `py_compile` на такое не смотрит, он не
#: проверяет имена. Поймал живой вызов на копии реестра.
HISTORY_LIMIT = 5


def save_registry(base: Path, data: dict) -> tuple[bool, str]:
    """Записать реестр целиком. Атомарно, иначе обрыв оставил бы битый файл.

    Сначала временный файл **рядом** с настоящим, потом `os.replace`.
    Замена атомарна только внутри одной папки: временный файл в
    `TEMP` на другой файловой системе заменой не станет, и при обрыве
    останется половина. Отказ возвращается текстом, а не молчанием —
    реестр это данные пользователя, и молчаливая потеря хуже отказа.

    Формат записи измерен: `ensure_ascii=False, indent=2` и перевод
    строки в конце воспроизводят нынешний файл побайтово, поэтому правка
    одного поля не переписывает остальное форматирование.
    """
    path = registry_path(base)
    body = json.dumps(data, ensure_ascii=False, indent=2) + "\n"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(path.name + ".новое")
        temp.write_text(body, encoding="utf-8")
        os.replace(temp, path)
    except OSError as exc:
        try:
            path.with_name(path.name + ".новое").unlink(missing_ok=True)
        except OSError:
            pass
        return False, f"не удалось записать реестр: {exc}"
    return True, f"реестр записан: {path}"


def _program_block(data: dict, server_id: str) -> dict | None:
    """Блок `program_install` сервера. None, если сервера или блока нет."""
    for server in data.get("servers") or []:
        if not isinstance(server, dict):
            continue
        if str(server.get("id") or "") != str(server_id):
            continue
        block = server.get("program_install")
        return block if isinstance(block, dict) else {}
    return None


def remember_version(base: Path, server_id: str, version: str) -> tuple[bool, str]:
    """Запомнить версию, которая стоит сейчас, ДО обновления.

    Вызывать надо до запуска `winget upgrade`. После обновления прежняя
    версия не известна никому: winget её не хранит, а мы её ещё не
    записали. Отсюда и порядок, и проверка порядка в селфтесте.

    Дубли не копим: повторное обновление той же версии не должно
    превращать список в ten одинаковых строк.
    """
    want = str(version or "").strip()
    if not want:
        return False, "нечего запоминать: версия не названа"
    data = load_registry(base)
    if not data:
        return False, "реестр не прочитан — записывать некуда"
    block = _program_block(data, server_id)
    if block is None:
        return False, f"сервера {server_id} нет в реестре"
    history = [str(v).strip() for v in (block.get("previous_versions") or [])
               if str(v).strip()]
    history = [v for v in history if v != want]
    history.insert(0, want)
    del history[HISTORY_LIMIT:]
    block["previous_versions"] = history
    return save_registry(base, data)


def set_installed_version(base: Path, server_id: str, version: str,
                          note: str = "") -> tuple[bool, str]:
    """Записать, какая версия стоит после обновления, — и чем это снято.

    Отдельно от `remember_version` намеренно: первое пишет историю ДО
    обновления, второе — состояние ПОСЛЕ. Перепутать их — значит либо
    потерять точку отката, либо записать в историю версию, которая
    никогда не стояла.
    """
    want = str(version or "").strip()
    if not want:
        return False, "нечего записывать: версия не названа"
    data = load_registry(base)
    if not data:
        return False, "реестр не прочитан — записывать некуда"
    block = _program_block(data, server_id)
    if block is None:
        return False, f"сервера {server_id} нет в реестре"
    block["installed_version"] = want
    block["version_measured"] = datetime.date.today().isoformat()
    block["version_note"] = str(note or "снято живьём через winget list")
    return save_registry(base, data)



# --------------------------------------------------------------- проверки


def _run(command: str, args: list[str]) -> tuple[bool, str]:
    """Запускает команду проверки. Ничего не устанавливает и не меняет.

    Отдельно про .cmd и .bat: на Windows такую обёртку нельзя выполнить
    напрямую, CreateProcess её не берёт — нужен cmd.exe. Наивный запуск
    «npx» молча падает, хотя npx на месте. Поэтому ищем настоящий файл
    и, если он пакетный, идём через cmd.
    """
    exe = shutil.which(command) or command
    argv = [exe, *args]
    if exe.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/c", exe, *args]
    try:
        done = subprocess.run(
            argv,
            capture_output=True, text=True, timeout=CHECK_TIMEOUT,
            encoding="utf-8", errors="replace",
        )
    except (OSError, subprocess.SubprocessError):
        return False, ""
    out = (done.stdout or "").strip() or (done.stderr or "").strip()
    return done.returncode == 0, out


def _find_by_pattern(pattern: str) -> str:
    """Ищет файл по шаблону пути и берёт самую новую версию.

    Только абсолютные шаблоны. Относительный означал бы угадывание
    («а где обычно ставят»), а угадывание молчит: не нашлось не там — и
    человек увидел «не установлена» про установленное.

    Версия выбирается по числам в имени папки, в которой лежит файл:
    `Blender 5.2` даёт (5, 2). Если чисел нет, порядок не меняется и
    берётся первый найденный — и это сказано здесь, потому что выбор
    «какая версия новее» по папке без номера иначе выглядел бы
    определённым.
    """
    if not re.match(r"^[A-Za-z]:[\\/]", pattern):
        return ""
    hits = [h for h in glob.glob(pattern) if Path(h).is_file()]
    if not hits:
        return ""
    best = max(hits, key=lambda h: _version_tuple(Path(h).parent.name))
    return str(Path(best).resolve())


def find_program(name: str) -> str:
    """Ищет исполняемый файл: PATH, ветка реестра Windows, шаблон пути.

    Шаблон нужен программам, которые кладут себя в папку с номером
    версии: `Blender 5.2`, `OBS 31`. Имя папки меняется при каждом
    обновлении, поэтому жёсткий путь протухает молча, а отсутствие пути
    вовсе выдаёт установленную программу за отсутствующую — и карточка
    предлагает поставить то, что уже стоит.
    """
    if any(ch in name for ch in "*?"):
        by_pattern = _find_by_pattern(name)
        if by_pattern:
            return by_pattern
        # Шаблон не нашёлся — не значит, что программы нет: она может
        # стоять в PATH, а в Program Files быть другой версии.
        return shutil.which(Path(name).name) or ""
    found = shutil.which(name)
    if found:
        return found
    key = PROGRAMS_IN_REGISTRY.get(name.lower())
    if not key:
        return ""
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        try:
            with winreg.OpenKey(hive, key) as handle:
                value, _ = winreg.QueryValueEx(handle, "")
        except OSError:
            continue
        path = str(value or "").strip()
        # Реестр отдаёт укороченный путь вида C:\PROGRA~2\... — он рабочий,
        # но показывать его пользователю неудобно, приводим к нормальному.
        if path and Path(path).exists():
            return str(Path(path).resolve())
    return ""


def _version_tuple(text: str) -> tuple[int, ...]:
    """Первая последовательность чисел в строке: v24.18.0 -> (24, 18, 0)."""
    match = re.search(r"(\d+(?:\.\d+)*)", text or "")
    if not match:
        return ()
    return tuple(int(part) for part in match.group(1).split("."))


def _plugin_spec(raw) -> PluginSpec | None:
    """Разобрать блок плагина. Нет блока или он не объект — плагина нет.

    Молчать об этом нельзя только в одном случае: если блок есть, но
    сломан. Пустой блок — обычное «плагина не описано», и карточка молчит
    правильно.
    """
    if not isinstance(raw, dict):
        return None
    return PluginSpec(
        finder=str(raw.get("finder") or "").strip(),
        name=str(raw.get("name") or "").strip(),
        button=str(raw.get("button") or "").strip(),
        hint=str(raw.get("hint") or "").strip(),
        unavailable=str(raw.get("unavailable") or "").strip(),
        fallback=str(raw.get("fallback") or "").strip(),
    )


def _check_obs_websocket(name: str) -> tuple[bool, str]:
    """Живая проверка плагина obs-websocket: библиотека на месте или нет.

    Импорт внутри функции намеренно. `bridges` — тяжёлый модуль про
    OBS и эмуляторы, а читать реестр можно и без него; тянуть мосты на
    этапе чтения незачем.
    """
    import bridges  # noqa: PLC0415 - тяжёлый модуль, нужен в одном месте

    present, _can_write, note = bridges.obs_plugin_state()
    return present, note


def _check_blender_addon(name: str) -> tuple[bool, str]:
    """Живая проверка аддона Blender: файл на месте или нет.

    Возвращает текст и про «осталось включить руками», потому что файл
    и работающий аддон — разные вещи. Импорт внутри функции: модуль
    аддона тянет за собой вещи тяжелее, чем чтение реестра, и на этапе
    чтения он не нужен.
    """
    import blender_addon  # noqa: PLC0415 - тяжёлый модуль, нужен здесь

    present, _can_write, note = blender_addon.addon_state()
    return present, note


#: Чем проверяется требование типа `plugin`. Ключ — то, что написано в
#: реестре в поле `check`. Это не список программ вкладки: программу
#: называет реестр, и без записи о плагине требования не будет.
_PLUGIN_CHECKS = {"obs-websocket": _check_obs_websocket,
                   "blender-mcp-addon": _check_blender_addon}


def check_requirement(spec: dict) -> Requirement:
    """Проверяет одно требование и возвращает его с результатом."""
    req = Requirement(
        what=str(spec.get("what") or ""),
        kind=str(spec.get("type") or "manual").strip().lower(),
        value=str(spec.get("check") or ""),
        args=[str(a) for a in (spec.get("args") or [])],
        min_version=int(spec.get("min_version") or 0),
        max_version=int(spec.get("max_version") or 0),
        until=str(spec.get("until") or "").strip(),
        note=str(spec.get("note") or ""),
        blocks=bool(spec.get("blocks")),
    )
    if not req.what:
        return req

    if req.kind == "manual":
        # Такое программа проверить не может: галочка в Excel, оплаченная
        # подписка, установленное приложение. Говорим прямо, а не угадываем.
        req.ok = False
        req.detail = "проверяется вручную"
        return req

    if req.kind == "plugin":
        # Плагин проверяется по-настоящему: папку установщик создаёт
        # всегда, даже когда плагин не выбран, и считать папку плагином —
        # значит вечно говорить «готово». Типа `manual` здесь быть не
        # должно: программа умеет и проверить, и починить, а «вручную»
        # скрыло бы и поломку, и кнопку.
        checker = _PLUGIN_CHECKS.get(req.value.strip().lower())
        if checker is None:
            req.ok = False
            req.detail = (f"в коде нет проверки для плагина «{req.value}» — "
                          f"состояние неизвестно, а не «есть»")
            return req
        req.ok, req.detail = checker(req.value)
        return req

    if req.kind == "program":
        path = find_program(req.value)
        req.ok = bool(path)
        req.detail = path or "не найден"
        return req

    # kind == "command"
    command = req.value
    if not command:
        req.detail = "нечего проверять"
        return req
    args = req.args or ["--version"]
    ok, out = _run(command, args)
    if not ok and not shutil.which(command):
        # Команды нет в PATH. На Windows npx/npm лежат .cmd-обёртками, и
        # shutil.which их находит, а вот запуск без расширения может
        # не сработать — пробуем с расширением .cmd.
        if shutil.which(command + ".cmd"):
            ok, out = _run(command + ".cmd", args)
    if not ok:
        req.ok = False
        req.detail = "не найден" if not out else f"не сработала: {out[:40]}"
        return req
    version = _version_tuple(out)
    if req.min_version and version and version[0] < req.min_version:
        req.ok = False
        req.detail = f"нужно {req.min_version}+, найдено {version[0]}"
        return req
    if req.max_version and version and version[0] > req.max_version:
        req.ok = False
        req.detail = f"слишком новая: нужно не выше {req.max_version}, найдено {version[0]}"
        return req
    if req.until and version and not versions.until_ok(req.until, version):
        # Строка та, что вернула программа, а не первый кусок: иначе про
        # 2026.2.1.8 человек прочитал бы «слишком новая: 2026» и не понял бы.
        req.ok = False
        req.detail = f"слишком новая: работает до {req.until}, найдено {'.'.join(str(n) for n in version)}"
        return req
    req.ok = True
    req.detail = out.splitlines()[0][:40] if out else "есть"
    return req


def load_extra_programs(base: Path) -> list[ProgramInstall]:
    """Программы вне списка серверов MCP: ставятся в той же вкладке, но
    серверами не являются.

    Отдельный ключ `extra_programs`, а не девятая запись в серверах. Иначе
    xray попал бы в проверку готовности серверов и в отчёт о подключении,
    а он там лишний: он не подключается к программе, а проксирует трафик.
    """
    data = load_registry(base)
    out: list[ProgramInstall] = []
    for raw in data.get("extra_programs") or []:
        if not isinstance(raw, dict):
            continue
        known = {f for f in ProgramInstall.__dataclass_fields__}
        extra = {k: v for k, v in raw.items() if k in known}
        extra.pop("id", None)
        out.append(ProgramInstall(**extra))
    return out


def load_servers(base: Path) -> list[Server]:
    """Читает реестр и проверяет требования каждого сервера."""
    data = load_registry(base)
    result: list[Server] = []
    for spec in data.get("servers") or []:
        if not isinstance(spec, dict):
            continue
        conn = spec.get("connection")
        server = Server(
            id=str(spec.get("id") or ""),
            name=str(spec.get("name") or ""),
            raw=spec,
            has_connection=isinstance(conn, dict) and bool(
                conn.get("command") or conn.get("url") or conn.get("manual_config")
            ),
        )
        for item in spec.get("requires") or []:
            if isinstance(item, dict):
                server.requirements.append(check_requirement(item))
        result.append(server)
    return result


# ---------------------------------------------------------- блок настроек


def manual_config_path(dest: Path, server_id: str) -> Path:
    """Куда кладётся конфигурация, вставленная человеком.

    Рядом с opencode.jsonc, то есть в папке настроек пользователя, а не
    в репозитории: токен студии не должен уезжать на GitHub.
    """
    return Path(dest) / f"mcp-{server_id}.json"


def load_manual_config(dest: Path, server_id: str) -> dict | None:
    """Читает вставленную конфигурацию. None, если её нет или она битая."""
    path = manual_config_path(dest, server_id)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "").strip()
    headers = data.get("headers")
    if not url or not isinstance(headers, dict):
        return None
    return {"url": url, "headers": {str(k): str(v) for k, v in headers.items()}}


def save_manual_config(dest: Path, server_id: str, text: str) -> tuple[bool, str]:
    """Разбирает вставленную конфигурацию и сохраняет адрес с заголовком.

    Текст берётся как есть из буфера обмена: человек копирует его в
    студии целиком, а разбирать приходится то, что именно скопировали.
    """
    raw = str(text or "").strip()
    if not raw:
        return False, "Пусто: скопируй конфигурацию в студии и вставь сюда."
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return False, (
            "Это не конфигурация в формате JSON. Скопируй её целиком "
            "кнопкой Copy Config в студии."
        )

    found_url = ""
    found_headers: dict = {}

    def walk(node: object, depth: int = 0) -> None:
        nonlocal found_url, found_headers
        if depth > 6 or not isinstance(node, dict):
            return
        url = node.get("url")
        if isinstance(url, str) and url.strip() and not found_url:
            found_url = url.strip()
            headers = node.get("headers")
            if isinstance(headers, dict) and headers:
                found_headers = {
                    str(k): str(v) for k, v in headers.items() if v
                }
        for value in node.values():
            walk(value, depth + 1)

    walk(data)
    if not found_url:
        return False, (
            "В конфигурации не нашёлся адрес сервера. Обычно он "
            "выглядит как http://localhost:63342/api/mcp."
        )
    path = manual_config_path(dest, server_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {"url": found_url, "headers": found_headers},
                ensure_ascii=False, indent=2,
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        return False, f"Не удалось сохранить конфигурацию: {exc}"
    tail = f", заголовков: {len(found_headers)}" if found_headers else ""
    return True, f"Конфигурация сохранена: {found_url}{tail}"


def drop_manual_config(dest: Path, server_id: str) -> bool:
    """Убирает сохранённую конфигурацию. True, если файла уже не было."""
    path = manual_config_path(dest, server_id)
    if not path.is_file():
        return True
    try:
        path.unlink()
        return True
    except OSError:
        return False


def resolve_command(command: list[str]) -> list[str]:
    """Подставляет пути вместо плейсхолдеров в команде запуска.

    Зачем. Реестр едет в публичный репозиторий, поэтому абсолютный путь
    компьютера в нём быть не может: у другого человека он неверен. Но в
    настройки opencode путь надо — иначе мост не запустится. Значит в
    реестре лежат плейсхолдеры, а подстановка происходит здесь, на
    машине человека.

    Плейсхолдеры:
        {PROGRAM}      — папка программы, где лежат мосты и thirdparty
        {DBAPP_PYTHON} — Python, на котором работает сама программа
    """
    if not any("{PROGRAM}" in part or "{DBAPP_PYTHON}" in part
               for part in command):
        return list(command)
    from core import find_python, program_root

    root = str(program_root())
    python = str(find_python() or "python")
    out: list[str] = []
    for part in command:
        part = part.replace("{PROGRAM}", root).replace("{DBAPP_PYTHON}", python)
        # Разделители в разных частях команды разные: путь программы на
        # Windows приходит с обратными слэшами, а остальное написано с
        # прямыми. В настройках это выглядит неровно, поэтому приводим к
        # одному виду. На других системах ничего не меняется.
        if os.name == "nt":
            part = part.replace("/", "\\")
        out.append(part)
    return out


def build_block(server: Server, manual: dict | None = None) -> str:
    """Кусок JSONC для секции mcp в opencode.jsonc. Пусто, если нечего.

    Формат ровно как у ncp_server_block: insert_entry ждёт готовую
    запись целиком — с именем сервера и запятой в конце, — а не только
    значение. Иначе в настройках появится лишняя скобка и файл
    перестанет читаться.
    """
    conn = server.raw.get("connection")
    if not isinstance(conn, dict):
        return ""
    url = str(conn.get("url") or "").strip()
    headers = {}
    if isinstance(manual, dict):
        url = str(manual.get("url") or url).strip()
        headers = manual.get("headers") or {}
    if url:
        body = {"type": "remote", "url": url, "enabled": True}
        if headers:
            body["headers"] = headers
    else:
        command = resolve_command(
            [str(part) for part in (conn.get("command") or [])]
        )
        if not command:
            return ""
        body = {"type": "local", "command": command, "enabled": True}
    inner = json.dumps(body, ensure_ascii=False, indent=2).splitlines()
    # Первая строка — «{», последняя — «}». Их не берём: свои скобки мы
    # ставим сами, вместе с именем сервера. Если взять и их, в настройках
    # получится «"имя": { { … }, }» и файл перестанет читаться.
    body_lines = "\n".join("    " + line for line in inner[1:-1])
    return f'"{server.id}": {{\n{body_lines}\n  }},'


def is_installed(dest: Path, server_id: str) -> bool:
    """Есть ли уже такой сервер в настройках opencode.

    has_entry ждёт текст самого объекта mcp, а не всего файла, поэтому
    сначала вырезаем объект по его границам.
    """
    from opencode_caps import find_key_object, has_entry  # локальный импорт

    cfg = Path(dest) / "opencode.jsonc"
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    bounds = find_key_object(text, "mcp")
    if bounds is None:
        return False
    return has_entry(text[bounds[0]:bounds[1]], server_id)


def _write_config(dest: Path, text: str) -> str:
    """Пишет настройки, предварительно сделав копию. Возвращает путь копии."""
    import time  # только для метки времени

    cfg = Path(dest) / "opencode.jsonc"
    if not cfg.is_file():
        cfg.write_text(
            '{\n  "$schema": "https://opencode.ai/config.json"\n}\n', encoding="utf-8"
        )
    backup_dir = Path(dest) / "_previous-version"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"opencode.jsonc-{stamp}"
    shutil.copy2(cfg, backup)
    cfg.write_text(text, encoding="utf-8")
    return str(backup)


def warm_up(server: Server, progress=None) -> str:
    """Прогревает кэш npx: скачивает пакет заранее, до вписывания блока.

    Зачем это. Первый запуск через npx качает пакет около минуты, а
    opencode на старт сервера даёт 30 секунд и показывает «Не удалось».
    То есть платить за скачивание должен тот, кто его инициирует, а не
    opencode при следующем запуске. Здесь команда стартует, успевает
    скачать и тут же останавливается — кэш прогрет, дальше старт 2–3 с.

    Возвращает пустую строку, если прогрев не нужен или не удался:
    отсутствие прогрева не повод отказывать во включении.
    """
    conn = server.raw.get("connection")
    if not isinstance(conn, dict) or not conn.get("warm_up"):
        return ""
    # Плейсхолдеры разрешаются так же, как при записи в настройки. Без
    # этого прогрев у моста, который запускается лаунчером, пытался бы
    # запустить «{DBAPP_PYTHON}» — то есть ничего, — и сервер выглядел бы
    # неработающим на ровном месте.
    command = resolve_command([str(part) for part in (conn.get("command") or [])])
    if not command:
        return ""

    def say(text: str) -> None:
        if progress:
            progress(text)

    say(f"Первый запуск «{server.name}»: скачиваю пакет, это до минуты…")
    exe = shutil.which(command[0]) or command[0]
    argv = [exe, *command[1:]]
    if exe.lower().endswith((".cmd", ".bat")):
        argv = [os.environ.get("COMSPEC") or "cmd.exe", "/c", exe, *command[1:]]
    try:
        proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except OSError as exc:
        return f"Прогрев не запустился: {exc}"
    try:
        try:
            proc.wait(timeout=WARM_UP_TIMEOUT)
        except subprocess.TimeoutExpired:
            # Сервер успешно стартовал и молча ждёт команды — значит
            # пакет уже скачан. Останавливаем, как и планировали.
            proc.kill()
        say(f"Пакет «{server.name}» скачан, кэш прогрет.")
        return ""
    finally:
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def enable(dest: Path, server: Server, progress=None) -> tuple[list[str], list[str]]:
    """Включает сервер. Возвращает (сообщения, ошибки).

    Порядок именно такой: сначала проверка требований, потом запись.
    Вписать сервер, который не запустится, хуже, чем не вписать его и
    объяснить, чего не хватает.
    """
    from opencode_caps import check_jsonc, ensure_object, insert_entry

    messages: list[str] = []
    errors: list[str] = []
    if not server.has_connection:
        return ([], ["В реестре нет команды подключения - включать нечем."])
    missing = server.missing
    if missing:
        return ([], [f"Не хватает: {', '.join(r.what for r in missing)}"])

    # Сервер, который настраивается руками: конфигурация лежит
    # рядом с opencode.jsonc. Если её нет - писать нечего: блок
    # без токена заведомо не подключится, и opencode покажет ошибку.
    _conn = server.raw.get("connection") or {}
    is_manual = bool(_conn.get("manual_config"))
    manual = None
    if is_manual:
        manual = load_manual_config(dest, server.id)
        if not manual:
            return ([], [
                "Для этого сервера нужна конфигурация из его программы. "
                "Включи сервер у себя, скопируй конфигурацию кнопкой "
                "Copy Config и вставь её здесь через «Включить»."
            ])
    elif server.blocking_manual:
        return ([], [
            "Сервер без этого работать не будет: "
            + ", ".join(r.what for r in server.blocking_manual)
        ])

    block = build_block(server, manual=manual)
    if not block:
        return ([], ["Команда подключения пустая — проверь реестр."])

    # Прогрев до вписывания: иначе opencode на своём старте не дождётся
    # скачивания и покажет «Не удалось».
    note = warm_up(server, progress)
    if note:
        errors.append(note)

    cfg = Path(dest) / "opencode.jsonc"
    try:
        original = cfg.read_text(encoding="utf-8") if cfg.is_file() else "{}"
    except (OSError, UnicodeDecodeError) as exc:
        return ([], [f"Настройки не прочитались: {exc}"])
    try:
        text = ensure_object(original, "mcp")
        text = insert_entry(text, "mcp", server.id, block)
    except ValueError as exc:
        return ([], [f"Настройки сломаны — правь вручную: {exc}"])
    if not check_jsonc(text):
        return ([], ["После вставки файл перестал читаться — не пишу его."])
    # Перезапись без нужды вредна: opencode следит за файлом и
    # перезапускает серверы MCP на каждое его изменение. Повторное
    # нажатие кнопки, когда блок уже записан, файл трогать не должно —
    # иначе на каждое нажатие рождается новый экземпляр моста, и
    # они копятся до трёх-четырёх копий.
    #
    # Сравниваем не тексты, а смысл: insert_entry при повторе оставляет
    # после себя лишние пустые строки, и побайтовое сравнение всегда
    # считает файл изменившимся.
    from opencode_caps import BEGIN_TPL, END_TPL
    begin = BEGIN_TPL.format(name=f"mcp.{server.id}")
    end = END_TPL.format(name=f"mcp.{server.id}")

    def _norm(chunk: str) -> str:
        # Запятая в конце строки не считается различием: build_block её
        # возвращает, а insert_entry перед записью срезает. Без этого
        # сравнение всегда говорило бы «файл изменился».
        return "\n".join(
            line.strip().rstrip(",")
            for line in chunk.splitlines() if line.strip()
        )

    if begin in original and end in original:
        current = original[original.index(begin) + len(begin):
                           original.index(end)]
        if _norm(current) == _norm(block):
            messages.append(
                f"Сервер «{server.name}» уже записан в настройки — "
                "файл не трогаю, opencode не перезапускаю"
            )
            return messages, errors
    try:
        backup = _write_config(dest, text)
    except OSError as exc:
        return ([], [f"Не записались настройки: {exc}"])
    messages.append(f"Сервер «{server.name}» вписан в настройки opencode")
    messages.append(f"Копия настроек: {backup}")
    if server.manual:
        messages.append(
            "Проверь вручную: "
            + "; ".join(f"{r.what} — {r.note}" for r in server.manual)
        )
    messages.append("Перезапусти opencode: серверы читаются при старте.")
    return messages, errors


def disable(dest: Path, server: Server) -> tuple[list[str], list[str]]:
    """Выключает сервер: убирает его блок из настроек."""
    from opencode_caps import check_jsonc, remove_entry

    messages: list[str] = []
    errors: list[str] = []
    cfg = Path(dest) / "opencode.jsonc"
    try:
        text = cfg.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return ([], [f"Настройки не прочитались: {exc}"])
    if server.id not in text:
        return ([f"Сервер «{server.name}» и так не вписан."], [])
    try:
        text = remove_entry(text, "mcp", server.id)
    except ValueError as exc:
        return ([], [f"Настройки сломаны — правь вручную: {exc}"])
    if not check_jsonc(text):
        return ([], ["После удаления файл перестал читаться — не пишу его."])
    try:
        backup = _write_config(dest, text)
    except OSError as exc:
        return ([], [f"Не записались настройки: {exc}"])
    messages.append(f"Сервер «{server.name}» убран из настроек")
    messages.append(f"Копия настроек: {backup}")
    # Токен - тоже часть включения. Оставлять его на диске после
    # «Выключить» значило бы соврать человеку про то, что он отключил.
    if manual_config_path(dest, server.id).is_file():
        if drop_manual_config(dest, server.id):
            messages.append("Сохранённая конфигурация с токеном удалена.")
        else:
            errors.append(
                "Файл с токеном не удалился: "
                f"{manual_config_path(dest, server.id)}"
            )
    return messages, errors


def mark_installed(servers: list[Server], dest: Path) -> None:
    for server in servers:
        server.installed = is_installed(dest, server.id)
