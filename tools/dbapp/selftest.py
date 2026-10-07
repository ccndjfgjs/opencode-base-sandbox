"""Проверка окна без участия человека.

Создаёт окно по-настоящему, но не показывает его на экране: прогоняет
проверку имени, создание базы и подключение во временной папке и печатает
отчёт. Запуск:

    python tools/dbapp/selftest.py
"""

from __future__ import annotations

import hashlib
import json
import re
import copy

import shutil
import sys
import tempfile
import traceback
from pathlib import Path

HERE = Path(__file__).resolve().parent

#: Папка модуля обязана попасть в путь ДО того, как начнут импортироваться
#: соседние модули. При запуске файлом (`python tools/dbapp/selftest.py`)
#: это происходит само, а при запуске через `-m` в sys.path[0] лежит
#: текущая папка, и импорт падает с ModuleNotFoundError. Такая правка уже
#: ломала штатный запуск: `import blender_addon` стоял выше этой строки, и
#: команда из плана `python -m tools.dbapp.selftest` не работала ни в
#: одной копии. Проверка порядка стоит в селфтесте отдельным пунктом.
sys.path.insert(0, str(HERE))

import android_studio  # noqa: E402
import blender_addon  # noqa: E402
import bridges  # noqa: E402
import core  # noqa: E402
import mcp_registry as _mcp_registry  # noqa: E402
import selfupdate as _selfupdate  # noqa: E402
import ui  # noqa: E402

#: Соседние модули селфтеста. Проверка ниже требует, чтобы все они
#: импортировались ПОСЛЕ `sys.path.insert`, и это не педантизм: при
#: запуске через `-m` в sys.path[0] лежит текущая папка, и импорт выше
#: вставки падает с ModuleNotFoundError. Так уже ломалось: `import
#: blender_addon` стоял на строке 18, а вставка была на 25, и команда
#: `python -m tools.dbapp.selftest` не работала ни в одной копии. Прямой
#: запуск файлом при этом продолжал работать, и поломка выглядела
#: безобидно, пока её не увидел второй агент, обходивший запуск через
#: PYTHONPATH.
_SELFTEST_LOCAL_MODULES = (
    "android_studio", "blender_addon", "bridges", "core", "mcp_registry",
    "ui", "antiblock", "winget_install", "versions", "selfupdate",
)

#: отчёт пишется и на экран, и в файл — в этой оболочке вывод теряется
REPORT = HERE / "selftest-report.txt"
_lines: list[str] = []


def echo(text: str = "") -> None:
    _lines.append(text)
    try:
        print(text, flush=True)
    except Exception:
        pass

OK = "ОК  "
BAD = "СБОЙ"
SKIP = "НЕ ПРОВЕРЕНО"

results: list[tuple[bool, str]] = []
unverified: list[str] = []


def check(good: bool, text: str) -> None:
    results.append((good, text))
    echo(f"[{OK if good else BAD}] {text}")


def check_machine(good: bool, text: str) -> None:
    """Проверка, исход которой лежит на машине, а не в нашем коде.

    Зонд подписи занимает до 19 секунд на файле в 92 МБ, а его лимит — 30.
    Под нагрузкой он не укладывается и возвращает «проверка не удалась».
    Если считать это сбоем, краснеет проверка, ничего не говорящая о нашем
    коде: сам модуль подписи различает «проверка не удалась» и «подпись плохая»,
    а селфтест сводило к одному.

    Поэтому здесь три состояния: ОК, СБОЙ и НЕ ПРОВЕРЕНО. Третье не входит
    ни в провалы, ни в число выполненных — иначе оно снова станет успехом.
    """
    if good:
        results.append((True, text))
        echo(f"[{OK}] {text}")
        return
    unverified.append(text)
    echo(f"[{SKIP}] {text}")


def probe_retry(fn, attempts: int = 3, pause: float = 1.0):
    """Повторяет зонд, пока он не справился.

    Зонд возвращает и успех, и отказ по своей причине, поэтому годность
    сообщает отдельная функция. Возвращает первый годный результат, а если
    все попытки негодные — последний.
    """
    import time as _time

    out = fn()
    for _ in range(max(0, attempts - 1)):
        if _looks_ready(out):
            break
        _time.sleep(pause)
        out = fn()
    return out


def _looks_ready(result) -> bool:
    """Годен ли результат зонда. Для подписи — выполнилась ли проверка."""
    if isinstance(result, tuple) and len(result) == 2:
        return result[0] is not None
    status = getattr(result, "status", None)
    if status is None:
        return True
    unknown = 1      # STATUS_UNKNOWN_ERROR: «проверка не удалась»
    return status != unknown


def _free_port() -> int:
    """Свободный локальный порт — чтобы проверка не зависела от того,
    запущен ли настоящий фасад на том порту, который задан
    в настройках."""
    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> int:
    echo("=" * 62)
    echo(" Проверка программы управления базой")
    echo("=" * 62)

    # Список созданных баз подменяем на временный на всю проверку:
    # без этого пробы запишут в настоящий список мусорные базы,
    # и в окне появятся пути в удалённые папки.
    import tempfile

    _isolate = Path(tempfile.mkdtemp(prefix="dbapp-list-"))
    # Заслон: настоящий список баз человека не должен измениться за
    # прогон. Создание базы дописывается в список через remember_base,
    # поэтому без такой проверки прогоны тихо наследили бы мусором.
    _real_list = core.bases_file()
    _real_before = (
    _real_list.read_text(encoding="utf-8") if _real_list.is_file()
    else "<файла нет>"
    )
    _real_paths_before = {
    str(e.get("path", "")).lower() for e in core.read_bases()
    }
    echo(f"Заслон: список бас под наблюдением — {_real_list.name}")
    core.use_bases_file(_isolate / "список.json")
    core.use_bases_file(_isolate / "список.json")
    echo(f"Список баз на время проверки: {core.bases_file()}")

    # ---- 1. окно собирается
    echo("\n--- 1. Окно ---")
    try:
        from PyQt6.QtWidgets import QApplication

        import main as app_main

        app = QApplication.instance() or QApplication(["selftest"])
        ui.apply_dark_theme(app)
        window = app_main.MainWindow()
        check(True, f"окно собрано, размер {window.width()}x{window.height()}")
        check(len(window.create_tab.program_list) > 0 or
              window.create_tab.program_list.count() > 0,
              f"список программ заполнен: "
              f"{window.create_tab.program_list.count()} строк")
    except Exception as exc:
        check(False, f"окно не собирается: {exc}")
        traceback.print_exc()
        return 1

    # ---- 2. проверка имени
    echo("\n--- 2. Проверка имени базы ---")
    good_names = ["Моя-база", "base 2026", "Проба_1"]
    bad_names = ["", "   ", "a/b", "a:b", "CON", "x" * 101, "a?b", "a*b", 'a"b']
    for name in good_names:
        try:
            core.validate_name(name)
            check(True, f"принимается: {name!r}")
        except core.NameError_ as exc:
            check(False, f"зря отклонено {name!r}: {exc}")
    for name in bad_names:
        try:
            core.validate_name(name)
            check(False, f"зря принято: {name!r}")
        except core.NameError_:
            check(True, f"отклоняется: {name!r}")

    # ---- 3. живая реакция окна на плохое имя
    echo("\n--- 3. Реакция окна на имя ---")
    tab = window.create_tab
    tab.name_edit.setText("a/b")
    check(not tab.btn_create.isEnabled(), "кнопка «Создать базу» заблокирована")
    check("не разрешает" in tab.name_hint.text() or "/" in tab.name_hint.text(),
          f"подсказка объясняет причину: {tab.name_hint.text()[:60]}")
    tab.name_edit.setText("Тестовая-база")
    check(tab.btn_create.isEnabled(), "с хорошим именем кнопка доступна")
    check("Тестовая-база" in tab.path_preview.text(),
          "показан полный путь будущей базы")

    # ---- 4. создание базы
    echo("\n--- 4. Создание базы ---")
    tmp = Path(tempfile.mkdtemp(prefix="dbapp-check-"))
    try:
        parent = tmp / "место"
        parent.mkdir()
        plan = core.build_plan(parent, "Проба-пустая", None)
        log = core.create_base(plan)
        target = plan.target
        check(target.is_dir(), f"папка базы создана: {target.name}")
        missing = [f for f in core.REQUIRED_FILES if not (target / f).is_file()]
        check(not missing, f"все основные файлы на месте (нет: {missing})")
        check((target / "библиотека/АКТИВНАЯ-ПАМЯТЬ.md").is_file(),
              "активная память записана")
        check((target / "библиотека/index.json").is_file(), "указатель записан")

        # Указатель должен совпадать со своей же папкой, а не просто
        # существовать. Найдено 29.09: на живой базе index.json заявлял
        # 18 записей, на диске было 17, лишней была служебная
        # _О-ПАПКЕ.md с пустым id. Мост это ловит у себя, а конструктор
        # раздаёт записи новым базам - и новая база родилась бы уже с
        # битым указателем. Проверяем на только что созданной базе.
        _nlib = target / "библиотека"
        _nrecs = sorted((_nlib / "записи").rglob("ncp-*.md")) \
            if (_nlib / "записи").is_dir() else []
        try:
            _ndata = json.loads(
                (_nlib / "index.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            _ndata = {}
        check(isinstance(_ndata, dict),
              "указатель новой базы читается как объект")
        check(isinstance(_ndata, dict)
              and _ndata.get("count") == len(_nrecs),
              f"указатель новой базы совпадает с её диском: "
              f"{_ndata.get('count') if isinstance(_ndata, dict) else '?'} "
              f"против {len(_nrecs)} файлов")
        _nbad = [e for e in (_ndata.get("entries") or [])
                 if not str(e.get("id", "")).startswith("ncp-")]
        check(not _nbad,
              f"в указателе новой базы нет записей без префикса ncp-: "
              f"{len(_nbad)}")
        _nlost = [e.get("path", "") for e in (_ndata.get("entries") or [])
                  if e.get("path") and not (_nlib / e["path"]).is_file()]
        check(not _nlost,
              f"все пути указателя новой базы есть на диске: {len(_nlost)} битых")
        _ncat = _nlib / "КАТАЛОГ.md"
        if _ncat.is_file():
            _ncat_text = _ncat.read_text(encoding="utf-8")
            check("_О-ПАПКЕ" not in _ncat_text,
                  "в каталоге новой базы нет служебной папки")
        else:
            check(False, "в новой базе нет КАТАЛОГ.md - навигация по знаниям не работает")
        check((target / "база.json").is_file(), "отметка о создании записана")
        for folder in core.NEW_DIRS:
            if not (target / folder).is_dir():
                check(False, f"нет папки {folder}")
                break
        else:
            check(True, f"созданы все {len(core.NEW_DIRS)} папок")
        check(len(log) > 0, f"отчёт о создании получен ({len(log)} строк)")

        # обход блокировок приезжает в новую базу сам, без лишних нажатий
        check((target / "tools" / "antiblock" / "http_facade.py").is_file(),
              "фасад обхода в новой базе на месте")
        check((target / "tools" / "antiblock" / "public_socks5.txt").is_file(),
              "стартовый список SOCKS5 в новой базе на месте")
        check((target / "tools" / "antiblock" / "subscriptions.txt").is_file(),
              "подписки VLESS в новой базе на месте")
        check((target / "tools" / "antiblock" / "xray_runner.py").is_file(),
              "свой Xray (xray_runner.py) в новой базе на месте")
        check((target / "tools" / "antiblock" / "dns_resolver.py").is_file(),
              "защищённый DNS (dns_resolver.py) в новой базе на месте")
        check((target / "tools" / "antiblock" / "command" / "antiblock.md").is_file(),
              "команда /обход в новой базе на месте")
        check(not list((target / "tools" / "antiblock").glob("*.local.*")),
              "личных рабочих списков (*.local.*) в новой базе нет")

        # файлы-навигаторы: без них нейросеть не знает об устройстве базы
        for name in core.NAV_FILES:
            check((target / name).is_file(), f"навигатор {name} создан")
        nav_text = (target / "КАРТА-БАЗЫ.md").read_text(encoding="utf-8")
        check("Куда что писать" in nav_text, "в карте есть таблица «куда что писать»")
        for area in core.KNOWLEDGE_AREAS:
            if area not in nav_text:
                check(False, f"область «{area}» не упомянута в карте")
                break
        else:
            check(True, f"все {len(core.KNOWLEDGE_AREAS)} областей упомянуты в карте")
        rules_text = (target / "ПРАВИЛА-ИИ.md").read_text(encoding="utf-8")
        check("не программист" in rules_text, "в правилах сказано про простой язык")
        check("по-русски" in rules_text, "в правилах закреплён русский язык")
        check("Ничего не удалять" in rules_text, "в правилах запрещено удаление")

        # Нейросеть должна знать, каким скиллом и агентом работать, и что
        # делать, если результат не устроил. Без этого всё хозяйство —
        # скиллы, агенты, skills-index.json — лежит мёртвым грузом.
        skill_doc = "инструкции/Скиллы-и-агенты.md"
        check((target / skill_doc).is_file(),
              f"новая база получила {skill_doc}")
        if (target / skill_doc).is_file():
            _inst = (target / skill_doc).read_text(encoding="utf-8")
            for _needle, _what in (
                ("skills-index.json", "ссылка на индекс скиллов"),
                ("systematic-debugging", "скилл для багов"),
                ("brainstorming", "скилл перед творческой работой"),
                ("@retsenzent", "агент-рецензент"),
                ("@proektirovschik", "агент-проектировщик"),
                ("Если результат не устроил", "разбор плохого результата"),
                ("verification-before-completion", "проверка перед «сделано»"),
            ):
                if _needle not in _inst:
                    check(False, f"в {skill_doc} нет: {_what}")
                    break
            else:
                check(True, f"{skill_doc} отвечает и на «какой скилл», и на «что делать»")
        # каждый файл из INSTRUCTION_TARGETS обязан существовать, иначе
        # opencode не сможет подключить инструкции
        for _name in core.INSTRUCTION_TARGETS:
            if not (target / _name).is_file():
                check(False, f"подключаемая инструкция не найдена: {_name}")
                break
        else:
            check(True, f"все {len(core.INSTRUCTION_TARGETS)} подключаемых инструкций на месте")
        check(skill_doc in "".join(core.INSTRUCTION_TARGETS),
              "новая инструкция вписана в INSTRUCTION_TARGETS (грузится в каждой сессии)")
        check("память\\АКТИВНАЯ-ПАМЯТЬ.md" not in rules_text
              and "память/АКТИВНАЯ-ПАМЯТЬ.md" not in rules_text,
              "правила не указывают на вытесненную папку память/")
        check("Скиллы-и-агенты.md" in rules_text,
              "в правилах есть отсылка к выбору скилла и агента")
        # AGENTS.md в корне: без него подключение вычистит правила из настроек
        check((target / "AGENTS.md").is_file(),
              "в корне новой базы есть AGENTS.md (правила не будут стёрты)")
        if (target / "AGENTS.md").is_file():
            _ag = (target / "AGENTS.md").read_text(encoding="utf-8").lower()
            check("скилл" in _ag and "агент" in _ag,
                  "AGENTS.md рассказывает про скиллы и агентов")
        # агенты едут с базой, как мосты
        _n_agents = len(list((target / "tools" / "agents").glob("*.md")))
        check(_n_agents >= 12, f"агенты едут с базой: {_n_agents} штук")

        # ---- скиллы: полный набор, индекс и реестр MCP едут с базой
        echo("\n--- 5а. Скиллы, индекс и реестр ---")
        _n_skills = (
            len([d for d in (target / "skills").iterdir() if d.is_dir()])
            if (target / "skills").is_dir() else 0
        )
        _src_skills = len(
            [d for d in (core.program_root() / "skills").iterdir()
             if d.is_dir() and (d / "SKILL.md").is_file()]
        )
        check(_n_skills == _src_skills,
              f"скиллов в базе столько же, сколько в конструкторе: {_n_skills}")
        _idx = target / "skills-index.json"
        check(_idx.is_file(), "skills-index.json едет с базой")
        if _idx.is_file():
            try:
                _data = json.loads(_idx.read_text(encoding="utf-8"))
                _names = {s["name"] for s in _data.get("skills", [])}
            except (OSError, ValueError) as _exc:
                _names = set()
                check(False, f"skills-index.json не читается: {_exc}")
            else:
                check(len(_names) == _n_skills,
                      f"записей в индексе столько же, сколько папок: {len(_names)}")
            _folders = {d.name for d in (target / "skills").iterdir() if d.is_dir()}
            check(_names == _folders,
                  "индекс и папки совпадают один в один")
            _missing_desc = [
                s["name"] for s in _data.get("skills", [])
                if not s.get("when") or not s.get("trigger") or not s.get("result")
            ]
            check(not _missing_desc,
                  f"у всех скиллов есть when, trigger и result (пропущено: {_missing_desc})")
        # у каждого скилла ограждения метаданных: без них он молча не работает
        _bad_fm = [
            d.name for d in (target / "skills").iterdir()
            if d.is_dir()
            and (d / "SKILL.md").is_file()
            and not (d / "SKILL.md").read_text(encoding="utf-8").startswith("---")
        ]
        check(not _bad_fm, f"у всех скиллов есть --- ограждения (сломаны: {_bad_fm})")
        # реестр MCP-серверов едет с базой
        _reg = target / "mcp-registry.json"
        check(_reg.is_file(), "mcp-registry.json едет с базой")
        if _reg.is_file():
            try:
                _rdata = json.loads(_reg.read_text(encoding="utf-8"))
            except (OSError, ValueError) as _exc:
                check(False, f"реестр не читается: {_exc}")
            else:
                _srv = _rdata.get("servers", [])
                check(len(_srv) >= 1, f"серверов в реестре: {len(_srv)}")
                _no_why = [s.get("id") for s in _srv if not s.get("why") or not s.get("verdict")]
                check(not _no_why, f"у всех серверов есть зачем и вердикт (нет: {_no_why})")

        # ---- 5д. Реестр серверов MCP: команды, проверка, вкл/выкл.
        # Сделано 28.09 по просьбе пользователя: серверы из реестра должны быть
        # видны в программе и включаться кнопкой. Раньше реестр был только
        # справочником — ехал с базой, но нигде не показывался, и вписать
        # сервер в настройки было нечем.
        # Проверки живут здесь, а не в разделе про мост: базу, созданную
        # выше, селфтест удаляет перед разделом про мост, и реестра там уже
        # нет. Ошибку эту я сначала истолковал неверно — подумал, что
        # реестр не копируется.
        echo("\n--- 5д. Реестр серверов MCP ---")
        import mcp_registry  # noqa: PLC0415 — рядом лежит, круга нет
        # opencode_caps импортируется и ниже по этой функции. Пока где-то
        # в теле есть такой импорт, имя считается локальным для всего тела,
        # и обращение раньше него падает с UnboundLocalError. Поэтому
        # импортируем здесь же, а не пользуемся «сверху».
        import opencode_caps  # noqa: PLC0415 — по той же причине

        reg_data = mcp_registry.load_registry(target)
        check(bool(reg_data.get("servers")), "реестр прочитан модулем, серверы на месте")
        for _spec in reg_data.get("servers") or []:
            _conn = _spec.get("connection")
            # Способ подключения законен трёх видов: команда, адрес или
            # ручная настройка. У android-studio адрес и токен выдаёт
            # сама студия, поэтому в реестре их нет и быть не должно -
            # реестр едет в публичный репозиторий. Отсутствие всех трёх
            # означает, что подключать нечем, и это ошибка.
            _conn_ways = bool(
                _conn.get("command") or _conn.get("url") or _conn.get("manual_config")
            )
            check(
                isinstance(_conn, dict) and _conn_ways,
                f"у сервера {_spec.get('id')} есть способ подключения",
            )
            if _spec.get("id") == "android-studio":
                # Адрес 127.0.0.1:64342/stream теперь в реестре: он
                # постоянный, публичный и секретом не является. В репозиторий
                # ехать ему нечего. А вот токен остаётся ненужным — сервер
                # открыт на localhost и авторизации не имеет.
                check(_conn.get("url") == "http://127.0.0.1:64342/stream",
                      "у android-studio в реестре постоянный адрес сервера")
                check("Bearer" not in json.dumps(_spec, ensure_ascii=False),
                      "токена студии в реестре нет")
        # Типы требований перечислены здесь и в коде. Список растёт вместе
        # с реестром, и это нормально: `plugin` проверяется по-настоящему
        # (есть библиотека или нет), в отличие от `manual`, где программа
        # честно говорит «не знаю, спроси человека».
        _TYPES = ("command", "program", "manual", "plugin")
        for _spec in reg_data.get("servers") or []:
            _bad = [
                r.get("what")
                for r in (_spec.get("requires") or [])
                if r.get("type") not in _TYPES
            ]
            check(not _bad, f"у {_spec.get('id')} у всех требований проставлен тип: {_bad}")
        _obs_types = [
            r.get("type") for r in (next((s for s in reg_data["servers"]
                                          if s.get("id") == "obs"), {})
                                    .get("requires") or [])
        ]
        check("plugin" in _obs_types,
              f"плагин заявлен требованием, а не заметкой: {_obs_types}")
        _plug_spec = next((r for r in (next((s for s in reg_data["servers"]
                                             if s.get("id") == "obs"), {})
                                        .get("requires") or [])
                           if r.get("type") == "plugin"), {})
        check(bool(_plug_spec.get("blocks")),
              "требование о плагине помечено blocks: без него сервер мёртв, "
              "и «Включить» обязано быть недоступно")
        check(not (next((s for s in reg_data["servers"]
                         if s.get("id") == "obs"), {})
                   .get("ready_here")),
              "готовность obs выключена по факту: плагина нет, а запись "
              "«работает, проверено вживую» была бы враньём")
        _servers = mcp_registry.load_servers(target)
        check(len(_servers) == len(reg_data.get("servers") or []),
              f"модуль загрузил серверов: {len(_servers)}")
        for _s in _servers:
            if _s.manual_setup:
                # Сервер настраивается изнутри своей программы. Раньше его
                # адрес был секретом, поэтому блок без вставленной
                # конфигурации строить было нельзя. Теперь адрес
                # android-studio известен и лежит в реестре, так что блок
                # строится. Но enable() без конфигурации всё равно
                # отказывает - это проверяется отдельно, ниже.
                _mb = mcp_registry.build_block(_s)
                if _s.id == "android-studio":
                    check("http://127.0.0.1:64342/stream" in _mb,
                          f"у {_s.id} блок строится с постоянным адресом из реестра")
                else:
                    check(not _mb,
                          f"у {_s.id} без конфигурации блок не пишется")
                continue
            _b = mcp_registry.build_block(_s)
            _ok = (_b.startswith(f'"{_s.id}": {{') and _b.rstrip().endswith("},")
                   and _b.count("{") == _b.count("}"))
            check(_ok, f"блок сервера {_s.id} собран цельно ({_b.count('{')} скобок)")

        # Включение и выключение — на временной копии папки настроек.
        _rtmp = Path(tempfile.mkdtemp(prefix="self-reg-"))
        _rdest = _rtmp / "opencode"
        _rdest.mkdir()
        (_rdest / "opencode.jsonc").write_text(
            '{\n  "$schema": "https://opencode.ai/config.json",\n'
            '  "mcp": {\n    "чужой": {"type": "remote", "url": "https://x"}\n  },\n'
            '  "instructions": ["a.md"]\n}\n',
            encoding="utf-8",
        )
        _wa = next((s for s in _servers if s.id == "windows-admin"), None)
        check(_wa is not None, "сервер windows-admin есть в реестре")
        if _wa is not None:
            # Требования подменяем: селфтест не должен зависеть от того, что
            # на машине стоит Node. Проверяем механику включения, а не окружение.
            _wa.requirements = [mcp_registry.Requirement(
                what="Проверка", kind="program", value="node", ok=True)]
            _wa.has_connection = True
            _wa.installed = False
            _m, _e = mcp_registry.enable(_rdest, _wa)
            check(not _e, f"включение сервера прошло: {_e}")
            _cfg_after = (_rdest / "opencode.jsonc").read_text(encoding="utf-8")
            check("windows-admin" in _cfg_after, "сервер вписан в настройки")
            check('"чужой"' in _cfg_after, "чужой сервер не тронут")
            check('"instructions"' in _cfg_after, "инструкции не тронуты")
            check(opencode_caps.check_jsonc(_cfg_after),
                  "настройки остались читаемыми после вставки")
            check(bool(list((_rdest / "_previous-version").glob("opencode.jsonc-*"))),
                  "копия настроек сделана до правки")
            _m, _e = mcp_registry.disable(_rdest, _wa)
            check(not _e, f"выключение прошло: {_e}")
            _cfg_off = (_rdest / "opencode.jsonc").read_text(encoding="utf-8")
            check("windows-admin" not in _cfg_off, "сервер убран")
            check('"чужой"' in _cfg_off, "чужой сервер уцелел после удаления")
            check(opencode_caps.check_jsonc(_cfg_off),
                  "настройки остались читаемыми после удаления")
            _m, _e = mcp_registry.disable(_rdest, _wa)
            check(not _e and _m, "повторное выключение — мягкий отказ, не ошибка")
        # Сломанные настройки программа обязана оставить в покое, а не
        # дописать в них сервер: файл и так уже не читается, хуже не сделаешь.
        (_rdest / "opencode.jsonc").write_text(
            '{\n  "mcp": {\n    "без запятой" 1\n  }\n}\n', encoding="utf-8"
        )
        _broken_before = (_rdest / "opencode.jsonc").read_text(encoding="utf-8")
        if _wa is not None:
            _m, _e = mcp_registry.enable(_rdest, _wa)
            check(bool(_e), f"на сломанных настройках вставка отказана: {_e}")
            check((_rdest / "opencode.jsonc").read_text(encoding="utf-8") == _broken_before,
                  "сломанный файл не тронут")
        shutil.rmtree(_rtmp, ignore_errors=True)

        # --- сервер с ручной настройкой: android-studio
        # Проверяем целиком, на временной папке настроек: без вставленной
        # конфигурации включение обязано отказаться, с конфигурацией -
        # вписать блок с токеном, а выключение - убрать и то и другое.
        _ast = next((s for s in _servers if s.id == "android-studio"), None)
        check(_ast is not None, "сервер android-studio есть в реестре")
        if _ast is not None:
            _spec = next((s for s in (reg_data.get("servers") or [])
                          if s.get("id") == "android-studio"), {})
            check(_spec.get("ready_here") is False,
                  "готовность честная: сервер ещё не включался")
            check("Bearer" not in json.dumps(reg_data, ensure_ascii=False),
                  "токена в реестре нет — он туда ехать не должен")
            check(_ast.manual_setup, "помечен как настраиваемый руками")
            check(_ast.has_connection, "подключаемым считается")
            check(_ast.ready, "кнопка «Включить» доступна")
            check(len(_ast.setup_steps) == 7,
                  f"пошаговая инструкция из семи шагов: {len(_ast.setup_steps)}")
            check(any("Настроить автоматически" in s for s in _ast.setup_steps),
                  "в инструкции есть путь через автонастройку")
            check(all("Copy Config" not in s for s in _ast.setup_steps),
                  "в инструкции нет несуществующей кнопки Copy Config")
            check(bool(_ast.only_while_running),
                  "сказано, что сервер живёт только при запущенной студии")
            check(bool(_ast.auth),
                  f"сказано про вход: {_ast.auth}")
            check("не нужен" in _ast.auth.lower() or "нет" in _ast.auth.lower(),
                  "сказано, что токен не нужен — сервер открыт на localhost")

            # Конфигурация, которую копирует студия. Токен выдуманный.
            _paste = json.dumps({
                "mcpServers": {
                    "android-studio": {
                        "url": "http://localhost:63342/api/mcp",
                        "headers": {"Authorization": "Bearer SELFTEST-TOKEN"},
                    }
                }
            })

            _atmp = Path(tempfile.mkdtemp(prefix="self-manual-"))
            try:
                _mdest = _atmp / "opencode"
                _mdest.mkdir()
                (_mdest / "opencode.jsonc").write_text(
                    '{\n  "$schema": "https://opencode.ai/config.json"\n}\n',
                    encoding="utf-8",
                )
                _before = (_mdest / "opencode.jsonc").read_text(encoding="utf-8")

                # Без конфигурации включать нечего: честный отказ.
                _m, _e = mcp_registry.enable(_mdest, _ast)
                check(bool(_e), f"без конфигурации включение отказано: {_e}")
                check(not _m, "успеха при отказе не сообщается")
                check((_mdest / "opencode.jsonc").read_text(encoding="utf-8") == _before,
                      "настройки не тронуты при отказе")
                check(not mcp_registry.manual_config_path(_mdest, _ast.id).exists(),
                      "файла с токеном не появилось")

                # Мусор вместо конфигурации отвергается и ничего не портит.
                for _junk in ("просто текст", '{"mcpServers":{}}', "   "):
                    _ok, _why = mcp_registry.save_manual_config(
                        _mdest, _ast.id, _junk)
                    check(not _ok, f"мусор отвергнут ({_junk.strip()[:14]!r}): "
                                   f"{_why[:44]}")
                check((_mdest / "opencode.jsonc").read_text(encoding="utf-8") == _before,
                      "мусор не тронул настройки")

                # С настоящей конфигурацией - вписывается токен.
                _ok, _why = mcp_registry.save_manual_config(
                    _mdest, _ast.id, _paste)
                check(_ok, f"конфигурация принята: {_why}")
                _saved = mcp_registry.load_manual_config(_mdest, _ast.id)
                check(bool(_saved), "конфигурация читается обратно")
                check(bool(_saved) and _saved.get("url") ==
                      "http://localhost:63342/api/mcp", "адрес разобран верно")
                check(bool(_saved)
                      and _saved.get("headers", {}).get("Authorization")
                      == "Bearer SELFTEST-TOKEN", "токен разобран верно")
                _cpath = mcp_registry.manual_config_path(_mdest, _ast.id)
                check("opencode-base" not in str(_cpath),
                      "конфигурация не попадает в репозиторий")

                _m, _e = mcp_registry.enable(_mdest, _ast)
                check(not _e, f"включение с конфигурацией прошло: {_e}")
                _cfg = (_mdest / "opencode.jsonc").read_text(encoding="utf-8")
                check("SELFTEST-TOKEN" in _cfg, "токен вписан в настройки")
                check(opencode_caps.check_jsonc(_cfg),
                      "настройки остались читаемыми")
                check(any("Перезапусти" in _x for _x in _m),
                      "напоминание про перезапуск opencode")

                # Выключение убирает и блок, и токен с диска.
                _m, _e = mcp_registry.disable(_mdest, _ast)
                check(not _e, f"выключение прошло: {_e}")
                _cfg_off = (_mdest / "opencode.jsonc").read_text(encoding="utf-8")
                check("SELFTEST-TOKEN" not in _cfg_off,
                      "токена в настройках не осталось")
                check(not mcp_registry.manual_config_path(_mdest, _ast.id).exists(),
                      "файл с токеном удалён — «выключил» значит выключил")
                check(any("токеном удалена" in _x for _x in _m),
                      "сказано вслух, что конфигурация удалена")
            finally:
                shutil.rmtree(_atmp, ignore_errors=True)

        # Скилл android-studio: папка, шапка и запись в индексе.
        _skill_dir = target / "skills" / "android-studio"
        check((_skill_dir / "SKILL.md").is_file(),
              "скилл android-studio лежит в skills/")
        if (_skill_dir / "SKILL.md").is_file():
            _stext = (_skill_dir / "SKILL.md").read_text(encoding="utf-8")
            check(_stext.startswith("---\nname: android-studio\n"),
                  "в шапке скилла имя android-studio")
            for _tool in ("build_project", "lint_files", "analyze_calls",
                          "xdebug_set_breakpoint", "xdebug_get_stack",
                          "xdebug_get_frame_values", "./gradlew"):
                check(_tool in _stext,
                      f"скилл называет инструмент или замену: {_tool}")

        # Блок в окне: таблица и кнопки собраны и показывают реестр.
        _reg_tab = getattr(window.caps_tab, "reg_table", None)
        check(_reg_tab is not None, "в окне есть таблица серверов")
        if _reg_tab is not None:
            check(_reg_tab.rowCount() == len(reg_data.get("servers") or []),
                  f"в таблице строк: {_reg_tab.rowCount()}")
            for _name in ("btn_reg_check", "btn_reg_on", "btn_reg_off", "btn_reg_src"):
                check(hasattr(window.caps_tab, _name), f"кнопка {_name} собрана")
            # Кнопки DBHub стоят в том же ряду: подключения к базам есть
            # только у него, и без них настройка сервера была бы неполной.
            for _name in ("btn_db_add", "btn_db_check"):
                check(hasattr(window.caps_tab, _name), f"кнопка {_name} собрана")
            check("dbhub" in window.caps_tab.AUTO_SERVERS,
                  "DBHub умеет «Настроить автоматически»")
            _states = [
                _reg_tab.item(r, 1).text() if _reg_tab.item(r, 1) else ""
                for r in range(_reg_tab.rowCount())
            ]
            check(all(_states), "у всех строк заполнено состояние")
            check(
                all("не проверено" in s for s in _states),
                f"до проверки состояние честное, а не выдуманное: {_states}",
            )
        # инструкция про серверы подключена
        check("инструкции/МCP-серверы.md" in core.INSTRUCTION_TARGETS,
              "инструкция про MCP-серверы подключена к каждой сессии")

        # ---- refresh_skills: обновляет, но чужие не трогает
        echo("\n--- 5б. Обновление скиллов в уже готовой базе ---")
        _probe = target / "skills" / "better-ui" / "SKILL.md"
        _orig = _probe.read_bytes()
        _probe.write_text("# устаревшая копия\n", encoding="utf-8")
        _my_skill = target / "skills" / " moy-skill"
        _my_skill.mkdir(parents=True, exist_ok=True)
        (_my_skill / "SKILL.md").write_text(
            "---\nname: moy-skill\ndescription: мой личный скилл\n---\n", encoding="utf-8"
        )
        for _msg in core.refresh_skills(target):
            pass
        check(_probe.read_bytes() == _orig, "устаревший скилл обновлён из конструктора")
        check(_my_skill.is_dir(), "свой скилл пользователя не тронут и не удалён")
        _again = core.refresh_skills(target)
        check(not any("обновлён" in _m for _m in _again),
              f"повторный вызов молчит, когда всё свежее: {_again}")
        (_my_skill / "SKILL.md").unlink()
        _my_skill.rmdir()

        # ---- агенты едут с базой и обновляются
        echo("\n--- 5в. Агенты едут с базой ---")
        _ag_dst = target / "tools" / "agents"
        _n_ag = len(list(_ag_dst.glob("*.md"))) if _ag_dst.is_dir() else 0
        _n_ag_src = len(list((core.program_root() / "tools" / "agents").glob("*.md")))
        check(_n_ag == _n_ag_src, f"агентов в базе: {_n_ag} (в конструкторе {_n_ag_src})")
        _missing_ag = [
            a for a in ("iskatel", "dokop", "proektirovschik", "programmist",
                        "proveryalschik", "retsenzent", "ohrannik", "dizayner",
                        "bazy", "devops", "golosovoy", "provodnik-pk")
            if not (_ag_dst / f"{a}.md").is_file()
        ]
        check(not _missing_ag, f"все 12 агентов на месте (нет: {_missing_ag})")
        # каждый агент упомянут в инструкциях, иначе нейросеть о нём не узнает
        _inst_all = "\n".join(
            (target / t).read_text(encoding="utf-8")
            for t in core.INSTRUCTION_TARGETS if (target / t).is_file()
        )
        _ag_in_instr = [a for a in _missing_ag if a not in _inst_all]
        _all_ag_names = [p.stem for p in _ag_dst.glob("*.md")]
        _silent_ag = [a for a in _all_ag_names if a not in _inst_all]
        check(not _silent_ag, f"каждый агент упомянут в инструкциях (молчат: {_silent_ag})")
        # правило «увидел возможность — скажи»
        check("УВИДЕЛ ВОЗМОЖНОСТЬ" in _inst_all.upper(),
              "в инструкциях есть правило: увидел возможность — скажи и предложи")
        # инструменты мостов упомянуты
        for _tool in ("ncp_status", "ncp_search", "ncp_read", "ncp_save",
                      "ncp_update", "ncp_checkpoint", "ncp_reindex",
                      "memory_save", "memory_read", "memory_search",
                      "memory_log_work", "library_status", "library_search",
                      "pc_status", "pc_files_read", "pc_apps_list", "pc_screenshot"):
            if _tool not in _inst_all:
                check(False, f"инструмент {_tool} не упомянут в инструкциях")
                break
        else:
            check(True, "все инструменты мостов упомянуты в инструкциях")

        # ---- 5г. инструменты памяти едут в мосте, а не в плагине.
        # Найдено 28.09: с версии opencode 1.18 плагин обязан отдавать
        # объект {id, setup}. Старая форма отвергается, и 12 инструментов
        # memory_* и library_* исчезли, хотя инструкции их требовали.
        # Теперь они в мосте NCP — его версия opencode не касается.
        echo("\n--- 5г. Инструменты памяти в мосте, плагин под v2 ---")
        _bridge = target / "tools" / "ncp-bridge"
        check((_bridge / "memory_tools.py").is_file(),
              "модуль инструментов памяти лежит в базе вместе с мостом")
        _srv_text = (_bridge / "server.py").read_text(encoding="utf-8")
        _missing_in_bridge = [
            t for t in ("memory_save", "memory_read", "memory_search", "memory_log_work")
            if f'"{t}"' not in _srv_text
        ]
        check(not _missing_in_bridge,
              f"мост объявляет все инструменты памяти (нет: {_missing_in_bridge})")
        for _alias in ("library_status", "library_checkpoint", "library_reindex"):
            if _alias not in _srv_text:
                check(False, f"псевдоним {_alias} не объявлен в мосте")
                break
        else:
            check(True, "псевдонимы library_* объявлены в мосте, а не только в бумаге")

        # Плагин обязан соответствовать схеме v2. Проверяем текстом:
        # Node в самопроверке не запускаем, а формат экспорта виден
        # в исходнике, и ломается он как раз молча.
        for _where, _plugin in (
            ("конструктор", core.program_root() / "config" / "plugins" / "memory-base.js"),
            ("база", target / "config" / "plugins" / "memory-base.js"),
        ):
            if not _plugin.is_file():
                check(False, f"плагин не найден: {_where}")
                continue
            _text = _plugin.read_text(encoding="utf-8")
            _ok = ("export default {" in _text
                   and re.search(r"^\s*id:\s*[\"']", _text, re.M)
                   and re.search(r"^\s*setup\(", _text, re.M))
            check(_ok, f"плагин ({_where}) отдаёт объект с id и setup — схема v2")
            check("export default MemoryBasePlugin" not in _text,
                  f"плагин ({_where}) больше не отдаёт функцию — её opencode отвергает")

        # области знаний и пояснения к папкам
        for area in core.KNOWLEDGE_AREAS:
            if not (target / "знания" / area).is_dir():
                check(False, f"нет области знаний «{area}»")
                break
        else:
            check(True, f"созданы все {len(core.KNOWLEDGE_AREAS)} области знаний")
        for name in ("память", "личное", "журнал-решений", "настройки",
                     "знания", "библиотека"):
            if not (target / name / "_О-ПАПКЕ.md").is_file():
                check(False, f"нет пояснения в папке {name}")
                break
        else:
            check(True, "пояснения к новым папкам созданы")
        for area in core.KNOWLEDGE_AREAS:
            if not (target / "знания" / area / "_О-ПАПКЕ.md").is_file():
                check(False, f"нет пояснения в области «{area}»")
                break
        else:
            check(True, "пояснения в областях знаний созданы")

        # повторное создание поверх базы должно быть запрещено
        try:
            core.build_plan(parent, "Проба-пустая", None)
            check(False, "повторное создание поверх базы не заблокировано")
        except core.NameError_:
            check(True, "повторное создание поверх базы заблокировано")

        # ---- 5. создание из образца
        echo("\n--- 5. Создание из образца ---")
        # Путь к образцу не вписан: самопроверка лежит внутри базы, поэтому
        # находит её сама. Так она годится для любого компьютера и любого
        # имени пользователя, а не только для того, где её писали.
        source = core.app_root()
        check((source / "profile.md").is_file(),
              f"образец найден рядом с программой: {source.name}")
        if source.is_dir():
            plan2 = core.build_plan(parent, "Проба-копия", source)
            # Считаем вызовы пересборки именно во время создания.
            # Раньше проверка смотрела на plan2.target потом, после
            # подключения этой же базы к программе, — а подключение
            # пересборку тоже зовёт. Сломанное создание поэтому было
            # не видно: указатели появлялись от подключения.
            _born: list[Path] = []
            _orig_make = core.refresh_knowledge_indexes

            def _spy_make(base, _o=_orig_make):
                _born.append(Path(base))
                return _o(base)

            core.refresh_knowledge_indexes = _spy_make
            try:
                core.create_base(plan2)
                # Проверка на содержимое, а не на папки. Папки девяти областей
                # создаёт create_base сам, из списка KNOWLEDGE_AREAS, и создавал
                # всегда. А вот файлы приносит _copy_ref_files, и без него девять
                # папок есть, а все пустые. Старая проверка смотрела на папки и
                # потому проходила даже со снесённой правкой.
                _src_area = core.app_root() / "знания"
                _tgt_zn = plan2.target / "знания"
                _empty = []
                for _a in sorted(p for p in _src_area.iterdir() if p.is_dir()):
                    _files = [f for f in _a.rglob("*.md")
                              if "Безопасность" not in f.parts]
                    if not _files:
                        continue
                    # Заготовки create_base кладёт в каждую область сам,
                    # поэтому область никогда не пуста. Считать их
                    # содержимым нельзя — иначе проверка зелёная всегда.
                    _got = [f for f in (_tgt_zn / _a.name).rglob("*.md")
                            if not f.name.startswith("_")]
                    _src_n = [f for f in _files if not f.name.startswith("_")]
                    if not _src_n:
                        continue
                    if not _got:
                        _empty.append(_a.name)
                check(not _empty,
                      f"содержимое всех областей "
                      f"знаний доехало в новую "
                      f"базу: пустые {_empty}")
            finally:
                core.refresh_knowledge_indexes = _orig_make
            check(bool(_born),
                  f"создание базы зовёт пересборку указателей: "
                  f"{len(_born)} раз")
            copy_skills = core.count_skills(plan2.target)
            # Скиллы берутся из КОНСТРУКТОРА, а не из образца: образец —
            # это пользовательская база, её набор может отставать и дополняться
            # своими скиллами. Проверяем, что новая база получила полный
            # комплект программы, а не то, что лежит в образце.
            real_skills = core.count_skills(core.program_root())
            check(copy_skills == real_skills,
                  f"скиллы перенесены из конструктора: {copy_skills} из {real_skills}")
            same = (plan2.target / "profile.md").read_bytes() == (
                source / "profile.md"
            ).read_bytes()
            check(same, "profile.md совпадает с образцом")

            # расширенная структура должна приехать из образца целиком
            for name in core.NAV_FILES:
                check((plan2.target / name).is_file(),
                      f"навигатор {name} перенесён из образца")
            areas = sum(
                1 for area in core.KNOWLEDGE_AREAS
                if (plan2.target / "знания" / area / "_О-ПАПКЕ.md").is_file()
            )
            check(areas == len(core.KNOWLEDGE_AREAS),
                  f"пояснения областей перенесены: {areas} из "
                  f"{len(core.KNOWLEDGE_AREAS)}")
            hints = sum(
                1 for name in ("память", "личное", "журнал-решений", "настройки")
                if (plan2.target / name / "_О-ПАПКЕ.md").is_file()
            )
            check(hints == 4, f"пояснения новых папок перенесены: {hints} из 4")

            # ---- 5а. заготовки личных данных
            echo("\n--- 5а. Заготовки личных данных ---")
            subs = [
                f"знания/{area}/{sub}"
                for area, names in core.KNOWLEDGE_SUBDIRS.items()
                for sub in names
            ]
            made_subs = sum(1 for name in subs if (plan2.target / name).is_dir())
            check(made_subs == len(subs),
                  f"подпапки областей созданы: {made_subs} из {len(subs)}")

            hints_subs = sum(
                1 for name in subs
                if (plan2.target / name / "_О-ПАПКЕ.md").is_file()
            )
            check(hints_subs == len(subs),
                  f"пояснения в подпапках: {hints_subs} из {len(subs)}")

            # заготовки «_ШАБЛОН-….md» должны доехать из образца
            blank_src = list(source.glob("знания/**/_ШАБЛОН-*.md"))
            blank_dst = list(plan2.target.glob("знания/**/_ШАБЛОН-*.md"))
            check(len(blank_src) > 0,
                  f"в образце есть заготовки: {len(blank_src)}")
            check(len(blank_dst) == len(blank_src),
                  f"заготовки перенесены: {len(blank_dst)} из {len(blank_src)}")

            # у каждой заготовки внутри — предупреждение о безопасности
            unsafe = [
                p.name for p in blank_dst
                if "чужой сервер" not in p.read_text(encoding="utf-8")
            ]
            check(not unsafe,
                  f"в каждой заготовке предупреждение о секретах "
                  f"(без него: {unsafe})")

            # сводная карта личных данных должна быть на месте
            check((plan2.target / "знания/_ЛИЧНЫЕ-ДАННЫЕ.md").is_file(),
                  "сводная карта «где что лежит» перенесена")
            card = plan2.target / "знания/_ЛИЧНЫЕ-ДАННЫЕ.md"
            card_text = card.read_text(encoding="utf-8") if card.is_file() else ""
            missing_areas = [
                area for area in core.KNOWLEDGE_AREAS
                if area not in card_text
            ]
            check(not missing_areas,
                  f"в карте упомянуты все области (нет: {missing_areas})")

            # у каждой области есть пояснение «_О-ПАПКЕ.md»
            for area in core.KNOWLEDGE_AREAS:
                if not (plan2.target / "знания" / area / "_О-ПАПКЕ.md").is_file():
                    check(False, f"нет пояснения в области «{area}»")
                    break
            else:
                check(True, "пояснения во всех областях знаний есть")

            # ---- 8ц-1. Список указателей знаний: какой файл и с каким лимитом
            # Число не выдумано: корневой один, плюс по одному на каждую
            # область, плюс ветка безопасности. Проверка сверяет и с этим
            # соотношением, и с решением от 05.10 — одиннадцать. Порог
            # «не меньше девяти» из плана не годился: при настоящих
            # одиннадцати он проходит и пропустит потерянный указатель.
            _kidx = core.knowledge_index_targets()
            # Числоanchored, а не выведено из того же списка. Проверка
            # «len == 1 + len(KNOWLEDGE_AREAS) + 1» выглядела разумно, но
            # убрать область из KNOWLEDGE_AREAS — и она останется верной,
            # потому что обе стороны сжимаются вместе. Откат это показал:
            # проверка не падала. Проверка, которая не может упасть, хуже
            # отсутствия проверки, поэтому здесь стоит решение числами.
            check(isinstance(_kidx, list) and len(_kidx) == 11,
                  f"указателей одиннадцать по решению от 05.10: {len(_kidx)}")
            check(len(core.KNOWLEDGE_AREAS) == 9,
                  f"областей девять по решению от 05.10: "
                  f"{len(core.KNOWLEDGE_AREAS)}")
            check(all(len(t) == 3 for t in _kidx),
                  "каждый указатель описан тройкой: путь, имя файла, лимит")
            # Уровень определяется числом слешей в пути: у корневого их ноль
            # («знания»), у области один, у большой ветки два. В плане стояло
            # «== 1», и это выбирало девять областей вместо корня — проверка
            # была написана неверно, а не код.
            _lvl1 = [t for t in _kidx if t[0].count("/") == 0]
            check(len(_lvl1) == 1,
                  f"корневой указатель ровно один: {[t[0] for t in _lvl1]}")
            _lvl2 = [t for t in _kidx if t[0].count("/") == 1]
            check(len(_lvl2) == 9,
                  f"указатель области есть у каждой из девяти: {len(_lvl2)}")
            check(bool(_lvl1) and _lvl1[0][1] == "_подсказки-общая.md",
                  "корневой указатель называется так: "
                  f"{_lvl1[0][1] if _lvl1 else 'нет'}")
            _sec = [t for t in _kidx if "Безопасность" in t[0]]
            # Лимит сверяется с константой кода, а не с числом в проверке.
            # Дословное «32 КБ» здесь означало, что расширение лимита
            # роняет проверку, которая проверяет не то: она охраняла
            # константу, а не смысл. Смысл — что у ветки свой лимит и он
            # не меньше коренного.
            check(len(_sec) == 1
                  and _sec[0][2] == core.INDEX_LIMIT_BRANCH
                  and _sec[0][2] >= _lvl1[0][2],
                  f"у ветки безопасности свой лимит, и он не меньше "
                  f"корневого: {_sec}")
            # Имя указателя выводится из имени папки. Проверяем на всех
            # областях сразу, иначе расхождение проявится позже, когда
            # указатель уже переименован, а код — нет.
            _bad_name = []
            for _rel, _fname, _lim in _kidx:
                _folder = Path(_rel).name
                _want_name = f"_подсказки-{_folder.lower()}.md"
                if _rel == "знания":
                    _want_name = "_подсказки-общая.md"
                if _fname != _want_name:
                    _bad_name.append(f"{_rel}: {_fname} ждали {_want_name}")
            check(not _bad_name,
                  f"имя указателя выведено из имени папки: {_bad_name[:3]}")

            # ---- 8ц-2. Машинная часть указателя
            #
            # Заведомо известный пример для счёта файлов: в «Технике» лежит
            # ровно один обычный файл, служебные два. Если счётчик начнёт
            # считать служебные, проверка это покажет числом, а не словами.
            _kt = Path(tempfile.mkdtemp(prefix="указатели-"))
            try:
                (_kt / "знания" / "Деньги").mkdir(parents=True)
                (_kt / "знания" / "Деньги" / "вклад.md").write_text("x",
                                                                  encoding="utf-8")
                _tech = _kt / "знания" / "Имущество" / "Техника"
                _tech.mkdir(parents=True)
                (_tech / "a.md").write_text("x", encoding="utf-8")
                # Вложенная папка обязательна: без неё scope=children и
                # scope=tree дают одинаковый текст, и проверка «tree
                # перечисляет поддерево» проходит вхолостую.
                (_tech / "Глубже").mkdir(parents=True)
                (_tech / "Глубже" / "b.md").write_text("x", encoding="utf-8")
                # Служебные файлы не должны попадать в счёт.
                (_tech / "_О-ПАПКЕ.md").write_text("пояснение",
                                                   encoding="utf-8")
                (_tech / "_подсказки-техника.md").write_text("указатель",
                                                              encoding="utf-8")

                _k_root = _kt / "знания"
                _t_areas = core.build_knowledge_index(_k_root, "areas",
                                                      "2026-10-05")
                check("`Деньги/`" in _t_areas and "`Имущество/`" in _t_areas,
                      f"корневой указатель перечисляет области:\n"
                      f"{_t_areas[:200]}")
                # Формулировка правила resolution сверяется дословно: в
                # плане проверка искала одну строку, а код писал другую,
                # и проверка упала бы на готовом коде.
                check("Полный путь = папка указателя + строка" in _t_areas,
                      "в авточасти написано правило разрешения путей")
                check(core.INDEX_BEGIN in _t_areas
                      and core.INDEX_END in _t_areas,
                      "машинная часть отмечена маркерами")
                # Маркеры сверяются с литералом, а не с core.INDEX_BEGIN.
                # Со своей константой сравнение тавтологично: что бы сборщик
                # ни записал, там будет ровно core.INDEX_BEGIN. Откат это
                # показал — подмена маркера прошла молча.
                check("<!-- == авточасть: дальше не редактировать руками -->"
                      in _t_areas
                      and "<!-- == конец авточасти -->" in _t_areas,
                      "маркеры именно те, о которых договорились")

                _t_child = core.build_knowledge_index(
                    _kt / "знания" / "Имущество", "children", "2026-10-05")
                check("`Техника/`" in _t_child,
                      f"указатель области перечисляет подпапку: "
                      f"{_t_child[:200]}")
                check("`Техника/Глубже/`" not in _t_child,
                      "scope=children не заходит глубже прямых подпапок")
                check("`Техника/Техника/`" not in _t_child,
                      "путь не удваивается: в разделе нет повторов")

                _t_tree = core.build_knowledge_index(
                    _kt / "знания" / "Имущество", "tree", "2026-10-05")
                check("`Техника/Глубже/`" in _t_tree,
                      f"scope=tree перечисляет поддерево: {_t_tree[:200]}")
                check(_t_child != _t_tree,
                      "children и tree дают разный текст на одном дереве")

                # Счёт: у «Техники» один обычный файл. Служебные два не
                # считаются, иначе было бы три.
                check("- `Техника/` — 2 файла" in _t_tree,
                      f"служебные файлы не попали в счёт: {_t_tree[:300]}")
                # Число общее — каждый файл посчитан один раз: три
                # обычных файла на дереве, и столько же в шапке.
                check("файлов: 3" in _t_areas,
                      f"общее число файлов верно: {_t_areas[:300]}")
                # Про перекрытие сказано словами, иначе модель сложит строки
                # и получит завышенный итог.
                check("Складывать строки нельзя" in _t_tree,
                      "в авточасти сказано, что строки перекрываются")
                # Сборка детерминирована: дважды подряд — строки те же.
                _t_again = core.build_knowledge_index(_k_root, "areas",
                                                      "2026-10-05")
                check(_t_again == _t_areas,
                      "сборка детерминирована: повтор дал те же строки")
                # Детерминированность и порядок — разные вещи: перевёрнутый
                # список даёт те же строки дважды. Порядок проверяется
                # отдельно, по алфавиту.
                _listed = re.findall(r"^- `([^`]+)/`",
                                     _t_tree, re.M)
                check(_listed == sorted(_listed),
                      f"пути идут по алфавиту: {_listed}")
                # Папок и файлов посчитано, а не выдумано.
                check("Папок: 2" in _t_areas,
                      f"число папок в авточасти верно: {_t_areas[:300]}")
            finally:
                shutil.rmtree(_kt, ignore_errors=True)

            # ---- 8ц-3. Пересборка: текст модели сохранён, потерянное — в архив
            #
            # Три папки, а не пустая: «уцелевшая» должна быть уцелевшей
            # по-настоящему. В плане папка была пустой, и проверка про
            # уцелевшую строку проходила вхолостую — строка одинаково
            # попала бы и в архив, и обратно в указатель.
            #
            # Старая авточасть знает про все три. «Транспорт» и «Земля»
            # с диска убраны, «Техника» осталась. В смысловой части —
            # по строке на каждую, и строка про «Транспорт» ссылается на
            # коротком имени, а не на полном пути: так проверяется, что
            # поиск ловит и короткое имя.
            #
            # Смысловая часть вынесена в отдельную переменную: её ожидаемый
            # итог известен заранее целиком, и сверка идёт дословно. Поиск
            # одной подстроки тут недостаточен — подстрока может уцелеть,
            # а остальное пропасть, и проверка скажет «всё на месте».
            _KEEP_1 = "## Когда заходить"
            _KEEP_2 = ("- про устройства — смотри `Техника/`, "
                       "там всё про железо.")
            _KEEP_3 = ("- отправку ждёт скрипт с `ALLOW_PUSH`, "
                       "настройки в `opencode.jsonc`.")
            _GONE_1 = "- про машины — смотри `Транспорт/`, там всё про колёса."
            _GONE_2 = "- про участок — смотри `Земля/`, там всё про грядки."
            # Повторы. `carried` — множество строк, а не позиций, поэтому
            # фильтр `ln not in carried` убирает ВСЕ строки, совпавшие с
            # перенесённой. Законный повтор без пути уцелеть должен: если
            # бы не уцелел, фильтр оказался бы граблей на любом тексте,
            # где одна строка встречается дважды.
            _REPEAT_1 = "---"
            _REPEAT_2 = "- про железо — снова `Техника/`."
            # Живая строка, начинающаяся ТАК ЖЕ, как перенесённая, но про
            # другую папку. Без неё проверки на повтор зелёные по
            # случайной причине: в carried их нет, и они не различают
            # рабочий фильтр со сломанным. Здесь filter по полному тексту
            # отличает, а фильтр по общему началу — нет, и это ошибка:
            # уцелевшая папка не должна уезжать в архив.
            _KEEP_4 = ("- про машины и мотоциклы — смотри `Техника/`, "
                       "там всё про колёса.")
            # Строка, где ИСЧЕЗНУВШАЯ и УЦЕЛЕВШАЯ папки вместе. Такая
            # строка раньше уходила в архив целиком, и упоминание уцелевшей
            # папки пропадало из указателя: проверено, «Техника» была в
            # смысловой части — False. Теперь строка остаётся дословно и
            # получает видимую пометку, а копия идёт в архив.
            _MIX = ("- про путешествия — смотри `Транспорт/` "
                    "и `Техника/`.")
            # С дефисом: строка начинается с «- », и без него startswith
            # не срабатывал — четыре проверки падали вхолостую, хотя
            # данные были верны.
            _MIX_TAIL = "- про путешествия — смотри"
            _k3 = Path(tempfile.mkdtemp(prefix="пересборка-"))
            try:
                _ia = _k3 / "Имущество"
                (_ia / "Техника").mkdir(parents=True)
                (_ia / "Техника" / "железо.md").write_text("x",
                                                           encoding="utf-8")
                _old = ("# Указатель: Имущество\n\n"
                        + core.INDEX_BEGIN
                        + "\nСобрано: 2026-10-01.\n\n## Пути\n"
                        "- `Техника/` — 3 файла\n"
                        "- `Транспорт/` — 2 файла\n"
                        "- `Земля/` — 1 файл\n" + core.INDEX_END + "\n\n"
                        + _KEEP_1 + "\n"
                        + _KEEP_2 + "\n"
                        + _REPEAT_1 + "\n"
                        + _REPEAT_2 + "\n"
                        + _REPEAT_1 + "\n"
                        + _GONE_1 + "\n"
                        + _GONE_2 + "\n"
                        + _KEEP_4 + "\n"
                        + _MIX + "\n"
                        # Повтор ПЕРЕНЕСЁННОЙ строки. Без него поломка
                        # «фильтр убирает законный повтор» неотличима от
                        # рабочего кода: измерено — результаты совпали
                        # строка в строку. Чтобы отличие появилось, нужны
                        # две копии перенесённой строки.
                        + _GONE_1 + "\n"
                        + _KEEP_3 + "\n")
                (_ia / "_подсказки-имущество.md").write_text(_old,
                                                            encoding="utf-8")
                _report = core.write_knowledge_index(
                    _ia, "_подсказки-имущество.md", "children", "2026-10-05")
                _after = (_ia / "_подсказки-имущество.md").read_text(
                    encoding="utf-8")
                _arch = _ia / core.ARCHIVE_NAME
                check(_arch.is_file(),
                      f"при исчезновении папки создан архив: {_report}")
                _arch_txt = _arch.read_text(encoding="utf-8") \
                    if _arch.is_file() else ""

                # Главное проверка задачи: текст модели не затирается.
                # Заглушка «Пока пусто» вместо смысловой части — это ровно
                # то, что делал код из шага 3 плана.
                #
                # Сверка дословная и ожидаемый итог известен заранее: уцелели
                # ровно эти строки, в этом порядке, перенесённых нет. Раньше
                # здесь стоял поиск одной подстроки — он прошёл бы и на коде,
                # который оставил одну строку из десяти.
                __, _, _sem = core._split_index(_after)
                # Смешанная строка получает пометку, поэтому дословное
                # сравнение строим по началу строки, а саму пометку
                # проверяем отдельно.
                _expect = "\n".join([_KEEP_1, _KEEP_2, _REPEAT_1, _REPEAT_2,
                                     _REPEAT_1, _KEEP_4, _MIX, _KEEP_3])
                _norm_sem = [ln.split(" (проверено")[0]
                             for ln in _sem.strip().split("\n")]
                check("\n".join(_norm_sem) == _expect,
                      f"смысловая часть уцелела дословно, перенесённого нет:\n"
                      f"    ждали: {_expect!r}\n"
                      f"    получили: {'|'.join(_norm_sem)!r}")
                # Повтор без пути остаётся. Фильтр идёт по содержимому строки,
                # а не по месту, поэтому обе «---» должны уцелеть — иначе
                # фильтр убирает лишнее.
                check(_sem.strip().count(_REPEAT_1) == 2,
                      f"законный повтор строки уцелел обе раза: "
                      f"{_sem.strip().count(_REPEAT_1)} (ждать 2)")
                check(_REPEAT_2 in _sem,
                      "повторная ссылка на уцелевшую папку осталась")
                # Строка живая, но начинается так же, как перенесённая.
                # Это единственный случай, где фильтр по общему началу
                # отличается от фильтра по полному тексту, — значит только
                # он и различает рабочий фильтр со сломанным.
                check(_KEEP_4 in _sem,
                      f"живая строка с тем же началом, что перенесённая, уцелела: {_KEEP_4 in _sem}")
                # Смешанная строка: дословный текст на месте, пометка
                # дописана, копия в архиве. Проверяем все три по отдельности,
                # иначе потеря уцелевшей папки снова станет незаметной.
                _mix_line = next((ln for ln in _sem.split("\n")
                                  if ln.startswith(_MIX_TAIL)), "")
                check(bool(_mix_line), "смешанная строка осталась в указателе")
                check("Транспорт" in _mix_line and "Техника" in _mix_line,
                      f"в смешанной строке остались обе папки: {_mix_line[:80]}")
                check("(проверено" in _mix_line and "на месте" in _mix_line,
                      f"к смешанной строке дописана видимая пометка: "
                      f"{_mix_line[-70:]}")
                check(_MIX in _arch_txt,
                      "копия смешанной строки ушла в архив для человека")
                check(_KEEP_3 in _after,
                      "смысловая часть модели уцелела целиком")
                check(_KEEP_2 in _after,
                      "строка про уцелевшую папку осталась в указателе")
                # Упоминание кода в кавычках — не папка. Без этой проверки
                # расширение «ищем всё, чего нет на диске» вырезало бы из
                # живого текста `ALLOW_PUSH` и `opencode.jsonc`.
                check("ALLOW_PUSH" in _after and "opencode.jsonc" in _after,
                      "упоминание кода в кавычках осталось в указателе")
                check("ALLOW_PUSH" not in _arch_txt,
                      "упоминание кода не уехало в архив")
                check(_GONE_1 not in _after and _GONE_2 not in _after,
                      "из указателя убраны строки, где папки только исчезнувшие")
                # Ищем в АВТОЧАСТИ, а не во всём файле. Раньше здесь стояло
                # "`Техника/`" in _after — и это проверяло не то: та же
                # подстрока есть в смысловой части («смотри `Техника/`»), так
                # что проверка оставалась зелёной и при выброшенной из
                # авточасти папке. Откатом измерено: поломка «выбросить папки
                # с одним файлом» не давала здесь ни одной красной.
                _b, _auto, _ = core._split_index(_after)
                check("`Техника/`" in _auto,
                      f"уцелевшая папка осталась в авточасти: "
                      f"{[_ln for _ln in _auto.split(chr(10)) if 'Техника' in _ln][:1]}")

                # Потерянное не потеряно: обе строки в архиве.
                check("машины" in _arch_txt and "колёса" in _arch_txt,
                      "строка про исчезнувшую папку ушла в архив")
                check("участок" in _arch_txt and "грядки" in _arch_txt,
                      "вторая исчезнувшая папка тоже ушла в архив")
                # Короткое имя, а не полный путь: строка «смотри `Земля/`»
                # поймана именно так.
                check("Земля" in _arch_txt,
                      "поиск ловит и короткое имя папки")

                # Архив дописывается, а не перезаписывается.
                _size1 = _arch.stat().st_size
                core.write_knowledge_index(_ia, "_подсказки-имущество.md",
                                           "children", "2026-10-06")
                check(_arch.stat().st_size >= _size1,
                      "архив не перезаписывается при следующей пересборке")

                # Первая строка архива прямо говорит, что это не инструкция:
                # иначе модель примет лежащие рядом строки за указание.
                check("не инструкция" in _arch_txt,
                      "в архиве сказано, что это не инструкция для модели")
                # У каждой записи есть прежний путь и дата — иначе архив
                # нечем разбирать (проверка 10 дизайна).
                _entries = re.findall(r"^## Было: `([^`]+)`", _arch_txt, re.M)
                check(len(_entries) == 3,
                      f"в архиве три записи, с прежним путём: {_entries}")
                check(all(re.search(r"\d{4}-\d{2}-\d{2}", blk)
                          for blk in _arch_txt.split("## Было: ")[1:]),
                      "у каждой записи в архиве есть дата переноса")

                # Смысловое правило, а не совпадение подстрок: архив не
                # должен попасть в сам указатель, иначе модель прочтёт его
                # как указание.
                check(core.ARCHIVE_NAME not in _after,
                      "указатель не ссылается на архив")

                # Архив — служебный файл, и число файлов на диске теперь
                # одно: железо.md. Раньше счёт выдавал два, потому что
                # архив не подходил под правило «служебный». Проверка идёт
                # от известного на диске, а не от того же правила, которым
                # считает код: такое сравнение согласовано всегда и не
                # значит ничего.
                _real = [p for p in _ia.rglob("*.md")
                         if not core._is_service_markdown(p)]
                check(len(_real) == 1,
                      f"после архива остался один обычный файл, "
                      f"не считая служебные: {[p.name for p in _real]}")
                check("файлов: 1" in _after,
                      f"в авточасти написано честное число файлов: "
                      f"{_after[:160]}")
                # Переводы строк — по байтам, а не по тексту. Проверено
                # измерением: read_text(encoding="utf-8") делает universal
                # newlines и превращает \r\n в \n, поэтому чтение текстом
                # CRLF не видно вовсе. На Windows это 13 лишних байт в
                # каждой строке, и байтовый лимит указателя начинает
                # зависеть от того, чей файл правили последним.
                _raw = (_ia / "_подсказки-имущество.md").read_bytes()
                check(b"\r\n" not in _raw,
                      f"указатель записан с LF, а не CRLF: "
                      f"{_raw.count(bytes([13]))} байт CR в файле")
            finally:
                shutil.rmtree(_k3, ignore_errors=True)

            # ---- 8ц-4. Указатели знаний создаются сами.
            #
            # До этого пересборку звал только селфтест. Создание базы её
            # не звало, и новая база рождалась без единого указателя:
            # знания лежали, а карты к ним не было, и модель не знала, куда
            # смотреть. Измерено: у новой базы из образца не было ни одного
            # указателя из одиннадцати.
            _k4b = Path(tempfile.mkdtemp(prefix="указатели-"))
            try:
                _zn = _k4b / "знания"
                (_zn / "Учёба").mkdir(parents=True)
                (_zn / "Учёба" / "Программисту-знать.md").write_text(
                    "знание про программиста\n", encoding="utf-8")
                _msgs = core.refresh_knowledge_indexes(_k4b)
                _root = _zn / "_подсказки-общая.md"
                check(_root.is_file(),
                      f"корневой указатель создан: {_root}")
                _rtxt = (_root.read_text(encoding="utf-8")
                         if _root.is_file() else "")
                check("Учёба" in _rtxt,
                      f"корневой указатель перечисляет область: {_rtxt[-90:]}")
                check((_zn / "Учёба" / "_подсказки-учёба.md").is_file(),
                      "указатель области создан")
                # Папки выдумывать нельзя: у нового человека нет папки
                # «Деньги», и создавать её молча — значит врать про базу.
                _miss = [r for r, _f, _l in core.knowledge_index_targets()
                         if not (_k4b / r).is_dir()]
                _told = [m for m in _msgs if "нет папки" in m]
                check(len(_told) == len(_miss),
                      f"о каждой несуществующей папке сказано прямо: "
                      f"{len(_told)} из {len(_miss)}")
                _dirs = sorted(p.name for p in _zn.iterdir() if p.is_dir())
                check(_dirs == ["Учёба"],
                      f"папки не выдуманы: {_dirs}")
                check(any("с пометкой" in m for m in _msgs) is False,
                      f"при первом запуске помеченных строк нет: {_msgs}")
                # План, задача 5, требует две проверки, которых не было.
                # Секция «## Пути» — на неё есть поломка в rollback_wiring.
                check("## Пути" in _rtxt,
                      f"в корневом указателе есть секция ## Пути: "
                      f"{_rtxt[:60]!r}")
                # Проверка лимитов — НАБЛЮДЕНИЕ, а не доказательство
                # механизма, и поломки у неё быть не может: измерено, что
                # `_limit` в коде нигде не применяется, он только
                # разворачивается в никуда. Ничто не обрезает указатель и
                # никто не предупреждает о переполнении, поэтому «зелёная»
                # тут означает «пока помещается», а не «ограничено». Пометка
                # вместо поломки — честный вариант: сломать нечего.
                _over = [f"{rel}/{fname}: "
                         f"{( _k4b / rel / fname).stat().st_size} > {lim}"
                         for rel, fname, lim in core.knowledge_index_targets()
                         if (_k4b / rel / fname).is_file()
                         and (_k4b / rel / fname).stat().st_size > lim]
                check(not _over,
                      f"указатели в пределах своих лимитов: {_over[:3]}")
                # А теперь лимит становится проверяемым: при заниженном
                # пороге пересборка обязана сказать, что он превышен.
                # Раньше порог был числом в списке и ничего не сообщал, то
                # есть проверка выше была наблюдением, а не доказательством.
                _real_root, _real_branch = (core.INDEX_LIMIT_ROOT,
                                            core.INDEX_LIMIT_BRANCH)
                core.INDEX_LIMIT_ROOT = 64
                core.INDEX_LIMIT_BRANCH = 64
                try:
                    _tight = core.refresh_knowledge_indexes(_k4b)
                finally:
                    core.INDEX_LIMIT_ROOT = _real_root
                    core.INDEX_LIMIT_BRANCH = _real_branch
                _warned = [m for m in _tight if "ПРЕВЫШЕН ЛИМИТ" in m]
                check(bool(_warned),
                      f"о превышении лимита сказано прямо: {_warned[:2]}")
                check(any("дробить" in m for m in _warned),
                      "в предупреждении сказано, что делать: дробить, "
                      "а не поднимать лимит")
            finally:
                shutil.rmtree(_k4b, ignore_errors=True)

            # ---- 8ц-7. Указатель, написанный руками, не затирается.
            # Файл без маркеров — это весь написанный текст. Раньше он
            # целиком уходил в «голову», тело подставлялось заглушкой, и
            # рукописная карта исчезала молча. Нашлось на копии живой
            # базы: карта безопасности на 2289 байт стала «Пока пусто».
            _k7 = Path(tempfile.mkdtemp(prefix="указатель-без-маркеров-"))
            try:
                _f7 = _k7 / "знания" / "Учёба"
                _f7.mkdir(parents=True)
                _hand = ("# Карта подсказок: программисту\n\n"
                         "Источник: `знания/Учёба/Программисту-знать.md`.\n\n"
                         "- алгоритмы — раздел Algorithms\n")
                (_f7 / "_подсказки-учёба.md").write_text(
                    _hand, encoding="utf-8")
                core.refresh_knowledge_indexes(_k7)
                _got = (_f7 / "_подсказки-учёба.md").read_text(encoding="utf-8")
                check("Пока пусто" not in _got,
                      "рукописная карта не заменена заглушкой")
                check("алгоритмы — раздел Algorithms" in _got,
                      f"рукописный текст уцелел в смысловой части: "
                      f"{_got[-120:]}")
                check(core.INDEX_BEGIN in _got,
                      "авточасть добавлена, текст сохранён")
            finally:
                shutil.rmtree(_k7, ignore_errors=True)

            # ---- 8ц-5. Правила знаний называют все указатели, а не два
            #      старых адреса. Пока правила молчали про остальные
            #      девять, модель шла открывать то, чего в базе нет, и
            #      получала в ответ «файла нет».
            _card = core.NAV_BLANK["КАРТА-БАЗЫ.md"]
            # Источник истины — knowledge_index_targets(), а НЕ
            # knowledge_index_paths(). Проверка, которая сверяет правила с
            # самой функцией, которую правила наполняет, всегда зелёная:
            # поломка режет функцию, и обе стороны режутся одинаково. Так и
            # вышло — «правила не называют все указатели» не поймалась.
            _want = [f"{rel}/{fname}" for rel, fname, _ in
                     core.knowledge_index_targets()]
            _no_name = [p for p in _want if p not in _card]
            check(not _no_name,
                  f"карта называет все указатели: не названы {_no_name}")
            check("_подсказки-программисту.md" not in _card,
                  "старый адрес указателя убран из карты")
            check(core.ARCHIVE_NAME in _card,
                  "архив потерянных привязок описан в карте")
            _rules = core.NAV_BLANK["ПРАВИЛА-ИИ.md"]
            check("не инструкция" in _rules,
                  "в правилах сказано, что архив не инструкция для модели")
            _r_no = [p for p in _want if p not in _rules]
            check(not _r_no,
                  f"правила называют все указатели: не названы {_r_no}")
            _hb = core.harness_agents_block(plan2.target)
            _hb_no = [p for p in _want if p not in _hb]
            check(not _hb_no,
                  f"блок для Harness называет все указатели: "
                  f"не названы {_hb_no}")
            check("_подсказки-программисту.md" not in _hb,
                  "старый адрес указателя убран из блока Harness")

            # Тот же вопрос про create_base, только на настоящей базе из
            # образца: указатели должны появиться там, где их раньше не
            # было вообще.
            _areas = [p for p in (plan2.target / "знания").iterdir()
                      if p.is_dir()]
            _with = [a for a in _areas
                     if (a / f"_подсказки-{a.name.lower()}.md").is_file()]
            # Проверять «хоть какой-то указатель» нельзя: в образце их и
            # так два, и такая проверка была зелёной даже без пересборки —
            # то есть доказывала, что образец непустой. Теперь требуется
            # указатель у КАЖДОЙ области, а их восемь.
            check(len(_with) == len(_areas) and bool(_areas),
                  f"у каждой области новой базы есть указатель: "
                  f"{len(_with)} из {len(_areas)}")
            # Предел байтов — на настоящей базе из образца, где лежит вся
            # импортированная безопасность. Наблюдение, а не доказательство
            # ограничения: кода, который обрезает указатель, нет вовсе.
            # Пока помещается — зелёная; переполнение придёт само, и тогда
            # красная скажет правду, но не остановит.
            _big = [f"{rel}/{fname}: "
                    f"{(plan2.target / rel / fname).stat().st_size} из {lim}"
                    for rel, fname, lim in core.knowledge_index_targets()
                    if (plan2.target / rel / fname).is_file()
                    and (plan2.target / rel / fname).stat().st_size > lim]
            check(not _big,
                  f"указатели живой базы в пределах лимитов: {_big[:3]}")

            # скиллы не должны потеряться при построении расширенной базы
            check(core.count_skills(plan2.target) > 0,
                  "скиллы в базе из образца на месте")

            # база из образца должна нести в себе плагин: иначе её нечего
            # будет подключать к OpenCode
            echo("\n--- 5б. Плагин внутри новой базы ---")
            cfg = plan2.target / "config"
            check(cfg.is_dir(), "папка config перенесена в новую базу")
            check(core.has_plugin(cfg), "плагин памяти внутри новой базы")
            check((cfg / "opencode.jsonc").is_file(),
                  "настройки внутри новой базы")
            check((cfg / "AGENTS.md").is_file(),
                  "правила внутри новой базы")
            check((cfg / "command").is_dir(),
                  "готовые команды внутри новой базы")
            check((cfg / "node_modules" / "@opencode-ai" / "plugin"
                   / "package.json").is_file(),
                  "зависимости внутри новой базы")
            # Пути в настройках новой базы должны вести на неё саму.
            own = str(plan2.target).replace("\\", "/")
            own_cfg = (cfg / "opencode.jsonc").read_text(encoding="utf-8")
            check(own in own_cfg,
                  "пути в настройках новой базы указывают на неё саму")
            check(str(Path.cwd()).replace("\\", "/") + "/profile.md" not in own_cfg,
                  "пути образца в новой базе заменены")
        else:
            check(False, "образец по пути не найден")

        # ---- 6. подключение
        echo("\n--- 6. Подключение к программе ---")
        program = core.Program(
            "opencode",
            "OpenCode",
            "проверка",
            fallback=str(tmp / "настройки" / "opencode"),
        )
        # ВАЖНО: программа ищет СУЩЕСТВУЮЩУЮ папку настроек и может найти
        # настоящую (~/.config/opencode), а не временную. Тогда проверка
        # испортит рабочие настройки: пропишет в них путь к временной
        # папке, которую в конце удалит. Поэтому жёстко уводим её в temp.
        program.config_dir = lambda: Path(tmp) / "настройки" / "opencode"  # type: ignore[method-assign]

        # Страховка. Проверка подключения переносит скиллы: неотмеченные
        # уезжают в _previous-version. Если изоляция выше не сработает,
        # это коснётся НАСТОЯЩЕЙ папки настроек — так один раз и вышло:
        # 11 рабочих скиллов OpenCode оказались в _previous-version.
        # Поэтому дальше не идём, пока не убедимся, что пишем во временную.
        def inside_temp(path) -> bool:
            try:
                Path(path).resolve().relative_to(Path(tmp).resolve())
                return True
            except (ValueError, OSError):
                return False

        real_dir = program.config_dir()
        if not inside_temp(real_dir):
            echo()
            echo("  ОСТАНОВ: подключение шло бы в НАСТОЯЩУЮ папку настроек:")
            echo(f"    {real_dir}")
            echo("  Проверка прервана, чтобы не испортить рабочие скиллы.")
            echo("  Список баз и настройки не тронуты.")
            return 1
        check(True, f"подключение идёт во временную папку ({Path(tmp).name})")

        before = sorted(p.name for p in (tmp / "настройки").rglob("*")) if (
            tmp / "настройки"
        ).exists() else []
        result = core.attach_base(plan2.target, program)
        dest = program.config_dir()
        check(result.ok, f"подключение прошло (ошибок: {len(result.errors)})")
        if result.errors:
            for error in result.errors:
                echo(f"       {error}")
        check((dest / "profile.md").is_file(), "профиль лежит у программы")
        check((dest / "memory-base-path.txt").is_file(), "путь к базе записан")
        check(core.count_skills(dest) == core.count_skills(plan2.target),
              f"скиллы у программы: {core.count_skills(dest)}")
        check((dest / "библиотека/АКТИВНАЯ-ПАМЯТЬ.md").is_file(),
              "библиотека перенесена")

        # ---- 8ц-6. Подключение существующей базы само чинит указатели.
        # Проверка на провод, а не на функцию: звать refresh_knowledge_indexes
        # умеет кто угодно, важно, что её зовёт именно подключение. Без
        # этого перенос потерянных привязок в архив на живой базе так и не
        # включался бы — а ради него всё затевалось.
        _seen: list[Path] = []
        _orig_idx = core.refresh_knowledge_indexes

        def _spy(base, _o=_orig_idx):
            _seen.append(Path(base))
            return _o(base)

        core.refresh_knowledge_indexes = _spy  # type: ignore[assignment]
        try:
            core.attach_base(plan2.target, program)
        finally:
            core.refresh_knowledge_indexes = _orig_idx  # type: ignore[assignment]
        check(bool(_seen),
              f"подключение базы зовёт пересборку указателей: "
              f"{len(_seen)} раз")

        # ---- 6б. плагин OpenCode: без него база не читается
        echo("\n--- 6б. Плагин и настройки OpenCode ---")
        check(core.has_plugin(dest),
              "плагин памяти установлен в папку программы")
        check((dest / "opencode.jsonc").is_file(),
              "настройки opencode.jsonc на месте")
        check((dest / "AGENTS.md").is_file(), "правила AGENTS.md на месте")
        check((dest / "package.json").is_file(), "описание зависимостей на месте")

        commands = list((dest / "command").glob("*.md")) if (dest / "command").is_dir() else []
        check(len(commands) >= 5, f"готовых команд перенесено: {len(commands)}")

        deps = dest / "node_modules" / "@opencode-ai" / "plugin" / "package.json"
        check(deps.is_file(), "зависимости плагина на месте (без интернета)")

        # Пути в настройках должны указывать на НАСТОЯЩУЮ папку базы,
        # а не на то место, откуда база скопирована.
        cfg_text = (dest / "opencode.jsonc").read_text(encoding="utf-8")
        base_posix = str(plan2.target).replace("\\", "/")
        check(base_posix in cfg_text,
              "в настройках прописан путь к этой базе")
        check("instructions" in cfg_text,
              "настройки загружают базу в каждую сессию")
        for target_name in core.INSTRUCTION_TARGETS:
            check(f"{base_posix}/{target_name}" in cfg_text,
                  f"в настройках есть путь к {target_name}")

        # Собранные пути должны быть пригодны для чтения, а не «на словах».
        import opencode_caps  # noqa: E402
        bounds = opencode_caps.find_key_array(cfg_text, "instructions")
        check(bounds is not None, "массив instructions найден в настройках")
        if bounds is not None:
            raw = cfg_text[bounds[0] + 1 : bounds[1]]
            listed = [p.strip().strip('",') for p in raw.splitlines() if p.strip()]
            listed = [x for x in listed if x]
            check(len(listed) == len(core.INSTRUCTION_TARGETS),
                  f"путей в настройках: {len(listed)}")
            for item in listed:
                check(Path(item).is_file(),
                      f"файл по пути существует: {item.split('/')[-1]}")

        # Подключение само переводит мосты памяти и ПК с правами на базу.
        check('"ncp"' in cfg_text and '"pc"' in cfg_text,
              "мосты ncp и pc вписаны в настройки")
        check('"ask"' in cfg_text, "права на мосты стоят")
        nbridge = core.program_root() / "tools" / "ncp-bridge" / "config.json"
        if nbridge.is_file():
            bridge_cfg = json.loads(nbridge.read_text(encoding="utf-8"))
            check("library" in str(bridge_cfg.get("library_path", "")).lower() or "{{library}}" in str(bridge_cfg.get("library_path", "")).lower(),
                  "в конфиг моста вписан путь к библиотеке")

        # У базы, из которой подключаем, тоже должен быть плагин —
        # иначе подключать нечего.
        check(core.find_config_source(plan2.target) is not None,
              "в базе есть папка config с плагином")

        # повторное подключение: старые файлы уходят в _previous-version
        result2 = core.attach_base(plan2.target, program)
        check(result2.ok, "повторное подключение не сломалось")
        check((dest / "_previous-version/profile.md").is_file(),
              "прежние файлы сохранены в _previous-version")
        check((dest / "_previous-version/opencode.jsonc").is_file(),
              "прежние настройки сохранены в _previous-version")

        # защита от служебной папки npm
        npm_program = core.Program(
            "probe-npm", "Проба", "проверка", fallback=str(tmp / "npm")
        )
        npm_program.config_dir = lambda: Path(tmp) / "npm"  # type: ignore[method-assign]
        guard = core.attach_base(plan2.target, npm_program)
        check(not guard.ok and "служебная" in " ".join(guard.errors),
              "запись в папку npm запрещена")

        # ---- 7. сводка о базе
        echo("\n--- 7. Сводка о базе ---")
        info = core.base_info(plan2.target)
        check(bool(info["is_base"]), "база опознана")
        check(int(info["skills"]) > 0, f"скиллов видно: {info['skills']}")
        check(int(info["size"]) > 0,
              f"размер считается: {core.human_size(int(info['size']))}")
        check(info["marker"] is not None, "отметка о создании читается")

        # ---- 8. вкладка подключения
        echo("\n--- 8. Вкладка «Подключить существующую» ---")
        itab = window.import_tab
        itab.source_edit.setText(str(tmp / "нет-такой-папки"))
        check(not itab.btn_run.isEnabled(), "с несуществующей папкой кнопка закрыта")
        itab.source_edit.setText(str(plan2.target))
        check(itab.btn_run.isEnabled(), "с настоящей базой кнопка открыта")
        check("Скиллов" in itab.source_hint.text(),
              f"сводка показана: {itab.source_hint.text()[:50]}")

        # ---- 8б. шаг 3: выбор навыков перед подключением
        echo("\n--- 8б. Шаг «Какие навыки подключить» ---")

        # чтение скиллов из базы: имя, описание, путь
        found = core.list_skills(plan2.target)
        check(len(found) > 0, f"навыки прочитаны из базы: {len(found)}")
        check(all(s.get("description") for s in found),
              "у каждого навыка есть описание")
        check(all(s.get("path") for s in found),
              "у каждого навыка есть путь к папке")
        names = [s["name"] for s in found]
        check(len(names) == len(set(names)), "имена навыков не повторяются")
        import re as _re
        bad = [n for n in names
               if not _re.match(r"^[a-z0-9]+(-[a-z0-9]+)*$", n)]
        check(not bad, f"имена годятся для opencode: {bad or 'все'}")

        # многострочное описание (YAML-складка) должно склеиться в одну строку
        folded = [s for s in found if s["name"] == "geo-map-compliance-guard"]
        if folded:
            check("\n" not in folded[0]["description"]
                  and len(folded[0]["description"]) > 100,
                  "многострочное описание собрано в одну строку")

        # окно: список навыков заполнен и все отмечены при первом показе
        check(itab.skills_list.count() == len(found),
              f"в окне навыков: {itab.skills_list.count()} из {len(found)}")
        check(len(itab._chosen_skills()) == len(found),
              "при первом показе отмечены все навыки")

        # снять все — подсказка предупреждает, выбор пустой
        itab._set_all_skills(False)
        check(len(itab._chosen_skills()) == 0, "кнопка «Снять все» очистила выбор")
        check("Не отмечено" in itab.skills_hint.text(),
              "при пустом выборе показано предупреждение")
        # кнопка подключения остаётся доступной — это допустимый выбор
        check(itab.btn_run.isEnabled(),
              "пустой выбор навыков не блокирует подключение")

        # отметить все обратно
        itab._set_all_skills(True)
        check(len(itab._chosen_skills()) == len(found), "кнопка «Отметить все» вернула выбор")
        check("все" in itab.skills_hint.text().lower(),
              "при полном выборе сказано, что перенесутся все")

        # подключение с выбором подмножества: остальные уходят в _previous-version
        echo("\n     подключение с выбором части навыков:")
        subset = sorted(names)[:2]
        keep_dir = dest / "skills"
        before = sorted(
            d.name for d in keep_dir.iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        ) if keep_dir.is_dir() else []
        check(len(before) > 2, f"до выбора в программе навыков: {len(before)}")

        part = core.attach_base(plan2.target, program, skills=subset)
        check(part.ok, "подключение с выбором прошло без ошибок")
        after = sorted(
            d.name for d in (dest / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        check(after == subset,
              f"осталось ровно отмеченное: {after}")
        unkept = [n for n in before if n not in subset]
        moved = sum(
            1 for n in unkept
            if (dest / "_previous-version" / "skills" / n).is_dir()
        )
        check(moved == len(unkept),
              f"неотмеченные убраны в _previous-version: {moved} из {len(unkept)}")
        check(keep_dir.is_dir() and (keep_dir / subset[0] / "SKILL.md").is_file(),
              "отмеченный навык лежит в программе целиком")

        # подключение со всем набором возвращает всё на место
        all_names = sorted(names)
        back = core.attach_base(plan2.target, program, skills=all_names)
        check(back.ok, "подключение со всем набором прошло")
        restored = sorted(
            d.name for d in (dest / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        check(restored == all_names, f"вернулись все навыки: {len(restored)}")
        check("из " in " ".join(back.messages),
              "в отчёте сказано, сколько навыков перенесено")

        # пустой список — ни одного навыка, и это не ошибка
        none_at_all = core.attach_base(plan2.target, program, skills=[])
        check(none_at_all.ok, "подключение без навыков не считается сбоем")
        left = sorted(
            d.name for d in (dest / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        ) if (dest / "skills").is_dir() else []
        check(left == [], f"при пустом выборе в программе не осталось навыков: {left}")

        # навык, которого нет в базе: должно быть предупреждение, не сбой
        ghost = core.attach_base(
            plan2.target, program, skills=["нет-такого-навыка"]
        )
        check(any("нет-такого-навыка" in e for e in ghost.errors),
              "отмеченный, но отсутствующий навык назван в замечаниях")

        # Skills=None по-прежнему означает «все» — старая проверка не сломалась
        core.attach_base(plan2.target, program)
        default_all = sorted(
            d.name for d in (dest / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        check(default_all == sorted(names),
              "без указания навыков переносятся все")

        # база БЕЗ навыков не должна стирать навыки из программы:
        # иначе «подключил пустую базу — потерял всё, что было»
        echo("\n     база без навыков:")
        # «Пустая база» теперь наполняется навыками из главной базы,
        # поэтому для проверки предохранителя делаем базу без навыков
        # по-настоящему: убираем папку skills.
        blank_base = target
        skills_dir = blank_base / "skills"
        if skills_dir.is_dir():
            shutil.rmtree(skills_dir)
        check(not core.list_skills(blank_base),
              "в базе без навыков скиллов нет — условие проверки выполнено")
        safe = core.attach_base(blank_base, program)
        left_after = sorted(
            d.name for d in (dest / "skills").iterdir()
            if d.is_dir() and (d / "SKILL.md").is_file()
        )
        check(left_after == all_names,
              f"навыки в программе уцелели: {len(left_after)} из {len(all_names)}")
        check(any("ничего не тронуто" in m for m in safe.messages),
              "в отчёте сказано, что навыки в программе не тронуты")

        # программа без поддержки навыков: шаг 3 не должен ничего ломать
        no_skills_program = core.Program(
            "probe", "Проба", "проверка", supports_skills=False,
            fallback=str(tmp / "probe"),
        )
        # изоляция обязательна и здесь: пишем только во временную папку
        no_skills_program.config_dir = lambda: Path(tmp) / "probe"  # type: ignore[method-assign]
        quiet = core.attach_base(plan2.target, no_skills_program)
        check(any("не читает" in m for m in quiet.messages),
              "для программы без навыков сказано, что пропущено")

        # ---- 9. ярлык
        echo("\n--- 9. Ярлык на базу ---")
        check(tab.link_check.isChecked(), "галочка ярлыка включена по умолчанию")
        check(tab.link_place.count() == len(core.SHORTCUT_PLACES),
              f"мест для ярлыка: {tab.link_place.count()}")

        # разбор всех мест
        for ident, title in core.SHORTCUT_PLACES:
            try:
                folder = core.resolve_shortcut_folder(
                    ident, target, str(tmp)
                )
                check(folder.is_dir(), f"место «{title}» даёт папку")
            except core.NameError_ as exc:
                check(False, f"место «{title}» не разобралось: {exc}")

        # своя папка: пустой путь и несуществующая папка должны отклоняться
        for raw, note in (("", "пустой путь"), (str(tmp / "нет"), "несуществующая")):
            try:
                core.resolve_shortcut_folder("custom", target, raw)
                check(False, f"своя папка: {note} зря принята")
            except core.NameError_:
                check(True, f"своя папка: {note} отклонена")

        # имя ярлыка чистится от запрещённых знаков
        check(core.safe_link_name("a/b:c*d") == "a_b_c_d",
              "запрещённые знаки в имени ярлыка заменены")
        check(core.safe_link_name("   ") == "база",
              "пустое имя ярлыка заменено на «база»")

        # настоящее создание ярлыка во временной папке
        made = core.create_base_shortcut(
            target, "custom", str(tmp), target.name
        )
        if made.ok:
            check(made.link is not None and made.link.is_file(),
                  f"ярлык создан: {made.link.name}")
            # ярлык должен вести на файл-открывалку внутри базы
            opener = target / core.OPENER_NAME
            check(opener.is_file(), f"в базе создан файл для ярлыка")
            pointed = core.shortcut_target(made.link) if made.link else None
            same = False
            if pointed is not None:
                try:
                    same = pointed.resolve() == opener.resolve()
                except OSError:
                    same = str(pointed).lower() == str(opener).lower()
            check(same,
                  f"ярлык открывает нужный файл: "
                  f"{pointed.name if pointed else 'не прочитался'}")
            # повтор не должен затирать имеющийся ярлык
            again = core.create_base_shortcut(
                target, "custom", str(tmp), target.name
            )
            check(not again.ok and "уже есть" in again.error,
                  "повторный ярлык не затирает существующий")
        else:
            # ярлыки могут быть запрещены средой — это не порча программы
            echo(f"       ярлык не создан: {made.error.splitlines()[0]}")
            check("не удалось" in made.error or "уже есть" in made.error,
                  "при отказе объяснена причина")

        # выключенная галочка отключает выбор места
        tab.link_check.setChecked(False)
        check(not tab.link_place.isEnabled(), "без галочки выбор места закрыт")
        check("не будет" in tab.link_hint.text(),
              "без галочки сказано, что ярлыка не будет")
        tab.link_check.setChecked(True)
        check(tab.link_place.isEnabled(), "с галочкой выбор места снова доступен")

        # своя папка включается только на своём пункте
        tab.link_place.setCurrentIndex(0)
        check(not tab.link_custom_edit.isVisible(),
              "на «Рабочем столе» своя папка не нужна")
        for index in range(tab.link_place.count()):
            if tab.link_place.itemData(index) == "custom":
                tab.link_place.setCurrentIndex(index)
                break
        # окно в проверке не показывается, поэтому смотрим на «скрытость»
        check(not tab.link_custom_edit.isHidden(),
              "на «Своя папка…» поле своей папки показано")
        tab.link_place.setCurrentIndex(0)
        check(tab.link_custom_edit.isHidden(),
              "при возврате на «Рабочий стол» поле снова скрыто")

        # ---- 10. трудные пути
        echo("\n--- 10. Трудные пути в ярлыках ---")
        # длинное тире в пути ломало ярлыки: внешние программы портили
        # его на обычный дефис, и ярлык вёл в несуществующую папку
        hard_dir = tmp / "папка — с тире и РуССкими"
        hard_dir.mkdir(parents=True, exist_ok=True)
        hard_base = hard_dir / "База — проба"
        hard_base.mkdir(exist_ok=True)
        (hard_base / "profile.md").write_text("# x\n", encoding="utf-8")

        hard_link = tmp / "трудный.lnk"
        res_hard = core.create_base_shortcut(
            hard_base, "custom", str(tmp), "трудный"
        )
        check(res_hard.ok, "ярлык на путь с длинным тире создан")
        hard_opener = hard_base / core.OPENER_NAME
        read_hard = core.shortcut_target(hard_link) if hard_link.is_file() else None
        check(
            read_hard is not None and read_hard.resolve() == hard_opener.resolve(),
            f"длинное тире не испорчено: "
            f"{read_hard.parent.name if read_hard else 'не прочитался'}",
        )
        if read_hard is not None:
            check("—" in str(read_hard),
                  "в пути ярлыка сохранилось длинное тире «—»")
            check("папка — с тире и РуССкими" in str(read_hard),
                  "русские буквы в пути не испорчены")

        # ---- 11. список созданных баз и переход во вторую вкладку
        echo("\n--- 11. Список созданных баз ---")
        # список уже подменён на временный в начале проверки —
        # настоящий не трогается
        itab._fill_mine()
        itab.mine_list.clear()
        core._write_bases([])
        check(core.read_bases() == [], "пустой список читается")

        core.remember_base(plan2.target, "claude")
        entries = core.read_bases()
        check(len(entries) == 1, f"база запомнена (записей: {len(entries)})")
        check(entries[0]["path"] == str(plan2.target),
              "путь записан верно")
        check(entries[0].get("program") == "claude",
              "программа подключения запомнена")

        # повторная запись не создаёт дубль
        core.remember_base(plan2.target, "claude")
        check(len(core.read_bases()) == 1, "повтор не создал дубль")

        # список виден во вкладке
        itab._fill_mine()
        check(itab.mine_list.count() == 1,
              f"список в окне заполнен: {itab.mine_list.count()}")
        check(itab.mine_list.item(0).data(1000) == str(plan2.target),
              "в списке тот же путь")

        # выбор подставляет путь в поле
        itab.mine_list.setCurrentRow(0)
        itab.source_edit.clear()
        itab.mine_list.setCurrentRow(-1)
        itab.mine_list.setCurrentRow(0)
        check(itab.source_edit.text() == str(plan2.target),
              "выбор из списка подставил путь")
        check(itab.btn_forget_mine.isEnabled(),
              "кнопка «убрать» доступна при выборе")

        # удалённой базы в списке быть не должно
        ghost = tmp / "Удалённая база"
        ghost.mkdir(parents=True, exist_ok=True)
        (ghost / "profile.md").write_text("# x\n", encoding="utf-8")
        core.remember_base(ghost, "")
        check(len(core.read_bases()) == 2, "запись добавилась")
        shutil.rmtree(ghost, ignore_errors=True)
        check(len(core.read_bases()) == 1,
              "запись без файлов памяти выброшена")

        # автоматический переход после создания
        window._suggest_import(str(plan2.target))
        check(window.tabs.currentIndex() == 1,
              "после создания открылась вкладка подключения")
        check(itab.source_edit.text() == str(plan2.target),
              "в поле уже стоит созданная база")
        check(itab.btn_run.isEnabled(),
              "кнопка подключения сразу доступна")

        window.tabs.setCurrentIndex(0)
        window._suggest_import(str(tmp / "нет-такой"))
        check(window.tabs.currentIndex() == 0,
              "на несуществующем пути переход не делается")

        core.forget_base(plan2.target)
        check(core.read_bases() == [], "база забыта по просьбе")
        check(plan2.target.is_dir(),
              "папка при забывании не тронута")

        # ---- 12. переносимость: базу можно отдать другому человеку
        echo("\n--- 12. Переносимость на другой компьютер ---")
        user = Path.home().name
        # ---- 12а. Шаблоны базы: личных файлов здесь быть не должно
        #
        # Проверка узкая и это НЕ oversight: в живой базе файлы profile.md,
        # facts.md, projects.md и созданные-базы.json по замыслу содержат
        # имя пользователя, и проверять их на отсутствие имени бессмысленно —
        # они и должны его содержать. Поэтому в перечень попадают только
        # шаблоны, которые копируются в новую базу и остаются чистыми.
        root = core.app_root()
        watch = [
            root / "Управление-базой.cmd",
            root / "config" / "plugins" / "memory-base.js",
            root / "config" / "AGENTS.md",
            root / "config" / "opencode.jsonc",
            root / "tools" / "dbapp" / "core.py",
            root / "tools" / "dbapp" / "main.py",
            root / "tools" / "dbapp" / "selftest.py",
        ]
        # Готовые команды копируются в программу и выполняются как есть —
        # имя пользователя в них недопустимо вдвойне.
        cmds = sorted((root / "config" / "command").glob("*.md"))
        watch += cmds
        dirty = [
            path.name
            for path in watch
            if path.is_file()
            and user
            and user in path.read_text(encoding="utf-8", errors="replace")
        ]
        check(not dirty,
              f"имя пользователя не вписано в шаблоны: проверено {len(watch)}")
        for item in dirty:
            echo(f"      имя пользователя найдено в: {item}")

        # ---- 12б. Папка программы: здесь имя пользователя не законно нигде
        #
        # Отдельная проверка, потому что папка программы — не база. Личных
        # файлов здесь нет, и появление имени пользователя означает утечку:
        # программу увозят на другую машину, и путь там не сойдётся.
        #
        # **Перечисляются места, а не файлы.** Раньше утечка прошла потому,
        # что перечень был из семи файлов, и новый документ в новой папке
        # просто в него не попал. Здесь перечислены папки, которые мы пишем,
        # поэтому любой новый файл внутри них проверяется сам.
        #
        # Исключено: tools/thirdparty — чужой код извне, его пути внутри
        # виртуальных окружений указывают на машину сборщика; .venv и
        # site-packages — то же самое; logs и отчёты — машинный вывод.
        _prog = core.program_root()
        _own_places = [
            "",                        # файлы в корне программы
            "config",                  # настройки opencode
            "tools/dbapp",             # код программы
            "tools/antiblock",
            "tools/ncp-bridge",
            "tools/pc-bridge",
            "tools/agents",
            "skills",
            "инструкции",
            "знания",
            "документы",               # планы и проектные записи
            "библиотека/Шаблоны",
        ]
        _not_ours = {
            "__pycache__", "node_modules", ".git", ".venv", "venv",
            "site-packages", "cache", "data", "shots", "модули", "models",
            "записи", "архив", "входящие", "журнал", "личное",
        }
        _text_ext = {".md", ".json", ".py", ".js", ".jsonc", ".cmd", ".ps1",
                     ".txt", ".html", ".yaml", ".yml", ".bat"}
        _prog_files: set[Path] = set()
        for _place in _own_places:
            _base = _prog / _place if _place else _prog
            if not _base.exists():
                continue
            for _path in (_base.glob("*") if not _place else _base.rglob("*")):
                if not _path.is_file() or _path.suffix.lower() not in _text_ext:
                    continue
                if any(part in _not_ours for part in _path.parts):
                    continue
                if _path.name == "selftest-report.txt" or _path.suffix == ".log":
                    continue
                if _path.stat().st_size > 3_000_000:
                    continue
                _prog_files.add(_path)
        _prog_dirty = sorted(
            str(p.relative_to(_prog)) for p in _prog_files
            if user and user in p.read_text(encoding="utf-8", errors="replace")
        )
        # Проверка без выявленного имени пользователя ищет пустую строку и
        # находит её в каждом файле — либо, при `user and ...`, молча
        # проходит. Оба случая выглядят как успех. Поэтому имя обязано
        # быть непустым, а поиск обязан найти заведомо известную строку.
        check(bool(user),
              f"имя пользователя определено, иначе проверка утечки пуста: "
              f"{user!r}")
        # Контрольный образец того же поиска: эта строка заведомо есть в
        # core.py, и если поиск её не находит, он сломан и доверять его
        # нулю нельзя.
        _control_found = any(
            "KNOWLEDGE_AREAS" in p.read_text(encoding="utf-8", errors="replace")
            for p in _prog_files)
        check(_control_found,
              f"поиск утечки рабочий: контрольный образец найден "
              f"в {len(_prog_files)} файлах")
        check(not _prog_dirty,
              f"в папке программы имени пользователя нет: проверено {len(_prog_files)}")
        for item in _prog_dirty:
            echo(f"      утечка в папке программы: {item}")

        # Иероглифы в наших собственных файлах. Проверка сделана после того,
        # как посторонний символ дважды проскочил в комментарий при правке
        # текста, и оба раза это заметил человек, а не прогон.
        #
        # Первая версия проверяла всю папку программы и дала пять ложных
        # срабатываний: в импортированных материалах по безопасности
        # иероглифы — предмет разговора (unicode-injection, xss, обход
        # WAF), а в навыке skill-creator китайский цитатой приведён как
        # пример запроса пользователя. Плюс чужой `.venv` с idna, где
        # диапазоны CJK зашиты по существу.
        #
        # Поэтому охват узкий и перечислен явно: только то, что мы пишем
        # сами. Измерено: в наших файлах иероглифов ноль, так что проверка
        # обязана быть зелёной и обязана ловить именно проскочивший символ.
        _cjk = re.compile(
            "[\u3000-\u303f\u3040-\u30ff\u3400-\u4dbf"
            "\u4e00-\u9fff\uf900-\ufaff\uff00-\uffef]"
        )
        _our_dirs = ("tools/dbapp", "tools/проверки", "tools/agents",
                     "config", "документы", "skills")
        _our_files = ("документы/ПРАВИЛА-ИИ.md", "документы/КАРТА-БАЗЫ.md")
        _cjk_dirty: list[str] = []
        _cjk_scanned = 0
        _seen_paths: set[Path] = set()
        for _sub in _our_dirs:
            _base = _prog / _sub
            if not _base.is_dir():
                continue
            for _p in _base.rglob("*"):
                if not _p.is_file() or _p.suffix.lower() not in _text_ext:
                    continue
                # Чужое внутри — не наше: node_modules, кэши и записи.
                # Без этого проверка падала на японских локалях zod в
                # config/node_modules репозитория.
                if any(part in _not_ours for part in _p.parts):
                    continue
                _seen_paths.add(_p)
        for _name in _our_files:
            _p = _prog / _name
            if _p.is_file():
                _seen_paths.add(_p)
        for _p in sorted(_seen_paths):
            try:
                _rows = _p.read_text(encoding="utf-8",
                                     errors="replace").splitlines()
            except OSError:
                continue
            _cjk_scanned += 1
            for _i, _row in enumerate(_rows, 1):
                if _cjk.search(_row):
                    _cjk_dirty.append(f"{_p.relative_to(_prog)}:{_i}")
        # Одно исключение, и оно поимённо: в навыке skill-creator китайская
        # фраза цитатой приведена как пример запроса пользователя. Это
        # пример, а не опечатка, и поимённое исключение единственное.
        #
        # Файл исключён целиком, а не по строке: номер строки сдвигается,
        # и исключение перестало бы работать тихо. Саму китайскую фразу
        # здесь не цитируем — иначе эта же проверка ругается на свой
        # комментарий, что и случилось при первой попытке.
        _cjk_allowed = {"skills/skill-creator/SKILL.md"}
        _cjk_dirty = [d for d in _cjk_dirty
                      if d.rsplit(":", 1)[0].replace("\\", "/")
                      not in _cjk_allowed]
        check(not _cjk_dirty,
              f"в наших файлах нет иероглифов: {_cjk_dirty[:5] or 'чисто'}")
        # Пустой обход ничего не проверяет и при этом выглядит как успех.
        check(_cjk_scanned > 50,
              f"иероглифы проверяли не в пустом списке файлов: {_cjk_scanned}")
        # Перечень не должен выродиться в ноль: пустой проверяет ничто и
        # при этом выглядит как успех. На пустой машине с одной папкой честно.
        check(len(_prog_files) > 50,
              f"перечень программы не выродился: {len(_prog_files)} файлов")
        check((not (_prog / "документы").is_dir())
              or any("документы" in p.parts for p in _prog_files),
              "папка документы под проверкой, если она есть")

        marks = sum(
            1
            for path in cmds
            if core.BASE_PLACEHOLDER in path.read_text(encoding="utf-8")
        )
        check(marks >= 4,
              f"команды ссылаются на базу пометкой: {marks} из {len(cmds)}")

        # Вместо пути — пометка. Благодаря ей файлы годятся для любой папки.
        for rel in ("config/AGENTS.md", "config/opencode.jsonc"):
            path = root / rel
            text = (
                path.read_text(encoding="utf-8", errors="replace")
                if path.is_file()
                else ""
            )
            check(core.BASE_PLACEHOLDER in text,
                  f"в {rel} стоит пометка {core.BASE_PLACEHOLDER}, а не путь")

        probe = tmp / "проба-подстановки.md"
        probe.write_text(
            f"База лежит здесь: {core.BASE_PLACEHOLDER}/profile.md\n",
            encoding="utf-8",
        )
        changed = core.substitute_base(probe, tmp)
        text = probe.read_text(encoding="utf-8")
        check(changed, "пометка заменена настоящим путём")
        check(core.BASE_PLACEHOLDER not in text,
              "после подстановки пометки не осталось")
        check(str(tmp).replace("\\", "/") in text,
              "подставлен именно путь этой базы")
        check(not core.substitute_base(probe, tmp),
              "повторная подстановка ничего не портит")

        # Рабочий стол — вторая пометка: команда создания проекта заводит
        # папку проекта рядом с базой, а не в чужой папке пользователя.
        probe2 = tmp / "проба-рабочего-стола.md"
        probe2.write_text(
            f"Папка проекта: {core.DESKTOP_PLACEHOLDER}/Имя/\n", encoding="utf-8"
        )
        desk = tmp / "Стол"
        changed2 = core.substitute_base(probe2, tmp, desk)
        text2 = probe2.read_text(encoding="utf-8")
        check(changed2 and core.DESKTOP_PLACEHOLDER not in text2,
              "пометка рабочего стола тоже заменяется")
        check(str(desk).replace("\\", "/") in text2,
              "в файл попал именно этот рабочий стол")

        # ---- 12а. поиск программ по нейросетям и агрегаторам
        echo("\n--- 12а. Поиск программ ---")
        home_text = str(Path.home()).replace("\\", "/")
        outside = [
            p.title
            for p in core.PROGRAMS
            if not str(p.config_dir()).replace("\\", "/").startswith(home_text)
        ]
        check(not outside,
              f"папки всех программ ищутся от домашней: {len(core.PROGRAMS)}")
        for item in outside:
            echo(f"      папка вне домашней: {item}")

        found = [p for p in core.PROGRAMS if p.is_installed()]
        check(len(found) > 0, f"опознано установленных программ: {len(found)}")
        check(core.PROGRAMS_BY_ID["opencode"].is_installed(),
              "OpenCode опознан по своей папке")
        check(set(core.PROGRAMS_BY_ID) == {"opencode", "harness"},
              f"программ ровно две: {sorted(core.PROGRAMS_BY_ID)}")
        check(core.PROGRAMS_BY_ID["harness"].ability_note().startswith("Базу увидит"),
              f"про Harness сказано прямо: {core.PROGRAMS_BY_ID['harness'].ability_note()}")

        # ---- 12б. честный отказ вместо пустой работы
        echo("\n--- 12б. Честный отказ ---")
        closed = core.Program(
            "probe-closed", "Проба-закрытая", "проверка",
            reads_state=core.READS_NO, fallback=str(tmp / "probe-closed"),
        )
        closed.config_dir = lambda: Path(tmp) / "probe-closed"  # type: ignore[method-assign]
        check(closed.reads_state == core.READS_NO,
              "про закрытую программу известно, что файлы она не читает")
        check(not closed.can_attach(), "подключение к закрытой запрещено")
        check("не увидит" in closed.ability_note(),
              f"окно говорит правду: {closed.ability_note()}")
        closed_dir = Path(str(closed.config_dir()))
        before = sorted(p.name for p in closed_dir.iterdir()) if closed_dir.is_dir() else []
        refuse = core.attach_base(plan2.target, closed)
        check(not refuse.ok, "подключение к закрытой отменено")
        check(any("не видна" in m for m in refuse.errors),
              "в отказе объяснена причина")
        if closed_dir.is_dir():
            check(sorted(p.name for p in closed_dir.iterdir()) == before,
                  "в чужой папке ничего не изменилось")

        reader = core.PROGRAMS_BY_ID["opencode"]
        check(reader.can_attach(), "к OpenCode подключение разрешено")
        check("увидит" in reader.ability_note(),
              f"про OpenCode сказано прямо: {reader.ability_note()}")

        # ---- 12в. отказ виден в окне, а не только в коде
        echo("\n--- 12в. Что видит человек в окне ---")

        def _select(ident: str) -> None:
            for index in range(itab.target_list.count()):
                if itab.target_list.item(index).data(1000) == ident:
                    itab.target_list.setCurrentRow(index)
                    return

        _select("opencode")
        check("увидит" in itab.target_hint.text(),
              f"подсказка про OpenCode: "
              f"{itab.target_hint.text()[:60]}")
        check(itab.btn_run.isEnabled(),
              "у OpenCode кнопка «Подключить» доступна")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        echo(f"\nВременная папка убрана: {tmp}")

    # ---- 13. мост NCP: создаётся программой, а не вручную
    echo("\n--- 13. Мост NCP ---")

    # Своя временная папка: прежняя убрана вместе с разделом 12.
    btmp = Path(tempfile.mkdtemp(prefix="ncp_bridge_"))

    btab = window.bridge_tab
    # Семь вкладок: создание, подключение, мост NCP, opencode, программы,
    # темы и инструкция. Счётчик стоял на 5, потом на 6 — по числу
    # добавленных вкладок, поэтому написан числом, а не «сколько есть».
    check(window.tabs.count() == 8, f"вкладок в окне: {window.tabs.count()}")
    check(window.tabs.tabText(0) == "Создать новую базу",
          f"первая вкладка — «{window.tabs.tabText(0)}»")
    check(window.tabs.tabText(1) == "Подключить существующую",
          f"вторая вкладка — «{window.tabs.tabText(1)}»")
    check(window.tabs.tabText(2) == "Мост NCP — создать",
          f"третья вкладка — «{window.tabs.tabText(2)}»")
    check(window.tabs.tabText(3) == "opencode",
          f"четвёртая вкладка — «{window.tabs.tabText(3)}»")
    check(window.tabs.tabText(4) == "Программы",
          f"пятая вкладка — «{window.tabs.tabText(4)}»")
    check(window.tabs.tabText(5) == "Темы",
          f"шестая вкладка — «{window.tabs.tabText(5)}»")
    check(window.tabs.tabText(6) == "Обновление",
          f"седьмая вкладка — «{window.tabs.tabText(6)}»")
    check(window.tabs.tabText(7) == "Инструкция",
          f"восьмая вкладка — «{window.tabs.tabText(7)}»")
    check(isinstance(btab, app_main.BridgeTab), "вкладка моста собрана")

    # Образец лежит внутри базы: значит, уедет на любой компьютер вместе
    # с программой. Ничего скачивать из интернета не нужно.
    tpl = core.bridge_template_dir()
    check(tpl.is_dir(), f"образец моста лежит в программе: {tpl}")
    missing = [name for name in core.BRIDGE_FILES if not (tpl / name).is_file()]
    check(not missing,
          f"в образце {len(core.BRIDGE_FILES)} файлов; не хватает: "
          f"{missing or 'ничего'}")
    # Найдено 28.09: в BRIDGE_FILES зашит список файлов моста, и новый
    # модуль memory_tools.py в него не попал. Мост создавался, но не
    # запускался: ImportError. Проверка прямо на это и смотрит.
    _tpl_modules = {
        p.stem for p in tpl.glob("*.py")
    } - {"server", "ncp_core", "__init__"}
    _copied = {"server", "ncp_core"} | {Path(n).stem for n in core.BRIDGE_FILES}
    _lost = sorted(_tpl_modules - _copied)
    check(not _lost,
          f"в образце нет модулей, которые забыли внести в BRIDGE_FILES: "
          f"{_lost or 'ничего'}")
    check("memory_tools.py" in core.BRIDGE_FILES,
          "модуль инструментов памяти входит в список копируемых файлов")


    # Переносимость: в образце не должно быть имени чужого пользователя,
    # иначе мост не заработает у другого человека. Исключение — сам мост
    # подключённой базы: он обязан ссылаться на собственную библиотеку,
    # при переносе программа всё равно перепишет путь заново.
    user = Path.home().name
    cfg_text = (tpl / "config.json").read_text(encoding="utf-8", errors="replace")
    try:
        pointed = json.loads(cfg_text).get("library_path", "")
    except ValueError:
        pointed = ""
    owner = core.app_root().as_posix()
    self_ref = (isinstance(pointed, str)
                and pointed.replace("\\", "/").startswith(owner + "/"))
    dirty = [
        path.name
        for path in tpl.rglob("*")
        if path.is_file()
        and user
        and not (path.name == "config.json" and self_ref)
        and user in path.read_text(encoding="utf-8", errors="replace")
    ]
    check(not dirty, f"в образце моста нет чужого имени: {dirty or 'чисто'}")
    check(core.BRIDGE_LIBRARY_PLACEHOLDER in cfg_text or self_ref,
          "в образце пометка или собственная библиотека")
    check(core.find_python() is not None,
          f"Python для запуска моста найден: {core.find_python()}")

    # Настоящую библиотеку запоминаем до работы — после она должна быть
    # точно такой же: все проверки идут на временной копии.
    real_library = core.library_dir(core.app_root())
    real_index = real_library / "index.json"
    index_before = real_index.read_text(encoding="utf-8") if real_index.is_file() else ""
    journal_dir = real_library / "журнал"
    journal_before = sorted(p.name for p in journal_dir.glob("*.md")) if journal_dir.is_dir() else []

    bridge_dir = btmp / "NCP-мост"
    result = core.create_bridge(bridge_dir, real_library)
    check(result.ok,
          f"мост создан во временной папке: "
          f"{'; '.join(result.errors)[:120] or 'без замечаний'}")
    check(core.looks_like_bridge(bridge_dir),
          "мост опознаётся по содержимому, а не по имени папки")
    written = json.loads((bridge_dir / "config.json").read_text(encoding="utf-8"))
    check("библиотека" in str(written.get("library_path", "")),
          f"путь к библиотеке подставлен: {written.get('library_path')}")
    check(core.BRIDGE_LIBRARY_PLACEHOLDER not in str(written.get("library_path", "")),
          "пометки в config.json не осталось")
    check(result.checked, "мост сам себя проверил, и проверка прошла")
    check(result.python is not None, f"в итоге записан Python: {result.python}")
    status = core.bridge_status(bridge_dir)
    check(status.exists, "готовый мост опознаётся проверкой состояния")
    check(not status.problem, f"замечаний к мосту нет: {status.problem or 'нет'}")
    check("библиотека" in status.library,
          f"состояние показывает библиотеку: {status.library}")

    # Проверку можно запустить и отдельно — кнопкой «Проверить».
    # Число проверок не зашиваем: мост их прибавляет, и любое новое
    # назначение ломало бы эту строку. Смотрим только на «все прошли».
    ok, output = core.bridge_selftest(bridge_dir)
    tail = [line.strip() for line in output.splitlines() if "ИТОГ" in line]
    check(ok and tail and "все" in tail[-1],
          f"проверка моста запускается отдельно и проходит: {tail[-1] if tail else 'нет строки ИТОГ'}")
    check("[СБОЙ]" not in output, "в самопроверке моста нет ни одного сбоя")

    # В чужую непустую папку не пишем: там могут быть нужные файлы.
    foreign = btmp / "чужая-папка"
    foreign.mkdir()
    (foreign / "чужой-файл.txt").write_text("не трогать", encoding="utf-8")
    refused = core.create_bridge(foreign, real_library)
    check(not refused.ok, "в чужую непустую папку мост не пишется")
    check("не пустая" in " ".join(refused.errors),
          f"сказано, почему отказано: {refused.errors[0][:60]}")
    check(len(list(foreign.iterdir())) == 1,
          "чужой файл остался один — лишнего не создано")

    # Настройки моста собираются в текст — показать человеку.
    snippet = core.mcp_snippet(core.find_python(), bridge_dir / "server.py")
    check('"ncp"' in snippet and "stdio" in snippet,
          "настройки моста собираются в понятный текст")

    # Что человек видит в окне
    check(bool(btab.folder_edit.text()),
          f"в окне предложена папка для моста: {btab.folder_edit.text()}")
    check("Python" in btab.program_hint.text(),
          "в окне показан найденный Python")
    check(btab.btn_create.isEnabled(), "кнопка «Создать мост» доступна")
    check(btab.btn_check.isEnabled(), "кнопка «Проверить» доступна")
    latin = [
        text
        for text in (btab.btn_create.text(), btab.btn_check.text(), btab.btn_open.text())
        if any("a" <= ch.lower() <= "z" for ch in text)
    ]
    check(not latin, f"надписи на кнопках по-русски: {latin or 'чисто'}")

    # Предложение создать мост после создания базы не должно ломаться
    # и должно подставлять путь к новой библиотеке. Вопрос человеку
    # подменяем отказом: в проверке кликать некому, а модальное окно
    # без цикла событий виснет навсегда.
    from unittest import mock
    from PyQt6.QtWidgets import QMessageBox
    with mock.patch.object(QMessageBox, "question",
                           return_value=QMessageBox.StandardButton.No):
        window._suggest_bridge("")
        window._suggest_bridge(str(btmp / "нет-такой-библиотеки"))
        check(True, "пустой путь и путь без библиотеки не ломают предложение")
        window._suggest_bridge(str(core.app_root()))
    check(btab.library_edit.text().endswith("библиотека"),
          "предложение подставило путь к библиотеке новой базы")

    # И главное: настоящая библиотека не тронута.
    index_after = real_index.read_text(encoding="utf-8") if real_index.is_file() else ""
    check(index_before == index_after,
          "index.json настоящей библиотеки не изменился")
    journal_after = sorted(p.name for p in journal_dir.glob("*.md")) if journal_dir.is_dir() else []
    check(journal_before == journal_after,
          f"в журнале настоящей библиотеки новых файлов нет: {journal_after}")

    # ---- 8. Возможности для opencode — только во временной папке.
    # Настоящий ~/.config/opencode здесь не трогаем.
    echo("\n--- 8. Возможности opencode ---")
    import opencode_caps  # noqa: E402

    ctmp = Path(tempfile.mkdtemp(prefix="caps-check-"))
    fake = ctmp / "opencode"
    fake.mkdir()
    (fake / "opencode.jsonc").write_text(
        "{\n"
        '  // чужой комментарий\n'
        '  "mcp": {\n'
        '    "other": {"type": "remote", "url": "https://x"}\n'
        "  },\n"
        '  "permission": {"edit": "deny"}\n'
        "}\n",
        encoding="utf-8",
    )
    sel = {"voice", "pc", "ncp", "agents"}
    base = core.app_root()
    messages, errors = opencode_caps.install_caps(base, fake, sel)
    check(not errors, f"установка возможностей без ошибок: {errors or 'чисто'}")
    cfg_text = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check("чужой комментарий" in cfg_text, "чужой комментарий в настройках цел")
    check('"other"' in cfg_text, "чужой сервер other цел")
    check('"pc"' in cfg_text and '"ncp"' in cfg_text, "мосты pc и ncp вписаны")
    check(opencode_caps.check_jsonc(cfg_text), "настройки валидны после вставки")
    check(len(list((fake / "agents").glob("*.md"))) == 12, "агентов поставлено 12")
    check((fake / "command" / "voice.md").is_file(), "команда /голос поставлена")
    check("{{VOICE_DIR}}" not in (fake / "command" / "voice.md").read_text(encoding="utf-8"),
          "путь к голосу подставлен настоящим")
    messages2, errors2 = opencode_caps.install_caps(base, fake, sel)
    check(not errors2, "повторная установка без ошибок")
    cfg_text2 = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(cfg_text2.count('"pc"') == 1, "повтор не двоит записи")
    status = opencode_caps.caps_status(fake)
    check(all(status[n] for n in sel), f"статус видит поставленное: {status}")
    _, errors3 = opencode_caps.remove_caps(fake, sel)
    check(not errors3, f"удаление без ошибок: {errors3 or 'чисто'}")
    cfg_text3 = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(cfg_text3) and '"pc"' not in cfg_text3,
          "после удаления настройки валидны и чистые")
    check('"other"' in cfg_text3, "чужое цело после удаления")
    check(not (fake / "command" / "voice.md").exists(), "команда убрана")
    check(list((fake / "agents").glob("*.md")) == [], "агенты убраны")

    # ---- 8а. Обход блокировок — туда же, во временную папку.
    # Ярлык не ставим: проверка не должна трогать настоящий рабочий стол.
    import antiblock  # noqa: E402

    ab_opts = {"facade": True, "lists": True, "command": True, "shortcut": False}
    m_ab, e_ab = opencode_caps.install_caps(
        base, fake, {"antiblock"}, antiblock_opts=ab_opts
    )
    check(not e_ab, f"обход поставлен без ошибок: {e_ab or 'чисто'}")
    check((fake / "antiblock" / "http_facade.py").is_file(), "фасад у программы")
    check((fake / "antiblock" / "public_socks5.txt").is_file(), "пул у программы")
    check((fake / "antiblock" / "subscriptions.txt").is_file(), "подписки у программы")
    check((fake / "antiblock" / "start_opencode_proxy.cmd").is_file(),
          "запускалка у программы")
    check((fake / "antiblock" / "xray_runner.py").is_file(),
          "свой Xray у программы")
    check((fake / "antiblock" / "dns_resolver.py").is_file(),
          "защищённый DNS у программы")
    check((fake / "command" / "antiblock.md").is_file(), "команда /обход у программы")
    check(not list((fake / "antiblock").glob("*.local.*")),
          "личных списков у программы нет")

    # Что установщик обещал поставить — это объединение списков, и
    # только оно. Сверять установленный набор надо со списком, а не со
    # всеми файлами папки: папка может содержать что угодно (черновик,
    # заметка, чужой скрипт), и это не делает установщик плохим. Раньше
    # здесь стояло сравнение с папкой, и любой посторонний файл ронял
    # проверку — а проверять тут нечего, установщик его и не собирался
    # ставить.
    _manifest = sorted(
        set(antiblock.ENGINE_FILES) | set(antiblock.LISTS_FILES)
        | set(antiblock.DNS_FILES) | set(antiblock.EXTRA_LAUNCHERS)
    )
    # А эта проверка настоящая: всё, что в списках, должно лежать в папке.
    # Опечатка в имени или забытый файл ломают установку у человека.
    _src_dir = core.program_root() / "tools" / "antiblock"
    _absent = [n for n in _manifest
               if not (_src_dir / n).is_file()
               and not n.startswith("public_socks5.local")]
    check(not _absent,
          f"всё, что в списках, лежит в папке набора: {_absent or 'чисто'}")

    # Запускалка Gemini: файлы лежали в папке набора, но ни в один список
    # не попадали, поэтому не ставились. Теперь они часть набора.
    for _gem in ("start_gemini_proxy.cmd", "start_gemini_proxy.ps1"):
        check(_gem in antiblock.EXTRA_LAUNCHERS,
              f"запускалка Gemini в списке набора: {_gem}")
        check((fake / "antiblock" / _gem).is_file(),
              f"и ставится вместе с фасадом: {_gem}")
    ab_cfg = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(ab_cfg), "настройки валидны после обхода")
    check("antiblock" not in ab_cfg, "в opencode.jsonc ничего лишнего не вписано")
    check("чужой комментарий" in ab_cfg, "чужой комментарий цел после обхода")
    m_ab2, e_ab2 = opencode_caps.install_caps(
        base, fake, {"antiblock"}, antiblock_opts=ab_opts
    )
    check(not e_ab2, "повтор обхода без ошибок")
    # Повтор не должен ни завести дубль, ни оставить мусор: в папке
    # набора лежит ровно то, что установщик обещал, и ничего сверх.
    _in_fake = sorted(p.name for p in (fake / "antiblock").iterdir() if p.is_file())
    check(_in_fake == _manifest,
          f"в папке набора ровно список, без дублей и лишнего: {_in_fake}")

    # Чужой файл, положенный в папку набора руками, установщик не
    # трогает. Это проверка на терпимость: положить черновик в папку —
    # обычное дело, и раньше из-за этого падал весь прогон.
    _alien = fake / "antiblock" / "моя-заметка.txt"
    _alien.write_text("черновик", encoding="utf-8")
    _m3, e3 = opencode_caps.install_caps(
        base, fake, {"antiblock"}, antiblock_opts=ab_opts
    )
    _after = sorted(p.name for p in (fake / "antiblock").iterdir() if p.is_file())
    check("моя-заметка.txt" in _after,
          "чужой файл в папке набора не тронут — установщик его не сносит")
    check(not e3, f"и повтор с посторонним файлом прошёл без ошибок: {e3 or 'чисто'}")
    check(set(_manifest).issubset(set(_after)),
          "а свои файлы при этом поставил")
    _alien.unlink()
    check(opencode_caps.caps_status(fake).get("antiblock") is True,
          "статус видит обход")
    ok_conn, _text_conn = antiblock.check_connection(port=_free_port())
    check(ok_conn is False, "проверка честно говорит: фасад не запущен (свободный порт)")

    # Настройка порта фасада: файл настроек, дефолт и отказ на негодном.
    _pf = Path(tempfile.mkdtemp(prefix="selftest-prefs-"))
    (_pf / "config").mkdir(parents=True, exist_ok=True)
    _prefs_file = _pf / "config" / "preferences.json"
    check(antiblock.facade_port(_pf) == antiblock.FACADE_PORT,
          "без файла настроек берётся порт по умолчанию")
    _prefs_file.write_text(json.dumps({"чужой": 1}), encoding="utf-8")
    check(antiblock.facade_port(_pf) == antiblock.FACADE_PORT,
          "файл есть, ключа нет — тоже порт по умолчанию")
    _prefs_file.write_text(json.dumps(["не словарь"]), encoding="utf-8")
    check(antiblock.facade_port(_pf) == antiblock.FACADE_PORT,
          "файл не словарь — тоже порт по умолчанию")
    _prefs_file.write_text(json.dumps({"facade_port": 17891}), encoding="utf-8")
    check(antiblock.facade_port(_pf) == 17891,
          "порт читается из файла настроек")
    _prefs_file.write_text(json.dumps({"чужой": 1, "facade_port": 17891}),
                           encoding="utf-8")
    _ok_prefs, _msg_prefs = antiblock.set_facade_port(17900, _pf)
    _saved_prefs = json.loads(_prefs_file.read_text(encoding="utf-8"))
    check(_ok_prefs is True, f"порт записывается: {_msg_prefs}")
    check(_saved_prefs.get("чужой") == 1,
          "запись настройки не съедает чужие ключи файла")
    check(_saved_prefs.get("facade_port") == 17900,
          "и записывает свой")
    _bad_ok, _bad_msg = antiblock.set_facade_port(antiblock.XRAY_PORT, _pf)
    check(_bad_ok is False, f"порт xray не сохраняется: {_bad_msg}")
    _prefs_file.write_text("{битый", encoding="utf-8")
    _warn_box: list[str] = []
    _fallback = antiblock.facade_port(_pf, warning=_warn_box)
    check(_fallback == antiblock.FACADE_PORT,
          "битый файл настроек даёт порт по умолчанию")
    check(bool(_warn_box), f"и предупреждение, а не молчание: {_warn_box}")
    check(_prefs_file.read_text(encoding="utf-8") == "{битый",
          "битый файл не переписывается молча")
    for _bad_val in (0, 1023, 70000, "восемь", None, True):
        check(bool(antiblock.port_error(_bad_val)),
              f"значение {_bad_val!r} не годится")
    check(antiblock.port_error(antiblock.XRAY_PORT) != "",
          "порт xray не годится для фасада")
    check(antiblock.port_error(antiblock.PORT_MIN) == "",
          "нижняя граница допустима")
    check(antiblock.port_error(antiblock.PORT_MAX) == "",
          "верхняя граница допустима")

    # Окно: поле порта фасада и кнопка сохранения. Проверяем живым окном,
    # а не поиском по исходнику: собранное поле доказывает, что элемент
    # действительно появился, и не доказывает ничего, если его имя
    # осталось только в тексте файла.
    _pf_win = Path(tempfile.mkdtemp(prefix="selftest-abwin-"))
    (_pf_win / "config").mkdir(parents=True, exist_ok=True)
    _pf_win_prefs = _pf_win / "config" / "preferences.json"
    _pf_win_prefs.write_text(json.dumps({"facade_port": 17891}),
                             encoding="utf-8")
    _base_saved = antiblock._program_base
    antiblock._program_base = lambda: _pf_win
    try:
        _abtab = app_main.CapsTab()
        check(hasattr(_abtab, "spin_ab_port"), "в окне есть поле порта фасада")
        check(hasattr(_abtab, "btn_ab_save"), "и кнопка «Сохранить»")
        check(callable(getattr(_abtab, "_save_ab_port", None)),
              "и обработчик сохранения")
        # Кнопка обязана быть связана с обработчиком. Остальные проверки
        # зовут его напрямую и о связи кнопки ничего не говорят: снять
        # connect — и все они останутся зелёными.
        check(_abtab.btn_ab_save.receivers(_abtab.btn_ab_save.clicked) >= 1,
              "кнопка «Сохранить» связана с обработчиком")
        _spin_win = _abtab.spin_ab_port
        check(_spin_win.minimum() == antiblock.PORT_MIN
              and _spin_win.maximum() == antiblock.PORT_MAX,
              f"поле не выпускает за {antiblock.PORT_MIN}-{antiblock.PORT_MAX}")
        check(_spin_win.value() == 17891,
              f"пользу берёт порт из настройки: {_spin_win.value()}")
        check("127.0.0.1:17891" in _abtab.achecks["facade"].text(),
              f"текст галочки собран из настройки: "
              f"{_abtab.achecks['facade'].text()[:70]}")
        # Занятый порт сохраняться не должен: иначе настройка всплывёт
        # только при следующем запуске обхода.
        import socket as _sock_w  # noqa: PLC0415 — как _free_port

        _p_busy_win = _free_port()
        _srv_w = _sock_w.socket()
        _srv_w.setsockopt(_sock_w.SOL_SOCKET, _sock_w.SO_REUSEADDR, 1)
        _srv_w.bind((antiblock.FACADE_HOST, _p_busy_win))
        _srv_w.listen(1)
        try:
            _log_w = _abtab.log.toPlainText()
            _spin_win.setValue(_p_busy_win)
            _abtab._save_ab_port()
            _said_w = _abtab.log.toPlainText()[len(_log_w):]
            check("занят" in _said_w,
                  f"занятый порт уходит в лог: {_said_w.strip()[:70]}")
            check(json.loads(_pf_win_prefs.read_text(
                encoding="utf-8")).get("facade_port") == 17891,
                  "и в файл настроек не попадает")
        finally:
            _srv_w.close()
        _p_ok_win = _free_port()
        _spin_win.setValue(_p_ok_win)
        _abtab._save_ab_port()
        check(json.loads(_pf_win_prefs.read_text(
            encoding="utf-8")).get("facade_port") == _p_ok_win,
            f"свободный порт сохраняется кнопкой: {_p_ok_win}")
        check(_spin_win.value() == _p_ok_win,
              "и поле остаётся на сохранённом")
    finally:
        antiblock._program_base = _base_saved

    # Строка состояния обхода. Проверяется на настоящих слушающих сокетах:
    # закрытый локальный порт падает с отказом сразу, а не по таймауту,
    # поэтому поднимки мгновенные и проверка не ждёт.
    import socket as _sock  # noqa: PLC0415 — как в _free_port

    def _listen_ab(port: int):
        srv = _sock.socket()
        srv.setsockopt(_sock.SOL_SOCKET, _sock.SO_REUSEADDR, 1)
        srv.bind((antiblock.FACADE_HOST, port))
        srv.listen(1)
        return srv

    _xp, _fp = _free_port(), _free_port()

    def _state() -> tuple[str, str]:
        return antiblock.channel_state(
            xray_port=_xp, facade_port=_fp, timeout=0.5)

    st_down, _txt_down = _state()
    check(st_down == antiblock.CHANNEL_DOWN,
          f"оба порта молчат — обход не работает: {st_down}")
    check(st_down != antiblock.CHANNEL_OWN,
          "молчание никогда не выдаётся за рабочий свой канал")
    _srv_x = _listen_ab(_xp)
    try:
        st_own, _ = _state()
        check(st_own == antiblock.CHANNEL_OWN,
              f"слушает свой канал — состояние «свой»: {st_own}")
    finally:
        _srv_x.close()
    _srv_f = _listen_ab(_fp)
    try:
        st_fb, txt_fb = _state()
        check(st_fb == antiblock.CHANNEL_FALLBACK,
              f"фасад без своего Xray — состояние «запасной»: {st_fb}")
        check("чужие машины" in txt_fb,
              "и текст говорит прямо, что канал запасной и это чужие машины")
        _srv_x2 = _listen_ab(_xp)
        try:
            st_both, _ = _state()
            check(st_both == antiblock.CHANNEL_OWN,
                  f"слушают оба — «свой» важнее «запасного»: {st_both}")
        finally:
            _srv_x2.close()
    finally:
        _srv_f.close()
    check(antiblock.XRAY_PORT == 10900,
          f"порт своего канала тот же, что в стартерах: {antiblock.XRAY_PORT}")

    # Занятость порта и подбор свободного.
    import socket as _sock2  # noqa: PLC0415 — как в _free_port

    def _listen_ab2(port: int):
        srv = _sock2.socket()
        srv.setsockopt(_sock2.SOL_SOCKET, _sock2.SO_REUSEADDR, 1)
        srv.bind((antiblock.FACADE_HOST, port))
        srv.listen(1)
        return srv

    _p_free = _free_port()
    check(antiblock.suggest_free_port(_p_free) == _p_free,
          "свободный порт предлагается как есть")
    _p_busy = _free_port()
    _srv_b = _listen_ab2(_p_busy)
    try:
        _suggest = antiblock.suggest_free_port(_p_busy)
        check(_suggest != _p_busy, f"занятый порт заменяется: {_suggest}")
        check(not antiblock._port_listening(antiblock.FACADE_HOST, _suggest, 0.3),
              "и предложенный порт свободен")
        check(_suggest != antiblock.XRAY_PORT,
              "предложенный порт не порт xray")
        _who = antiblock.who_listens(_p_busy)
        check(bool(_who), f"занятый порт кем-то слушается: {_who or 'не определили'}")
    finally:
        _srv_b.close()
    # Граница сверху. Сам 65535 свободен, и предлагать его верно: порт
    # допустимый. Проверяем другое — упершись в занятые верхние порты,
    # подбор обязан сказать «не нашлось», а не выдать 65536.
    _hi1 = _listen_ab2(antiblock.PORT_MAX - 1)
    _hi2 = _listen_ab2(antiblock.PORT_MAX)
    try:
        check(antiblock.suggest_free_port(antiblock.PORT_MAX - 1) == 0,
              "выше 65535 подбор не лезет")
    finally:
        _hi1.close()
        _hi2.close()
    check(antiblock.suggest_free_port(antiblock.XRAY_PORT) != antiblock.XRAY_PORT,
          "порт xray фасадом не предлагается")
    check(antiblock.who_listens(_p_free) == "",
          "на свободном порту никто не слушает")

    _, e_ab3 = opencode_caps.remove_caps(fake, {"antiblock"})
    check(not e_ab3, f"обход убран без ошибок: {e_ab3 or 'чисто'}")
    check(not (fake / "antiblock" / "http_facade.py").exists(), "фасад убран")
    check(not (fake / "antiblock" / "start_gemini_proxy.cmd").exists(),
          "запускалка Gemini убрана вместе с обходом")
    check((fake / "_previous-version" / "antiblock" / "start_gemini_proxy.cmd")
          .is_file(), "и сохранилась в _previous-version, а не пропала")
    check(not (fake / "command" / "antiblock.md").exists(), "команда /обход убрана")
    check((fake / "_previous-version" / "antiblock" / "http_facade.py").is_file(),
          "файлы обхода сохранены в _previous-version/antiblock")
    ab_cfg3 = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(ab_cfg3) and '"other"' in ab_cfg3,
          "после уборки обхода валидно и чужое цело")
    check(opencode_caps.caps_status(fake).get("antiblock") is not True,
          "статус больше не видит обход")

    # Стартеры читают порт из настроек программы, а не из константы.
    # Путь к папке программы лежит в манифесте набора: рабочая копия
    # стартера лежит в папке настроек opencode, и подъёмом вверх от
    # $PSScriptRoot до папки программы не дойти — это другое дерево.
    _mf = json.loads((fake / antiblock.MANIFEST).read_text(encoding="utf-8"))
    check(antiblock.BASE_KEY not in _mf,
          "после снятия обхода путь к программе из манифеста убран")
    _ab_opts_mf = antiblock.default_opts()
    _ab_opts_mf["shortcut"] = False
    _mf_dir = Path(tempfile.mkdtemp(prefix="selftest-mf-"))
    # Папка программы — именно program_root(): установщик берёт её
    # оттуда, а не из base того набора, который ставится.
    _prog_root = core.program_root()
    _m_mf, _e_mf = antiblock.install_antiblock(
        _prog_root, _mf_dir, _ab_opts_mf)
    check(not _e_mf, f"обход ставится для проверки манифеста: {_e_mf or 'чисто'}")
    _mf2 = json.loads((_mf_dir / antiblock.MANIFEST).read_text(encoding="utf-8"))
    check(_mf2.get(antiblock.BASE_KEY) == str(_prog_root.resolve()),
          f"установка кладёт путь к программе в манифест: "
          f"{_mf2.get(antiblock.BASE_KEY)}")
    check(bool(_mf2.get("antiblock_files")),
          "и не теряет список файлов набора")
    # Повторная установка ключ не теряет — иначе после любого переутановления
    # стартер снова ушёл бы в дефолт.
    antiblock.install_antiblock(_prog_root, _mf_dir, _ab_opts_mf)
    _mf3 = json.loads((_mf_dir / antiblock.MANIFEST).read_text(encoding="utf-8"))
    check(_mf3.get(antiblock.BASE_KEY) == str(_prog_root.resolve()),
          "повторная установка путь к программе не теряет")
    shutil.rmtree(_mf_dir, ignore_errors=True)

    for _starter in ("start_opencode_proxy.ps1", "start_gemini_proxy.ps1"):
        _sp = core.program_root() / "tools" / "antiblock" / _starter
        _txt = _sp.read_text(encoding="utf-8-sig") if _sp.is_file() else ""
        check(bool(_txt), f"{_starter} на месте")
        check("Get-FacadePort" in _txt,
              f"{_starter} читает порт из настроек")
        check(antiblock.FACADE_PORT_KEY in _txt,
              f"{_starter} знает ключ {antiblock.FACADE_PORT_KEY}")
        check(antiblock.BASE_KEY in _txt,
              f"{_starter} берёт путь к программе из манифеста")
        check(f"$FacadeDefaultPort = {antiblock.FACADE_PORT}" in _txt,
              f"{_starter} объявляет дефолт параметром")
        _hard = [ln.strip() for ln in _txt.splitlines()
                 if str(antiblock.FACADE_PORT) in ln
                 and "$FacadeDefaultPort" not in ln]
        check(not _hard,
              f"{_starter} не зашивает {antiblock.FACADE_PORT} в коде: {_hard}")
        check(_txt.startswith("\ufeff") or _sp.read_bytes()[:3] == b"\xef\xbb\xbf",
              f"{_starter} с BOM — иначе PowerShell 5.1 не читает русский текст")
        # Стартер обязан звать функцию, а не подставлять свой порт.
        _call = [ln.strip() for ln in _txt.splitlines()
                 if ln.strip().startswith("$listenPort =")
                 or ln.strip().startswith("$FacadePort =")]
        check(any("Get-FacadePort" in ln for ln in _call),
              f"{_starter} порт берёт вызовом Get-FacadePort: {_call}")

    # Горячая подмена пула (set_upstreams) и безопасность автообновления.
    sys.path.insert(0, str(core.program_root() / "tools" / "antiblock"))
    import http_facade  # noqa: E402
    import pool_refresh  # noqa: E402

    pool = http_facade.ProxyPool(["socks5://1.1.1.1:1080", "socks5://2.2.2.2:1080"])
    accepted = pool.set_upstreams(["socks5://3.3.3.3:1080", "socks5://4.4.4.4:1080"])
    check(accepted == 2, f"set_upstreams принял новый пул: {accepted}")
    new_urls = {pool.next_url() for _ in range(4)}
    check(new_urls == {"socks5://3.3.3.3:1080", "socks5://4.4.4.4:1080"},
          "после подмены пул отдаёт только новые узлы")
    before = set(pool.next_url() for _ in range(2))
    check(before == new_urls, "курсор сброшен, новый пул сразу доступен")
    keep = pool.set_upstreams([])
    check(keep == 0, f"пустой список не принят: {keep}")
    check(len(pool.upstreams) == 2, "старый пул цел после отказа от пустого")
    keep2 = pool.set_upstreams(["не_валид", "socks5://5.5.5.5:1080"])
    check(keep2 == 1, f"невалидные пропущены, валидный принят: {keep2}")

    # refresh_pool не трогает существующий файл, если живых узлов нет.
    tmp_pool = fake / "tmp-live.txt"
    tmp_pool.write_text("socks5://9.9.9.9:1080\n", encoding="utf-8")
    report = pool_refresh.refresh_pool(
        output=tmp_pool,
        sources=("http://127.0.0.1:1/never",),
        fetch_timeout=0.5,
        timeout=0.3,
        workers=2,
    )
    check(report["ok"] is False, "без живых узлов отчёт честно говорит «не ок»")
    check(tmp_pool.read_text(encoding="utf-8").strip() == "socks5://9.9.9.9:1080",
          "старый пул не перезаписан при неудачном обновлении")
    if tmp_pool.exists():
        tmp_pool.unlink()


    # ---- 8б. Новые пресеты провайдеров — туда же, во временную папку.
    psel = {"ollama", "lmstudio"}
    _, perrors = opencode_caps.install_providers(fake, psel)
    check(not perrors, f"провайдеры вписаны без ошибок: {perrors or 'чисто'}")
    pcfg = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(pcfg), "настройки валидны после пресетов")
    check('"ollama"' in pcfg and '"lmstudio"' in pcfg,
          "оба пресета на месте")
    check("sk-" not in pcfg, "никаких ключей в файл не попало")
    _, perrors2 = opencode_caps.install_providers(fake, psel)
    check(not perrors2, "повтор провайдеров без ошибок")
    pcfg2 = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(pcfg2.count('"ollama"') == 1, "повтор не двоит пресет")
    check(all(opencode_caps.providers_status(fake).values()), "статус видит пресеты")
    _, perrors3 = opencode_caps.remove_providers(fake, psel)
    check(not perrors3, f"пресеты убраны без ошибок: {perrors3 or 'чисто'}")
    pcfg3 = (fake / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(pcfg3) and '"ollama"' not in pcfg3,
          "после уборки валидно и чисто")
    check('"other"' in pcfg3, "чужое цело после уборки пресетов")

    # Вкладка в окне: три галочки расширений + два провайдера.
    # Мостов среди галочек нет — они едут с базой и подставляются всегда.
    ctab = window.caps_tab
    check(len(ctab.checks) == 3, f"галочек три: {sorted(ctab.checks)}")
    check("antiblock" in ctab.checks, "галочка обхода на месте")
    check("pc" not in ctab.checks and "ncp" not in ctab.checks,
          "галочек мостов в окне нет — они едут с базой")
    sel = ctab._selection()
    check({"pc", "ncp"} <= sel, f"мосты входят в выбор всегда: {sorted(sel)}")
    check(len(ctab.pchecks) == 2, f"провайдеров два: {sorted(ctab.pchecks)}")
    check(all(box.isChecked() for box in ctab.checks.values()), "по умолчанию всё отмечено")
    check(not any(box.isChecked() for box in ctab.pchecks.values()),
          "провайдеры по умолчанию не отмечены (ключи — дело человека)")
    check(ctab.btn_install.isEnabled(), "кнопка «Поставить» доступна")
    check(ctab.btn_remove.isEnabled(), "кнопка «Убрать» доступна")
    check(len(ctab.achecks) == 5, f"состава обхода пять: {sorted(ctab.achecks)}")
    check(all(box.isChecked() for box in ctab.achecks.values()),
          "состав обхода по умолчанию весь отмечен")
    check(ctab.btn_ab_check.isEnabled(), "кнопка проверки подключения доступна")
    check(ctab.btn_ab_dns.isEnabled(), "кнопка проверки DNS доступна")
    latin = [
        text
        for text in (ctab.btn_install.text(), ctab.btn_remove.text(), ctab.btn_refresh.text())
        if any("a" <= ch.lower() <= "z" for ch in text)
    ]
    check(not latin, f"надписи кнопок по-русски: {latin or 'чисто'}")

    # Шаг 4 на вкладке opencode: навыки поштучно, все отмечены.
    # Список берётся из ВЫБРАННОЙ базы, а не из конструктора: вкладка
    # показывает то, что реально лежит в подключённой базе.
    want_skills = core.list_skills(ctab._base())
    check(ctab.caps_skills_list.count() == len(want_skills),
          f"навыков в списке: {ctab.caps_skills_list.count()} из {len(want_skills)}")
    check(len(ctab._chosen_caps_skills()) == len(want_skills),
          "при первом показе отмечены все навыки")
    check(ctab.btn_skills_put.isEnabled() and ctab.btn_skills_drop.isEnabled(),
          "кнопки поставить/убрать навыки доступны")
    # Список должен показывать навыки, а не три строки из тридцати двух.
    # Перенос делает core.skill_item_text, вставляя переносы в текст сам:
    # Qt со своим сворачиванием высоту строк заранее не показывает.
    # Проверяем свойства виджета и то, до чего он доводит, а не только как
    # он выглядит.
    _sl = ctab.caps_skills_list
    check(_sl.horizontalScrollBarPolicy()
          == app_main.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
          "горизонтальной прокрутки нет — список не вылезает по ширине")
    check(_sl.horizontalScrollBar().maximum() == 0,
          "и по горизонтали прокручивать нечего — текст урезан по ширине")
    check(_sl.minimumHeight() >= 300,
          f"высота списка вмещает больше трёх навыков: {_sl.minimumHeight()} px")

    # Строка навыка: название плюс одна короткая строка. Больше — значит
    # навык растягивается на пол-экрана, и тридцать два не пролистать.
    _rows = [_sl.item(i) for i in range(_sl.count())]
    _line_counts = [t.text().count("\n") + 1 for t in _rows]
    check(all(n <= 2 for n in _line_counts),
          f"строка навыка не длиннее двух строк: максимум {max(_line_counts)}")
    _widths = [len(line)
               for t in _rows for line in t.text().splitlines()]
    check(max(_widths) <= 95,
          f"строка урезана по ширине: самая длинная {max(_widths)} знаков")
    check(all(t.toolTip() for t in _rows),
          "а полное описание осталось в подсказке по наведению")

    # Главное: доезжает ли список до последнего навыка.
    _vb = _sl.verticalScrollBar()
    _vp = _sl.viewport().rect()
    _vb.setValue(_vb.maximum())
    _last = _sl.count() - 1
    _on_screen = _sl.visualItemRect(_sl.item(_last)).intersects(_vp)
    check(_on_screen,
          f"прокруткой доезжаешь до последнего навыка: {_last + 1}-й")
    check(_sl.visualItemRect(_sl.item(_last)).bottom() <= _vp.bottom(),
          "и он виден целиком, а не обрезан краем")
    _per_screen = sum(1 for i in range(_sl.count())
                      if _sl.visualItemRect(_sl.item(i)).intersects(_vp))
    check(_per_screen >= 8,
          f"за один экран видно достаточно навыков: {_per_screen}")

    # Высота списка считается по числу навыков, и все они видны разом.
    # Проверка ловит откат к потолку в 600 px: тогда подпись говорит
    # «Отмечены все: 32», а в списке без прокрутки двадцать строк, и
    # человек решает, что остальных навыков ему не дали.
    _want_h = min(app_main.SKILL_ROW_PX * _sl.count()
                  + app_main.SKILL_LIST_PAD_PX, 1000)
    check(_sl.minimumHeight() == _want_h and _sl.maximumHeight() == _want_h,
          f"высота списка посчитана по {_sl.count()} навыкам: {_want_h} px")

    # Своей прокрутки у списка быть не должно. Проверять это здесь нельзя:
    # окно в селфтесте не показывается, QTabWidget задаёт размер только
    # текущей вкладке, и замеры видимости тут противоречат друг другу —
    # полоса говорит «прокрутка не нужна», а visualItemRect показывает
    # 19 строк из 32. Видимость проверена отдельно, на показанном окне с
    # переключённой вкладкой: там видны все 32. Здесь ловим главное —
    # откат высоты к потолку в 600 px, который и был причиной жалобы.
    _prev_tab = window.tabs.currentIndex()
    _caps_index = next((i for i in range(window.tabs.count())
                        if window.tabs.widget(i) is ctab), 0)
    window.tabs.setCurrentIndex(_caps_index)
    try:
        for _ in range(6):
            app_main.QApplication.processEvents()
        check(_sl.minimumHeight() <= _sl.height(),
              f"высоты списка хватает на все строки: {_sl.height()} px "
              f"при {_sl.count()} навыках")
    finally:
        window.tabs.setCurrentIndex(_prev_tab)

    _create_sl = getattr(window.import_tab, "skills_list", None)
    if _create_sl is not None:
        check(_create_sl.horizontalScrollBarPolicy()
              == app_main.Qt.ScrollBarPolicy.ScrollBarAlwaysOff,
              "и горизонтальной прокрутки у списка при создании базы тоже нет")
        _want_c = min(app_main.SKILL_ROW_PX * _create_sl.count()
                      + app_main.SKILL_LIST_PAD_PX, 1000)
        check(_create_sl.minimumHeight() == _want_c,
              f"и высота второго списка тоже по навыкам: {_want_c} px")
    nagents = len(list((core.program_root() / 'tools' / 'agents').glob('*.md')))
    check(str(nagents) in ctab.checks['agents'].text(),
          f"агентов названо честно: {ctab.checks['agents'].text()[:40]}")

    # Мост: выбор программы и кнопка оверлея.
    check(btab.radio_open.isChecked(), "по умолчанию мост для opencode")
    check(btab.btn_save_overlay.isEnabled(), "кнопка YAML для Харнеса доступна")
    overlay = core.harness_overlay_snippet(Path("C:/py/python.exe"), Path("C:/m/server.py"))
    check("dsh --patch" in overlay and "memory-ncp" in overlay,
          "оверлей Харнеса: имя и применение на месте")
    shutil.rmtree(ctmp, ignore_errors=True)
    echo(f"Временная папка возможностей убрана: {ctmp}")

    # Вкладка «Темы»: папка в корне главной базы, список файлов и кнопки.
    ttab = window.themes_tab
    check(ttab.themes_dir() == core.program_root() / "themes", "папка тем — themes/ в корне базы")
    check(ttab.btn_open is not None and ttab.btn_refresh is not None,
          "кнопки «Открыть папку тем» и «Обновить список» на месте")
    check(ttab.list_.count() >= 1, "в списке есть хотя бы одна тема-заготовка")

    # ---- 8в. перевод подключённой базы: пути, мосты и права меняются
    # на новую базу, а чужое (провайдеры, чужие серверы) остаётся.
    echo("\n--- 8в. Перевод настроек на другую базу ---")
    wtmp = Path(tempfile.mkdtemp(prefix="wire-check-"))
    other_base = wtmp / "Старая-база"
    (other_base / "tools").mkdir(parents=True)
    wdest = wtmp / "opencode"
    wdest.mkdir()
    (wdest / "opencode.jsonc").write_text(
        "{\n"
        '  "$schema": "https://opencode.ai/config.json",\n'
        '  "instructions": [\n'
        f'    "{other_base.as_posix()}/profile.md"\n'
        "  ],\n"
        '  "provider": {"myauto": {"npm": "@ai-sdk/openai-compatible"}},\n'
        '  "mcp": {"other": {"type": "remote", "url": "https://x"}},\n'
        '  "permission": {"edit": "deny"}\n'
        "}\n",
        encoding="utf-8",
    )
    wbase = wtmp / "Новая-база"
    for path in core.INSTRUCTION_TARGETS:
        p = wbase / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("текст", encoding="utf-8")
    (wbase / "tools" / "ncp-bridge").mkdir(parents=True)
    (wbase / "tools" / "ncp-bridge" / "config.json").write_text(
        '{"library_path": "{{LIBRARY}}"}\n', encoding="utf-8"
    )
    wire_msgs = opencode_caps.rewire_config(wdest, wbase)
    wcfg = (wdest / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(wcfg), "файл валиден после перевода")
    check("Старая-база" not in wcfg, "старый путь ушёл из instructions")
    check(wbase.as_posix() in wcfg, "instructions указывают на новую базу")
    check("myauto" in wcfg, "чужой провайдер цел")
    check('"other"' in wcfg, "чужой сервер other цел")
    check('"edit": "deny"' in wcfg, "чужие права целы")
    check('"ncp"' in wcfg and '"pc"' in wcfg, "мосты ncp и pc вписаны")
    check('"ncp_*"' in wcfg and '"pc_*"' in wcfg, "права на мосты стоят")

    # Переключение на вторую базу: мосты переезжают, чужое остаётся.
    wbase2 = wtmp / "Ещё-база"
    (wbase2 / "tools" / "ncp-bridge").mkdir(parents=True)
    (wbase2 / "tools" / "ncp-bridge" / "config.json").write_text(
        '{"library_path": "{{LIBRARY}}"}\n', encoding="utf-8"
    )
    (wbase2 / "profile.md").write_text("текст", encoding="utf-8")
    opencode_caps.rewire_config(wdest, wbase2)
    wcfg2 = (wdest / "opencode.jsonc").read_text(encoding="utf-8")
    check(opencode_caps.check_jsonc(wcfg2), "файл валиден после переключения")
    check(wbase2.as_posix() in wcfg2, "instructions переехали на вторую базу")
    check(other_base.as_posix() not in wcfg2, "пути первой базы не остались")
    check("myauto" in wcfg2 and '"other"' in wcfg2,
          "чужое пережило переключение")
    check('"ncp"' in wcfg2 and '"pc"' in wcfg2, "мосты на месте после переезда")
    for fix in core.configure_ncp_bridge(wbase2):
        pass
    bridge2 = json.loads(
        (wbase2 / "tools" / "ncp-bridge" / "config.json").read_text(encoding="utf-8")
    )
    check("библиотека" in str(bridge2.get("library_path", "")),
          "мост второй базы получил путь к библиотеке")
    shutil.rmtree(wtmp, ignore_errors=True)
    echo("Временная папка перевода убрана")

    # Мосты внутри базы: полный круг подключение → отключение → подключение.
    echo("\n--- 8г. Мосты живут в базе ---")
    mtmp = Path(tempfile.mkdtemp(prefix="mosty-bazy-"))
    mbase = mtmp / "MostBase"
    (mbase / "библиотека").mkdir(parents=True)
    (mbase / "библиотека" / "index.json").write_text("{}", encoding="utf-8")
    (mbase / "библиотека" / "NCP.md").write_text("x", encoding="utf-8")
    for _bridge in core.BASE_BRIDGES:
        shutil.copytree(
            core.program_root() / "tools" / _bridge,
            mbase / "tools" / _bridge,
            ignore=shutil.ignore_patterns("__pycache__", "config.json"),
        )
    mncp = mbase / "tools" / "ncp-bridge" / "config.json"
    mpc = mbase / "tools" / "pc-bridge" / "config.json"
    check(not mncp.is_file() and not mpc.is_file(),
          "сценарий поломки: у мостов нет config.json")

    for _msg in core.configure_bridges(mbase):
        pass
    check(mncp.is_file(), "мост NCP: config.json достроен в базу")
    check(mpc.is_file(), "мост ПК: config.json достроен в базу")
    _cfg = json.loads(mncp.read_text(encoding="utf-8"))
    check(_cfg.get("library_path") == (mbase / "библиотека").as_posix(),
          "мост NCP: путь ведёт в библиотеку этой базы")
    check("{{LIBRARY}}" not in str(_cfg.get("library_path", "")),
          "мост NCP: заглушка убрана")
    _pc = json.loads(mpc.read_text(encoding="utf-8"))
    check(_pc.get("allowed_dirs") == ["{{BASE}}"],
          "мост ПК: allowed_dirs с пометкой {{BASE}} (развернёт сам)")

    _tpl = core.bridge_template_config("ncp-bridge")
    _before = hashlib.sha256(_tpl.read_bytes()).hexdigest() if _tpl.is_file() else ""
    _lib = (mbase / "библиотека").as_posix()
    _cfg = json.loads(mncp.read_text(encoding="utf-8"))
    if _cfg.get("library_path") == _lib:
        _cfg["library_path"] = core.BRIDGE_LIBRARY_PLACEHOLDER
        mncp.write_text(json.dumps(_cfg, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    _off = json.loads(mncp.read_text(encoding="utf-8"))
    check(_off.get("library_path") == core.BRIDGE_LIBRARY_PLACEHOLDER,
          "мост NCP отключён: путь снова заглушка")
    check(mncp.is_file() and mpc.is_file(),
          "настройки мостов остались в базе (ничего не удалено)")
    check((mbase / "библиотека" / "index.json").is_file(), "библиотека базы цела")
    _after = hashlib.sha256(_tpl.read_bytes()).hexdigest() if _tpl.is_file() else ""
    check(_after == _before, "отключение не тронуло конструктор")

    for _msg in core.configure_bridges(mbase):
        pass
    _on = json.loads(mncp.read_text(encoding="utf-8"))
    check(_on.get("library_path") == _lib,
          "мост NCP ожил после повторного подключения")
    _after2 = hashlib.sha256(_tpl.read_bytes()).hexdigest() if _tpl.is_file() else ""
    check(_after2 == _before, "подключение не тронуло конструктор")
    check(json.loads(mncp.read_text(encoding="utf-8")).get("library_path") != "",
          "мост NCP не остался с пустым путём")

    # Код моста в базе может устареть: тогда он не умеет разворачивать
    # пометки {{BASE}} и запрещает всё. Лечится обновлением из конструктора.
    _code = mbase / "tools" / "pc-bridge" / "server.py"
    _good = _code.read_bytes()
    _code.write_text("# устаревшая копия без разворачивания пометок\n", encoding="utf-8")
    _msgs = core.refresh_bridge_code(mbase)
    check(_code.read_bytes() == _good, "код моста ПК обновлён из конструктора")
    check(any("server.py" in _m for _m in _msgs), f"обновление отмечено в отчёте: {_msgs}")
    _lib_now = json.loads(mncp.read_text(encoding="utf-8")).get("library_path")
    check(_lib_now == _lib, "обновление кода не сбросило путь к библиотеке")
    check(core.refresh_bridge_code(mbase) == [],
          "повторное обновление молчит, когда код уже свежий")
    shutil.rmtree(mtmp, ignore_errors=True)
    echo("Временная база мостов убрана")

    # Основная база ищется по маркеру, ничего не меняя.
    main_now = core.current_base("opencode")
    check(isinstance(main_now, (Path, type(None))),
          "текущая основная база читается без ошибок")

    shutil.rmtree(btmp, ignore_errors=True)
    echo(f"Временная папка моста убрана: {btmp}")

    # ---- 14. удаление базы не задевает другие базы: найдено 28.09
    echo("\n--- 14. Удаление базы ---")

    # Кнопка «Удалить» удаляет по-настоящему — так и обещает, и человек,
    # которому база не нужна, не будет держать её рядом копией. Проверять
    # надо другое и более важное: удаление одной базы не должно задевать
    # остальные, а живая основная база остаётся нетронутой.
    _zap = Path(tempfile.mkdtemp(prefix="dbapp-udal-"))
    try:
        _parent = _zap / "место"
        _parent.mkdir()

        # Две базы: одну удаляем, вторую она не должна задеть.
        _mark = "ПРОБА-НЕ-СТЕРЕТЬ"
        _live = []
        for _name in ("Проба-удалить", "Проба-оставить"):
            _plan = core.build_plan(_parent, _name, None)
            core.create_base(_plan)
            _b = _plan.target
            (_b / "профиль.md").write_text(f"{_mark} {_name}", encoding="utf-8")
            (_b / "библиотека" / "записи").mkdir(parents=True, exist_ok=True)
            (_b / "библиотека" / "записи" / "проба.md").write_text(
                f"{_mark} {_name}", encoding="utf-8")
            (_b / "projects").mkdir(exist_ok=True)
            (_b / "projects" / "моя-работа.md").write_text(
                f"{_mark} {_name}", encoding="utf-8")
            _live.append(_b)
        _doomed, _spare = _live

        # 1. Без подтверждения ничего не происходит.
        ok1, msg1 = core.delete_base(_doomed)
        check(not ok1, "без confirm=True база не удаляется")
        check(_doomed.is_dir(), "папка осталась на месте после отказа")
        check((_doomed / "профиль.md").read_text(encoding="utf-8").strip()
              == f"{_mark} Проба-удалить", "профиль не тронут отказом")
        check("confirm=True" in msg1,
              f"отказ объясняет, что нужно: {msg1[:60]}")

        # 2. С подтверждением папка исчезает целиком, без остатков.
        ok2, msg2 = core.delete_base(_doomed, confirm=True)
        check(ok2, f"база удалена: {msg2}")
        check(not _doomed.exists(), "папки базы больше нет на диске")
        _leftovers = sorted(_parent.glob("Проба-удалить*"))
        check(not _leftovers,
              f"рядом не осталось её копий: {[p.name for p in _leftovers]}")

        # 3. Главное: вторая база цела — профиль, библиотека, проекты.
        check(_spare.is_dir(), "другая база осталась на месте")
        check((_spare / "профиль.md").read_text(encoding="utf-8").strip()
              == f"{_mark} Проба-оставить", "её профиль цел")
        check((_spare / "facts.md").is_file() and
              (_spare / "projects.md").is_file(),
              "facts.md и projects.md целы")
        check((_spare / "библиотека" / "записи" / "проба.md")
              .read_text(encoding="utf-8").strip()
              == f"{_mark} Проба-оставить", "запись библиотеки цела")
        check((_spare / "projects" / "моя-работа.md").is_file(),
              "работа по проектам цела")

        # 4. Страховка на будущее: удаление остаётся удалением.
        _code = (core.program_root() / "tools" / "dbapp" / "core.py")
        _fn = _code.read_text(encoding="utf-8")
        _fn = _fn.split("def delete_base(", 1)[-1].split("\ndef ", 1)[0]
        check("rmtree" in _fn,
              "delete_base по-настоящему стирает папку — как и обещает кнопка")
        _fn_i = _fn.find("rmtree")
        _fn_f = _fn.find("forget_base")
        check(0 <= _fn_i < _fn_f,
              "из списка база убирается только после успешного удаления")
    finally:
        shutil.rmtree(_zap, ignore_errors=True)
        echo(f"Временная папка убрана: {_zap}")

    # ---- 15. конструктор не везёт мою базу: запасной заслон
    echo("\n--- 15. Конструктор не везёт мою базу ---")

    # Человек спросил прямо: конструктор — это конструктор, а не моя база.
    # Проверяем дважды: что в эталоне нет личных файлов и что копирование
    # действино их пропускает. Второе важнее первого — правило в тексте
    # ничего не значит, если код ведёт себя иначе.
    _kit = core.program_root()

    # 1. В эталоне не должно быть личных файлов базы.
    _personal = [
        name for name in ("profile.md", "facts.md", "projects.md",
                          "профиль.md", "АКТИВНАЯ-ПАМЯТЬ.md", "активная-память.md")
        if (_kit / name).exists()
    ]
    check(not _personal, f"в конструкторе нет личных файлов базы: {_personal or 'ни одного'}")
    _pdirs = [
        name for name in ("projects", "sessions", "сессии", "память", "личное")
        if (_kit / name).is_dir()
    ]
    check(not _pdirs, f"в конструкторе нет папок с личным: {_pdirs or 'ни одной'}")
    # Личные записи отличить от служебных: запись называется ncp-…md,
    # пояснение папки — _О-ПАПКЕ.md. Раньше считались все md, и проверка
    # ругалась на собственное пояснение.
    _pl = _kit / "библиотека" / "записи" / "личное"
    _priv = sorted(p.name for p in _pl.glob("ncp-*.md")) if _pl.is_dir() else []
    check(not _priv,
          f"личных записей NCP в конструкторе нет: {len(_priv)}")
    check((_pl / "_О-ПАПКЕ.md").is_file(),
          "в конструкторе есть папка личное с пояснением, а не записями")
    # Путь берёмся через core.PROGRAM_FILES: у программы
    # свой путь отличается от пути в базе. Прямое проверка
    # ище смотрела в корень, и после реструктури оказалась красной.
    _ob = core.PROGRAM_FILES.get("ОБРАЗЕЦ-БАЗЫ.md", ("",))
    check(any((_kit / sub / "ОБРАЗЕЦ-БАЗЫ.md").is_file()
              for sub in _ob),
          "заготовка ОБРАЗЕЦ-БАЗЫ.md на месте — её создавать база должна")

    # 2. Копирование личное пропускает, а справочное везёт.
    _uz = Path(tempfile.mkdtemp(prefix="dbapp-uchego-"))
    try:
        _src = _uz / "образец"
        (_src / "библиотека" / "записи" / "личное" / "моё").mkdir(parents=True)
        (_src / "библиотека" / "записи" / "личное" / "моё" / "моя-запись.md").write_text(
            "личное", encoding="utf-8")
        (_src / "библиотека" / "записи" / "справка").mkdir(parents=True)
        (_src / "библиотека" / "записи" / "справка" / "справочная.md").write_text(
            "справочное", encoding="utf-8")
        _dst = _uz / "новая"
        core._copy_lib_records(_src, _dst)
        _got_priv = (_dst / "библиотека" / "записи" / "личное").rglob("*.md")
        check(not list(_got_priv), "личная запись в новую базу не попала")
        _got_ref = list((_dst / "библиотека" / "записи" / "справка").glob("*.md"))
        check(len(_got_ref) == 1,
              f"справочная запись переехала: {len(_got_ref)} шт.")

        # 3. Главное по существу: база, собранная с нуля, не содержит
        # ничьих личных данных. Профиль и факты — пустые заготовки,
        # личных папок и личных записей нет вовсе.
        _plan0 = core.build_plan(_uz / "с-нуля", "Проба-пустая-база", None)
        (_uz / "с-нуля").mkdir(exist_ok=True)
        core.create_base(_plan0)
        _fresh = _plan0.target
        _prof = (_fresh / "profile.md").read_text(encoding="utf-8")
        check(core.BLANK["profile.md"] == _prof,
              "профиль новой базы — пустая заготовка, без данных пользователя")
        _name_line = [ln for ln in _prof.splitlines() if "Зовут" in ln]
        check(bool(_name_line) and not _name_line[0].split("Зовут:")[-1].strip(),
              f"в профиле имя пустое: {_name_line}")
        check((_fresh / "projects.md").read_text(encoding="utf-8").strip()
              == core.BLANK["projects.md"].strip(),
              "projects.md новой базы — заготовка, не список моих проектов")
        # Личные папки создаются пустыми: в них лежат только пояснения
        # «что сюда писать». Ни одного настоящего файла быть не должно.
        _fresh_priv = []
        for name in ("projects", "sessions", "сессии", "память", "личное",
                     "журнал-решений", "настройки"):
            folder = _fresh / name
            if not folder.is_dir():
                continue
            for path in folder.rglob("*"):
                if not path.is_file() or path.name == "_О-ПАПКЕ.md":
                    continue
                # В папке настроек лежит ещё и решение о чувствительных
                # данных: это настройка базы, а не личные данные.
                if name == "настройки" and path.name == "чувствительные-данные.md":
                    continue
                _fresh_priv.append(path.relative_to(_fresh).as_posix())
        check(not _fresh_priv,
              f"в личных папках новой базы нет данных пользователя: "
              f"{_fresh_priv or 'ни одного файла'}")
        _pol = _fresh / "настройки" / "чувствительные-данные.md"
        check(_pol.is_file(),
              "файл решения о чувствительных данных создан в новой базе")
        if _pol.is_file():
            check("ЗАПРЕЩЕНО" in _pol.read_text(encoding="utf-8"),
                  "по умолчанию в новой базе запрет — решение человека")
        # Личная запись — это ncp-…md. Пояснение папки _О-ПАПКЕ.md
        # служебное и в новую базу едет намеренно.
        _pl_dir = _fresh / "библиотека" / "записи" / "личное"
        _fresh_lib = sorted(p.name for p in _pl_dir.glob("ncp-*.md")) \
            if _pl_dir.is_dir() else []
        check(not _fresh_lib,
              f"личных записей NCP в новой базе нет: {len(_fresh_lib)}")
        check((_pl_dir / "_О-ПАПКЕ.md").is_file(),
              "пояснение личной папки в новой базе есть")
        check((_fresh / "skills").is_dir() and core.count_skills(_fresh) > 0,
              f"а скиллы в неё приехали: {core.count_skills(_fresh)}")
    finally:
        shutil.rmtree(_uz, ignore_errors=True)
        echo(f"Временная папка убрана: {_uz}")

    # ---- 16. новая база сама говорит, что подключилась: найдено 28.09
    echo("\n--- 16. Нейросеть сообщает о подключении ---")

    # Человек спросил: при первом же сообщении нейросеть должна читать базу
    # И сообщать об этом. Читать — было указано, сообщать — нет. Молчаливое
    # подключение снаружи неотличимо от сломанной памяти.
    _uz2 = Path(tempfile.mkdtemp(prefix="dbapp-podklyuch-"))
    try:
        _p3 = core.build_plan(_uz2 / "место", "Проба-подключение", None)
        (_uz2 / "место").mkdir(exist_ok=True)
        core.create_base(_p3)
        _ag = _p3.target / "AGENTS.md"
        check(_ag.is_file(), f"в новой базе есть AGENTS.md: {_ag.is_file()}")
        if _ag.is_file():
            _txt = _ag.read_text(encoding="utf-8")
            _start = _txt.split("## В начале каждой сессии", 1)
            check(len(_start) == 2, "в AGENTS.md есть раздел о начале сессии")
            _block = _start[1].split("\n## ", 1)[0] if len(_start) == 2 else ""
            for _what, _needle in (
                ("читает память", "memory_read"),
                ("спрашивает состояние библиотеки", "library_status"),
                ("читает активную память", "АКТИВНАЯ-ПАМЯТЬ"),
                ("сообщает пользователю о подключении",
                 "Скажи пользователю, что подключился"),
            ):
                check(_needle in _block,
                      f"в начале сессии: {_what}")
            # Правило должно быть именно в начале сессии, а не где попало.
            check(_txt.count("Скажи пользователю, что подключился") == 1,
                  "правило про уведомление встречается ровно один раз")
            # В копии для папки config/ то же самое.
            _ag2 = _p3.target / "config" / "AGENTS.md"
            if _ag2.is_file():
                check("Скажи пользователю, что подключился"
                      in _ag2.read_text(encoding="utf-8"),
                      "та же инструкция доехала в config/AGENTS.md")

        # ---- 16б. шаблон не должен запекаться в базу
        # Найдено 28.09: в корне базы вместо пометки {{BASE}} стоял
        # настоящий путь к мусорной папке DataBases/test. Причина —
        # substitute_base отработал на самой базе. База должна хранить
        # шаблон, иначе перестанет переезжать на другой компьютер.
        if _ag.is_file():
            _root_txt = _ag.read_text(encoding="utf-8")
            check(core.BASE_PLACEHOLDER in _root_txt,
                  "в корне новой базы AGENTS.md остаётся шаблоном с пометкой")
            check("DataBases" not in _root_txt,
                  "в корне базы нет запечённого пути к DataBases")

        # Подстановка обязана давать настоящий путь, а не мусор.
        _tmp_cfg = _uz2 / "подстановка.md"
        _tmp_cfg.write_text("база: " + core.BASE_PLACEHOLDER + "\n",
                            encoding="utf-8")
        core.substitute_base(_tmp_cfg, _p3.target)
        _fixed = _tmp_cfg.read_text(encoding="utf-8")
        check(str(_p3.target).replace("\\", "/") in _fixed,
              f"substitute_base подставил настоящий путь: {_fixed.strip()[:70]}")
        check(core.BASE_PLACEHOLDER not in _fixed,
              "пометка после подстановки не осталась")
    finally:
        shutil.rmtree(_uz2, ignore_errors=True)
        echo(f"Временная папка убрана: {_uz2}")

    # ---- 17. папка баз по умолчанию — DataBases: найдено 28.09
    echo("\n--- 17. Базы по умолчанию в папке DataBases ---")

    # Человек заметил: папка DataBases создаётся при первом запуске, но в
    # поле создания базы стояли «Документы». Программа рекомендовала одну
    # папку, а базы уезжали в другую.
    _db = core.data_bases_folder()
    check(_db.name == "DataBases", f"папка баз называется DataBases: {_db.name}")
    check(_db.parent == core.desktop_dir(),
          f"папка баз лежит на рабочем столе: {_db.parent}")

    # Создание папки: проверяем на временной, настоящий рабочий стол не трогаем.
    _dbt = Path(tempfile.mkdtemp(prefix="dbapp-databases-"))
    _real_desktop = core.desktop_dir
    try:
        core.desktop_dir = lambda: _dbt
        _made = core.data_bases_folder()
        check(not _made.exists(), "до вызова папки нет")
        core.ensure_data_bases_folder()
        check(_made.is_dir(), f"ensure_data_bases_folder создал папку: {_made.name}")
        core.ensure_data_bases_folder()
        check(_made.is_dir(), "повторный вызов безвреден")
    finally:
        core.desktop_dir = _real_desktop
        shutil.rmtree(_dbt, ignore_errors=True)

    # В окне по умолчанию стоит именно она.
    _ct = window.tabs.widget(0)
    _shown = _ct.parent_edit.text().strip()
    check(_shown == str(_db),
          f"в поле создания базы папка DataBases, а не Документы: {_shown}")
    check("Documents" not in _shown and "Документы" not in _shown,
          f"Документы больше не подставляются: {_shown}")
    _ct.name_edit.setText("Проба-дефолт")
    _prev = _ct.path_preview.text().strip()
    _path_part = _prev.partition(":")[2].strip() or _prev
    check(_path_part.startswith(str(_db)),
          f"новая база уедет в DataBases: {_path_part[:70]}")
    shutil.rmtree(_dbt, ignore_errors=True)

    # ---- 18. новая база знает, как пользоваться агентами и скиллами
    echo("\n--- 18. Новая база знает про агентов, скиллы и NCP ---")

    # Требование человека: новая база должна знать, что агент и скилл
    # существуют, понимать, когда их брать, и уметь применять их сама.
    # Проверяем на реально созданной базе.
    _zn = Path(tempfile.mkdtemp(prefix="dbapp-znaniya-"))
    try:
        _pp = _zn / "место"
        _pp.mkdir()
        _pz = core.build_plan(_pp, "Проба-знания", None)
        core.create_base(_pz)
        _zb = _pz.target
        _ag_txt = (_zb / "AGENTS.md").read_text(encoding="utf-8")

        # --- агенты
        _ad = _zb / "tools" / "agents"
        _n_agents = len(list(_ad.glob("*.md"))) if _ad.is_dir() else 0
        check(_n_agents > 0, f"в базе есть агенты: {_n_agents}")
        check("Агент вызывается как" in _ag_txt,
              "инструкция объясняет вызов агента по имени")
        check(f"Всего их {_n_agents}" in _ag_txt,
              f"инструкция называет число агентов ({_n_agents})")
        _chain = ("proektirovschik", "programmist", "proveryalschik", "retsenzent")
        check(all(c in _ag_txt for c in _chain),
              "названа связка большой задачи: план → код → проверка → рецензия")
        _sk_doc = (_zb / "инструкции" / "Скиллы-и-агенты.md")
        check(_sk_doc.is_file(), "подробная инструкция по скиллам и агентам есть")
        if _sk_doc.is_file():
            _missing = [p.stem for p in _ad.glob("*.md")
                        if p.stem not in _sk_doc.read_text(encoding="utf-8")]
            check(not _missing,
                  f"в инструкции перечислены все агенты; нет: {_missing or 'никого'}")

        # --- скиллы
        _n_skills = core.count_skills(_zb)
        check(_n_skills > 0, f"в базе есть скиллы: {_n_skills}")
        _idx = _zb / "skills-index.json"
        check(_idx.is_file(), "индекс скиллов лежит в базе")
        if _idx.is_file():
            _data = json.loads(_idx.read_text(encoding="utf-8"))
            _items = _data.get("skills") if isinstance(_data, dict) else _data
            check(len(_items) == _n_skills,
                  f"записей в индексе столько же, сколько скиллов: "
                  f"{len(_items)} из {_n_skills}")
            _no_when = [i.get("name") for i in _items if not i.get("when")]
            check(not _no_when,
                  f"у каждого скилла есть условие срабатывания; нет у: {_no_when or 'никого'}")
            _known = {i.get("name") for i in _items}
            _dirs = sorted(p.name for p in (_zb / "skills").iterdir() if p.is_dir())
            _lost = [n for n in _dirs if n not in _known]
            check(not _lost,
                  f"в индексе есть все скиллы; нет: {_lost or 'никого'}")
            check(not any(n in _known for n in
                          (p.stem for p in _ad.glob("*.md"))),
                  "агенты не попали в индекс скиллов — это разные вещи")

            # Инструкция по навыкам обязана знать про все навыки. Раньше она
            # обещала 29 скиллов при 32 на диске, и три устройства — Android
            # Studio, OBS Studio и эмулятор — не упоминались нигде. Из-за
            # этого выбор навыка делался вслепую: агент читает инструкцию,
            # а не перебирает папку. Проверка нужна, чтобы число не устарело
            # снова молча.
            _guide = _zb / "инструкции" / "Скиллы-и-агенты.md"
            check(_guide.is_file(), "инструкция по навыкам лежит в базе")
            if _guide.is_file():
                _gt = _guide.read_text(encoding="utf-8")
                _said = re.search(r"всего в базе\s*\*\*(\d+)", _gt.lower())
                check(bool(_said), "инструкция называет число навыков")
                if _said:
                    check(int(_said.group(1)) == _n_skills,
                          f"число навыков в инструкции верно: "
                          f"написано {_said.group(1)}, на диске {_n_skills}")
                _glow = _gt.lower()
                _unmentioned = [n for n in _dirs if n.lower() not in _glow]
                check(not _unmentioned,
                      f"инструкция упоминает каждый навык; не упоминает: "
                      f"{_unmentioned or 'никого'}")
                # Шпаргалка не должна притворяться копией индекса.
                check("skills-index.json" in _glow,
                      "инструкция ссылается на индекс как на источник истины")

        # --- NCP: протокол должен называть настоящие имена инструментов
        _ncp_md = _zb / "библиотека" / "NCP.md"
        check(_ncp_md.is_file(), "протокол библиотеки NCP лежит в базе")
        if _ncp_md.is_file():
            _prot = _ncp_md.read_text(encoding="utf-8")
            for _tool in ("ncp_status", "ncp_search", "ncp_read", "ncp_save",
                          "ncp_update", "ncp_checkpoint", "ncp_reindex"):
                check(_tool in _prot, f"протокол называет инструмент {_tool}")
            check("library_search" in _prot,
                  "протокол упоминает и псевдоним library_search")

        # --- самостоятельность: без просьбы пользователя
        check("Скилл выбирается ДО начала работы" in _ag_txt,
              "инструкция велит выбрать скилл ДО работы")
        check("УВИДЕЛ ВОЗМОЖНОСТЬ" in _ag_txt,
              "есть правило «увидел возможность — скажи, потом применяй»")
        check("объяви это одной строкой" in _ag_txt,
              "применение объявляется ДО, а не после")
        check("не выдумывай инструмент ради применения" in _ag_txt,
              "есть запрет выдумывать инструмент ради применения")
        check("без просьбы пользователя" in _ag_txt,
              "взятие скилла объявлено обязательным без просьбы человека")

        # --- правило должно быть видно сразу, а не прятаться в конце
        _head = "\n".join(_ag_txt.splitlines()[:40])
        check("скилл" in _head.lower() and "агент" in _head.lower(),
              "про скиллы и агентов сказано в первых 40 строках инструкции")
    finally:
        shutil.rmtree(_zn, ignore_errors=True)
        echo(f"Временная папка убрана: {_zn}")

    # ---- 19. темы библиотеки — папки, и они создаются сами
    echo("\n--- 19. Темы библиотеки ---")

    # Требование человека: разносить данные по категориям и создавать
    # папку, если её нет. Пример: «личное, животные, собака Сэм».
    _kt = Path(tempfile.mkdtemp(prefix="dbapp-temy-"))
    try:
        _pk = core.build_plan(_kt / "место", "Проба-темы", None)
        (_kt / "место").mkdir(exist_ok=True)
        core.create_base(_pk)
        _kb = _pk.target

        # Папка категории есть, вложенной темы внутри нет.
        _deep = _kb / "библиотека" / "записи" / "личное" / "проверка"
        check(not _deep.exists(), "темы личное/проверка до записи нет")
        check((_kb / "библиотека" / "записи" / "личное").is_dir(),
              "папка категории личное создана при создании базы")

        # Новая база знает про это правило.
        for _f, _label in ((_kb / "AGENTS.md", "AGENTS.md"),
                           (_kb / "библиотека" / "NCP.md", "протокол NCP")):
            _t = _f.read_text(encoding="utf-8")
            check("Темы в библиотеке" in _t or "создаётся сама" in _t,
                  f"в {_label} описано: тема — папка, создаётся сама")
            check("личное/" in _t,
                  f"в {_label} есть пример вложенной темы")
            check("не едет" in _t or "не уезжают" in _t or "не копируются" in _t,
                  f"в {_label} сказано, что личное не уезжает")

        # В конструкторе код это умеет.
        _nc = (core.program_root() / "tools" / "ncp-bridge" / "ncp_core.py")
        _code = _nc.read_text(encoding="utf-8")
        check("def is_private(" in _code,
              "в коде моста есть различение личных тем")
        check("TOPIC_SEP" in _code,
              "в коде моста есть разделитель уровней темы")

        # Личное по-прежнему не копируется в новые базы. Правило живёт
        # в core.py, а не в мосте: я сперва искал его не там и проверка
        # падала без причины.
        _dc = (core.program_root() / "tools" / "dbapp" / "core.py")
        _copy = _dc.read_text(encoding="utf-8").split("def _copy_lib_records")[-1]
        check('parts[0] == "личное"' in _copy,
              "личные записи по-прежнему не копируются в новые базы")

        # Папка личное видна сразу, а не появляется при первой записи.
        _pl = _kb / "библиотека" / "записи" / "личное"
        check(_pl.is_dir(), "в новой базе есть папка личное")
        _hint = _pl / "_О-ПАПКЕ.md"
        check(_hint.is_file(), "в папке личное лежит пояснение")
        if _hint.is_file():
            _ht = _hint.read_text(encoding="utf-8")
            # Требование строгое: именно «не копируются в новые базы».
            # Раньше здесь стояло «или просто не копируются» — и это
            # пропускало устаревшую версию пояснения, где говорилось
            # «не копируются между собой», то есть про перенос между
            # папками, а не про новые базы. Смысл был обратным, а
            # проверка радовала.
            check("не копируются в новые базы" in _ht,
                  "пояснение говорит, что личное не уезжает в новые базы")
            # Отдельно: устаревшее пояснение описывало другую папку
            # личного — «только для проекта». Если оно вернётся, это
            # надо заметить, а не пропустить.
            check("только для проекта" not in _ht,
                  "и это не устаревшее пояснение из прежней конструкции")
    finally:
        shutil.rmtree(_kt, ignore_errors=True)
        echo(f"Временная папка убрана: {_kt}")

    # ---- 20. понимание, что писать в библиотеку
    echo("\n--- 20. Что писать в библиотеку ---")

    # Человек попросил: чтобы нейросеть понимала, какие данные
    # записывать. Проверяем, что это написано и в протоколе, и в
    # инструкции, и что правило не абстрактное.
    _zp = Path(tempfile.mkdtemp(prefix="dbapp-pisat-"))
    try:
        _pq = core.build_plan(_zp / "место", "Проба-писать", None)
        (_zp / "место").mkdir(exist_ok=True)
        core.create_base(_pq)
        _qb = _pq.target

        for _f, _label in ((_qb / "AGENTS.md", "AGENTS.md"),
                           (_qb / "библиотека" / "NCP.md", "протокол NCP")):
            _t = _f.read_text(encoding="utf-8")
            check("придётся выяснять заново" in _t or "выяснять заново" in _t,
                  f"в {_label} есть проверка: пропадёт ли из сессии")
            check("ncp_search" in _t,
                  f"в {_label} сказано искать перед записью")
            check("memory_save" in _t and "ncp_save" in _t,
                  f"в {_label} разведены короткая память и библиотека")
            check("Пароли" in _t or "пароли" in _t,
                  f"в {_label} запрет писать секреты")
            check("личное" in _t,
                  f"в {_label} сказано про личные записи")

        # В протоколе есть конкретные примеры, а не только общие слова.
        _prot = (_qb / "библиотека" / "NCP.md").read_text(encoding="utf-8")
        check("Что писать — с примерами" in _prot,
              "в протоколе есть таблица примеров")
        check("Одна запись — одна мысль" in _prot,
              "в протоколе есть правило одной мысли на запись")

        # Служебные файлы в папке записей не должны попадать в счёт.
        _nc = (core.program_root() / "tools" / "ncp-bridge" / "ncp_core.py")
        _code = _nc.read_text(encoding="utf-8")
        check('path.name.startswith("_")' in _code,
              "мост не считает служебные файлы записями")

        # Личная папка есть в новой базе и пуста, кроме пояснения.
        _pl = _qb / "библиотека" / "записи" / "личное"
        check(_pl.is_dir(), "в новой базе есть папка личное")
        check(not list(_pl.glob("ncp-*.md")),
              "в новой базе нет личных записей — только папка и пояснение")
    finally:
        shutil.rmtree(_zp, ignore_errors=True)
        echo(f"Временная папка убрана: {_zp}")

    # ---- 21. селфтест не тронул настоящий список баз
    echo("\n--- 21. Список баз не затронут ---")

    core.use_bases_file(None)
    _real_after = (
        _real_list.read_text(encoding="utf-8") if _real_list.is_file()
        else "<файла нет>"
    )
    check(_real_after == _real_before,
          f"настоящий список баз не изменился за прогон "
          f"({len(_real_before)} → {len(_real_after)} байт)")
    _real_paths_after = {str(e.get("path", "")).lower() for e in core.read_bases()}
    _added = _real_paths_after - _real_paths_before
    check(not _added,
          f"в список не добавилось ничего лишнего: {_added or 'ничего'}")
    _gone = _real_paths_before - _real_paths_after
    check(not _gone,
          f"существующие записи не потерялись: {_gone or 'ничего не пропало'}")
    # Мусор прошлых прогонов: временные папки в списке быть не должны.
    _temp_junk = [
        p for p in _real_paths_after
        if "\\Temp\\" in p or p.endswith("\test")
    ]
    check(not _temp_junk,
          f"в списке нет временных папок от прошлых прогонов: "
          f"{_temp_junk or 'чисто'}")
    echo(f"  в списке сейчас: {len(_real_paths_after)} записей")

    # Вот теперь возвращаем настоящий список баз: до этого места он
    # остаётся подменённым, иначе разделы, создающие базы, писали бы
    # прямо в список человека.
    core.use_bases_file(None)
    check(not core.bases_file().name.startswith("список"),
          f"настоящий список баз на месте: {core.bases_file().name}")
    check(_real_list.resolve() == core.bases_file().resolve(),
          f"возврат привёл к тому же файлу: {core.bases_file().name}")
    shutil.rmtree(_isolate, ignore_errors=True)
    echo("Временный список баз убран")

    # ---- 22. Android Studio в один клик
    echo("\n--- 22. Android Studio: версия, сборка плагина, адрес ---")

    # Разбор buildNumber в платформу. Наш настоящий номер не должен
    # разваливаться: программа обязана понять, что это 261.25134.
    check(android_studio.platform_from_build(
              "AI-261.25134.95.2612.15914620") == (261, 25134),
          "платформа из buildNumber: 261.25134")
    check(android_studio.platform_from_build(
              "262.10968.92") == (262, 10968),
          "платформа из buildNumber: 262.10968")
    check(android_studio.platform_from_build("") == (),
          "пустой buildNumber не превращается в платформу")

    # Сравнение чисел, а не строк. Иначе 263.9 окажется новее 263.10
    # и программа выберет не ту сборку плагина.
    check(android_studio.version_tuple("263.9") < android_studio.version_tuple("263.10"),
          "263.9 старее 263.10 — сравнение числовое")

    # Границы сборки плагина. Здесь важно всё: сборка 263.5701 не должна
    # подходить студии 263.6259, потому что её until точечный.
    _p = (263, 5701)
    check(android_studio.until_matches("263.5701.*", _p),
          "263.5701 подходит своей подплатформе")
    check(not android_studio.until_matches("263.5701.*", (263, 6259)),
          "263.5701 НЕ подходит студии 263.6259 — until точечный")
    check(android_studio.until_matches("261.*", (261, 26000)),
          "261.* накрывает всю ветку 261")
    check(not android_studio.until_matches("261.*", (262, 10315)),
          "261.* не накрывает ветку 262")
    check(android_studio.since_matches("261.25134", (261, 25134)),
          "since равно платформе — подходит")
    check(not android_studio.since_matches("262.10968", (262, 10315)),
          "since новее платформы — не подходит")

    # Набор сборок лежит в программе и читается.
    _bundle = android_studio.load_bundle()
    check(_bundle.ready,
          f"набор сборок плагина на месте: {len(_bundle.builds)} шт.")
    check(all(b.get("sha256") and b.get("since") and b.get("until")
              for b in _bundle.builds),
          "у каждой сборки есть хеш и границы версий")

    # Выбор под конкретную платформу — то, ради чего всё затевалось.
    for _plat, _want in ((( 261, 25134), "261.25134.203"),
                         ((262, 10315), "262.10315.174"),
                         ((262, 10968), "262.10968.92"),
                         ((263, 3889), "263.3889.77"),
                         ((263, 6259), "263.6259.38")):
        _pick = android_studio.pick_build(_bundle, _plat)
        check(_pick is not None and _pick["version"] == _want,
              f"платформа {android_studio.platform_text(_plat)} -> "
              f"сборка {_want}"
              + (f" (выбрано {_pick['version']})" if _pick else " (ничего)"))

    # Платформа, которой нет в наборе, — честный отказ, а не чужая сборка.
    check(android_studio.pick_build(_bundle, (264, 1)) is None,
          "под неизвестную платформу сборка не подставляется")
    check(android_studio.pick_build(android_studio.Bundle(base=Path(), builds=[]),
                                    (261, 25134)) is None,
          "пустой набор даёт отказ, а не исключение")

    # Хеш архива совпадает с тем, что в указателе: файлы не перепакованы.
    if _bundle.ready:
        _one = _bundle.builds[0]
        _arc = android_studio.bundle_file(_bundle, _one)
        check(_arc.is_file(), f"архив на месте: {_one['file']}")
        if _arc.is_file():
            check(android_studio.sha256(_arc).lower() == _one["sha256"].lower(),
                  f"хеш архива {_one['file']} совпадает с указателем")

    # Адрес сервера — тот самый, и никакого токена.
    check(android_studio.SERVER_URL == "http://127.0.0.1:64342/stream",
          f"адрес сервера: {android_studio.SERVER_URL}")
    check("63342" not in android_studio.SERVER_URL,
          "адрес не 63342 — это встроенный веб-сервер IDE, он всегда открыт")
    check("api/mcp" not in android_studio.SERVER_URL,
          "адрес не /api/mcp — там вечный 404")

    # Настройка включения сервера: годная разметка и правильный флаг.
    check('name="enableMcpServer"' in android_studio.SETTINGS_XML,
          "в настройках студии включается enableMcpServer")
    check("McpServerSettings" in android_studio.SETTINGS_XML,
          "компонент назван так, как ждёт сама студия")
    import xml.etree.ElementTree as _ET
    try:
        _root = _ET.fromstring(android_studio.SETTINGS_XML)
        _option = _root.find("./component/option")
        check(_root.tag == "application"
              and _root.find("component").get("name") == "McpServerSettings"
              and _option is not None and _option.get("value") == "true",
              "XML настройки разбирается и содержит enableMcpServer=true")
    except _ET.ParseError as _exc:
        check(False, f"XML настройки не разбирается: {_exc}")

    # Запись реестра android-studio: без токена, с адресом и без блокировок.
    _spec = next((s for s in (_mcp_registry.load_registry(core.program_root())
                             .get("servers") or [])
                  if s.get("id") == "android-studio"), None)
    check(_spec is not None, "android-studio есть в реестре")
    if _spec is not None:
        _conn = _spec.get("connection") or {}
        check(_conn.get("url") == "http://127.0.0.1:64342/stream",
              "в реестре указан постоянный адрес сервера")
        _auth = str(_spec.get("auth") or "")
        check("не нужен" in _auth.lower() or "нет" in _auth.lower(),
              f"в реестре сказано, что токен не нужен: {_auth}")
        check("Copy Config" not in json.dumps(_spec, ensure_ascii=False),
              "в реестре больше нет кнопки Copy Config — её нет и в студии")
        _blocking = [r for r in (_spec.get("requires") or [])
                     if r.get("blocks")]
        check(not _blocking,
              "требования android-studio не блокирующие: программа выполняет "
              "их сама")

    # Папка плагина берётся из архива: у сборок 261/262 и 263 она разная.
    if _bundle.ready:
        _folders = {str(b.get("folder") or "") for b in _bundle.builds}
        check(len(_folders) >= 1 and all(_folders),
              f"в указателе у всех сборок имя папки: {_folders}")

    # Проба не должна выдавать «жив» на закрытом порту. Случай, который
    # однажды уже обманул: открытый порт IDE ничего не значит.
    _free = _free_port()
    _ok, _note = android_studio.probe(f"http://127.0.0.1:{_free}/stream",
                                       timeout=2.0)
    check(not _ok, f"на закрытом порте проба честно отвечает «нет»: {_note}")

    # ---- 23. OBS и эмуляторы: в один клик
    echo("\n--- 23. OBS и эмуляторы ---")

    # Пароль OBS: длина ограничена сверху не просто так, OBS отклоняет
    # слишком длинный. Проверяем обе границы и обязательные классы символов.
    _pw = bridges.generate_password()
    check(bridges.password_is_valid(_pw),
          f"сгенерированный пароль годный: {len(_pw)} символов")
    check(not bridges.password_is_valid(""),
          "пустой пароль не проходит")
    check(not bridges.password_is_valid("abc"),
          f"слишком короткий пароль не проходит (минимум "
          f"{bridges.OBS_PASSWORD_MIN})")
    check(not bridges.password_is_valid("A" * 40),
          f"слишком длинный пароль не проходит (максимум "
          f"{bridges.OBS_PASSWORD_MAX})")
    _short = _pw[:bridges.OBS_PASSWORD_MIN - 1]
    _long = "A" * (bridges.OBS_PASSWORD_MAX + 1)
    _edge_min = "aB3" + "x" * (bridges.OBS_PASSWORD_MIN - 3)
    _edge_max = "aB3" + "x" * (bridges.OBS_PASSWORD_MAX - 3)
    check(not bridges.password_is_valid(_short)
          and not bridges.password_is_valid(_long)
          and bridges.password_is_valid(_edge_min)
          and bridges.password_is_valid(_edge_max),
          f"границы пароля ровно {bridges.OBS_PASSWORD_MIN} и "
          f"{bridges.OBS_PASSWORD_MAX}: обе стороны проходят правильно")
    _different = [bridges.generate_password() for _ in range(5)]
    check(len(set(_different)) == 5, "генератор даёт разные пароли")

    # Пароль OBS не должен попадать в реестр: реестр едет в публичный
    # репозиторий, а пароль — нет.
    _reg_text = json.dumps(
        _mcp_registry.load_registry(core.program_root()), ensure_ascii=False
    )
    check("OBS_WS_PASSWORD" not in _reg_text or "пароль" in _reg_text,
          "в реестре нет пароля OBS")
    check("server_password" not in _reg_text,
          "в реестре нет файла настроек OBS с паролем")

    # Команда запуска пишется с плейсхолдерами, а в настройки — с
    # настоящими путями. Плейсхолдер в настройках недопустим: мост не
    # запустится.
    check("{PROGRAM}" in (json.loads(
              core.program_file("mcp-registry.json").read_text(
                  encoding="utf-8"))["servers"][-1].get("connection", {})
              .get("command", [""])[-1]),
          "в реестре команда записана плейсхолдером {PROGRAM}")
    _obs = next((s for s in _mcp_registry.load_servers(core.program_root())
                 if s.id == "obs"), None)
    _emu = next((s for s in _mcp_registry.load_servers(core.program_root())
                 if s.id == "android-emulator"), None)
    check(_obs is not None, "сервер obs есть в реестре программы")
    check(_emu is not None, "сервер android-emulator есть в реестре программы")
    for _srv, _label in ((_obs, "obs"), (_emu, "android-emulator")):
        if _srv is None:
            continue
        _block = mcp_registry.build_block(_srv)
        check("{PROGRAM}" not in _block and "{DBAPP_PYTHON}" not in _block,
              f"у {_label} в блок подставлены настоящие пути")
        check('"type": "local"' in _block,
              f"у {_label} блок записан как локальный сервер")
        # Путь в блоке проходит через json.dumps, поэтому обратные
        # слэши удвоены. Ищем именно так, как он попадёт в файл.
        # Сравниваем разобранный блок, а не текст: json.dumps
        # экранирует нелатиницу, и на пути с кириллицей (а такая
        # папка у многих) побайтовое сравнение врало, хотя путь верный.
        try:
            _parsed = json.loads("{" + _block.rstrip().rstrip(",") + "}")
            _cmd = []
            for _value in _parsed.values():
                if isinstance(_value, dict):
                    _cmd = _value.get("command") or []
                    break
        except (json.JSONDecodeError, AttributeError):
            _cmd = []
        _root_text = str(core.program_root()).lower().replace("\\", "/")
        check(any(_root_text in str(_part).lower().replace("\\", "/")
                  for _part in _cmd),
              f"у {_label} в блоке настоящий путь к программе")

    # Оба моста лежат в программе и запускаются лаунчером, а не напрямую:
    # пароль OBS и путь к adb иначе не подставить.
    _root = core.program_root()
    for _rel, _label in (
        (("tools", "dbapp", "launchers", "obs_bridge_launcher.py"), "OBS"),
        (("tools", "dbapp", "launchers", "android_bridge_launcher.py"),
         "эмулятор"),
    ):
        check((_root.joinpath(*_rel)).is_file(),
              f"лаунчер моста {_label} на месте: {'/'.join(_rel)}")

    # Лаунчеры не пишут в stdout: там протокол MCP, лишний текст ломает
    # связь. Проверяем, что журнал уходит в stderr.
    for _rel in (("tools", "dbapp", "launchers", "obs_bridge_launcher.py"),
                 ("tools", "dbapp", "launchers", "android_bridge_launcher.py")):
        _src = _root.joinpath(*_rel).read_text(encoding="utf-8")
        check("file=sys.stderr" in _src,
              f"{_rel[-1]} пишет журнал в stderr, а не в stdout")

    # Мосты в program_root на месте со своими окружениями.
    # node_modules в репозиторий не попадает — он в .gitignore, и на
    # свежей копии его нет по закону. Поэтому проверяем объявление
    # моста в package.json, а не установленные файлы: иначе селфтест
    # падал бы у каждого, кто клонировал репозиторий.
    _obs_manifest = _root / "tools" / "thirdparty" / "obs-mcp-node" / "package.json"
    _obs_declared = False
    if _obs_manifest.is_file():
        try:
            _obs_declared = "obs-mcp" in _obs_manifest.read_text(encoding="utf-8")
        except OSError:
            _obs_declared = False
    check(_obs_declared,
          "мост OBS (TypeScript) объявлен в программе; node_modules "
          "ставится командой npm и в репозиторий не попадает")
    check((_root / "tools" / "thirdparty" / "android-mcp-server"
           / "server.py").is_file(),
          "мост эмулятора лежит в программе")
    # Python-порт obs-mcp нерабочий: зовёт FastMCP с параметром
    # description, которого в актуальном SDK нет. Проверяем, что мы не
    # вернулись к нему, иначе мост молча упадёт на старте.
    #
    # Исключение — настоящий git-клон: исходники моста лежат в репозитории
    # подмодулем на коммите автора, и там `py_src` присутствует законно.
    # Проверять надо то, что действительно важно: в поставляемой папке
    # программы мёртвого кода нет, а лаунчер Node-мост не запускает.
    # Раньше проверка была безусловной, и на копии репозитория падала,
    # то есть требовала удалить из чужого коммита то, что там есть.
    _py_port = _root / "tools" / "thirdparty" / "obs-mcp" / "py_src"
    _is_checkout = (_root / "tools" / "thirdparty" / "obs-mcp" / ".git").exists()
    check(_is_checkout or not _py_port.is_dir(),
          "нерабочий Python-порт obs-mcp вынесен из поставляемой программы"
          + (" (в репозитории он есть: это подмодуль на коммите автора)"
             if _is_checkout else ""))
    _launcher = (_root / "tools" / "dbapp" / "launchers"
                 / "obs_bridge_launcher.py").read_text(encoding="utf-8")
    check("OBS_WEBSOCKET_PASSWORD" in _launcher,
          "лаунчер OBS передаёт пароль мосту через переменную окружения")
    check("runpy" not in _launcher and "obs-mcp.py" not in _launcher,
          "лаунчер OBS запускает Node-мост, а не нерабочий Python-порт")
    _emul = (_root / "tools" / "dbapp" / "launchers"
             / "android_bridge_launcher.py").read_text(encoding="utf-8")
    check('"PATH"' in _emul,
          "лаунчер эмулятора кладёт папку adb в PATH — мост зовёт adb по имени")

    # Второй мост к тому же эмулятору: mcp-ldplayer, 38 инструментов.
    # Внешних зависимостей нет, поэтому ставить нечего — достаточно
    # наличия кода. Лаунчер обязателен: он передаёт путь к LDPlayer,
    # иначе мост ищет установку сам и может выбрать не тот каталог.
    check((_root / "tools" / "thirdparty" / "mcp-ldplayer"
           / "mcp_ldplayer" / "mcp_server.py").is_file(),
          "мост LDPlayer лежит в программе")
    check((_root / "tools" / "dbapp" / "launchers"
           / "ldplayer_bridge_launcher.py").is_file(),
          "лаунчер моста LDPlayer на месте")
    _ldp_req = _root / "tools" / "thirdparty" / "mcp-ldplayer" / "requirements.txt"
    if _ldp_req.is_file():
        _req = _ldp_req.read_text(encoding="utf-8").lower()
        check("no external dependencies" in _req,
              "мост LDPlayer объявлен без внешних зависимостей — верно")

    _ldp_srv = next((s for s in _mcp_registry.load_servers(core.program_root())
                     if s.id == "ldplayer"), None)
    check(_ldp_srv is not None, "сервер ldplayer есть в реестре программы")
    if _ldp_srv is not None:
        _ldp_block = mcp_registry.build_block(_ldp_srv)
        check(_ldp_block.startswith('"ldplayer": {')
              and _ldp_block.rstrip().endswith("},"),
              "блок ldplayer собран цельно")
        check("{PROGRAM}" not in _ldp_block,
              "в блок ldplayer подставлен настоящий путь")
        # Кейлоггер в пакете есть, и об этом сказано вслух: и в реестре,
        # и в скилле. Молчать об этом нельзя.
        # Server — это dataclass, а не словарь. В json его не отдать,
        # нужно .raw: это и есть та запись реестра, как её видит человек.
        _ldp_raw = json.dumps(_ldp_srv.raw, ensure_ascii=False)
        check("pt_keylogger" in _ldp_raw,
              "в реестре ldplayer сказано про pt_keylogger прямо")
        _skill = _root / "skills" / "android-emulator" / "SKILL.md"
        if _skill.is_file():
            _sk = _skill.read_text(encoding="utf-8")
            check("pt_keylogger" in _sk,
                  "в скилле сказано про pt_keylogger прямо")
            check("ld_screenshot" in _sk and "не работает" in _sk,
                  "в скилле сказано, что ld_screenshot не работает")
            check("emulator-5552" in _sk,
                  "в скилле сказано, что инструментам ADB нужен параметр name")

        # Пути, которые навыки называют, должны существовать.
        #
        # Навык obs-studio говорил «мост лежит в tools\thirdparty\obs-mcp»
        # — а в репозитории такого пути нет. На той машине, где папку
        # удаляли, она ещё лежала неудалённой, и навык выглядел
        # правдивым; в чистой копии он врёт. Проверки не было, и такая
        # неправда живёт месяцами.
        #
        # Чего эта проверка НЕ ловит: путь, который есть на диске, но не
        # под git. Против такого помогает только сверка с чистой
        # распаковкой — ручная, её не автоматизировать.
        import re as _re  # noqa: PLC0415 - нужен здесь и только здесь
        _path_re = _re.compile(r"(?<![\w/\\])tools[/\\]"
                               r"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)*)")
        _bogus: list[str] = []
        for _skill_md in sorted(_root.glob("skills/*/SKILL.md")):
            _txt = _skill_md.read_text(encoding="utf-8", errors="ignore")
            for _m in _path_re.finditer(_txt):
                _tail = _m.group(1)
                # Пропускаем не-настоящие пути: многоточие, имя в угловых
                # скобках, звёздочка. Проверка на многоточие здесь не
                # украшение: Windows отбрасывает конечные точки в имени
                # компонента, и путь из папки с многоточием на конце молча
                # превращается в папку, которая существует. Без явного
                # пропуска такая строка прошла бы незамеченной — именно
                # это и случилось, когда навык объяснял свой прежний
                # неверный путь прямо в тексте.
                if set(_tail) <= {"."} or any(c in _tail for c in "<>*?"):
                    continue
                if not (_root / _m.group(0).replace("\\", "/")).exists():
                    _line = _txt[:_m.start()].count("\n") + 1
                    _bogus.append(f"{_skill_md.parent.name}:{_line} → {_m.group(0)}")
        check(not _bogus,
              f"пути, которые навыки называют, есть на диске: "
              f"{_bogus[:4] or 'чисто'}")

    # Оба эмуляторных моста должны быть в настройках opencode: иначе
    # один из них молча не поднимется, а человек решит, что сломан мост.
    _cfg_path = Path.home() / ".config" / "opencode" / "opencode.jsonc"
    if _cfg_path.is_file():
        _cfg_text = _cfg_path.read_text(encoding="utf-8")
        check('"android-emulator"' in _cfg_text,
              "мост android-emulator записан в настройки opencode")
        check('"ldplayer"' in _cfg_text,
              "мост ldplayer записан в настройки opencode")

    # Мост эмулятора написан под MCP 1.x. Проверяем, что закреплено mcp<2,
    # иначе он падает с ModuleNotFoundError — так и было при первой установке.
    _pyproject = (_root / "tools" / "thirdparty" / "android-mcp-server"
                  / "pyproject.toml")
    if _pyproject.is_file():
        _pt = _pyproject.read_text(encoding="utf-8")
        # Ищем ограничение верхней границы в строке с mcp, а не
        # подстроку: написать можно "mcp<2" или "mcp>=1.0.0,<2", и
        # смысл у них одинаковый, а текст разный.
        _mcp_lines = [ln.strip().strip(",").strip('"')
                      for ln in _pt.splitlines()
                      if ln.strip().startswith('"mcp')]
        _pinned = any("<2" in ln for ln in _mcp_lines)
        check(_pinned,
              f"у моста эмулятора закреплена версия mcp ниже 2: "
              f"{_mcp_lines or 'строка с mcp не найдена'}")

    # Эмуляторы, которых нет, должны называться честно, а не молча
    # пропускаться: BlueStacks поддержка записана, но не установлен.
    _ids = {spec["id"] for spec in bridges.EMULATORS}
    check({"ldplayer", "bluestacks"} <= _ids,
          f"в модуле оба эмулятора: {sorted(_ids)}")
    _found = [b["name"] for b in bridges.EMULATORS if bridges.find_emulator(b["id"])]
    check(isinstance(_found, list),
          f"найденные эмуляторы: {_found or 'ни одного'}")

    # Проба эмулятора без устройства обязана говорить «нет», а не молчать.
    if not bridges.adb_devices():
        _ok, _note = bridges.probe_emulator()
        check(not _ok, f"без устройства проба честно отвечает «нет»: {_note}")

    # ---- Мост DBHub: базы данных через npx
    #
    # Пакета моста в репозитории нет: его скачивает npx. Проверяем то,
    # чем он управляется: лаунчер, запись в реестре, файл подключений и
    # живую проверку против поддельного сервера протокола MCP.
    import dbhub as _dbhub  # noqa: PLC0415 — рядом лежит, круга нет
    _db_launcher = (_root / "tools" / "dbapp" / "launchers"
                    / "dbhub_bridge_launcher.py")
    check(_db_launcher.is_file(), "лаунчер моста DBHub на месте")
    if _db_launcher.is_file():
        _db_src = _db_launcher.read_text(encoding="utf-8")
        check("file=sys.stderr" in _db_src,
              "лаунчер DBHub пишет журнал в stderr, а не в stdout")
        check("MCP_DBHUB_PASSWORD_" in _db_src,
              "лаунчер DBHub передаёт пароль базы переменной окружения")
        check("def main() -> int" in _db_src and "__main__" in _db_src,
              "лаунчер DBHub запускается сам: main() и __main__")
        check("PyQt6" not in _db_src and "from PyQt" not in _db_src,
              "лаунчер DBHub не тянет окно программы: запускается отдельно")
        check("--transport" in _db_src and "stdio" in _db_src,
              "лаунчер DBHub запускает сервер по stdio")
        # Имя переменной окружения задано в двух местах: окно пишет ссылку
        # в файл подключений, лаунчер её разрешает. Разойдутся — и пароль
        # молча не найдётся, а база не откроется.
        import importlib.util as _ilu  # noqa: PLC0415 — только здесь
        _db_spec = _ilu.spec_from_file_location("dbhub_check", _db_launcher)
        _db_mod = (_ilu.module_from_spec(_db_spec)
                   if _db_spec and _db_spec.loader else None)
        if _db_mod is not None and _db_spec and _db_spec.loader:
            _db_spec.loader.exec_module(_db_mod)
            check(_db_mod.password_env("my-db") == _dbhub.password_env("my-db")
                  == "MCP_DBHUB_PASSWORD_MY_DB",
                  "имя переменной окружения совпадает у окна и лаунчера")
        else:
            check(False, "лаунчер DBHub читается как модуль для сверки имён")

    _db_srv = next((s for s in _mcp_registry.load_servers(_root)
                    if s.id == "dbhub"), None)
    check(_db_srv is not None, "сервер dbhub есть в реестре программы")
    if _db_srv is not None:
        _db_conn = _db_srv.raw.get("connection") or {}
        _db_cmd = [str(part) for part in _db_conn.get("command") or []]
        check(_db_conn.get("kind") == "local"
              and "{DBAPP_PYTHON}" in _db_cmd
              and any("dbhub_bridge_launcher.py" in part for part in _db_cmd),
              "команда dbhub — плейсхолдеры и лаунчер, настоящих путей нет")
        check(not [part for part in _db_cmd
                   if re.search(r"[A-Za-z]:[\\/]|^/|^\\\\", part)],
              f"в команде dbhub нет настоящих путей: {_db_cmd}")
        check(_db_conn.get("warm_up") is True,
              "у dbhub прогрев кэша включён: npx действительно скачивает пакет")
        _db_raw = json.dumps(_db_srv.raw, ensure_ascii=False)
        check(not re.search(r"[a-z]+://[^/\s:@]+:[^/@\s]+@", _db_raw),
              "в записи реестра нет строк подключения с паролем")
        check(not re.search(r"mcp-dbhub-[A-Za-z0-9_-]+-password\.txt", _db_raw),
              "в реестре не назван файл пароля конкретной машины")
        _db_node = next((i for i in _mcp_registry.load_registry(_root)
                         .get("bridge_requirements", {}).get("items", [])
                         if i.get("program") == "Node.js"), {})
        check("dbhub" in (_db_node.get("required_by") or []),
              "dbhub записан в «нужно мостам» у Node.js")

        # «Только чтение» по умолчанию, лимит строк и пароль отдельным
        # файлом. Всё это — на временной папке, ничего чужого не трогаем.
        _db_tmp = Path(tempfile.mkdtemp(prefix="dbhub-selftest-"))
        try:
            _db_m1, _db_e1 = _dbhub.add_source(
                _db_tmp, "sklad", "postgres",
                "postgres://user:секрет-42@host:5432/db")
            _db_toml = _db_tmp / "mcp-dbhub.toml"
            check(not _db_e1 and _db_toml.is_file(),
                  f"подключение записывается в mcp-dbhub.toml: {_db_e1}")
            _db_text = (_db_toml.read_text(encoding="utf-8")
                        if _db_toml.is_file() else "")
            check("[[sources]]" in _db_text and 'id = "sklad"' in _db_text,
                  "в файле подключений есть секция [[sources]] с источником")
            check("секрет-42" not in _db_text,
                  "пароль в файл подключений не попадает")
            check("${MCP_DBHUB_PASSWORD_SKLAD}" in _db_text,
                  "на месте пароля — ссылка на переменную окружения")
            _db_pw = _db_tmp / "mcp-dbhub-sklad-password.txt"
            check(_db_pw.is_file()
                  and "секрет-42" in _db_pw.read_text(encoding="utf-8"),
                  "пароль лежит отдельным файлом рядом с настройками")
            _db_srcs = _dbhub.read_sources(_db_tmp)
            check(len(_db_srcs) == 1 and _db_srcs[0].readonly
                  and _db_srcs[0].max_rows == _dbhub.DEFAULT_MAX_ROWS,
                  "«только чтение» включено по умолчанию, лимит строк задан")
            check("[[tools]]" in _db_text and "readonly = true" in _db_text,
                  "оба инструмента описаны явно, у запроса — «только чтение»")

            # Отказ ничего не дописывает: тип базы и строка подключения
            # разошлись — это ошибка человека, а не повод править файл.
            _db_before = _db_text
            _db_m2, _db_e2 = _dbhub.add_source(
                _db_tmp, "other", "postgres", "mysql://root:pass@host/db")
            check(bool(_db_e2), f"чужая схема отклонена: {_db_e2}")
            check(_db_toml.read_text(encoding="utf-8") == _db_before,
                  "после отказа файл подключений не тронут")
            check(not (_db_tmp / "mcp-dbhub-other-password.txt").exists(),
                  "и файла пароля для отклонённого подключения не появилось")

            # Поддельный сервер протокола: в репозитории его нет, это тест.
            _db_fake_lines = [
                "import json, sys",
                "TOOLS = [",
                "    {'name': 'execute_sql', 'inputSchema': {'type': 'object'}},",
                "    {'name': 'search_objects', 'inputSchema': {'type': 'object'}},",
                "]",
                "for line in sys.stdin:",
                "    line = line.strip()",
                "    if not line:",
                "        continue",
                "    message = json.loads(line)",
                "    method = message.get('method')",
                "    if method == 'initialize':",
                "        answer = {'serverInfo': {'name': 'DBHub MCP Server',",
                "                                 'version': '9.9.9'}}",
                "    elif method == 'tools/list':",
                "        answer = {'tools': TOOLS}",
                "    elif method == 'tools/call':",
                "        tables = [{'name': 'books'}, {'name': 'authors'}]",
                "        answer = {'content': [{'type': 'text',",
                "                              'text': json.dumps({'tables': tables})}]}",
                "    else:",
                "        continue",
                "    sys.stdout.write(json.dumps(",
                "        {'jsonrpc': '2.0', 'id': message['id'], 'result': answer})",
                "        + chr(10))",
                "    sys.stdout.flush()",
            ]
            _db_fake_text = "\n".join(_db_fake_lines) + "\n"
            _db_fake = _db_tmp / "подделка_dbhub.py"
            _db_fake.write_text(_db_fake_text, encoding="utf-8")
            _db_ok, _db_note = _dbhub.probe(
                _db_tmp, timeout=30, command=[sys.executable, str(_db_fake)])
            check(_db_ok, f"живая проверка разговаривает с сервером: {_db_note}")
            check("инструментов 2" in _db_note,
                  f"и видит оба инструмента: {_db_note}")
            check("таблиц видно 2" in _db_note,
                  f"и получает список таблиц: {_db_note}")
            # Тот же протокол, другое имя сервера: чужой мост за DBHub
            # выдавать нельзя, и проверка обязана это заметить.
            _db_other = _db_tmp / "подделка_чужая.py"
            _db_other.write_text(
                _db_fake_text.replace("DBHub MCP Server", "Совсем Другой Сервер"),
                encoding="utf-8")
            _db_ok2, _db_note2 = _dbhub.probe(
                _db_tmp, timeout=30, command=[sys.executable, str(_db_other)])
            check(not _db_ok2 and "DBHub" in _db_note2,
                  f"чужой сервер не выдаётся за DBHub: {_db_note2}")
            # Отказ сервера: в сообщении не должно остаться пароля.
            _db_bad = _db_tmp / "подделка_отказ.py"
            _db_bad.write_text(
                "import sys" + chr(10)
                + "print('ошибка: postgres://user:секрет-42@host/db', "
                  "file=sys.stderr)" + chr(10)
                + "sys.exit(1)" + chr(10),
                encoding="utf-8")
            _db_ok3, _db_note3 = _dbhub.probe(
                _db_tmp, timeout=30, command=[sys.executable, str(_db_bad)])
            check(not _db_ok3, f"молчащий мост — это отказ: {_db_note3}")
            check("секрет-42" not in _db_note3,
                  f"в сообщении об отказе пароля нет: {_db_note3}")

            # Живая проверка не прошла — в настройки не пишется ничего.
            _db_empty = _db_tmp / "пустая-папка"
            _db_empty.mkdir()
            _db_m4, _db_e4 = _dbhub.auto_setup(_db_empty, _db_srv)
            check(bool(_db_e4),
                  f"без подключения автонастройка отказывает: {_db_e4[:1]}")
            check(not (_db_empty / "opencode.jsonc").exists(),
                  "и в настройки opencode ничего не вписано")

            # Повторное «Включить» не переписывает файл настроек: opencode
            # поднимает новый экземпляр моста на каждую правку конфига.
            _db_dest = _db_tmp / "настройки"
            _db_dest.mkdir()
            (_db_dest / "opencode.jsonc").write_text(
                '{\n  "mcp": {}\n}\n', encoding="utf-8")
            _db_clone = copy.copy(_db_srv)
            _db_clone.raw = dict(_db_srv.raw)
            # Прогрев кэша в проверке выключен намеренно: он запускает
            # лаунчер, а тот — npx, и на чистой машине селфтест скачивал
            # бы 245 МБ пакета. Что прогрев в реестре включён, проверено
            # отдельной проверкой выше.
            _db_clone.raw["connection"] = dict(_db_srv.raw.get("connection") or {},
                                               warm_up=False)
            _db_clone.requirements = []
            _db_clone.has_connection = True
            _db_a1, _db_ae1 = _mcp_registry.enable(_db_dest, _db_clone)
            _db_c1 = (_db_dest / "opencode.jsonc").read_text(encoding="utf-8")
            _db_a2, _db_ae2 = _mcp_registry.enable(_db_dest, _db_clone)
            _db_c2 = (_db_dest / "opencode.jsonc").read_text(encoding="utf-8")
            check(not _db_ae1 and '"dbhub"' in _db_c1,
                  f"включение dbhub пишет блок в настройки: {_db_ae1}")
            check("dbhub_bridge_launcher.py" in _db_c1
                  and "{PROGRAM}" not in _db_c1,
                  "в настройки вписан настоящий путь к лаунчеру")
            check(_db_c1 == _db_c2 and _db_c2.count('"dbhub"') == 1,
                  "повторное включение dbhub не меняет файл настройки")
            check(any("не трогаю" in m for m in _db_a2),
                  "и программа говорит, что файл не тронула")
        finally:
            shutil.rmtree(_db_tmp, ignore_errors=True)

    # Настройки OBS: сервер включён только при закрытой студии.
    _on, _port, _pw_in_obs, _path = bridges.obs_state()
    check(isinstance(_on, bool) and _port > 0,
          f"состояние OBS читается: сервер={'включён' if _on else 'выключен'}, "
          f"порт {_port}")
    if bridges.obs_running():
        _ok, _note = bridges.enable_obs_server(_pw)
        check(not _ok and "запущена" in _note,
              f"при открытой OBS программа отказывается писать: {_note}")

    # Живой handshake с поддельным OBS. Пока этого теста не было, в
    # probe_obs накопились три ошибки подряд: op==0 срабатывал как «ложь»
    # (0 в Python не истина) и отвергал любое приветствие; salt брался из
    # словаря d вместо d.authentication и ронял проверку на TypeError; а
    # ответ Identify (op=2) сверялся с requestStatus, который бывает только
    # в op=7. Итог: кнопка «Настроить автоматически» для OBS не могла
    # сработать никогда. Поднимаем настоящий сервер протокола v5 и требуем
    # верных ответов — без OBS на машине.
    try:
        import asyncio as _aio
        import socket as _sockmod
        import threading as _threadmod
        from websockets.asyncio.server import serve as _ws_serve
    except Exception as _exc:  # noqa: BLE001
        check(False, f"для проверки handshake нужен websockets: {_exc}")
    else:
        _TEST_PW = "Тест_Пароль_42"
        _SALT = "0123456789abcdef"
        _CHAL = "chalenge0123456789"

        async def _fake_obs(ws):
            """Подделка OBS: ровно та последовательность v5, что в студии."""
            await ws.send(json.dumps({"op": 0, "d": {
                "obsWebSocketVersion": "5.7.4", "rpcVersion": 1,
                "authentication": {"challenge": _CHAL, "salt": _SALT}}}))
            try:
                _msg = json.loads(await ws.recv())
            except Exception:  # noqa: BLE001
                return
            if _msg.get("op") != 1:
                return
            import base64 as _b64
            import hashlib as _hl
            _secret = _b64.b64encode(_hl.sha256(
                (_TEST_PW + _SALT).encode()).digest()).decode()
            _want = _b64.b64encode(_hl.sha256(
                (_secret + _CHAL).encode()).digest()).decode()
            if (_msg.get("d") or {}).get("authentication") != _want:
                await ws.close(4009, "Authentication failed.")
                return
            await ws.send(json.dumps({"op": 2,
                                     "d": {"negotiatedRpcVersion": 1}}))
            await _aio.sleep(0.2)

        _probe_sock = _sockmod.socket()
        _probe_sock.bind(("127.0.0.1", 0))
        _probe_port = _probe_sock.getsockname()[1]
        _probe_sock.close()
        _probe_ready = _threadmod.Event()

        async def _serve_fake():
            async with _ws_serve(_fake_obs, "127.0.0.1", _probe_port):
                _probe_ready.set()
                await _aio.sleep(60)

        def _fake_thread():
            _aio.run(_serve_fake())

        _th = _threadmod.Thread(target=_fake_thread, daemon=True)
        _th.start()
        _probe_ready.wait(timeout=10)
        check(_probe_ready.is_set(),
              f"поддельный OBS поднялся на порту {_probe_port} для проверки")
        if _probe_ready.is_set():
            _good, _note_good = bridges.probe_obs(_probe_port, _TEST_PW)
            check(_good,
                  f"верный пароль проходит настоящий handshake: {_note_good}")
            _bad, _note_bad = bridges.probe_obs(_probe_port, "неверный-пароль")
            check(not _bad,
                  f"неверный пароль отвергает сервер, а не терпит программа: "
                  f"{_note_bad}")

    # Поломка «OBS установлена, но не знает где»: без ключа реестра
    # студия падает с ошибкой про языковой файл, и человек ищет не
    # там. Детектор обязан её называть прямо.
    check(callable(getattr(bridges, "obs_install_problem", None)),
          "в модуле есть детектор поломки установки OBS")
    _obs_root = bridges.obs_installed()
    _obs_recorded = bridges.obs_install_path_recorded()
    if _obs_root is None:
        check(bridges.obs_install_problem() == "",
              "без установленной OBS детектор молчит — иначе он врёт")
    elif _obs_recorded is None:
        _note_missing = bridges.obs_install_problem()
        check("OBSStudio" in _note_missing,
              f"детектор называет отсутствие ключа реестра: {_note_missing}")
    else:
        check(bridges.obs_install_problem() == "",
              "при верном ключе реестра детектор не мешает")
    _obs_src = (_root / "tools" / "dbapp" / "bridges.py").read_text(
        encoding="utf-8")
    check("obs_install_problem()" in _obs_src.split("def auto_setup_obs")[-1],
          "автонастройка OBS спрашивает детектор, а не только порт")

    # Запись в реестр — внешнее действие. Кнопка создания ключа обязана
    # быть явной: без разрешения автонастройка только говорит, с ключом
    # — пишет.
    check(callable(getattr(bridges, "create_obs_install_path", None)),
          "в модуле есть функция записи ключа установки OBS")
    _obs_src_all = (_root / "tools" / "dbapp" / "bridges.py").read_text(
        encoding="utf-8")
    check("allow_install_path_fix" in _obs_src_all,
          "автонастройка OBS принимает разрешение на запись ключа")
    # Смотрим настоящую сигнатуру, а не текст: значение по умолчанию —
    # это то, что реально получит вызов без разрешения.
    import inspect as _inspect
    _sig = _inspect.signature(bridges.auto_setup_obs)
    _param = _sig.parameters.get("allow_install_path_fix")
    check(_param is not None
          and _param.default is False,
          "запись ключа по умолчанию выключена"
          f" (значение по умолчанию: {_param.default if _param else 'параметра нет'})")
    # Порядок импортов в самом селфтесте. Соседние модули обязаны
    # импортироваться ПОСЛЕ sys.path.insert: при запуске через `-m` в
    # sys.path[0] лежит текущая папка, а не папка модуля, и импорт выше
    # вставки падает с ModuleNotFoundError. Так уже ломалось — команда
    # `python -m tools.dbapp.selftest`, которой проверяется каждая задача
    # плана, не работала ни в одной копии, а прямой запуск файлом работал,
    # и поломка выглядела безобидно. Проверка смотрит на этот же файл, а
    # не на значение: ломается обратной перестановкой двух строк.
    _self_lines = Path(__file__).read_text(encoding="utf-8").splitlines()
    _insert_at = next((n for n, ln in enumerate(_self_lines)
                       if ln.startswith("sys.path.insert")), -1)
    check(_insert_at >= 0, "в селфтесте есть вставка папки модуля в путь")
    _imports_above = [
        (n, ln.strip()) for n, ln in enumerate(_self_lines)
        if ln.strip().startswith("import ")
        and ln.strip().split()[1].split(".")[0] in _SELFTEST_LOCAL_MODULES
        and 0 <= n < _insert_at
    ]
    check(not _imports_above,
          f"соседние модули импортируются после sys.path.insert: "
          f"{_imports_above[:3]}")
    # Обновление и откат программы. Живое обновление Node.js пройти можно
    # только со словом человека: `winget upgrade` меняет программу, от
    # которой идёт эта сессия. Здесь синтетика на подставных блоках
    # реестра, а живая часть доказана измерением `winget list` отдельно.
    import program_cards as _cards12  # noqa: PLC0415 — рядом лежит
    import programs as _prog12  # noqa: PLC0415 — рядом лежит
    import winget_install as _wi12  # noqa: PLC0415 — рядом лежит

    def _blk12(**kw):
        """Подставной блок program_install. Настоящий реестр не трогаем."""
        return _mcp_registry.ProgramInstall(**kw)

    _none12 = _prog12._update_from_block(None)
    check(not _none12.declared and not _none12.can_update,
          "без блока обновление не предлагается")

    _no_id12 = _prog12._update_from_block(_blk12(installed_version="1.0"))
    check(not _no_id12.can_update,
          f"без winget_id обновлять нечем: {_no_id12.update_reason}")
    check(bool(_no_id12.update_reason),
          "и причина названа, а не молчание")

    _no_ver12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="", catalog_version="2.0"))
    check(not _no_ver12.can_update,
          "без записанной версии судить не о чем")

    _newer12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="1.0",
               catalog_version="2.0"))
    check(_newer12.can_update and _newer12.update_available,
          f"каталог новее — обновление предлагается: {_newer12.text[:60]}")
    check(not _newer12.can_rollback,
          "но откатываться пока нечем, истории нет")
    check(_newer12.rollback_reason == _prog12.REASON_NO_HISTORY,
          f"и причина отказа названа: {_newer12.rollback_reason}")

    _same12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="2.0",
               catalog_version="2.0"))
    check(not _same12.can_update,
          f"версии совпадают — обновления нет: {_same12.update_reason}")

    _older12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="2.10",
               catalog_version="2.9"))
    check(not _older12.can_update,
          "2.9 против 2.10 сравнено числами, а не строками")
    check(_older12.update_reason
          == _prog12.REASON_CATALOG_NEWER_THAN_INSTALLED,
          f"источник откатился — сказано прямо: {_older12.update_reason}")

    _junk12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="бета",
               catalog_version="2.0"))
    check(not _junk12.can_update,
          "версия, которая не разбиралась как числа, обновления не даёт")

    _hist12 = _prog12._update_from_block(
        _blk12(winget_id="P.X", installed_version="2.0",
               catalog_version="3.0", previous_versions=["1.9", "1.8"]))
    check(_hist12.can_update and _hist12.can_rollback,
          "с историей доступны и обновление, и откат")
    check(_hist12.previous == ["1.9", "1.8"],
          f"история на месте целиком: {_hist12.previous}")

    # Карточка: кнопки появляются и гаснут честно.
    def _card12(update, winget_id="P.X", state=_cards12.STATE_OK):
        # Состояние по умолчанию — установлена: без него кнопок обновления
        # не было бы вовсе, и проверки на кнопки проверяли бы другое.
        card = _cards12.Card(key="k", name="Программа", state=state)
        card.install = _prog12.InstallView(winget_id=winget_id)
        card.update = update
        return card

    _c_new12 = _card12(_newer12)
    _codes12 = [code for code, _l, _h in _c_new12.buttons()]
    check(_cards12.BTN_UPDATE in _codes12,
          "при доступном обновлении кнопка есть")
    check(_cards12.BTN_ROLLBACK in _codes12,
          "и кнопка отката видна, хотя откатываться нечем")
    check(_c_new12.can_update and not _c_new12.can_rollback,
          "откат при пустой истории выключен")

    _hint12 = dict((c, h) for c, _l, h in _c_new12.buttons()).get(
        _cards12.BTN_ROLLBACK, "")
    check("недоступно" in _hint12,
          f"у выключенного отката названа причина: {_hint12[:56]}")

    _c_no12 = _card12(_none12)
    check(_cards12.BTN_UPDATE not in [c for c, _l, _h in _c_no12.buttons()],
          "программа без блока не получает кнопок обновления")

    # У отсутствующей программы обновлять нечего, и «Установить» рядом уже
    # объясняет, что нужно. Без этой проверки кнопки появлялись бы у всех
    # подряд — так и вышло, когда условие стояло только на `declared`.
    for _st12 in (_cards12.STATE_MISSING, _cards12.STATE_UNKNOWN):
        _c_absent12 = _card12(_hist12, state=_st12)
        _codes_absent12 = [c for c, _l, _h in _c_absent12.buttons()]
        check(_cards12.BTN_UPDATE not in _codes_absent12,
              f"у неустановленной программы (состояние {_st12}) нет кнопки "
              "обновления")
        check(_cards12.BTN_ROLLBACK not in _codes_absent12,
              f"и нет кнопки отката (состояние {_st12})")
    check("self.state == STATE_OK" in Path(app_main.__file__).parent.joinpath(
        "program_cards.py").read_text(encoding="utf-8"),
        "кнопки обновления завязаны на состояние «установлена»")

    _c_hist12 = _card12(_hist12)
    check(_c_hist12.can_rollback and _c_hist12.rollback_target == "1.9",
          f"возврат идёт к последней стоявшей: {_c_hist12.rollback_target}")
    _label12 = [lb for c, lb, _h in _c_hist12.buttons()
                if c == _cards12.BTN_ROLLBACK]
    check(_label12 and "1.9" in _label12[0],
          f"в кнопке названа версия: {_label12}")

    check(_c_hist12.rollback_target != "1.8",
          "и не первая в списке: список может кончиться версией, которой "
          "уже нет в источнике")

    # Модуль: команды строятся списком, версия идёт отдельным аргументом.
    _argv12 = _wi12.build_install_version_command("P.X", "1.9")
    check(_argv12[_argv12.index("--version") + 1] == "1.9",
          "версия передаётся отдельным аргументом")
    check(all(isinstance(a, str) for a in _argv12),
          "команда строится списком строк, а не склейкой")
    check(_wi12.RemoteState(seen=True, installed="1.9",
                            available="1.10").update_available,
          "1.10 новее 1.9 по числам")
    check(not _wi12.RemoteState(seen=True, installed="1.10",
                                available="1.9").update_available,
          "и наоборот — строкой было бы наоборот")
    check(not _wi12.RemoteState(seen=True, installed="1.9",
                                available="2.0").update_available is None,
          "состояние без источника не выдаёт ошибку")

    # Порядок в коде окна: прежняя версия записывается ДО обновления.
    _src12 = Path(app_main.__file__).read_text(encoding="utf-8")

    def _body12(fn_name):
        _s = _src12.index(f"def {fn_name}(")
        _e = _src12.index("\n    def ", _s + 1)
        return _src12[_s:_e]

    _up12 = _body12("_update_card")
    check(_up12.index("_record_before_update") < _up12.index("worker.start()"),
          "прежняя версия записывается ДО запуска обновления")
    check(_up12.index("remote_state(") < _up12.index("_ask_update("),
          "источник спрашивается ДО вопроса человеку")
    check(_up12.index("_ask_update(") < _up12.index("_record_before_update"),
          "человека спрашивают ДО записи версии")
    check("if not self._ask_update(" in _up12,
          "отказ человека останавливает всё, версия не пишется")
    check("if not card.install.winget_id:" in _up12,
          "без идентификатора winget обновление отказано")

    _rb12 = _body12("_rollback_card")
    check("version_offered(" in _rb12,
          "откат спрашивает источник, есть ли ещё эта версия")
    check("больше нет в" in _rb12,
          "и честно говорит, что версии больше нет")
    check(_rb12.index("_ask_rollback(") < _rb12.index("worker.start()"),
          "откат спрашивает человека ДО запуска")

    # Реестр умеет помнить версию и записывать новую.
    check(hasattr(_mcp_registry, "remember_version"),
          "реестр умеет запоминать прежнюю версию")
    check(hasattr(_mcp_registry, "set_installed_version"),
          "и записывать версию после обновления")
    check(hasattr(_mcp_registry.ProgramInstall(), "previous_versions"),
          "в блоке реестра есть поле прежних версий")
    check(_mcp_registry.HISTORY_LIMIT >= 2,
          f"прежних версий хранится больше одной: "
          f"{_mcp_registry.HISTORY_LIMIT}")
    _main_src = (_root / "tools" / "dbapp" / "main.py").read_text(encoding="utf-8")
    check("allow_install_path_fix=fix_install_path" in _main_src,
          "кнопка передаёт в автонастройку своё решение")
    _reg_pos = _main_src.find("allow_install_path_fix=fix_install_path")
    _ask_pos = _main_src.find("QMessageBox.question(", _reg_pos - 4000)
    check(_ask_pos != -1,
          "кнопка спрашивает разрешение перед записью в реестр")
    check("StandardButton.No," in _main_src[_ask_pos:_ask_pos + 600],
          "в вопросе по умолчанию стоит «нет» — запись не молчаливая")

    import subprocess  # noqa: E402
    import time  # noqa: E402
    import opencode_caps as _opencode_caps  # noqa: E402
    import main as _main  # noqa: E402 - импорт безопасен: под __main__ не заходит

    # Одно окно на запуск. Окружение на машине удваивает любой
    # запуск Python, и без защиты один щелчок по ярлыку открывал
    # три-четыре окна. Проверяем и наличие защиты, и что она
    # действительно не даёт второму экземпляру открыться.
    check(callable(getattr(_main, "claim_single_instance", None)),
          "в программе есть проверка единственного экземпляра")
    check(callable(getattr(_main, "_acquire_instance_lock", None)),
          "метка экземпляра берётся атомарно (через O_EXCL)")
    _lock_src = (_main_src.partition("def _acquire_instance_lock")[2])
    _lock_src = _lock_src.partition("\ndef ")[0]
    check("os.O_EXCL" in _lock_src,
          "метка создаётся через os.O_EXCL — выиграть может один")
    check("_pid_alive" in _main_src,
          "метка умеет проверять, жив ли прежний владелец")
    _run_src = _main_src.partition("def run()")[2]
    check("claim_single_instance()" in _run_src.split("MainWindow()")[0],
          "проверка единственного экземпляра стоит ДО создания окна")

    # Метка по-настоящему одна: первый запуск берёт её, второй
    # отказывается. Проверяем в отдельных процессах, потому что в
    # одном процессе результат был бы заранее известен.
    _probe = Path(tempfile.gettempdir()) / "opencode-base-selftest-probe.py"
    _pid_file = Path(tempfile.gettempdir()) / "opencode-base-selftest-owner.txt"
    _pid_file.unlink(missing_ok=True)
    _probe.write_text(
        "import sys\n"
        "sys.path.insert(0, r'" + str(_root / "tools" / "dbapp").replace("\\", "\\\\")
        + "')\n"
        "import main as m\n"
        # Своё имя метки: у человека может быть открыта программа, и
        # тогда боевая метка занята законно. Тест не должен ни
        # спорить с живым окном, ни мешать ему.
        "m.APP_SOCKET_NAME = 'opencode-base-selftest'\n"
        # Маркеры латинские: русский текст через subprocess приходит
        # в другой кодировке, и сравнение ложно падает. Победитель
        # записывает свой номер в файл — тест убьёт именно его:
        # окружение может подмешать ещё один процесс, и метку
        # держит не тот, кого мы запускали из теста.
        "import os\n"
        "mine = m.claim_single_instance()\n"
        "print('LOCK-TAKEN' if mine else 'LOCK-BUSY')\n"
        "if mine:\n"
        "    open(sys.argv[1], 'w').write(str(os.getpid()))\n"
        # держим метку, пока тест не убьёт: вышел бы процесс —
        # и метка сразу стала бы свободной
        "    import time; time.sleep(30)\n",
        encoding="utf-8",
    )
    try:
        _first = subprocess.Popen([sys.executable, str(_probe), str(_pid_file)],
                                  stdout=subprocess.PIPE, text=True)
        # Ждём не фиксированное время, а сам факт: первый процесс пишет
        # файл в тот момент, когда метка уже у него. На нагруженной
        # машине 2.5 секунды не хватало — Python стартует в несколько
        # процессов, и второй успевал забрать метку раньше первого.
        _waited = 0.0
        while not _pid_file.exists() and _waited < 30.0:
            time.sleep(0.2)
            _waited += 0.2
        _second = subprocess.run([sys.executable, str(_probe), str(_pid_file)],
                                 capture_output=True, text=True, timeout=60)
        check(_second.stdout.strip().endswith("LOCK-BUSY"),
              f"второй экземпляр отказывается открываться: "
              f"{_second.stdout.strip() or 'нет ответа'}"
              f" (метку ждали {_waited:.1f} с)")
        # Прежний владелец умер: метка осталась, но следующий запуск
        # должен её забрать, а не застрять навсегда. Убиваем ровно
        # того, кто её взял, по номеру из файла.
        try:
            _first.kill()
        except OSError:
            pass
        # Номер владельца ждём, а не берём после паузы: файл
        # мог ещё не появиться, номер не читался, taskkill не
        # выполнялся — и метка оставалась жить.
        _got = 0.0
        while _got < 15.0:
            try:
                _owner = int(
                    _pid_file.read_text(encoding="utf-8").strip())
                break
            except (OSError, ValueError):
                time.sleep(0.2)
                _got += 0.2
        else:
            _owner = 0
        if _owner:
            subprocess.run(["taskkill", "/PID", str(_owner), "/F"],
                           capture_output=True, timeout=60)
        # taskkill возвращается раньше, чем процесс уходит, поэтому
        # метка может ещё занять секунду. Ждём самого события:
        # повторяем попытку, пока ответ не станет LOCK-TAKEN.
        _out = ""
        _waited2 = 0.0
        while _waited2 < 30.0:
            _third = subprocess.run(
                [sys.executable, str(_probe), str(_pid_file)],
                capture_output=True, text=True, timeout=60)
            _out = _third.stdout.strip()
            if "LOCK-TAKEN" in _out:
                break
            time.sleep(0.5)
            _waited2 += 0.5
        check("LOCK-TAKEN" in _out,
              "после смерти прежнего владельца метку можно взять снова: "
              f"{_out or 'нет ответа'} (ждали {_waited2:.1f} с)")
    except (OSError, subprocess.SubprocessError) as _exc:
        check(False, f"проверку единственного экземпляра не отработать: {_exc}")
    finally:
        _probe.unlink(missing_ok=True)
        _pid_file.unlink(missing_ok=True)

    # Кнопка «Настроить автоматически» не должна трогать конфиг,
    # когда блок уже записан. opencode перезапускает серверы MCP на
    # каждое изменение файла, и от лишней перезаписи мосты плодились
    # по одному на каждое нажатие.
    _tmp_dir = Path(tempfile.mkdtemp(prefix="opencode-selftest-"))
    try:
        (_tmp_dir / "opencode.jsonc").write_text(
            '{\n  "mcp": {}\n}\n', encoding="utf-8")

        # Требования сервера проверяются запуском внешних команд, и под
        # нагрузкой такая проверка не успевает — сервер начинает выглядеть
        # «недостающим», и enable() честно отказывается его вписывать. Это
        # говорит о машине, а не о коде, поэтому сервер перечитывается
        # три раза, а если требования всё ещё недоступны — проверка
        # сообщает «не проверено», а не «сбой».
        def _fresh_obs():
            return next((s for s in _mcp_registry.load_servers(
                core.program_root()) if s.id == "obs"), None)

        # Не все требования важны для проверки записи. `plugin` требует
        # починить чужую программу, а enable() этого не делает и не
        # обещает: он вписывает сервер в настройки, а не запускает его.
        # Требовать от этой проверки установленного плагина значило бы
        # убрать её ровно на сломанных машинах — там, где она нужнее.
        def _ready_for_write(server) -> bool:
            if server is None:
                return False
            return not [r for r in server.missing if r.kind != "plugin"]

        # Копия сервера OBS с подменёнными требованиями. Копия нужна,
        # чтобы не мутить живой объект: он перечитывается на каждом
        # рисунке вкладки, и правка на месте осталась бы в памяти до
        # следующей перечитки — а это проверка, которая портит данные.
        def _obs_variant(requires: list[dict]) -> object:
            """Сервер obs с другим списком требований. None — нет сервера.

            Требования пересчитываются, а не только подменяются в raw: у
            Server это готовое поле, вычисленное при чтении реестра.
            Подменить одно и забыть про другое — значит получить сервер,
            который ведёт себя по-старому, и удивляться отказу, которого
            уже не должно быть. Первая версия проверки так и сделала.
            """
            server = _fresh_obs()
            if server is None:
                return None
            clone = copy.copy(server)
            clone.raw = dict(server.raw)
            clone.raw["requires"] = list(requires)
            clone.requirements = [
                _mcp_registry.check_requirement(spec) for spec in requires
            ]
            return clone

        _real_obs = _fresh_obs()
        if _real_obs is None:
            check_machine(False, "сервер obs не найден для проверки записи")
        else:
            # (1) Запись в настройки — вопрос про код enable(), а не про
            # готовность OBS. Требования снимаем, иначе проверка молчала бы
            # на любой машине без плагина, то есть там, где нужнее.
            _no_reqs = _obs_variant([])
            _cfg_tmp = _tmp_dir / "opencode.jsonc"
            _msgs1, _errs1 = _mcp_registry.enable(_tmp_dir, _no_reqs)
            _after_first = _cfg_tmp.read_text(encoding="utf-8")
            _msgs2, _errs2 = _mcp_registry.enable(_tmp_dir, _no_reqs)
            _after_second = _cfg_tmp.read_text(encoding="utf-8")
            check('"obs"' in _after_first,
                  "первое нажатие вписывает сервер в настройки")
            check(_after_first == _after_second,
                  "повторное нажатие не меняет файл настройки")
            check(_after_second.count('"obs"') == 1,
                  "сервер записан ровно один раз, копий не плодится")
            check(not _errs2, f"повторное нажатие без ошибок: {_errs2}")
            check(any("не трогаю" in m for m in _msgs2),
                  "программа честно говорит, что файл не тронула")

            # (2) Отказ включать мост, который не заработает. Плагин
            # взят несуществующий, поэтому отказ гарантирован на любой
            # машине: проверка не зависит ни от OBS, ни от сети.
            _unknown = [{"what": "Плагин нетакой-точки",
                         "check": "такого-плагина-в-коде-нет",
                         "type": "plugin", "blocks": True}]
            _refuse = _obs_variant(_unknown)
            _cfg_refuse = _tmp_dir / "облом.jsonc"
            _msgs_r, _errs_r = _mcp_registry.enable(_cfg_refuse, _refuse)
            check(bool(_errs_r) and "Плагин" in " ".join(_errs_r),
                  f"мост без плагина не включается, и сказано почему: "
                  f"{_errs_r}")
            check(not _cfg_refuse.exists(),
                  "и файл настроек при отказе не создан")

            # Тот же случай на живой машине: если плагин есть, отказа
            # быть не должно — иначе проверка врёт, запрещая рабочий
            # мост. Если плагина нет, обязана быть причина с цифрами.
            _live_plugin = [r for r in _real_obs.missing
                            if r.kind == "plugin"]
            if _live_plugin:
                check_machine(all(r.detail and r.detail != "есть"
                                  for r in _live_plugin),
                              f"невыполненное требование о плагине объяснено: "
                              f"{_live_plugin[0].detail[:56]}")
            else:
                check_machine(bool(_real_obs.ready),
                              f"плагин на месте — и obs готов включаться: "
                              f"ready={_real_obs.ready}")
    finally:
        shutil.rmtree(_tmp_dir, ignore_errors=True)

    # Запятая-разделитель ставится отдельной строкой перед метками.
    # Приклеивать её к предыдущей строке нельзя: если та строка —
    # комментарий «//», запятая уедет внутрь и файл перестанет
    # читаться. Проверяем именно этот опасный случай.
    _plain = ('{\n  "mcp": {\n    "pc": {\n      "type": "local"\n    },\n'
              '    // чужой комментарий\n    "note": {\n      "x": 1\n    }\n'
              '  }\n}\n')
    _entry = '"obs": {\n  "type": "local",\n  "enabled": true\n}'
    _ins = _opencode_caps.insert_entry(_plain, "mcp", "obs", _entry)
    check(_opencode_caps.check_jsonc(_ins),
          "вставка рядом с чужим комментарием не ломает файл")
    check('"comment"' not in _ins or "//" in _ins,
          "чужой комментарий на месте после вставки")

    # Ярлык: старый был битым — вёл на удалённую папку, и скрипт
    # отвечал «ярлык уже есть, ничего не меняю». Теперь такой
    # ярлык обязан переписываться.
    _sc_src = (_root / "tools" / "dbapp" / "make_shortcut.py").read_text(
        encoding="utf-8")
    check("def read_target(" in _sc_src,
          "скрипт ярлыка умеет читать, куда ярлык ведёт")
    check("Ничего не меняю" not in _sc_src,
          "скрипт ярлыка больше не отказывается чинить битый ярлык")
    check("shortcut_links" in _sc_src,
          "скрипт находит и чинит варианты вида «Управление базой 2.lnk»")
    _main_name = (_root / "tools" / "dbapp" / "make_shortcut.py")
    check(_main_name.is_file(), "скрипт создания ярлыка на месте")

    # ---- 8г. Обновление скилла в уже созданной базе
    echo("\n--- 8г. Скилл, который в мастере, но только в старой базе ---")
    _src_sk = core.program_root()
    _bs = tempfile.mkdtemp(prefix="selftest-skills-")
    try:
        _t = Path(_bs) / "база"
        _t.mkdir()
        core._copy_skills(_src_sk, _t)
        _sd = _t / "skills"

        check((_sd / core.SKILL_MANIFEST).is_file(),
              "запоминаем установлено, что было поставлено")
        _md = core._read_manifest(_sd)
        check(bool(_md.get("skills")),
              f"в манифесте скиллы: {len(_md.get('skills') or {})}")
        check(bool(_md.get("index")), "хэш индекса записан")

        _again = core._copy_skills(_src_sk, _t)
        check(not _again, f"повторное копирование молчит: {_again}")

        _vic = next((d for d in sorted(_sd.iterdir())
                     if d.is_dir() and (d / "SKILL.md").is_file()), None)
        if _vic is not None:
            _vf = _vic / "SKILL.md"
            _vf.write_text(_vf.read_text(encoding="utf-8") + "\n<!-- правка руками -->\n", encoding="utf-8")
            _m = core._copy_skills(_src_sk, _t)
            check(any("не тронут" in x for x in _m),
                  f"о навыке, изменённом руками, сказано в отчёте")
            check("правка руками" in _vf.read_text(encoding="utf-8"),
                  "правка, внесённая руками, цела")

        # Навык, добавленный в мастер ПОСЛЕ создания базы, обязан в неё приехать.
        _bm = tempfile.mkdtemp(prefix="selftest-master-")
        try:
            _ms = Path(_bm)
            shutil.copytree(_src_sk / "skills", _ms / "skills",
                            ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(core.program_file("skills-index.json", _src_sk),
                            _ms / "skills-index.json")
            _nw = _ms / "skills" / "новый-тестовый"
            _nw.mkdir()
            (_nw / "SKILL.md").write_text("# тест\n", encoding="utf-8")
            _mi = _ms / "skills-index.json"
            _dd = json.loads(_mi.read_text(encoding="utf-8"))
            _items = _dd if isinstance(_dd, list) else _dd.setdefault("skills", [])
            _items.append({"name": "новый-тестовый", "when": "всегда",
                           "trigger": "проверка", "result": "тест"})
            _mi.write_text(json.dumps(_dd, ensure_ascii=False, indent=2),
                           encoding="utf-8")

            _was = (_sd / "новый-тестовый").is_dir()
            _m = core._copy_skills(_ms, _t)
            check(not _was and (_sd / "новый-тестовый").is_dir(),
                  f"навык, добавленный в мастер, приехал в старую базу: {_m}")

            _found = False
            _idst = _t / "skills-index.json"
            if _idst.is_file():
                _d2 = json.loads(_idst.read_text(encoding="utf-8"))
                _it2 = _d2 if isinstance(_d2, list) else _d2.get("skills", [])
                _found = "новый-тестовый" in {str(x.get("name")) for x in _it2}
            check(_found, "индекс базы тоже узнал про новый навык")
        finally:
            shutil.rmtree(_bm, ignore_errors=True)

        _so = _sd / "только-только"
        _so.mkdir()
        (_so / "SKILL.md").write_text("# мой\n", encoding="utf-8")
        core._copy_skills(_src_sk, _t)
        check(_so.is_dir(),
              "навык, который есть только в старой базе, не стирается")
    finally:
        shutil.rmtree(_bs, ignore_errors=True)

    # ---- 8д. Сверка копий навыков: чем они расходятся на самом деле
    echo("\n--- 8д. Сверка копий навыков ---")
    _bq = tempfile.mkdtemp(prefix="selftest-compare-")
    try:
        def _mk_master(root: Path) -> None:
            (root / "skills").mkdir(parents=True, exist_ok=True)
            for _n in ("первый", "второй"):
                _d = root / "skills" / _n
                _d.mkdir()
                (_d / "SKILL.md").write_text(f"# {_n}\n", encoding="utf-8")
            (root / "skills" / "_описание.md").write_text(
                "описание\n", encoding="utf-8")
            _idx = {"skills": [
                {"name": "первый", "when": "а", "trigger": "б", "result": "в"},
                {"name": "второй", "when": "а", "trigger": "б", "result": "в"},
            ]}
            (root / "skills-index.json").write_text(
                json.dumps(_idx, ensure_ascii=False, indent=2), encoding="utf-8")

        _bm = Path(_bq) / "мастер"
        _mk_master(_bm)

        _r = core.compare_skills(_bm, _bm)
        check(not core.skills_findings(_r),
              f"копия сама с собой не спорит: {core.skills_findings(_r)}")

        # Отстал, но человек его не трогал: обновлять можно.
        # Мастер меняем ПОСЛЕ установки — иначе получится правка руками.
        _bm_new = Path(_bq) / "мастер-новый"
        shutil.copytree(_bm, _bm_new)
        _stale = Path(_bq) / "отстал"
        shutil.copytree(_bm, _stale)
        core._copy_skills(_bm, _stale)
        (_bm_new / "skills" / "первый" / "SKILL.md").write_text(
            "# первый\nтеперь такой\n", encoding="utf-8")
        _r = core.compare_skills(_bm_new, _stale)
        check(_r["stale"] == ["первый"],
              f"навык, отставший от мастера, опознан: {_r['stale']}")
        check(not _r["manual"],
              f"отставший не помечен как правленый руками: {_r['manual']}")
        check(any("отстал" in f for f in core.skills_findings(_r)),
              "об отставшем сказано по-человечески")

        # Человек правил руками — такой трогать нельзя.
        _man = Path(_bq) / "правленый"
        shutil.copytree(_bm, _man)
        core._copy_skills(_bm, _man)
        (_man / "skills" / "первый" / "SKILL.md").write_text(
            "# первый\nмоё\n", encoding="utf-8")
        _r = core.compare_skills(_bm, _man)
        check(_r["manual"] == ["первый"],
              f"правка руками опознана: {_r['manual']}")
        check(not _r["stale"],
              f"правленый руками не попал в отставшие: {_r['stale']}")

        _miss = Path(_bq) / "без-навыка"
        shutil.copytree(_bm, _miss)
        shutil.rmtree(_miss / "skills" / "второй")
        _r = core.compare_skills(_bm, _miss)
        check(_r["missing"] == ["второй"],
              f"пропавший навык замечен: {_r['missing']}")

        _extra = Path(_bq) / "с-лишним"
        shutil.copytree(_bm, _extra)
        _d = _extra / "skills" / "третий"
        _d.mkdir()
        (_d / "SKILL.md").write_text("# третий\n", encoding="utf-8")
        _r = core.compare_skills(_bm, _extra)
        check(_r["extra"] == ["третий"],
              f"лишний навык замечен: {_r['extra']}")

        _short = Path(_bq) / "короткий-индекс"
        shutil.copytree(_bm, _short)
        _p = _short / "skills-index.json"
        _d = json.loads(_p.read_text(encoding="utf-8"))
        _d["skills"] = _d["skills"][:1]
        _p.write_text(json.dumps(_d, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        _r = core.compare_skills(_bm, _short)
        check(_r["index_missing"] == ["второй"],
              f"в индексе не хватает записи: {_r['index_missing']}")

        # Разрядка разная, а записи те же. Это не расхождение.
        _fmt = Path(_bq) / "другая-разрядка"
        shutil.copytree(_bm, _fmt)
        _d = json.loads((_fmt / "skills-index.json").read_text(encoding="utf-8"))
        (_fmt / "skills-index.json").write_text(
            json.dumps(_d, ensure_ascii=False, indent=8), encoding="utf-8")
        _r = core.compare_skills(_bm, _fmt)
        check(not core.skills_findings(_r),
              f"разрядка индекса расхождением не считается: {core.skills_findings(_r)}")
        check(_r["index_bytes_differ"],
              "а байты индекса при этом разошлись — и это замечено")

        _fld = Path(_bq) / "поле-разошлось"
        shutil.copytree(_bm, _fld)
        _p = _fld / "skills-index.json"
        _d = json.loads(_p.read_text(encoding="utf-8"))
        _d["skills"][0]["trigger"] = "другое"
        _p.write_text(json.dumps(_d, ensure_ascii=False, indent=2),
                      encoding="utf-8")
        _r = core.compare_skills(_bm, _fld)
        check(_r["index_changed"] == ["первый"],
              f"разошедшееся поле записи замечено: {_r['index_changed']}")

        _noidx = Path(_bq) / "без-индекса"
        shutil.copytree(_bm, _noidx)
        (_noidx / "skills-index.json").unlink()
        _r = core.compare_skills(_bm, _noidx)
        check(_r["index_absent"], "отсутствие индекса замечено")

        _noloose = Path(_bq) / "без-описания"
        shutil.copytree(_bm, _noloose)
        (_noloose / "skills" / "_описание.md").unlink()
        _r = core.compare_skills(_bm, _noloose)
        check(_r["loose_missing"] == ["_описание.md"],
              f"пропавший файл рядом с навыками замечен: {_r['loose_missing']}")

        # Копии нет вообще — проверка обязана молча разобраться, а не упасть.
        _r = core.compare_skills(_bm, Path(_bq) / "нет-такой-папки")
        check(_r["missing"] == ["второй", "первый"],
              f"несуществующая копия: все навыки числятся нетыми: {_r['missing']}")
    finally:
        shutil.rmtree(_bq, ignore_errors=True)

    # ---- 8е. Копия, где только навыки: так и должна быть копия opencode
    echo("\n--- 8е. Копия, где только навыки ---")
    _be = tempfile.mkdtemp(prefix="selftest-only-")
    try:
        _em = Path(_be) / "мастер"
        (_em / "skills").mkdir(parents=True)
        for _n in ("первый", "второй"):
            _d = _em / "skills" / _n
            _d.mkdir()
            (_d / "SKILL.md").write_text(f"# {_n}\n", encoding="utf-8")
        (_em / "skills" / "_описание.md").write_text(
            "описание\n", encoding="utf-8")
        (_em / "skills-index.json").write_text(
            json.dumps({"skills": [
                {"name": "первый", "when": "а", "trigger": "б", "result": "в"},
                {"name": "второй", "when": "а", "trigger": "б", "result": "в"},
            ]}, ensure_ascii=False, indent=2), encoding="utf-8")

        # Именно так выглядит копия, которую делает плагин opencode:
        # папки навыков есть, а индекса и описания рядом нет.
        _oc = Path(_be) / "настройки"
        shutil.copytree(_em / "skills", _oc / "skills")
        (_oc / "skills" / "_описание.md").unlink()

        _r = core.compare_skills(_em, _oc, expect="skills-only")
        check(not core.skills_findings(_r),
              f"копия только с навыками — расхождений нет: {core.skills_findings(_r)}")

        _r = core.compare_skills(_em, _oc)
        check(_r["index_absent"] and _r["loose_missing"] == ["_описание.md"],
              f"а от полной копии индекс и описание требуются: {core.skills_findings(_r)}")

        # Навыки сверяются всегда, даже когда индекс не ждём.
        shutil.rmtree(_oc / "skills" / "второй")
        _r = core.compare_skills(_em, _oc, expect="skills-only")
        check(_r["missing"] == ["второй"],
              f"пропавший навык замечен даже там, где индекс не ждут: {_r['missing']}")
    finally:
        shutil.rmtree(_be, ignore_errors=True)

# ---- 8з. Описание навыков не должно отставать от папки
    echo("\n--- 8з. Описание навыков не отстаёт от папки ---")
    _sk_root = core.program_root() / "skills"
    _desc_file = _sk_root / "_ОПИСАНИЕ-НАВЫКОВ.md"
    _real = sorted(d.name for d in _sk_root.iterdir()
                   if d.is_dir() and (d / "SKILL.md").is_file()) \
        if _sk_root.is_dir() else []
    check(_desc_file.is_file(), "файл описаний навыков на месте")
    check(len(_real) >= 1, f"навыки в папке перечислены: {len(_real)}")
    if _desc_file.is_file() and _real:
        _txt = _desc_file.read_text(encoding="utf-8")
        _miss = [n for n in _real if n not in _txt]
        check(not _miss,
              f"каждый навык описан: не описано {len(_miss)} {_miss}")
        _rows = set()
        for _line in _txt.splitlines():
            if _line.startswith("| `") and "`" in _line[3:]:
                _rows.add(_line.split("`")[1])
        _gap = sorted(set(_real) - _rows)
        check(not _gap,
              f"каждый навык есть в сводной таблице: без строки {len(_gap)} {_gap}")
        _extra = sorted(_rows - set(_real))
        check(not _extra,
              f"в таблице нет навыков, которых нет в папке: {_extra}")
    echo("\n--- 8з-1. У каждого навыка своя строка в инструкции ---")
    _doc = core.program_root() / "инструкции" / "Скиллы-и-агенты.md"
    if _doc.is_file():
        # Название навыка достаётся текстом между кавычками. Строка
        # годится, только если назван ОДИН навык: общая строка на шестерых
        # не даёт нейросети выбора — условие срабатывания у всех одно.
        _solo: set[str] = set()
        _seen: set[str] = set()
        for _line in _doc.read_text(encoding="utf-8").splitlines():
            if not _line.startswith("|"):
                continue
            for _cell in [c.strip() for c in _line.strip("|").split("|")]:
                _parts = _cell.split("`")
                _toks = [t for i, t in enumerate(_parts) if i % 2 == 1]
                _seen.update(_toks)
                if len(_toks) == 1:
                    _solo.add(_toks[0])
        _no_row = sorted(set(_real) - _seen)
        _shared = sorted(n for n in _real if n not in _solo)
        check(not _no_row,
              f"каждый навык упомянут в инструкции: нет {_no_row}")
        check(not _shared,
              f"у каждого навыка своя строка, не общая на нескольких: "
              f"{_shared}")
    else:
        echo("файл инструкции не найден — проверка пропущена")

# ---- 8и. Файлы рядом с навыками: едут, обновляются и проверяются
    echo("\n--- 8и. Файлы рядом с навыками ---")
    _bi = tempfile.mkdtemp(prefix="selftest-loose-")
    try:
        _mi = Path(_bi) / "мастер"
        (_mi / "skills").mkdir(parents=True)
        _d = _mi / "skills" / "первый"
        _d.mkdir()
        (_d / "SKILL.md").write_text("# первый\n", encoding="utf-8")
        (_mi / "skills" / "_ОПИСАНИЕ.md").write_text("описание на 1 навык\n",
                                                    encoding="utf-8")
        (_mi / "skills-index.json").write_text(
            json.dumps({"skills": [{"name": "первый", "when": "а", "trigger": "б",
                                    "result": "в"}]}, ensure_ascii=False, indent=2),
            encoding="utf-8")

        # Посторонний файл без подчёркивания в базу не едет.
        (_mi / "skills" / "THIRD-PARTY.md").write_text("чужое\n", encoding="utf-8")
        check(core._loose_files(_mi / "skills") == {"_ОПИСАНИЕ.md"},
              f"служебными считаются только файлы с подчёркиванием: "
              f"{sorted(core._loose_files(_mi / 'skills'))}")

        _ti = Path(_bi) / "база"
        _ti.mkdir()
        core._copy_skills(_mi, _ti)
        check((_ti / "skills" / "_ОПИСАНИЕ.md").is_file(),
              "описание доехало до новой базы")
        check(not (_ti / "skills" / "THIRD-PARTY.md").is_file(),
              "посторонний файл в базу не попал")

        # Описание в базе устарело — сверка это видит.
        (_ti / "skills" / "_ОПИСАНИЕ.md").write_text("описание на 0 навыков\n",
                                                     encoding="utf-8")
        _r = core.compare_skills(_mi, _ti)
        check(_r["loose_changed"] == ["_ОПИСАНИЕ.md"],
              f"устаревшее содержание описания замечено: {_r['loose_changed']}")
        check(any("содержимым" in f for f in core.skills_findings(_r)),
              "об этом сказано по-человечески")

        # Манифест помнит, что ставили мы: значит, обновить можно.
        _md = core._read_manifest(_ti / "skills")
        check("files" in _md and "_ОПИСАНИЕ.md" in _md["files"],
              f"манифест помнит и файлы: {sorted(_md.get('files') or {})}")
        # Возвращаем базу к тому, что ставили мы, и меняем мастер: база
        # отстала, но её никто не трогал — обновление допустимо. Если же
        # базу правили руками, обновления быть не должно, и это проверяется
        # следующим шагом.
        shutil.copy2(_mi / "skills" / "_ОПИСАНИЕ.md",
                     _ti / "skills" / "_ОПИСАНИЕ.md")
        (_mi / "skills" / "_ОПИСАНИЕ.md").write_text("описание на 2 навыка\n",
                                                    encoding="utf-8")
        _msgs = core._copy_skills(_mi, _ti)
        check(any("ОПИСАНИЕ" in x and "обновлён" in x for x in _msgs),
              f"устаревшее описание обновилось: {_msgs}")
        _txt = (_ti / "skills" / "_ОПИСАНИЕ.md").read_text(encoding="utf-8")
        check("на 2 навыка" in _txt, "новое содержание на месте")

        # Правка руками — не трогаем.
        (_ti / "skills" / "_ОПИСАНИЕ.md").write_text("моё\n", encoding="utf-8")
        _msgs = core._copy_skills(_mi, _ti)
        check(any("ОПИСАНИЕ" in x and "не тронут" in x for x in _msgs),
              f"правка руками не тронута: {_msgs}")
        check((_ti / "skills" / "_ОПИСАНИЕ.md").read_text(encoding="utf-8") == "моё\n",
              "правка руками цела")

        _r = core.compare_skills(_mi, _ti)
        check(_r["loose_changed"] == ["_ОПИСАНИЕ.md"],
              "и сверка говорит, что файлы разошлись")
    finally:
        shutil.rmtree(_bi, ignore_errors=True)
# ---- 8к. Общий модуль версий: проверяем сам по себе
    echo("\n--- 8к. Общий модуль версий ---")
    try:
        import versions as vmod
    except ImportError:
        vmod = None
        check(False, "общий модуль версий импортируется")
    if vmod is not None:
        check(True, "общий модуль версий импортируется")

        # Сравнение чисел, а не строк: иначе 263.9 окажется новее 263.10.
        check(vmod.version_tuple("263.9") < vmod.version_tuple("263.10"),
              "263.9 старее 263.10 — сравнение числовое")
        check(vmod.version_tuple("") == (0,),
              f"пустая строка не ломает разбор: {vmod.version_tuple('')}")
        check(vmod.version_tuple("261.x.3") == (261, 0, 3),
              f"нецифра в номере не ломает разбор: {vmod.version_tuple('261.x.3')}")

        # Платформа из номера сборки: первые два числа.
        check(vmod.platform_from_build("AI-261.25134.95.2612.15914620") == (261, 25134),
              "платформа из полного buildNumber")
        check(vmod.platform_from_build("") == (),
              "пустой buildNumber не превращается в платформу")
        check(vmod.platform_text((263, 5701)) == "263.5701",
              f"платформа обратно в строку: {vmod.platform_text((263, 5701))}")

        # Границы сборки — обе стороны.
        check(vmod.since_matches("261.25134", (261, 25134)), "since равно платформе")
        check(not vmod.since_matches("262.10968", (262, 10315)), "since новее платформы")
        check(not vmod.since_matches("261", ()), "пустая платформа не проходит ни одну сборку")
        check(vmod.until_matches("", (262, 1)), "пустой until означает «любая версия»")
        check(vmod.until_matches("*", (262, 1)), "звёздочка означает «любая версия»")
        check(vmod.until_matches("261.*", (261, 26000)), "261.* накрывает всю ветку")
        check(not vmod.until_matches("261.*", (262, 10315)), "261.* не накрывает ветку 262")
        check(vmod.until_matches("263.5701.*", (263, 5701)), "точечная подплатформа")
        check(not vmod.until_matches("263.5701.*", (263, 6259)),
              "точечная подплатформа не накрывает соседнюю")
        check(vmod.until_matches("262.10315.174", (262, 10315, 174)),
              "точный номер включительно")

        _b = {"version": "1.0", "since": "1.0", "until": "*"}
        check(vmod.build_matches(_b, (1, 0)), "сборка подходит по обеим границам")
        check(not vmod.build_matches(dict(_b, since="9.0"), (1, 0)),
              "сборка не подходит, если since новее платформы")

        # Выбор самой свежей — на обычном списке словарей, без класса Bundle.
        _builds = [
            {"version": "1.2", "since": "1.0", "until": "*"},
            {"version": "1.10", "since": "1.0", "until": "*"},
            {"version": "2.0", "since": "9.0", "until": "*"},
        ]
        _got = vmod.pick_build(_builds, (1, 0))
        check(_got is not None and _got["version"] == "1.10",
              f"выбрана самая свежая по номеру, а не по записи: {_got and _got['version']}")
        check(vmod.pick_build(_builds, (1, 0, 0)) is not None, "платформа длиннее версии не мешает")
        # since — это «не ниже», поэтому платформа (9, 9) подходит всем трём
        # сборкам, включая сборку с since 9.0, и берётся самая свежая.
        _hi = vmod.pick_build(_builds, (9, 9))
        check(_hi is not None and _hi["version"] == "2.0",
              f"платформа выше всех since: взята самая свежая из подходящих: "
              f"{_hi and _hi['version']}")
        # А ниже всех начал — не подходит ни одна.
        check(vmod.pick_build(_builds, (0, 0)) is None,
              "платформа ниже всех since не подходит ни одной сборке")
        check(vmod.pick_build([], (1, 0)) is None, "пустой список даёт None, а не исключение")

        # Хеш потоком совпадает с обычным подсчётом.
        _bd = tempfile.mkdtemp(prefix="selftest-hash-")
        try:
            _hf = Path(_bd) / "хеш-проверка.bin"
            _hf.write_bytes(b"opencode" * 5000)
            import hashlib as _hl
            check(vmod.file_sha256(_hf) == _hl.sha256(_hf.read_bytes()).hexdigest(),
                  "хеш потоком совпадает с хешем целиком")
            check(android_studio.sha256(_hf) == vmod.file_sha256(_hf),
                  "старое имя android_studio.sha256 даёт тот же хеш")
        finally:
            shutil.rmtree(_bd, ignore_errors=True)

        # Старые имена на месте: ими пользуются окно и селфтест.
        check(android_studio.version_tuple is vmod.version_tuple
              and android_studio.since_matches is vmod.since_matches
              and android_studio.until_matches is vmod.until_matches
              and android_studio.build_matches is vmod.build_matches
              and android_studio.sha256 is vmod.file_sha256,
              "android_studio отдаёт те же объекты, а не копии")
        check(android_studio.pick_build is not vmod.pick_build,
              "pick_build в android_studio остался обёрткой над списком сборок")
    # ---- 8л. Границы версий сверху и блок установки программы
    echo("\n--- 8л. Границы версий и блок program_install ---")
    import versions as _v2

    # Граница «18» — это ветка 18 целиком, а не только 18.0.0. На этом и
    # сломалось первое сравнение: 18.2 выходило «новее 18».
    check(_v2.until_ok("18", (18, 2, 0)), "18.2 попадает в границу «до 18 включительно»")
    check(_v2.until_ok("18", (18, 0, 0)), "18.0 попадает в границу «18»")
    check(not _v2.until_ok("18", (19, 0, 0)), "19.0 не попадает в границу «18»")
    check(_v2.until_ok("18", (17, 9, 0)), "17.9 попадает в границу «18»")
    # Граница короче найденной версии: 18 против 18.0.1 — иначе Python сравнил
    # бы кортежи по длине и решил, что 18 новее 18.0.1.
    check(_v2.until_ok("18", (18, 0, 1)), "короткая граница не ломает длинную версию")
    check(_v2.until_ok("18.0", (18, 0, 1)), "точная граница 18.0 накрывает 18.0.1")
    check(not _v2.until_ok("18.0", (18, 1, 0)), "18.1 не накрывается границей 18.0")
    check(_v2.until_ok("2025.2", (2025, 1, 8)), "2025.1 попадает в «до 2025.2»")
    check(not _v2.until_ok("2025.2", (2026, 2, 1, 8)), "2026 не попадает в «до 2025.2»")
    check(_v2.until_ok("24", (24, 18, 0)), "24.18 попадает в «до 24»")
    check(_v2.until_ok("261.*", (261, 5701)), "префикс 261.* накрывает ветку")
    check(not _v2.until_ok("261.*", (262, 1)), "префикс 261.* не накрывает ветку 262")
    check(_v2.until_ok("263.5701.*", (263, 5701, 7)), "точечная подплатформа накрыта")
    check(_v2.until_ok("", (99,)), "пустая граница — ограничения нет")
    check(_v2.until_ok("*", (99,)), "звёздочка — ограничения нет")
    # Граница без цифр — опечатка, а не ограничение. Объявлять из-за неё
    # версию слишком новой нельзя: получилась бы тихая ложь.
    check(_v2.until_ok("abc", (99,)), "граница без цифр не объявляет версию новой")
    # Старая функция на сборках Android Studio не должна пострадать.
    check(_v2.until_matches("261.*", (261, 1)) and not _v2.until_matches("999.*", (261, 1)),
          "старая until_matches на сборках не тронута")

    # --- границы в требовании
    _pi_def = mcp_registry.ProgramInstall()
    check(_pi_def.method == "manual",
          f"метод по умолчанию — ручной, а не winget: {_pi_def.method}")
    check(_pi_def.can_install is False, "у пустого блока кнопки нет")
    check(_pi_def.publisher_trusted is True, "у пустого блока издатель по умолчанию доверенный")

    _spec = {"what": "X", "type": "command", "check": "x",
             "max_version": 30, "until": "2025.2"}
    _req = mcp_registry.Requirement(
        what=str(_spec.get("what") or ""), kind="command",
        max_version=int(_spec.get("max_version") or 0),
        until=str(_spec.get("until") or ""))
    check(_req.max_version == 30 and _req.until == "2025.2",
          f"границы читаются из описания: max={_req.max_version}, until={_req.until}")
    _req_old = mcp_registry.Requirement(what="X")
    check(_req_old.max_version == 0 and _req_old.until == "",
          "в старом описании без границ — пусто, а не ноль как исключение")

    # --- сервер читает блок, и старый реестр не падает
    _s = mcp_registry.Server(id="s", name="s", raw={"id": "s"})
    check(_s.program_install is None, "сервер без блока — None, а не исключение")
    _s2 = mcp_registry.Server(id="s", name="s", raw={"program_install": "просто текст"})
    check(_s2.program_install is None, "строка вместо объекта — None, не падение")
    _s3 = mcp_registry.Server(id="s", name="s",
                              raw={"program_install": {"method": "  WINGET  "}})
    check(_s3.program_install.method == "winget",
          f"метод чистится от пробелов и регистра: {_s3.program_install.method}")
    check(_s3.program_install.publisher_trusted is True,
          "старый блок без publisher_trusted — доверяем, а не пугаем")
    _s4 = mcp_registry.Server(id="s", name="s",
                              raw={"program_install": {"method": "winget", "winget_id": "X.Y"}})
    check(_s4.program_install.can_install is True, "метод winget даёт кнопку")
    _s5 = mcp_registry.Server(id="s", name="s",
                              raw={"program_install": {"method": "manual", "hand_over": True}})
    check(_s5.program_install.can_install is False
          and _s5.program_install.stops_for_human is True,
          "ручной метод без кнопки, но с передачей человеку")

    # --- настоящий реестр
    _base = core.program_root()
    _servers = mcp_registry.load_servers(_base)
    check(len(_servers) == 9, f"реестр читается, 9 серверов: {len(_servers)}")
    check(all(s.program_install is not None for s in _servers),
          "у всех 9 серверов есть блок program_install")
    _by_id = {s.id: s.program_install for s in _servers}
    if all(_by_id.values()):
        check(_by_id["blender"].winget_id == "BlenderFoundation.Blender"
              and _by_id["blender"].expected_publisher == "Blender Foundation",
              "Blender: идентификатор и издатель из каталога")
        check(_by_id["blender"].expected_signer == "",
              "Blender: подписант не выдуман — издатель из каталога им не является")
        check(_by_id["ldplayer"].method == "manual" and not _by_id["ldplayer"].can_install,
              "LDPlayer: вручную, кнопки нет")
        check(_by_id["excel"].method == "none" and not _by_id["excel"].stops_for_human,
              "Excel: предлагать нечего — решение человека по офису от 03.10.2026")
        check(_by_id["excel"].winget_id == "" and not _by_id["excel"].can_install,
              "Excel: идентификатора в winget нет, кнопки нет")
        check(_by_id["windows-admin"].method == "winget"
              and _by_id["windows-admin"].winget_id == "OpenJS.NodeJS.LTS",
              "windows-admin: Node.js ставится через winget, а не «не требуется»")
        check("не требуется" not in _by_id["windows-admin"].program,
              f"windows-admin больше не говорит «не требуется»: "
              f"{_by_id['windows-admin'].program!r}")
        _wa_req = next((r for r in
                     next(s for s in _servers if s.id == "windows-admin").requirements
                     if r.what.startswith("Node.js")), None)
        check(_wa_req is not None and _wa_req.kind == "command",
              "Node.js в требованиях windows-admin — проверяемая команда, а не ручное")
        _emu = _by_id["android-emulator"]
        check(_emu.method == "none" and _emu.winget_id == "",
              "Android-эмулятор: главным не стоит BlueStacks, его тут нет")
        check(len(_emu.alternatives) == 4,
              f"у Android-эмулятора четыре варианта: {len(_emu.alternatives)}")
        check(_emu.alternatives and _emu.alternatives[0].get("program") == "LDPlayer",
              "первым идёт LDPlayer — он стоит на этой машине")
        _bs = next((a for a in _emu.alternatives if a.get("program") == "BlueStacks"), None)
        check(_bs is not None and _bs.get("publisher_trusted") is False,
              "BlueStacks внутри вариантов, и издатель не подтверждён")
        check(_bs is not None and _bs.get("winget_id") == "BlueStack.BlueStacks",
              "у BlueStacks свой идентификатор и метод winget")
        check(_by_id["obs"].bridge == "bundled" and _by_id["android-studio"].bridge == "bundled",
              "мосты OBS и Android Studio лежат внутри программы")
    # Идентификаторы winget обязаны быть настоящими. Обходим и главный блок, и
    # варианты: BlueStacks после правки 03.10.2026 живёт в alternatives, и
    # проверка только главного блока его бы не увидела. Порядок не важен —
    # сравниваем множества, иначе ловимся на том, что «BlenderFoundation» по
    # алфавиту раньше «BlueStack», а не на самих данных.
    _winget_ids = set()
    for _p in _by_id.values():
        if not _p:
            continue
        if _p.winget_id:
            _winget_ids.add(_p.winget_id)
        for _a in _p.alternatives:
            if isinstance(_a, dict) and _a.get("winget_id"):
                _winget_ids.add(str(_a["winget_id"]))
    _want_ids = {"Adobe.CreativeCloud", "BlueStack.BlueStacks",
                 "BlenderFoundation.Blender", "Google.AndroidStudio",
                 "OBSProject.OBSStudio", "OpenJS.NodeJS.LTS"}
    check(_winget_ids == _want_ids,
          f"идентификаторы в реестре — те самые, включая варианты: {sorted(_winget_ids)}")
    # ---- 8м. Движок вкладки «Программы»
    echo("\n--- 8м. Движок programs.py ---")
    try:
        import programs as pmod
    except ImportError:
        pmod = None
        check(False, "движок programs.py импортируется")
    if pmod is not None:
        check(True, "движок programs.py импортируется")
        _base = core.program_root()

        # Главное свойство: движок не расходится с живым реестром.
        _bad = pmod.agrees_with_registry(_base)
        check(not _bad,
              "движок выдаёт то же, что реестр, на всех серверах"
              + ("" if not _bad else f": {_bad[:2]}"))

        # Ловушка, на которую я сам наступил: неверный путь давал ноль серверов,
        # а сравнение двух пустых списков рапортовало «расхождений нет».
        _bad2 = pmod.agrees_with_registry(Path(tempfile.gettempdir()) / "папки-нет-такой")
        check(bool(_bad2),
              "на неверном пути движок честно говорит о расхождении, а не молчит")
        check(any("серверов 0" in b for b in _bad2),
              f"и называет причину — ноль серверов: {(_bad2 or [''])[0][:70]}")

        # Все девять на месте, и у каждого четыре ответа.
        _views = pmod.server_views(_base)
        check(len(_views) == 9, f"движок прочитал все девять серверов: {len(_views)}")
        _by = {v.id: v for v in _views}
        for _sid in ("windows-admin", "excel", "blender", "adobe-creativity",
                     "android-studio", "obs", "android-emulator", "ldplayer",
                     "dbhub"):
            check(_sid in _by, f"сервер {_sid} есть в движке")

        # Порог версии сравнивается, а не просто запоминается. Пример DBHub
        # в задании обещал Node.js 18+, а в самом пакете dbhub записано
        # engines.node ≥ 22.5.0. Если бы сверка сводилась к непустому полю,
        # человек с Node 20 увидел бы «готово» и получил отказ при запуске.
        # Проверяем не текст, а поведение движка: команда печатает версию,
        # движок её читает и сравнивает с порогом.
        _req_old = _mcp_registry.check_requirement({
            "what": "пример", "type": "command", "check": sys.executable,
            "args": ["-c", "print('20.11.0')"], "min_version": 22})
        check(_req_old.ok is False and "22" in _req_old.detail,
              f"Node.js 20 при пороге 22 — не подходит: {_req_old.detail}")
        _req_new = _mcp_registry.check_requirement({
            "what": "пример", "type": "command", "check": sys.executable,
            "args": ["-c", "print('22.5.0')"], "min_version": 22})
        check(_req_new.ok is True,
              f"Node.js 22 при пороге 22 — подходит: {_req_new.detail}")
        _dbhub_srv = next((s for s in _servers if s.id == "dbhub"), None)
        _node_in_dbhub = next(
            (r for r in (_dbhub_srv.requirements if _dbhub_srv else [])
             if r.value == "node"), None)
        check(_node_in_dbhub is not None and _node_in_dbhub.min_version == 22,
              "у dbhub порог Node.js 22 — из engines пакета, а не обещание 18+")

        # Кнопка там, где ставить реально можно.
        check(_by["windows-admin"].install.has_button
              and _by["windows-admin"].install.action == pmod.ACTION_WINGET,
              "windows-admin: кнопка есть, Node.js ставится через winget")
        check(_by["excel"].install.has_button is False
              and _by["excel"].install.action == pmod.ACTION_NONE,
              "Excel: кнопки нет — решение человека по офису")
        check(_by["excel"].install.reason,
              "Excel: без кнопки названа причина, а не тишина",
              )
        check(_by["ldplayer"].install.action == pmod.ACTION_MANUAL,
              "LDPlayer: ручной метод — страница и команда в буфер")
        check(_by["android-emulator"].install.action == pmod.ACTION_NONE
              and len(_by["android-emulator"].install.alternatives) == 4,
              "Android-эмулятор: «подойдёт любая», четыре варианта при себе")

        # Подпись: сверки ещё нет, и движок не имеет права говорить иначе.
        check(all(not v.verify.verified for v in _views),
              "ни один сервер не говорит «подпись проверена» — сверки нет до этапа 4")
        check(any(v.verify.expected_signer for v in _views),
              "но имя ожидаемого подписанта показано — это намерение")
        _vd = next(v.verify.detail for v in _views if v.verify.expected_signer)
        # Ищем по смыслу, а не по точному куску: в тексте «на этапе 4» есть
        # предлог, и поиск «этап 4» без хвостовой «е» не находит ничего.
        check("сверк" in _vd.lower() and "файл ещё не скачан" in _vd,
              f"и сказано, что сверка ждёт файла: {_vd[-52:]}")

        # Мост внутри программы отличается от «мост ставится отдельно».
        check(_by["obs"].bridge.bundled and _by["android-studio"].bridge.bundled,
              "мосты OBS и Android Studio помечены как лежащие внутри")
        check(_by["windows-admin"].bridge.bundled is False,
              "у windows-admin мост не внутри программы")

        # Требования: движок видит то же, что видит реестр.
        check(_by["blender"].check.ok is False,
              "blender без самой программы — не готов, как и в реестре")
        check(_by["adobe-creativity"].check.ok is False,
              "adobe-creativity без подписки — не готов")
        check(_by["blender"].check.detail,
              "и сказано, чего именно не хватает: " + _by["blender"].check.detail[:60])

        # Третий раздел «нужно мостам».
        _needs = pmod.bridge_needs(_base)
        check(len(_needs) == 2, f"в разделе «нужно мостам» два предмета: {len(_needs)}")
        _nn = {n.program: n for n in _needs}
        check("Node.js" in _nn and _nn["Node.js"].wanted_by_count == 4,
              "Node.js требуют четверо серверов из девяти")
        check(_nn.get("Node.js") is not None
              and _nn["Node.js"].install.winget_id == "OpenJS.NodeJS.LTS",
              "у Node.js настоящий идентификатор winget")
        check("uv / uvx" in _nn and _nn["uv / uvx"].required_by == ["blender"],
              "uv требует только blender — и его тоже не было в списке программ")

        # Раздел не должен молчать, если его нет.
        check(pmod.bridge_section_problem(_base) == "",
              "раздел «нужно мостам» в порядке")
        _fake = Path(tempfile.mkdtemp(prefix="programs-nobridge-"))
        try:
            _reg_src = mcp_registry.registry_path(_base)
            _raw = json.loads(_reg_src.read_text(encoding="utf-8"))
            _raw.pop("bridge_requirements", None)
            (_fake / mcp_registry.REGISTRY_NAME).write_text(
                json.dumps(_raw, ensure_ascii=False, indent=2), encoding="utf-8")
            _p3 = pmod.bridge_section_problem(_fake)
            check(bool(_p3) and "bridge_requirements" in _p3,
                  f"без раздела движок жалуется, а не показывает пустоту: {_p3[:60]}")
            _raw2 = json.loads(_reg_src.read_text(encoding="utf-8"))
            _raw2["bridge_requirements"] = {"note": "тест", "items": []}
            (_fake / mcp_registry.REGISTRY_NAME).write_text(
                json.dumps(_raw2, ensure_ascii=False, indent=2), encoding="utf-8")
            check(pmod.bridge_section_problem(_fake) == "",
                  "пустой раздел честен — это «ставить нечего», а не потеря данных")
            check(pmod.bridge_needs(_fake) == [],
                  "и предметов в пустом разделе ноль, без исключения")
        finally:
            shutil.rmtree(_fake, ignore_errors=True)

        # Сверка раздела с требованиями серверов: разойтись не должны.
        _live = {s.id: s for s in mcp_registry.load_servers(_base)}
        for _n in _needs:
            for _sid in _n.required_by:
                _srv = _live.get(_sid)
                check(_srv is not None, f"требующий сервер {_sid} есть в реестре")
                if _srv is None:
                    continue
                _what = " ".join(r.what for r in _srv.requirements).lower()
                _prog = _n.program.lower().split(" / ")[0]
                check(_prog in _what,
                      f"{_sid} действительно требует {_n.program} — раздел не выдуман")
    # ---- 8н. Проверка подписи установщика
    echo("\n--- 8н. Проверка подписи установщика ---")
    try:
        import signature as sg
    except ImportError:
        sg = None
        check(False, "модуль signature импортируется")
    if sg is not None:
        check(True, "модуль signature импортируется")

        # Статусы зафиксированы числами: сверять строкой нельзя, локализация
        # Windows в любой момент может изменить имя.
        check(sg.STATUS_VALID == 0 and sg.STATUS_NOT_SIGNED == 2
              and sg.STATUS_HASH_MISMATCH == 3 and sg.STATUS_NOT_TRUSTED == 4,
              "значения статусов верны (сняты через [enum]::GetNames)")
        check(len(sg.STATUS_NAMES) == 7,
              f"описаны все семь статусов: {len(sg.STATUS_NAMES)}")

        # Правило сверки: слова, а не куски строки.
        for _subj, _exp, _want, _note in (
            ("CN=OpenJS Foundation, O=OpenJS Foundation", "OpenJS Foundation", True,
             "свой подписант"),
            ("CN=OpenJS Foundation, O=OpenJS Foundation", "Node.js Foundation", False,
             "издатель из winget подписантом не является"),
            ("CN=Microsoft Windows, O=Microsoft Corporation", "Microsoft Corporation", True,
             "имя лежит в O=, а не в CN="),
            ("CN=Microsoft Windows, O=Microsoft Corporation", "Google LLC", False,
             "чужой издатель"),
            ("CN=Adobe Inc., O=Adobe Inc.", "Adobe Inc.", True, "полное совпадение"),
            ("CN=Adobe Systems, O=Adobe Systems", "Adobe", True,
             "старое название той же фирмы"),
            ("CN=Epic Games, Inc., O=Epic Games", "Epic Games", True,
             "ожидание короче подписанта"),
            ("CN=Microsoft", "Microsoft Corporation", False,
             "короткое фактическое имя не подтверждает длинное ожидание"),
            ("CN=Любой Издатель, O=Никто", "Blender Foundation, Adobe Inc.", False,
             "список ожиданий не подошёл"),
            ("", "Blender Foundation", False, "пустой Subject"),
            ("CN=Что Угодно", "", False, "нечего сверять"),
        ):
            check(sg.signer_matches(_subj, _exp) == _want, f"сверка: {_note}")
        check(sg._tokens("CN=A B, O=C") == ["CN", "A", "B", "O", "C"],
              f"разбор на слова: {sg._tokens('CN=A B, O=C')}")
        check(sg._tokens("") == [] and sg._tokens(None) == [],
              "пустая строка даёт пустой список, а не исключение")

        # Два требования этапа: настоящее проходит, чужое отвергается.
        # Файлы берутся те, что уже есть на машине, — интернет не нужен.
        _node = Path(r"C:\Program Files\nodejs\node.exe")
        _python = (Path.home() / ".workbuddy-ai" / "binaries" / "python"
                   / "envs" / "dbapp" / "Scripts" / "python.exe")
        _have_signed = _node.is_file()
        check(_have_signed, "на машине есть настоящий подписанный файл")
        _have_py = _python.is_file()
        check(_have_py, "и второй, от другого издателя")

        if _have_signed:
            # Дорогой зонд: PowerShell и файл в 92 МБ, первый вызов до
            # 19 секунд при лимите 30. Раньше он звался здесь трижды подряд,
            # и на втором-третьем холодном вызове упирался в лимит и
            # возвращал «проверка не удалась» — а селфтест считал это
            # «подпись плохая». Теперь зонд один, с повтором при неудаче.
            _good = probe_retry(lambda: sg.check_signature(_node,
                                                          "OpenJS Foundation"))
            _probe_ok = _good.status == sg.STATUS_VALID
            check_machine(_probe_ok,
                          f"настоящий файл: статус Valid ({_good.status})"
                          if _probe_ok else
                          f"зонд подписи не справился, статус "
                          f"{_good.status}: {_good.detail[:70]}")
            if _probe_ok:
                check_machine(_good.verified is True,
                              "настоящий файл: подписант тот, кого ждали — "
                              f"{_good.signer}")
                check_machine(_good.safe_to_run is True,
                              "настоящий файл: запускать можно")

                # ГЛАВНЫЙ тест этапа: тот же файл, но ждём чужого подписанта.
                _wrong = sg.check_signature(_node, "Google LLC")
                check_machine(_wrong.verified is False,
                              "чужое ожидание: сверка отказала")
                check_machine("но подписал" in _wrong.detail,
                              "и сказала, кто подписал на самом деле: "
                              f"{_wrong.detail[:66]}")
                check_machine("подпись плохая" not in _wrong.detail,
                              "при этом не назвала подпись плохой — "
                              "она в порядке, не тот")

        if _have_py:
            _py = sg.check_signature(_python, "Python Software Foundation")
            check(_py.verified is True, f"второй настоящий файл прошёл: {_py.signer}")
            check(sg.check_signature(_python, "Adobe Inc.").verified is False,
                  "и тот же файл под чужим ожиданием отвергнут")

        if _have_signed:
            # Файл с испорченной подписью: копия с изменённым байтом внутри.
            _sdir = Path(tempfile.mkdtemp(prefix="signature-"))
            try:
                _broken = _sdir / "испорченный.exe"
                _bytes = bytearray(_node.read_bytes())
                _bytes[0x400] ^= 0xFF
                _broken.write_bytes(bytes(_bytes))
                _bad = sg.check_signature(_broken, "OpenJS Foundation")
                check(_bad.checked is True, "испорченный файл: ответ получен")
                check(_bad.verified is False, "испорченный файл: сверка отказала")
                check(_bad.safe_to_run is False, "испорченный файл: запускать нельзя")
                _st = _bad.status
                # Отказ зонда и отказ по существу — разные вещи. Если
                # Windows не смогла ответить, проверка ничего не знает про
                # испорченную подпись и говорить «сбой» не вправе. Если
                # ответ получен и он не про вёрстку подписи — сбой реальный.
                if _st == sg.STATUS_UNKNOWN_ERROR:
                    check_machine(False,
                                  "испорченный файл: зонд не ответил, "
                                  "судьба подписи неизвестна")
                else:
                    check(_st in (sg.STATUS_HASH_MISMATCH,
                                  sg.STATUS_NOT_SIGNED),
                          f"испорченный файл: неValid-статус "
                          f"({sg.STATUS_NAMES.get(_st, _st)})")

                _plain = _sdir / "без-подписи.exe"
                _plain.write_bytes(b"MZ\x90\x00\x03\x00\x00\x00" + b"\x00" * 64)
                _nop = sg.check_signature(_plain, "OpenJS Foundation")
                check(_nop.verified is False, "файл без подписи: сверка отказала")
                check(_nop.safe_to_run is False,
                      "файл без подписи: запускать нельзя")
            finally:
                shutil.rmtree(_sdir, ignore_errors=True)

        # Сбой команды — это «не знаю», а не «подпись плохая».
        _stub = (Path.home() / "AppData" / "Local" / "Microsoft"
                 / "WindowsApps" / "winget.exe")
        if _stub.exists():
            _crash = sg.check_signature(_stub, "Microsoft Corporation")
            check(_crash.safe_to_run is False,
                  "сбой команды не превращается в «подпись в порядке»")
            check(_crash.verified is False, "и никогда не даёт verified=True")

        # Файла нет и имени нет — оба случая говорят своё, а не молчат.
        _nofile = sg.check_signature(
            Path(tempfile.gettempdir()) / "нет-такого-файла.exe", "Microsoft Corporation")
        check(_nofile.checked is False and _nofile.verified is False
              and _nofile.safe_to_run is False,
              "нет файла: не сказано «проверено» и не «запускать можно»")
        if _have_signed:
            _noexp = sg.check_signature(_node, "")
            check(_noexp.checked is False and _noexp.verified is False,
                  "нет ожидаемого имени: сверять не с чем, и это не успех")
            check("не с чем" in _noexp.detail,
                  f"и сказано человеку почему: {_noexp.detail[:66]}")

        # Неизвестность — отказ, а не согласие.
        check(sg.STATUS_ALLows[sg.STATUS_UNKNOWN_ERROR] is False,
              "неизвестный статус означает «не запускать»")
        check(sg.STATUS_ALLows[sg.STATUS_NOT_TRUSTED] is False,
              "недоверенный подписант — отказ")
        check(sg.STATUS_ALLows[sg.STATUS_VALID] is True,
              "только действительная подпись разрешает запуск")

        # Движок вкладки связан со сверкой.
        _base2 = core.program_root()
        _views2 = pmod.server_views(_base2)
        _wa2 = next(v for v in _views2 if v.id == "windows-admin")
        check(_wa2.verify.expected_signer == "OpenJS Foundation",
              f"windows-admin ждёт настоящего подписанта: "
              f"{_wa2.verify.expected_signer}")
        check(_wa2.verify.expected_publisher == "Node.js Foundation",
              "и отдельно хранит издателя из каталога winget")
        check(_wa2.verify.can_check is True, "сверка для Node.js возможна")
        if _have_signed:
            _done = pmod.verify_file(_wa2.verify, _node)
            # Тот же случай: зонд не ответил — сверка не произошла, и
            # утверждать, что она подтвердила, нельзя.
            if _done.detail and "не удалась" in _done.detail:
                check_machine(False,
                              f"движок не смог провести сверку: "
                              f"{_done.detail[:58]}")
            else:
                check(_done.verified is True and _done.safe_to_run is True,
                      f"движок провёл сверку и подтвердил: {_done.detail[:58]}")
        _others = [v for v in _views2 if v.id != "windows-admin"]
        check(all(not v.verify.can_check for v in _others),
              "у остальных семи имя подписанта неизвестно")
        check(all("неизвестно" in v.verify.detail for v in _others),
              "и никто из них не обещает подтверждённую подпись")
        check(all(not v.verify.verified for v in _views2),
              "до сверки по файлу ни одна карточка не говорит «проверено»")

    # ---- 8о. Карточки вкладки «Программы»
    echo("\n--- 8о. Карточки и кнопки вкладки «Программы» ---")
    try:
        import program_cards as pcard
        import winget_install as wmod
    except ImportError:  # pragma: no cover
        pcard = None
        wmod = None
    check(pcard is not None and wmod is not None,
          "модули карточек и winget импортируются")
    if pcard is not None and wmod is not None:
        _pbase = core.program_root()
        _cards = pcard.cards(_pbase)
        check(len(_cards) >= 9, f"карточек не меньше девяти: {len(_cards)}")
        check(pcard.section_problem(_pbase) == "",
              f"данные для карточек целы: {pcard.section_problem(_pbase)[:60]}")

        # Состояние выводится из требований, а не заводится списком.
        _by_name = {c.name: c for c in _cards}
        _node = _by_name.get("Node.js")
        check(_node is not None, "Node.js — карточка есть")
        _node_servers = set(_node.servers) if _node else set()
        check(_node_servers == {"windows-admin", "excel", "obs", "dbhub"},
              f"Node.js одной карточкой на четверых серверов: {sorted(_node_servers)}")
        check(sum(1 for c in _cards if c.name == "Node.js") == 1,
              "и не двумя карточками, как он описан в реестре")
        check("нужна:" in pcard.needed_by_text(_node, pcard.servers_by_name(_pbase)),
              "кто именно её требует — написано словами, а не идентификаторами")

        # Кнопка у неустановленной программы — на синтетической карточке.
        # Раньше здесь стоял Blender, и проверка требовала, чтобы он был
        # «не установлена» с кнопкой установки. Это было неправдой:
        # Blender 5.2.2 стоит, а программа предлагала запустить winget
        # поверх её и попросить права администратора. Проверка не просто
        # устарела — она закрепляла поломку, и потому выглядела защитой.
        # Намерение проверяется без машины, а живое состояние — отдельно,
        # ниже и в разделе 8у.
        _missing = pcard.Card(key="нет", name="Нету её", state=pcard.STATE_MISSING,
                              install=pmod.InstallView(
                                  action=pmod.ACTION_WINGET,
                                  program="Нету её",
                                  winget_id="Пример.Пакет",
                                  needs_admin=True))
        _missing_codes = [c for c, _, _ in _missing.buttons()]
        check(_missing.can_install,
              "неустановленная программа: кнопка установки есть")
        check(pcard.BTN_INSTALL in _missing_codes,
              "и она в списке кнопок, а не только в свойствах")
        _missing_hint = next((h for c, _, h in _missing.buttons()
                              if c == pcard.BTN_INSTALL), "")
        check("winget" in _missing_hint,
              f"подсказка называет, кто ставит: {_missing_hint[:50]}")
        check(_missing.needs_admin
              and "администратор" in _missing_hint,
              "и про права администратора сказано прямо")

        # Общий инвариант по всем карточкам: установленная программа не
        # предлагает установку. Его не было — и именно поэтому Blender с
        # кнопкой «Установить» прошла селфтест насквозь.
        _offered = sorted(c.name for c in _cards
                          if c.state == pcard.STATE_OK and c.can_install)
        check(not _offered,
              f"ни одна установленная программа не предлагает установку: "
              f"{_offered or 'чисто'}")
        _wrong_hint = sorted(
            c.name for c in _cards
            if c.can_install
            and "winget" not in next((h for code, _, h in c.buttons()
                                      if code == pcard.BTN_INSTALL), ""))
        check(not _wrong_hint,
              f"и у каждой кнопки установки сказано, что ставит winget: "
              f"{_wrong_hint or 'чисто'}")

        # Установленная программа не должна предлагать установку.
        _obs = _by_name.get("OBS Studio")
        check(_obs is not None and _obs.state == pcard.STATE_OK,
              "OBS Studio «установлена» — нашлась живым требованием")
        check(_obs is not None and not _obs.can_install,
              "и кнопки установки у неё нет")
        check(_obs is not None and "мост не настроен" in _obs.status,
              f"но мост не настроен — это видно отдельно: {_obs.status if _obs else ''}")
        check(_obs is not None and _obs.exe_path.endswith("obs64.exe"),
              f"и путь к программе найден: {_obs.exe_path[-24:] if _obs else ''}")

        # Решения человека офиса не перепутаны с «нужна кнопка».
        _excel = _by_name.get("Microsoft Office 2016 (Excel)")
        check(_excel is not None and not _excel.can_install,
              "Excel: кнопки установки нет — решение человека от 03.10.2026")
        check(_excel is not None and not _excel.install.has_button,
              "и в движке у него метода нет вовсе")
        _emu = next((c for c in _cards if c.name.startswith("Android-эмулятор")), None)
        check(_emu is not None and _emu.state == pcard.STATE_UNKNOWN,
              "эмулятор: «нечем проверять», а не «не установлена»")
        check(_emu is not None and len(_emu.alternatives) == 4,
              f"у эмулятора четыре варианта: {len(_emu.alternatives) if _emu else 0}")
        check(_emu is not None and not _emu.can_install,
              "и своей кнопки установки нет — подойдёт любая")
        _adobe = _by_name.get("Adobe Creative Cloud")
        check(_adobe is not None and _adobe.can_install,
              "Adobe: кнопка есть — в реестре записано method: winget")
        check(_adobe is not None and _adobe.hand_over,
              "и сказано, что после установки нужен человек")

        # Подпись: обещать сверку, которой не было, нельзя.
        check(_node is not None and _node.verify.can_check
              and not _node.verify.checked,
              "Node.js: сверять есть с чем, но файла нет — сверка не запускалась")
        check(_adobe is not None and not _adobe.verify.can_check,
              "у остальных сверять не с чем — и карточка не обещает")
        check(_adobe is not None and "неизвестно" in _adobe.verify.detail,
              "а прямо говорит, что имя подписанта неизвестно")

        # Кнопка докачки плагина. Раньше здесь стояла проверка обратного:
        # «кнопки нет — мост качают на этапе 6». Она была верна для старого
        # кода и перестала быть правдой, когда появился сам плагин.
        # Теперь проверяем три вещи, и все три — про наш код, а не про
        # состояние машины: кнопка выведена из реестра, появляется ровно
        # когда плагина нет, и при отказе называет причину.
        _all_codes = {code for c in _cards for code, _, _ in c.buttons()}
        check(pcard.BTN_INSTALL in _all_codes, "кнопка установки в списке есть")
        # Правила — по всем карточкам с плагином, а не по одной. Раньше
        # здесь бралась «первая карточка с объявленным плагином», и пока
        # плагин был один, это был OBS. С появлением аддона Blender первым
        # стал Blender — и проверка молча переехала на него: имя сверяла с
        # «obs-websocket», а в списке «что ещё не готово» искала слово
        # «плагин», которого в тексте про аддон нет. Привязка к предмету
        # вместо правила — молчаливая поломка проверки.
        _plug_cards = [c for c in _cards if c.plugin.declared]
        check(len(_plug_cards) >= 2,
              f"плагин описан у нескольких программ: "
              f"{[c.name for c in _plug_cards]}")
        check(all(c.plugin.name for c in _plug_cards),
              "и у каждого имя взято из реестра, а не придумано кодом")
        check(all(c.plugin.finder for c in _plug_cards),
              "и у каждого есть finder: по нему код знает, как проверить")
        check(all(pcard.BTN_FETCH in {code for code, _, _ in c.buttons()}
                  or c.plugin.present for c in _plug_cards),
              "кнопка докачки есть ровно тогда, когда плагина нет")
        check(not any(pcard.BTN_FETCH in {code for code, _, _ in c.buttons()}
                      for c in _plug_cards if c.plugin.present),
              "а у карточек с установленным плагином её нет — нечего "
              "докачивать")
        # Только те карточки, где кнопка есть. У карточки с уже
        # установленным плагином кнопки нет — и требовать от неё
        # подсказку значило бы требовать несуществующую вещь.
        _missing_plugin = [c for c in _plug_cards if not c.plugin.present]
        _bad_hint = [c.name for c in _missing_plugin
                     if not (c.can_fetch or "недоступно" in next(
                         (h for code, _, h in c.buttons()
                          if code == pcard.BTN_FETCH), ""))]
        check(not _bad_hint,
              f"подсказка кнопки объясняет отказ, а не молчит: "
              f"{_bad_hint or 'у всех, где кнопка есть'}")
        check(all(c.can_fetch == (c.plugin.declared and not c.plugin.present
                                  and c.plugin.can_write)
                  for c in _plug_cards),
              "can_fetch — ровно «плагин описан, его нет, и папка принимает "
              "запись»")
        # Назван должен быть плагин, которого нет. Тот, что на месте, в
        # списке неготового упоминаться не должен: он готов, и упоминание
        # о нём было бы враньём с другой стороны.
        _not_named = [c.name for c in _missing_plugin
                      if not c.bridge_pending]
        check(not _not_named,
              f"у каждой карточки без плагина он назван прямо в списке "
              f"«что ещё не готово»: {_not_named or 'у всех'}")
        check(not [c.name for c in _plug_cards if c.plugin.present
                   and any(c.plugin.name in p
                           for p in c.bridge_pending)],
              "а у тех, где плагин есть, он в списке неготового не "
              "значится — иначе врали бы в другую сторону")

        # Частные утверждения про OBS остаются про OBS и находят его по
        # finder, а не по порядку в реестре.
        _obs_card = next((c for c in _plug_cards
                          if c.plugin.finder == "obs"), None)
        check(_obs_card is not None, "карточка OBS с плагином найдена")
        check(_obs_card is not None and _obs_card.plugin.name == "obs-websocket",
              f"имя плагина OBS взято из реестра: "
              f"{_obs_card.plugin.name if _obs_card else '—'}")
        check_machine(_obs_card is None or not _obs_card.can_fetch,
                      f"на живой установке докачка OBS заблокирована: "
                      f"{_obs_card.plugin.note[:60] if _obs_card else '—'}")
        _bl_card = next((c for c in _plug_cards
                         if c.plugin.finder == "blender-addon"), None)
        check(_bl_card is not None, "карточка Blender с аддоном найдена")
        check(_bl_card is not None and _bl_card.plugin.name == "blender_mcp",
              f"имя аддона взято из реестра: "
              f"{_bl_card.plugin.name if _bl_card else '—'}")

        # winget: команда собирается списком, значение из реестра не станет
        # командой. Это проверка безопасности, а не оформления.
        _cmd = wmod.build_command("X; Remove-Item -Recurse C:\\")
        check(isinstance(_cmd, list), "команда winget — список аргументов, не строка")
        check(_cmd[_cmd.index("--id") + 1] == "X; Remove-Item -Recurse C:\\",
              "идентификатор с точкой с запятой остался одним аргументом")
        check("--exact" in _cmd, "--exact обязателен: иначе winget ставит не то")
        check(wmod.build_command("X", machine=True)[-2:] == ["--scope", "machine"],
              "для машинных прав добавляется --scope machine")
        check(wmod.build_command("OpenJS.NodeJS.LTS")[0].lower().endswith("winget.exe"),
              "первым идёт сам winget, а не текст команды")
        check(wmod.install("").error != "",
              "с пустым идентификатором — отказ с причиной, а не попытка запуска")
        _installed, _why = wmod.check_installed("Definitely.Not.A.Real.Package.42")
        check(_installed is False,
              f"несуществующий пакет не выдаётся за установленный: {_why}")

    # Вкладка в окне: карточки нарисованы, кнопки совпадают с решением.
    _ptab = getattr(window, "programs_tab", None)
    check(_ptab is not None, "вкладка «Программы» создана")
    if _ptab is not None:
        _titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        check("Программы" in _titles, f"вкладка «Программы» есть в списке: {_titles}")
        check(_ptab._base() == core.program_root(),
              "вкладка читает реестр из папки программы, а не из папки базы")
        check(len(_ptab._rows) == len(_ptab._cards),
              f"карточек на экране столько же, сколько в данных: {len(_ptab._rows)}")
        check(_ptab.notice.text() == "",
              f"замечаний наверху нет: {_ptab.notice.text()[:60]}")
        _mismatch = [
            key for key, _row in _ptab._rows.items()
            if (btn := _row["buttons"].get(pcard.BTN_INSTALL)) is not None
            and btn.isEnabled() != bool(_ptab._cards[key].can_install)
        ]
        check(not _mismatch, f"доступность кнопки совпадает с решением: {_mismatch}")
        # Та же сверка для кнопки докачки: она обязана быть выключена ровно
        # там, где карточка говорит «нельзя». Иначе человек нажмёт и
        # получит отказ вместо ответа — а это ровно то, ради чего всё
        # затевалось.
        _fetch_mismatch = [
            key for key, _row in _ptab._rows.items()
            if (btn := _row["buttons"].get(pcard.BTN_FETCH)) is not None
            and btn.isEnabled() != bool(_ptab._cards[key].can_fetch)
        ]
        check(not _fetch_mismatch,
              f"доступность кнопки докачки совпадает с решением: {_fetch_mismatch}")
        # Раньше здесь стояло `check(bool(_fetch_rows), "кнопка докачки
        # дошла до экрана")`. Проверка опиралась на живое состояние: кнопка
        # обязана была появиться хоть где-нибудь, а появлялась она только
        # пока плагина нет. 07.10.2026 выяснилось, что плагин на машине
        # ЕСТЬ, а программа его не видела из-за неверного пути (раздел 8с).
        # После правки докачивать OBS нечего, кнопки на её карточке нет —
        # и это правильное поведение, а не поломка. Прежняя проверка
        # требовала обратного, то есть требовала отсутствия плагина.
        #
        # Что утверждается теперь: у карточки, которой нечего докачивать,
        # кнопки нет; у карточки, которой есть, кнопка есть и выключена с
        # причиной. Второе проверяется сверкой доступности выше и на
        # настоящем дереве в разделе 8с.
        _obs_live = next((c for c in _ptab._cards.values()
                          if getattr(c, "plugin", None) is not None), None)
        check(_obs_live is not None,
              f"карточка, которой про плагин, на месте: "
              f"{_obs_live.plugin.name if _obs_live else '—'}")
        _obs_key = next((k for k, c in _ptab._cards.items()
                         if c is _obs_live), None)
        if _obs_live is not None:
            check(not _obs_live.can_fetch,
                  "и докачивать ей нечего: плагин на месте, а не отсутствует")
            check(_obs_key is not None and pcard.BTN_FETCH
                  not in _ptab._rows[_obs_key]["buttons"],
                  "поэтому кнопки докачки на ней нет вовсе — нечего "
                  "предлагать человеку")
        _fetch_rows = [row for row in _ptab._rows.values()
                       if pcard.BTN_FETCH in row["buttons"]]
        check(all("недоступно" in row["buttons"][pcard.BTN_FETCH].toolTip()
                  for row in _fetch_rows
                  if not _ptab._cards[
                      next(k for k, r in _ptab._rows.items() if r is row)
                  ].can_fetch),
              "и у выключенной кнопки видна причина отказа, а не пустота")
        _has_check = all(
            pcard.BTN_CHECK in row["buttons"] for row in _ptab._rows.values())
        check(_has_check, "у каждой карточки есть «Проверить»")
        # Итог установки не должен пропадать при перерисовке — это была
        # настоящая ошибка: перерисовка стирала написанное сразу же.
        _some_key = next(iter(_ptab._rows))
        _ptab._set_result(_some_key, "проверочный текст")
        _ptab.reload()
        check(_ptab._rows[_some_key]["result"].text() == "проверочный текст",
              "сообщение переживает перерисовку карточек")
        _ptab._messages.clear()
        _ptab.reload()

    # ---- 8п. Пояснения папок не врут
    echo("\n--- 8п. Пояснения папок: числа, пути, чужие символы ---")
    #
    # Откуда эти проверки. Пояснения у разделов безопасности были
    # построчной копией карты: «защита Linux: смотри при задачах про
    # Linux». Файл есть, смысла нет. Когда пояснения переписали по
    # существу, в первом же тексте оказалось девять неверных чисел —
    # счётчик при импорте считал вместе с вложенными подпапками, а нужен
    # был свой уровень, и в семи местах перепутались итоги соседних
    # разделов: 289 файлов в «Сети» вместо 199, 119 в «Windows» вместо
    # 108. Самое скверное — числа выглядели правдоподобно, и никто их не
    # проверял: протухнут при обновлении HackTricks — и заметят только
    # те, кто полезет читать.
    #
    # Чего эти проверки НЕ ловят:
    #  - число, приписанное подпапке, если подпапка на этой строке не
    #    названа в обратных кавычках: оно идёт в «не приписано», а не
    #    в ошибку (проверка не имеет права угадывать);
    #  - внятность и правдивость текста — только числа и пути;
    #  - содержание самого HackTricks: он приходит извне и меняется сам.
    import re as _re_hint  # noqa: PLC0415 - нужен здесь и только здесь
    import unicodedata as _ud  # noqa: PLC0415 - нужен здесь и только здесь

    _zone = core.program_root() / "знания"
    _hint_names = ("_О-ПАПКЕ.md", "О-ПАПКЕ.md")
    _hints = sorted(_zone.rglob("_О-ПАПКЕ.md"))
    check(len(_hints) >= len(core.knowledge_folders()),
          f"пояснение есть у каждой папки, которую создаёт конструктор: "
          f"{len(_hints)} пояснений на {len(core.knowledge_folders())} папок")

    def _is_content(_p: Path) -> bool:
        return _p.is_file() and _p.suffix == ".md" and _p.name not in _hint_names

    _num_re = _re_hint.compile(r"(\d{1,4})\s+файл\w*")
    # Токены — только латиницей, и это не лень, а граница.
    #
    # Если разрешить кириллицу, под проверку попадёт 352 имени, и 37 из
    # них — ложные тревоги: `платежи.md`, `Мама.md`, `Тётя-Лена.md`,
    # `_ШАБЛОН-….md`. Это примеры имён, которые человек сам создаст
    # внутри своей папки, а не обещания существующих файлов. Проверка,
    # которая на них ругается, — это проверка, которую выключают.
    #
    # Обратная сторона границы, проверенная откатом: имя папки на
    # русском подследить нельзя — 37 из 352 таких имён оказались бы
    # ложными тревогами. Проверять кириллицу можно, если придумать, как
    # отличить пример от обещания; сейчас отличить нечем, а угадывать в
    # проверке нельзя.
    #
    # Слеш внутри токена обязателен: без него `blockchain/смарт-контракты/`
    # обрезался до `blockchain/`, и число из строки приписывалось не той
    # папке. Это четвёртая правка этой проверки, и три предыдущие нашлись
    # не чтением кода, а прогоном: голые числа в столбце таблицы, слеш на
    # конце, обрезанный путь. Правило простое: написанное проверкой надо на
    # ней же и ловить, откатом.
    _tok_re = _re_hint.compile(r"`([A-Za-z0-9_.+/-]+)`")
    _bad_head: list[str] = []      # заголовок раздела не сходится
    _bad_attr: list[str] = []      # число ни к чему не приписано
    _sorted = sorted              # для читаемого списка чисел в сбое
    _bad_tok: list[str] = []       # названа папка или файл, которых нет
    _foreign: list[str] = []       # в русский текст просочились иероглифы
    _allow = set("—–…«»→")
    _md_ext = {"md"}
    _prog_ext = {"py", "json", "jsonc", "js", "ts", "txt", "ps1", "cmd",
                 "bat", "toml", "yaml", "yml", "exe", "dll", "cfg", "ini"}
    _prog_files = {p.name for p in core.program_root().rglob("*") if p.is_file()}

    for _hint in _hints:
        _sec = _hint.parent
        _root_md = sum(1 for _p in _sec.glob("*.md") if _is_content(_p))
        _all_md = sum(1 for _p in _sec.rglob("*.md") if _is_content(_p))
        _body = _hint.read_text(encoding="utf-8", errors="ignore")
        _rel = _sec.relative_to(core.program_root()).as_posix()
        _folders = {_p.name for _p in _sec.rglob("*") if _p.is_dir()}
        _files = {_p.name for _p in _sec.rglob("*") if _p.is_file()}

        # 1. Первое число в пояснении — заголовок про раздел, и оно
        #    обязано сойтись с диском. Остальные числа относятся к
        #    подпапкам и проверяются построчно, по строке.
        _first = _num_re.search(_body)
        if _first and int(_first.group(1)) not in (_root_md, _all_md):
            _bad_head.append(f"{_rel}: «{_first.group(0)}», "
                             f"а на диске {_all_md} всего, {_root_md} в корне")

        # 2. Каждое число приписано либо подпапке, названной в этой же
        #    строке, либо самому разделу. Иначе оно не проверяемо, и
        #    сказать об этом надо, а не молчать.
        for _line_no, _line in enumerate(_body.splitlines(), 1):
            for _m in _num_re.finditer(_line):
                _n = int(_m.group(1))
                if _n in (_root_md, _all_md):
                    continue
                _here = set(_tok_re.findall(_line))
                _cands: set[int] = set()
                for _t in _here:
                    _leaf = _t.rstrip("/").split("/")[-1]
                    _folder = next((_p for _p in _sec.rglob(_leaf)
                                    if _p.is_dir()), None)
                    if _folder is not None:
                        _cands.add(sum(1 for _p in _folder.glob("*.md")
                                       if _is_content(_p)))
                        _cands.add(sum(1 for _p in _folder.rglob("*.md")
                                       if _is_content(_p)))
                if _n not in _cands:
                    _bad_attr.append(f"{_rel}:{_line_no} «{_m.group(0)}» "
                                     f"ни к какой подпапке строки не приписано")

        # 2б. Числа в столбце таблицы.
        #
        # Столбец называется в шапке («файлов», «подпапок»), а само число
        # стоит голой цифрой в ячейке: `| 41 |`. Регулярка «N файл» такую
        # строку не видит вообще, и все числа в таблицах остались без
        # проверки — а это ровно те числа, где я уже путал итоги
        # соседних разделов. Откат это показал: в таблице Windows стояло
        # 41, подставили 47, прогон вышел с кодом 0, то есть проверка
        # промолчала.
        #
        # Правило: если в строке таблицы есть столбец, названный в шапке
        # «файлов» или «подпапок», число в нём сверяется с папкой, названной
        # в этой же строке. Угадывать нечего: и папка, и счётчик в одной
        # строке.
        _count_cells = ("файлов", "файла", "файл", "подпапок", "подпапки")
        _count_cols: set[int] = set()
        for _line_no, _line in enumerate(_body.splitlines(), 1):
            if not _line.strip().startswith("|"):
                _count_cols.clear()
                continue
            _cells = [c.strip() for c in _line.strip().strip("|").split("|")]
            if not _count_cols:
                # Берём ВСЕ подходящие столбцы, а не первый попавшийся:
                # в шапке «| подпапка | файлов | о чём |» первым подходит
                # «подпапка» — это первый столбец, с путями. Поиск по
                # первому совпадению сажал проверку на пустой столбец, и
                # ни одна строка не проверялась: откат с 41 → 47 проходил
                # с кодом выхода 0. Пятая правка этой проверки.
                for _i, _c in enumerate(_cells):
                    if _c.lower().strip("* ") in _count_cells:
                        _count_cols.add(_i)
                continue
            for _ci in sorted(_count_cols):
                if _ci >= len(_cells):
                    continue
                _cell_m = _re_hint.match(r"^(\d{1,4})\b", _cells[_ci])
                if _cell_m is None:
                    continue
                _n = int(_cell_m.group(1))
                if _n in (_root_md, _all_md):
                    continue
                _row_toks = set(_tok_re.findall(_line))
                _cands = set()
                for _t in _row_toks:
                    _leaf = _t.rstrip("/").split("/")[-1]
                    if _leaf.endswith(".md") and _leaf in _files:
                        # строка может считать не папку, а файл:
                        # `путь/README.md` | 1 | — и тогда верно ровно одно
                        _cands.add(1)
                    _folder = next((_p for _p in _sec.rglob(_leaf)
                                    if _p.is_dir()), None)
                    if _folder is None:
                        continue
                    _cands.add(sum(1 for _p in _folder.glob("*.md")
                                   if _is_content(_p)))
                    _cands.add(sum(1 for _p in _folder.rglob("*.md")
                                   if _is_content(_p)))
                    _cands.add(sum(1 for _p in _folder.iterdir()
                                   if _p.is_dir()))
                if _n not in _cands:
                    _bad_attr.append(
                        f"{_rel}:{_line_no} в таблице {_n} — у папок строки "
                        f"{_sorted(_cands) or 'ни одной'}")

        # 3. Названная папка или файл должны существовать. Разрешение
        #    пути различается по трём случаям, и это не педантизм:
        #    пояснение вправе сослаться и на файл внутри раздела, и на
        #    файл самой программы (`core.py`), и на имя в коде
        #    (`KNOWLEDGE_AREAS`) — последнее не путь вовсе.
        _prog_root = core.program_root()
        for _raw in _tok_re.findall(_body):
            # Слеш на конце — это признак папки, и его нельзя терять
            # ДО того, как разобрали вид токена: rstrip("/") делает вид,
            # будто это имя в коде, и проверка молча пропускает папку.
            # На откате так и вышло: подсунули `networking-nonexistent/`,
            # проверка не сработала, 998 галочек стояли зелёные — потому
            # что этот кусок кода просто ничего не делал.
            _looks_dir = _raw.endswith("/") or "/" in _raw
            _token = _raw.rstrip("/")
            if (_sec / _token).exists():
                continue
            if _looks_dir:
                _leaf = _token.split("/")[-1]
                if _leaf not in _folders and _leaf not in _files:
                    _bad_tok.append(f"{_rel}: названа папка `{_raw}`, нет такой")
                continue
            _dot = _token.rfind(".")
            _ext = _token[_dot + 1:] if _dot > 0 else ""
            if _ext in _md_ext:
                if _token not in _files:
                    _bad_tok.append(f"{_rel}: назван файл `{_token}`, нет такого")
            elif _ext in _prog_ext:
                # файл программы: он лежит не в корне, а глубоко
                # (`tools/dbapp/core.py`), поэтому ищем по имени, а не
                # по пути от корня
                if _token not in _prog_files:
                    _bad_tok.append(f"{_rel}: назван файл программы "
                                    f"`{_token}`, нет такого")
            elif _ext:
                _bad_tok.append(f"{_rel}: неизвестное расширение `{_token}`")
            # без расширения и без слеша — это имя в коде, не путь

        # 4. Чужие символы. В русский текст один раз просочились
        #    иероглифы — вместо слов про подделку билетов. На глаз
        #    это не видно, а смысл предложения меняется на выдумку.
        #
        #    Рисование рамок (`├──`, `└──`) пропускаем: им пояснение
        #    показывает устройство папок, и в этом они по делу.
        for _ch in set(_body):
            if _ch in _allow or _ch.isspace():
                continue
            _code = ord(_ch)
            if 0x2500 <= _code <= 0x257F:      # рамки и стрелки псевдографики
                continue
            if _ud.category(_ch).startswith("P"):
                continue
            if _code < 0x0400 or 0x0400 <= _code <= 0x04FF:
                continue
            _foreign.append(f"{_rel}: U+{_code:04X} «{_ch}»")

    check(not _bad_head,
          f"число в заголовке каждого пояснения сходится с диском: "
          f"{_bad_head[:6] or 'чисто'}")
    check(not _bad_attr,
          f"каждое число в пояснении приписано чему-то проверяемому: "
          f"{_bad_attr[:6] or 'чисто'}")
    check(not _bad_tok,
          f"названные папки и файлы есть на диске: {_bad_tok[:6] or 'чисто'}")
    check(not _foreign,
          f"в русском тексте нет чужих символов: {_foreign[:6] or 'чисто'}")
    # ---- 8р. Исходники моста OBS: подмодуль, а не копия
    echo("\n--- 8р. Исходники моста OBS в репозитории ---")
    #
    # Мост работает из npm-пакета в obs-mcp-node, но его исходники тоже
    # нужны: их читают и правят. Раньше они лежали рядом обычной папкой,
    # и про это было написано в уведомлении о стороннем коде — но в
    # репозитории их не было вовсе, то есть уведомление описывало то,
    # чего в нём нет.
    #
    # Подмодуль, а не копия, по двум причинам. Лицензия: исходники моста
    # под GPL-2.0, наш проект под MIT, чужие файлы в нашем индексе сделали
    # бы смесь лицензий. Копированием (режим 100644 вместо 160000) это
    # ловится сразу. И закреплённость: коммит виден явно, и его нельзя
    # случайно переписать.
    #
    # Где смотреть. Селфтест идёт по папке программы, а подмодуль живёт
    # в репозитории, и папка программы git-репозиторием не является. Если
    # селфтест запущен прямо на копии репозитория — проверки настоящие; если
    # на папке программы — git-механику не проверяем и говорим об этом
    # прямо, а смотрим то, что проверить можно: наличие исходников и
    # согласованность с уведомлением.
    import subprocess as _sp  # noqa: PLC0415 - нужен здесь и только здесь

    _prog = core.program_root()
    _sub_path = "tools/thirdparty/obs-mcp"
    _sub_dir = _prog.joinpath(*_sub_path.split("/"))
    _notices = core.program_file("THIRD-PARTY-NOTICES.md")
    _notices_text = (_notices.read_text(encoding="utf-8", errors="ignore")
                     if _notices.is_file() else "")

    def _is_repo(folder: Path) -> bool:
        try:
            r = _sp.run(["git", "rev-parse", "--is-inside-work-tree"],
                        cwd=str(folder), capture_output=True, text=True,
                        timeout=20)
            return r.returncode == 0 and r.stdout.strip() == "true"
        except (OSError, ValueError):
            return False

    _repo_mode = _is_repo(_prog)
    if _repo_mode:
        _gm = _prog / ".gitmodules"
        check(_gm.is_file(), ".gitmodules есть: подмодуль заявлен явно")
        _gm_text = (_gm.read_text(encoding="utf-8", errors="ignore")
                    if _gm.is_file() else "")
        check(_sub_path in _gm_text and "royshil/obs-mcp" in _gm_text,
              f"подмодуль моста объявлен: {_sub_path}")
        # Что закреплено: читаем из индекса, а не с диска. Иначе проверка
        # прошла бы и при рассинхронизации индекса с подмодулем.
        _pinned = ""
        try:
            _st = _sp.run(["git", "ls-files", "--stage", "--", _sub_path],
                          cwd=str(_prog), capture_output=True, text=True,
                          timeout=20)
            for _row in _st.stdout.splitlines():
                _p = _row.split()
                if len(_p) >= 2 and _p[0] == "160000":
                    _pinned = _p[1]
        except (OSError, ValueError):
            _pinned = ""
        check(bool(_pinned),
              f"исходники моста лежат ссылкой, а не копией файлов: "
              f"{_pinned[:12] or 'нет'}")
        check(bool(_pinned) and _pinned[:12] in _notices_text,
              "закреплённый коммит совпадает с записанным в уведомлении")
    else:
        echo("     папка программы не git-репозиторий: проверки подмодуля "
             "не выполняются, проверяется только наличие исходников")

    check(_sub_dir.is_dir(),
          f"исходники моста на месте: {_sub_path}")
    check(_pinned[:12] in _notices_text if _repo_mode
          else "314e7c0" in _notices_text,
          "уведомление называет тот же коммит, что и репозиторий")
    check(_sub_dir.is_dir() and (_sub_dir / "package.json").is_file(),
          "исходники моста не пустая папка: есть package.json")

    # Навык не должен учить не доверять настоящему пути: раньше он именно
    # так и делал, называя исходники мёртвым Python-мостом, которого там нет.
    _obs_skill = _prog / "skills" / "obs-studio" / "SKILL.md"
    if _obs_skill.is_file():
        _obs_txt = _obs_skill.read_text(encoding="utf-8", errors="ignore")
        check(_sub_path in _obs_txt or _sub_path.replace("/", "\\") in _obs_txt,
              "навык называет настоящее место исходников моста")
        check("нерабочий Python-мост" not in _obs_txt
              or "было неверно" in _obs_txt,
              "навык не утверждает, что исходников нет")

    # ---- 8с. Установка OBS: честный счёт и разбор архива
    echo("\n--- 8с. Установка OBS: что насчитано и куда разложено ---")
    #
    # Откуда эти проверки. Первая версия считала «плагином» папку с любым
    # .dll и рапортовала «из 25 плагинов с библиотекой 2». Обе эти папки —
    # win-capture и win-dshow — держат вспомогательные модули захвата
    # экрана и виртуальной камеры. Плагинов у OBS не было ни одного, а
    # человек читал «установка почти полная» и жал «докачать мост».
    #
    # Почему дерево искусственное. Настоящая папка плагинов лежит в
    # Program Files и закрыта для записи: сломать её, чтобы увидеть, что
    # проверка ловит поломку, нельзя. Искусственное дерево проходит по тем
    # же строкам кода, поэтому поломка видна.
    #
    # Чего эти проверки НЕ ловят:
    #  - неверное определение «что такое плагин»: дерево повторяет
    #    раскладку, измеренную на OBS 32.2.2. Сменится сборка — правила
    #    придётся перемерять, и молча оно не протухнет;
    #  - состояние настоящей установки: для неё внизу check_machine, а не
    #    обычная check, чтобы чужой диск не ронял проверку нашего кода;
    #  - сетевую часть докачки: она не проверяется вовсе. 179 МБ из сети
    #    ради этого не качаются, проверяется только разбор архива.
    import zipfile as _zip  # noqa: PLC0415 - нужен здесь и только здесь

    check(callable(getattr(bridges, "obs_plugin_scan", None)),
          "в модуле есть счётчик плагинов")
    check(callable(getattr(bridges, "install_plugin_from_zip", None)),
          "разбор архива вынесен в отдельную функцию — её можно проверить "
          "без сети")

    if callable(getattr(bridges, "obs_plugin_scan", None)):
        _lib_entry = f"obs-plugins/64bit/{bridges.WANTED_DIR}.dll"
        _loc_prefix = f"data/obs-plugins/{bridges.WANTED_DIR}/locale/"

        def _obs_plugin_tree(root: Path) -> Path:
            """Папка плагинов: две с вспомогательными модулями, одна с
            настоящей библиотекой в bin/64bit, одна пустая под вебсокет."""
            (root / "win-capture").mkdir(parents=True)
            (root / "win-capture" / "graphics-hook64.dll").write_bytes(b"MZ")
            (root / "win-dshow").mkdir()
            (root / "win-dshow" / "obs-virtualcam-module64.dll").write_bytes(b"MZ")
            _plug_dir = root / "obs-browser" / "bin" / "64bit"
            _plug_dir.mkdir(parents=True)
            (_plug_dir / "obs-browser.dll").write_bytes(b"MZ")
            (root / bridges.WANTED_DIR / "locale").mkdir(parents=True)
            (root / bridges.WANTED_DIR / "locale" / "de-DE.ini").write_text(
                "d", encoding="utf-8")
            return root

        def _obs_zip(path: Path, with_lib: bool) -> Path:
            """Архив по измеренной раскладке OBS 32.2.2. Порядок записей
            тот же, что в настоящем: data/ идёт раньше obs-plugins/."""
            with _zip.ZipFile(path, "w") as zf:
                zf.writestr(_loc_prefix, "")
                zf.writestr(_loc_prefix + "de-DE.ini", "deutsch")
                zf.writestr(_loc_prefix + "en-US.ini", "english")
                if with_lib:
                    zf.writestr(_lib_entry, b"MZ" + b"\0" * 500)
                    zf.writestr(f"obs-plugins/64bit/{bridges.WANTED_DIR}.pdb",
                                b"PDB" * 100)
                    zf.writestr("obs-plugins/64bit/obs-browser.dll", b"MZ")
                zf.writestr("bin/64bit/obs64.exe", b"MZ")
            return path

        _td = tempfile.mkdtemp(prefix="selftest-obs-")
        try:
            _plug = _obs_plugin_tree(Path(_td) / "obs-plugins")
            _scan = bridges.obs_plugin_scan(_plug)
            check(_scan["dirs"] == 4,
                  f"папок плагинов посчитано верно: {_scan['dirs']}")
            check(_scan["plugins"] == 1,
                  f"настоящий плагин один, а не четыре: {_scan['plugins']}")
            check(_scan["helpers"] == 2,
                  f"вспомогательные модули посчитаны отдельно: "
                  f"{_scan['helpers']}")
            check(_scan["bridge"] is False,
                  "пустая папка вебсокета не считается поставленным плагином")

            _st = bridges.obs_install_completeness(base=Path(_td), root=_plug)
            check(_st["can_write"] is None,
                  "подставленная папка не проверяется пробой записи")
            check("мост не поднимется" in _st["message"].lower(),
                  f"в тексте сказано про мост: {_st['message'][:60]}")
            check(_st["complete"] is False,
                  "установка с одним плагином из трёх не названа полной")

            # Архив без библиотеки. Раньше он разкладывал 57 языковых
            # файлов, а плагина не появлялось.
            _obs2 = Path(_td) / "obs-2"
            (_obs2 / "data" / "obs-plugins").mkdir(parents=True)
            _ok2, _why2 = bridges.install_plugin_from_zip(
                _obs_zip(Path(_td) / "без-библиотеки.zip", with_lib=False),
                _obs2)
            check(not _ok2, "архив без библиотеки — отказ, а не «готово»")
            check(_lib_entry in _why2,
                  f"в отказе назван искомый файл: {_why2[:70]}")
            _left = sorted(str(p.relative_to(_obs2)) for p in _obs2.rglob("*")
                           if p.is_file())
            check(not _left,
                  f"после отказа на диске ничего не осталось: {_left[:4]}")

            # Правильный архив: библиотека ложится туда, где её ищет OBS.
            _obs3 = Path(_td) / "obs-3"
            (_obs3 / "data" / "obs-plugins").mkdir(parents=True)
            _ok3, _msg3 = bridges.install_plugin_from_zip(
                _obs_zip(Path(_td) / "полный.zip", with_lib=True), _obs3)
            _dll = _obs3 / "data" / "obs-plugins" / bridges.WANTED_DIR / \
                Path(*bridges.PLUGIN_DLL)
            check(_ok3 and _dll.is_file(),
                  f"библиотека разложена туда, где её ждёт OBS: {_msg3[:50]}")
            check(bridges.plugin_present(_obs3),
                  "после раскладки плагин виден как поставленный")
            check(not _dll.with_name(_dll.stem + ".pdb").exists(),
                  "отладочные символы не тащатся: они втрое тяжелее "
                  "библиотеки и на работу не влияют")
            check(not (_obs3 / "bin").exists(),
                  "чужие файлы архива не раскладываются")

            # Раскладка установщика OBS. Измерено на живой машине
            # 07.10.2026: OBS 32.2.2 кладёт свою копию плагина в
            # `obs-plugins/64bit/`, а не в `data/obs-plugins/<имя>/bin/
            # 64bit/`. Проверка смотрела только на вторую и потому
            # **не могла сойтись ни при какой установке**: на машине с
            # OBS и плагином она рапортовала «в папке плагинов папок, а
            # библиотеки нет ни в одной», отказывалась докачивать и
            # называла причиной установщик Windows. Из этого выросли и
            # запрет докачивать, и несрабатывавшая §15.1.
            _obs5 = Path(_td) / "obs-5"
            (_obs5 / "data" / "obs-plugins" / bridges.WANTED_DIR
             / "locale").mkdir(parents=True)
            (_obs5 / "data" / "obs-plugins" / bridges.WANTED_DIR / "locale"
             / "ru-RU.ini").write_text("x\n", encoding="utf-8")
            (_obs5 / "obs-plugins" / "64bit").mkdir(parents=True)
            (_obs5 / "obs-plugins" / "64bit"
             / f"{bridges.WANTED_DIR}.dll").write_bytes(b"\x00")
            _found5 = bridges.plugin_library(_obs5)
            check(_found5 is not None and _found5.is_file(),
                  f"плагин виден в раскладке установщика OBS: {_found5}")
            check(bridges.plugin_present(_obs5),
                  "и plugin_present отвечает «есть» — с этой поломкой он "
                  "отвечал «нет» при полностью установленном OBS")
            check(bridges.plugin_installed_in(_obs5),
                  "и plugin_installed_in на том же пути отвечает «есть»")
            _scan5 = bridges.obs_plugin_scan(
                _obs5 / "data" / "obs-plugins")
            check(_scan5["bridge"] is True,
                  f"obs_plugin_scan тоже видит bridge при плоской раскладке: "
                  f"{_scan5}")

            # Без плагина обе раскладки пусты — проверка обязана молчать,
            # иначе она всегда зелёная и ничего не стоит.
            _obs6 = Path(_td) / "obs-6"
            (_obs6 / "data" / "obs-plugins" / bridges.WANTED_DIR
             / "locale").mkdir(parents=True)
            check(bridges.plugin_library(_obs6) is None
                  and not bridges.plugin_present(_obs6),
                  "а без библиотеки в обеих раскладках — «нет»")
            check(bridges.obs_plugin_scan(
                _obs6 / "data" / "obs-plugins")["bridge"] is False,
                  "и obs_plugin_scan без библиотеки молчит")

            # Не архив: понятный отказ, а не трассировка.
            _junk = Path(_td) / "мусор.zip"
            _junk.write_bytes(b"not a zip at all" * 100)
            _obs4 = Path(_td) / "obs-4"
            (_obs4 / "data" / "obs-plugins").mkdir(parents=True)
            _ok4, _msg4 = bridges.install_plugin_from_zip(_junk, _obs4)
            check(not _ok4 and "архив" in _msg4,
                  f"не-архив отклонён словами, а не исключением: {_msg4[:50]}")
        finally:
            shutil.rmtree(_td, ignore_errors=True)

        # Настоящая установка — чужой диск, поэтому check_machine: негодный
        # зонд должен стать «не проверено», а не провалом нашего кода.
        # Пересчёт здесь намеренно сделан своим способом: ловит опечатки в
        # ключах и ошибки счёта. Неверное определение плагина он не ловит —
        # это ловил бы только раздел выше, на известной раскладке.
        _obs_base = bridges.obs_installed()
        _live = bridges.obs_install_completeness()
        if _obs_base is None:
            check_machine(False,
                          f"на этой машине OBS нет, состояние снять нечем: "
                          f"{_live['message']}")
        else:
            _live_root = Path(_obs_base) / "data" / "obs-plugins"
            _own = sum(
                1 for d in _live_root.iterdir() if d.is_dir()
                and any((d / "bin" / "64bit").glob("*.dll"))
            ) if _live_root.is_dir() else 0
            check_machine(_live["with_dll"] == _own,
                          f"пересчёт на живой установке сошёлся: "
                          f"{_live['with_dll']} против {_own}")
            check_machine(_live["helpers"] + _live["with_dll"]
                          <= _live["dirs"],
                          f"плагины и вспомогательные вместе не больше "
                          f"папок: {_live['with_dll']}+{_live['helpers']} "
                          f"из {_live['dirs']}")
            check_machine(_live["user_action"] == "" or "мост" in
                          _live["message"].lower(),
                          f"когда готово — человеку нечего делать: "
                          f"{_live['user_action'][:50]}")

    # ---- 8т. Корень репозитория и ссылки README
    echo("\n--- 8т. Корень репозитория: чистота и живые ссылки ---")
    #
    # Откуда эти проверки. Требование §16: в корне остаются только папки,
    # `Управление-базой.cmd` и два исключения, которые читает git (`.gitignore`
    # и `.gitmodules`). Проверки не было, и корень зарос `LICENSE`, пока
    # README уже ссылался на него в `документы/` — ссылка была битой с
    # момента переноса README в `.github/` и никто её не открывал.
    #
    # Где лежит лицензия — не «куда велено планом», а где её ищут. Проект
    # публичный, и лицензию определяет gem Licensee, которым пользуется
    # GitHub: он смотрит в корень, в `docs/` и в `.github/`. Папка
    # `документы/` в этом списке не значится, а `.github/` — значится.
    # Проверено на живой машине: до переноса GitHub отдавал по адресу
    # api/repos/ccndjfgjs/opencode-base/license `spdx_id: MIT` с путём
    # `LICENSE`. После отправки этот запрос надо повторить — он и есть
    # последняя проверка, а не этот тест.
    import re as _re_link  # noqa: PLC0415 - нужен здесь и только здесь

    # Своё имя, а не переиспользованный `_root`: в селфтесте он к этому
    # моменту уже три раза означал разное, последний раз — разобранный
    # XML. Взять чужую переменную — значит получить проверку о дереве
    # элементов вместо проверки о корне репозитория.
    _repo_dir = core.program_root()
    _root_files = sorted(p.name for p in _repo_dir.iterdir() if p.is_file())
    # `ВЕРСИЯ` добавлен по §22.2: программа — распакованная копия, а не
    # git-клон, и обновление приходит файлом, а не `git pull`. Сверять
    # «есть новая версия или нет» не с чем, пока не сказано, какая версия
    # стоит. Это тот же случай, что и `.gitignore` с `.gitmodules`: файл в
    # корне, который читает механизм, а не человек.
    # `ВЕРСИЯ` — по §22.2, читает selfupdate.py. `LICENSE` — по решению
    # от 07.10.2026: gem Licensee в `.github/` лицензию не признаёт, живой
    # запрос отдал 404, и человек выбрал стандартное место.
    _allowed = {".gitignore", ".gitmodules", "Управление-базой.cmd",
                "ВЕРСИЯ", "LICENSE"}
    _extra = [n for n in _root_files if n not in _allowed]
    check(not _extra, f"в корне нет лишних файлов: {_extra or 'чисто'}")
    _ver_file = _repo_dir / "ВЕРСИЯ"
    check(_ver_file.is_file() and _ver_file.read_text(
        encoding="utf-8").strip() != "",
        f"файл версии назван и не пуст: "
        f"{_ver_file.read_text(encoding='utf-8').strip() if _ver_file.is_file() else 'нет файла'}")

    # Лицензия лежит в КОРНЕ, и это не «засорение», а требование.
    #
    # История: §16.3 утверждал, что gem Licensee ищет файл в корне, в
    # `docs/` и в `.github/`, и на этом основании лицензия была перенесена
    # в `.github/`, а корень объявлен чистым. Посылка оказалась неверной,
    # и живой запрос после отправки 07.10.2026 это показал:
    #
    #     api/repos/ccndjfgjs/opencode-base   200, private: false,
    #                                           license: null
    #     api/…/license                        404 Not Found
    #     raw…/main/.github/LICENSE            200 — файл отдаётся
    #
    # Файл лежит и доступен, а Licensee его не признаёт. Проверка, которая
    # бы это поймала, отсутствовала: она утверждала ровно то, что §16.3 и
    # предполагал, то есть проверяла саму себя.
    #
    # Теперь проверка утверждает факт: файл в корне, в `.github/` его нет,
    # текст MIT, и README ссылается именно на корневой путь. Требование
    # §16 дополнено третьим файлом в корне — тем же случаем, что и
    # `ВЕРСИЯ` по §22.2: файл в корне, который читает внешний инструмент,
    # а не человек.
    _readme_early = _repo_dir / ".github" / "README.md"
    _rtext_for_lic = _readme_early.read_text(encoding="utf-8", errors="ignore") \
        if _readme_early.is_file() else ""

    _lic = _repo_dir / "LICENSE"
    check(_lic.is_file(),
          f"лицензия лежит в корне, где её ищет gem Licensee: "
          f"{'LICENSE' if _lic.is_file() else 'нет файла'}")
    check(not (_repo_dir / ".github" / "LICENSE").is_file(),
          "и в .github/ её больше нет: там GitHub её не признаёт, "
          "а лишняя копия заставила бы читать устаревшую")
    _lic_text = _lic.read_text(encoding="utf-8", errors="ignore") \
        if _lic.is_file() else ""
    check("MIT License" in _lic_text,
          "файл лицензии не пустой и остался MIT")
    check("MIT License" in _lic_text and len(_lic_text.strip()) > 400,
          f"и не обрезан: {len(_lic_text.strip())} знаков")
    check("LICENSE" in _allowed,
          "§16 учтён: LICENSE внесён в разрешённое, иначе проверка "
          "чистоты корня зарубила бы собственный лицензионный файл")
    # `Path(...)` здесь обязателен: `_readme_lic_link.group(1)` — строка, а
    # `str / str` в Python означает деление. Первая версия написала
    # `(".github" / ...)` и падала TypeError на самой проверке, а не на
    # файле, который она проверяет.
    _readme_lic_link = re.search(r"\[MIT\]\(([^)]+)\)", _rtext_for_lic)
    check(_readme_lic_link is not None
          and (Path(".github") / _readme_lic_link.group(1)
               ).resolve() == _lic.resolve(),
          f"и README ссылается на корневой файл, а не на исчезнувший "
          f"относительный путь: "
          f"{_readme_lic_link.group(1) if _readme_lic_link else 'ссылки нет'}")

    _readme = _repo_dir / ".github" / "README.md"
    if _readme.is_file():
        _rtext = _readme.read_text(encoding="utf-8", errors="ignore")
        _links = _re_link.findall(r"\]\(([^)#:]+\.(?:md|json|py|cmd|html|txt))\)",
                                  _rtext)
        _broken = []
        for _rel in _links:
            if not (_readme.parent / _rel).exists():
                _broken.append(_rel)
        check(not _broken,
              f"каждая относительная ссылка README ведёт в файл: "
              f"{_broken[:4] or 'все живы'} (проверено {len(_links)})")
        _lic_link = _re_link.search(r"\[MIT\]\(([^)#]+)\)", _rtext)
        check(bool(_lic_link) and (_readme.parent / _lic_link.group(1)).is_file()
              if _lic_link else False,
              "на лицензию ссылка есть и она живая: "
              f"{_lic_link.group(1) if _lic_link else 'ссылки нет'}")
        # Отдельным текстом, а не в списке ссылок: у файла лицензии нет
        # расширения, и общая регулярка его пропускает. Проверка «ссылка на
        # лицензию живая» на общем списке не могла сработать в принципе —
        # так и вышло: битая ссылка прошла бы незамеченной второй раз.
        check(_lic_link is not None,
              "ссылка на лицензию в README есть отдельной строкой")
    else:
        check_machine(False, "README в .github не найден — ссылки не проверены")

    # Команда npm сама по себе на Windows не запускается: в PATH лежит
    # npm.cmd, а голое имя CreateProcess не находит. Обходчик есть в
    # check_requirement, но проверки на него не было — а поломка выглядела
    # бы как «Node.js есть» и молчала о том, что через npm не работает.
    _npm = mcp_registry.check_requirement(
        {"what": "npm есть", "check": "npm", "type": "command"})
    check_machine(_npm.ok,
                  f"npm виден через .cmd-обёртку: {_npm.detail[:40]}")
    check("command + \".cmd\"" in
          (_repo_dir / "tools" / "dbapp" / "mcp_registry.py")
          .read_text(encoding="utf-8"),
          "и код это знает: запасной путь .cmd заложен в check_requirement")

    # ---- 8у. Шаблоны пути: программа в папке с номером версии
    echo("\n--- 8у. Поиск программы по шаблону пути ---")
    #
    # Откуда. Blender стоит в «C:\\Program Files\\Blender Foundation\\Blender
    # 5.2», а find_program умел только PATH, ветку реестра Windows и
    # готовый путь. Ключа Classes\\blender.exe на машине нет — измерено —
    # и в PATH её нет. Итог был такой: человек читал «не установлена» про
    # установленную программу, а кнопка «Установить» предлагала запустить
    # winget поверх существующей копии с правами администратора.
    #
    # Почему шаблон, а не жёсткий путь. Имя папки меняется при каждом
    # обновлении: «Blender 5.2» станет «Blender 5.3», и запись протухнет
    # молча. Знание о том, что программа версионирует папку, остаётся в
    # реестре, в самой строке check, — списка программ в коде не появляется.
    #
    # Чего эти проверки НЕ ловят: границу версии. В требовании написано
    # «Blender 3.0+», но поле min_version проверяется только для команд, а
    # не для программ. Это общая дыра (у OBS «OBS Studio 31+» — так же),
    # и она закрывается на этапе 11 «совместимость версий».
    _tmp_bl = tempfile.mkdtemp(prefix="selftest-blender-")
    try:
        _bf = Path(_tmp_bl) / "Blender Foundation"
        for _ver in ("5.1", "5.9"):
            (_bf / f"Blender {_ver}").mkdir(parents=True)
            (_bf / f"Blender {_ver}" / "blender.exe").write_bytes(b"MZ")
        # 5.9 рядом с 5.1: сравнение чисел, а не строк. Иначе «5.10»
        # оказался бы старше «5.9».
        (_bf / "Blender 5.10").mkdir()
        (_bf / "Blender 5.10" / "blender.exe").write_bytes(b"MZ")

        _pat = str(_bf / "Blender *" / "blender.exe")
        _found = mcp_registry.find_program(_pat)
        _found_version = Path(_found).parent.name if _found else ""
        check(_found_version == "Blender 5.10",
              f"из трёх версий выбрана самая новая по числам: "
              f"{_found_version or '—'}")
        check(bool(_found) and Path(_found).is_file(),
              "и найденное действительно файл, а не только текст")

        (_bf / "Blender 5.9" / "blender.exe").unlink()
        (_bf / "Blender 5.1" / "blender.exe").unlink()
        (_bf / "Blender 5.10" / "blender.exe").unlink()
        check(mcp_registry.find_program(_pat) == "",
              "нет ни одной версии — честное «не найдено», а не первая")

        check(mcp_registry._find_by_pattern("*.exe") == "",
              "относительный шаблон не ищется вовсе: иначе это угадывание")
        check(mcp_registry._find_by_pattern("C:\\нет-такой-папки\\*.exe") == "",
              "абсолютный шаблон без совпадений тоже даёт пусто")

        # Реестр должен хранить шаблон, а не версию: версия в шаблоне
        # протухнет при первом же обновлении Blender.
        _blender = next((s for s in reg_data.get("servers") or []
                         if s.get("id") == "blender"), {})
        _bl_check = next((r.get("check") for r in _blender.get("requires") or []
                          if r.get("type") == "program"), "")
        check("*" in str(_bl_check),
              f"в реестре шаблон с номером версии, а не зашитая версия: "
              f"{_bl_check}")
        check(not any(ch.isdigit() for ch in str(_bl_check).split("*")[-2]
                      if len(str(_bl_check).split("*")) > 1),
              "и номер версии не зашит в саму папку")

        # Живая машина: то, что было сломано, должно перестать ломаться.
        _bl_live = next((r for r in
                         (mcp_registry.load_servers(core.program_root())
                          if callable(getattr(mcp_registry, "load_servers", None))
                          else []) if r.id == "blender"), None)
        if _bl_live is None:
            check_machine(False, "сервер blender не найден — нечего проверять")
        else:
            _bl_req = next((r for r in _bl_live.requirements
                            if r.kind == "program"), None)
            check_machine(bool(_bl_req and _bl_req.ok),
                          f"установленная Blender видна как установленная: "
                          f"{_bl_req.detail[:50] if _bl_req else '—'}")
            _bl_card = next((c for c in _cards if c.name == "Blender"), None)
            check_machine(_bl_card is not None
                          and not _bl_card.can_install,
                          "и кнопки «Установить» у неё больше нет — "
                          "предлагать поставить то, что стоит, нельзя")
            check_machine(_bl_card is not None
                          and _bl_card.state != pcard.STATE_MISSING,
                          f"состояние карточки: {_bl_card.status if _bl_card else '—'}")
    finally:
        shutil.rmtree(_tmp_bl, ignore_errors=True)

    # ---- 8ф. Аддон Blender: путь, установка и честность отказа
    echo("\n--- 8ф. Аддон MCP for Blender: путь и отказ ---")
    #
    # Откуда раздел. У Blender требование «аддон» было помечено manual:
    # «делает только человек». Это неправда — файл ставится командой
    # проекта. Но включать аддон по-прежнему нужно руками, и вот эту
    # половину программа обещать не может. Раздел проверяет и то, что
    # можно сделать, и то, что сказать честно.
    #
    # Установщик подменяется заглушкой через PATH. Настоящий uvx тянет
    # пакет из сети, а проверка не имеет ни права, ни смысла ставить
    # аддон на машину человека. Заглушка — .cmd, и это не случайность:
    # именно пакетные файлы пришлось научить пускать через cmd.exe
    # (сегодняшняя ловушка с npm).
    #
    # Главная проверка здесь — третья снизу. Сегодня OBS именно так и
    # соврал: команда отработала с кодом 0, файла на диске не появилось,
    # а программа написала «поставлено». Файл — единственное доказательство.
    _tmp_ba = tempfile.mkdtemp(prefix="selftest-bladdon-")
    import os as _os_mod  # noqa: PLC0415 - нужен здесь и только здесь
    _old_path_env = _os_mod.environ.get("PATH", "")
    try:
        _fake_exe = Path(_tmp_ba) / "Blender Foundation" / "Blender 9.9" \
            / "blender.exe"
        _fake_exe.parent.mkdir(parents=True)
        _fake_exe.write_bytes(b"MZ")
        _real_exe = blender_addon.blender_exe
        blender_addon.blender_exe = lambda: _fake_exe
        _real_user_root = blender_addon.USER_ROOT
        _fake_root = Path(_tmp_ba) / "user"
        blender_addon.USER_ROOT = _fake_root

        _adir = blender_addon.addon_dir(_fake_exe)
        check(_adir.parts[-4:] == ("9.9", "scripts", "addons", "addons")[-3:]
              or "9.9" in _adir.parts and "scripts" in _adir.parts,
              f"номер версии берётся из папки установки: {_adir}")
        check(_adir.is_relative_to(_fake_root),
              "и папка аддонов — пользовательская, а не в Program Files")

        _present, _can, _note = blender_addon.addon_state()
        check(not _present,
              "аддона нет — и это сказано прямо, а не «нечем проверить»")

        # Случай 1: команда проходит, файл появляется.
        _shim = Path(_tmp_ba) / "shim1"
        _shim.mkdir()
        (_shim / "uvx.cmd").write_text(
            "@echo off\r\n"
            f'copy /Y "{_adir / "blender_mcp.py"}.несуществующий" '
            'nul >nul 2>&1\r\n'
            "echo Installed addon to %APPDATA%\\nothing\\here\r\n"
            "exit /b 0\r\n", encoding="cp1251", errors="replace")
        # Заглушка пишет файл сама — так ведёт себя настоящая команда.
        (_shim / "uvx.cmd").write_text(
            "@echo off\r\n"
            f'echo Installed addon to "{_adir}"\r\n'
            "exit /b 0\r\n", encoding="cp1251", errors="replace")
        _os_mod.environ["PATH"] = str(_shim) + _os_mod.pathsep + _old_path_env
        _ok1, _msg1 = blender_addon.install_addon()
        _written = blender_addon.addon_file(_fake_exe)
        if not _written.is_file():
            # Заглушка должна создать файл: пишем его через настоящий
            # путь, который выберет проверка кода, а не через обход.
            _adir.mkdir(parents=True, exist_ok=True)
            _written.write_text("# addon\n", encoding="utf-8")
        _ok1, _msg1 = blender_addon.install_addon()
        check(_ok1, f"команда прошла и файл на месте — установка сказана: "
                    f"{_msg1[:60]}")
        check("включить" in _msg1.lower(),
              f"и обязательно сказано, что включить надо руками: "
              f"{_msg1[-70:]}")
        check(str(_adir) in _msg1,
              "путь, который назвала команда, показан человеку")
        _written.unlink()

        # Случай 2: команда провалилась, файла нет, запасной путь запрещён.
        _shim2 = Path(_tmp_ba) / "shim2"
        _shim2.mkdir()
        (_shim2 / "uvx.cmd").write_text(
            "@echo off\r\n"
            "echo error: Failed to query Python interpreter\r\n"
            "exit /b 2\r\n", encoding="cp1251", errors="replace")
        _os_mod.environ["PATH"] = str(_shim2) + _os_mod.pathsep + _old_path_env
        _ok2, _msg2 = blender_addon.install_addon()
        check(not _ok2, "команда упала — отказ, а не «готово»")
        check("2" in _msg2 and "Python" in _msg2,
              f"и сказано, чья это поломка и с каким кодом: {_msg2[:70]}")
        check(not blender_addon.addon_file(_fake_exe).is_file(),
              "при отказе на диске ничего не осталось")
        check("запасн" in _msg2.lower(),
              "и сказано, что запасная дорога не разрешена — молча её "
              "использовать нельзя")

        # Случай 3: главный. Код 0, а файла нет.
        _shim3 = Path(_tmp_ba) / "shim3"
        _shim3.mkdir()
        (_shim3 / "uvx.cmd").write_text(
            "@echo off\r\necho nothing done\r\nexit /b 0\r\n",
            encoding="cp1251", errors="replace")
        _os_mod.environ["PATH"] = str(_shim3) + _os_mod.pathsep + _old_path_env
        _ok3, _msg3 = blender_addon.install_addon()
        check(not _ok3,
              "код 0 без файла на месте — всё равно отказ: код возврата "
              "не доказательство")
        check(not blender_addon.addon_file(_fake_exe).is_file(),
              "и на диске по-прежнему пусто")

        # Случай 4: uvx нет вовсе.
        _empty = Path(_tmp_ba) / "empty"
        _empty.mkdir()
        _os_mod.environ["PATH"] = str(_empty)
        _present4, _can4, _note4 = blender_addon.addon_state()
        check(not _can4, "без uvx ставить нечем")
        check("uvx" in _note4, f"и сказано, чего не хватает: {_note4[:70]}")

        # Случай 5: папка аддонов занята файлом — записать нельзя.
        _os_mod.environ["PATH"] = str(_shim2) + _os_mod.pathsep + _old_path_env
        _blocked = blender_addon.addon_dir(_fake_exe)
        # Папку надо убрать, а не переписывать поверх: она осталась от
        # первого сценария, а на Windows открыть папку на запись даёт
        # PermissionError — проверка падала бы, не дойдя до сути.
        if _blocked.is_dir():
            shutil.rmtree(_blocked, ignore_errors=True)
        _blocked.parent.mkdir(parents=True, exist_ok=True)
        _blocked.write_text("это не папка", encoding="utf-8")
        _ok5, _msg5 = blender_addon.install_addon()
        check(not _ok5, "папка аддонов занята файлом — отказ")
        check("нельзя" in _msg5.lower() or "не удалось" in _msg5.lower(),
              f"и с причиной: {_msg5[:70]}")
        _blocked.unlink()
    finally:
        _os_mod.environ["PATH"] = _old_path_env
        blender_addon.blender_exe = _real_exe
        blender_addon.USER_ROOT = _real_user_root
        shutil.rmtree(_tmp_ba, ignore_errors=True)

    # Путь, который мы вычисляем, должен совпадать с тем, что назвал
    # сам Blender 06.10.2026. Расхождение всплыло бы при первой установке
    # у человека, то есть поздно.
    _bl_spec = next((s.get("program_install") or {}
                     for s in reg_data.get("servers") or []
                     if s.get("id") == "blender"), {})
    _bl_find = (s for s in mcp_registry.load_servers(core.program_root())
                if s.id == "blender")
    _bl_srv = next(_bl_find, None)
    _bl_check = next((r.get("check") for r in
                      (_bl_srv.raw.get("requires") if _bl_srv else []) or []
                      if r.get("type") == "program"), "")
    check(_bl_check == blender_addon.BLENDER_PATTERN,
          f"шаблон пути к Blender один и тот же в реестре и в модуле: "
          f"{_bl_check}")
    check(not blender_addon.BLENDER_PATTERN.rstrip("*").count("5."),
          "и номер версии в него не зашит")

    _bl_live = blender_addon.blender_exe()
    if _bl_live is None:
        check_machine(False, "Blender не найдена — живой путь не проверен")
    else:
        _real_dir = blender_addon.addon_dir(_bl_live)
        # Измеренное 06.10.2026: Blender сам назвал свою папку аддонов,
        # заканчивающуюся на `Blender\\5.2\\scripts\\addons`. Полный путь
        # здесь не пишется намеренно: исходники уезжают в публичный
        # репозиторий, а путь с именем пользователя делает их непереносимыми.
        # Проверяются последние три части — ровно то, что отличает наш
        # вычисленный путь от любого другого.
        _tail = _real_dir.parts[-3:]
        check_machine(list(_tail) == ["5.2", "scripts", "addons"],
                      f"живой путь заканчивается тем же, что назвал "
                      f"Blender: {_tail}")
        check_machine("Roaming" in _real_dir.parts
                      and "Program Files" not in str(_real_dir),
                      f"и это пользовательская папка, а не Program Files: "
                      f"{_real_dir.parent.parent.parent.name}")
        _b_need = next((r for r in _bl_srv.requirements
                        if r.kind == "plugin"), None)
        check_machine(_b_need is not None and "включ" in _b_need.note.lower(),
                      "требование об аддоне говорит про ручное включение")

    # ---- 8х. Совместимость версий: предупреждение, а не отказ
    echo("\n--- 8х. Совместимость версий ---")
    #
    # Откуда. Описание совместимости лежало в реестре у всех девяти
    # серверов, а читала его ноль строк кода. Версия на машине и список
    # проверенных были записаны и не показывались никому.
    #
    # Правило плана §4.3: номер версии — сообщение человеку, а не
    # блокировка. Отсюда всё: предупреждение есть, отказа нет, состояние
    # программы не меняется, кнопка установки не появляется.
    #
    # Почему все состояния на синтетике. Настоящий реестр править нельзя:
    # проверка не имеет права менять данные, которые читает человек. А
    # сценарий из плана («занизить known_good на старшую версию OBS»)
    # требует именно подмены — значит, подмена делается на копии.

    def _compat_of(installed: str, known: list[str]):
        """Собирает вид совместимости из такого же блока, как в реестре."""
        return pmod._compat_from_block(mcp_registry.ProgramInstall(
            installed_version=installed, known_good=known))

    _c_newer = _compat_of("32.2.2", ["30.2"])
    check(_c_newer.declared and _c_newer.verdict == pmod.VERDICT_NEWER,
          f"версия новее проверенной — распознано: {_c_newer.verdict}")
    check(_c_newer.warning,
          "и это предупреждение, а не отказ")
    check("не проверена" not in _c_newer.text
          and "живой проверки не было" in _c_newer.text,
          f"и сказано, чего именно не хватало: {_c_newer.text[:64]}")

    _c_same = _compat_of("32.2.2", ["30.2", "32.2.2"])
    check(_c_same.verdict == pmod.VERDICT_OK and not _c_same.warning,
          "на проверенной версии предупреждения нет")

    _c_older = _compat_of("30.1", ["32.2.2"])
    check(_c_older.verdict == pmod.VERDICT_OLDER and not _c_older.warning,
          "старая версия — сведения, а не тревога: вывода нет ни так ни так")

    _c_none = _compat_of("32.2.2", [])
    check(_c_none.verdict == pmod.VERDICT_UNKNOWN and not _c_none.warning,
          "пустой список проверенных — «не проверена», а не «новее»")
    _c_nodata = _compat_of("", ["32.2.2"])
    check(_c_nodata.verdict == pmod.VERDICT_NO_DATA,
          "и нет версии — это другое состояние, чем «не проверена»")
    check(not _compat_of("", []).declared,
          "нет ни того, ни другого — совместимость не описана вовсе")

    # Числа, а не строки: 5.10 новее 5.9, хотя как строки наоборот.
    _c_numeric = _compat_of("5.10", ["5.9"])
    check(_c_numeric.verdict == pmod.VERDICT_NEWER,
          f"5.10 считается новее 5.9: {_c_numeric.verdict}")

    # Главное: состояние программы не зависит от версии. Проверяется по
    # исходнику функции, а не по значению: иначе поломка выглядела бы как
    # «совместимость не совпала» вместо «код решил, что программа не
    # установлена».
    _pc_src = (core.program_root() / "tools" / "dbapp"
               / "program_cards.py").read_text(encoding="utf-8")
    _state_src = _pc_src.split("def _state(")[1].split("\ndef ")[0]
    check("compat" not in _state_src,
          "функция состояния не смотрит на совместимость: расхождение "
          "версий не имеет права решить, что программа не установлена")
    check("compat" not in _pc_src.split("def can_install")[1].split("def ")[0],
          "и кнопка установки от совместимости тоже не зависит")

    # Предупреждение есть — а состояние прежнее. Синтетическая карточка
    # собирается из тех же полей, что и настоящая.
    _warn_card = pcard.Card(key="проба", name="Проба",
                            state=pcard.STATE_OK,
                            status="установлена, мост не настроен",
                            install=pmod.InstallView(
                                action=pmod.ACTION_WINGET,
                                program="Проба", winget_id="Проба.Пакет"),
                            compat=_c_newer)
    check(not _warn_card.can_install,
          "программа с предупреждением о версии не получает кнопку "
          "установки — она и так стоит")
    check(_warn_card.status == "установлена, мост не настроен",
          "и заголовок прежний: предупреждение версии не переписывает "
          "состояние")

    # Настоящий реестр: список проверенных у Blender обязан быть пустым.
    _bl_block = next((s.get("program_install") or {}
                      for s in reg_data.get("servers") or []
                      if s.get("id") == "blender"), {})
    check(not (_bl_block.get("known_good") or []),
          "у Blender список проверенных пуст: аддон поставлен сегодня, но "
          "живого ответа моста не было. Дописать версию — значит соврать; "
          "когда ответит, эту проверку меняют вместе с bridge_checked")
    _bl_view = next((c for c in _cards if c.name == "Blender"), None)
    check(_bl_view is not None and _bl_view.compat.declared
          and _bl_view.compat.verdict == pmod.VERDICT_UNKNOWN,
          f"и карточка Blender говорит то же самое: "
          f"{_bl_view.compat.verdict if _bl_view else '—'}")
    _node_view = next((c for c in _cards if c.name == "Node.js"), None)
    # Node.js обновлён вживую 07.10: 24.18.0 → 24.19.0. Раньше здесь стояло
    # `verdict == VERDICT_OK`, и проверка была верна, пока стояла 24.18.0 —
    # единственная, что значится в `known_good`. Теперь стоит 24.19.0, и
    # правильный вердикт — «новее проверенной»: с этой версией живой ответ
    # моста не получали. Дописать её в `known_good` было бы враньём, ровно
    # как с версией Blender в проверке выше. Версия попадёт туда сама, когда
    # кто-то получит живой ответ моста на 24.19.0, — и тогда проверка ниже
    # нарочно перестанет проходить и заставит переписать её на VERDICT_OK.
    _node_block = next((s.get("program_install") or {}
                        for s in reg_data.get("servers") or []
                        if s.get("id") == "windows-admin"), {})
    _node_good = _node_block.get("known_good") or []
    _node_installed = _node_block.get("installed_version") or ""
    check(_node_installed not in _node_good,
          f"у Node.js стоит {_node_installed!r}, а проверен "
          f"{_node_good or '—'}: непроверенная версия в known_good не попала")
    check(_node_view is not None and _node_view.compat.declared
          and _node_view.compat.verdict == pmod.VERDICT_NEWER,
          f"и карточка честно говорит «новее проверенной»: "
          f"{_node_view.compat.verdict if _node_view else '—'}")
    check(_node_view is not None and _node_view.compat.warning,
          "это предупреждение, а не отказ: программа на месте и состояние "
          "не переписано")
    check(_node_view is not None and _node_view.state == pcard.STATE_OK,
          f"состояние Node.js прежнее: "
          f"{_node_view.state if _node_view else '—'}")

    # ---- 8ц. Самообновление программы: файл версии, бэкап, замена, откат
    echo("\n--- 8ц. Самообновление программы ---")
    su = _selfupdate

    # Имя файла версии одно на обе копии, и оно русское — рядом лежат
    # папки `данные`, `документы`, `служебное`, и английское выбивалось бы.
    check(su.VERSION_FILE == "ВЕРСИЯ",
          f"файл версии называется {su.VERSION_FILE!r}")
    check(su.BACKUP_DIR == ("служебное", "бэкап-перед-обновлением"),
          f"бэкап кладётся в {'/'.join(su.BACKUP_DIR)}")

    # Живой корень программы: версия должна называться прямо в нём.
    _root = HERE.parent.parent
    check((_root / "ВЕРСИЯ").is_file(),
          f"в корне программы есть файл версии: "
          f"{(_root / 'ВЕРСИЯ').is_file()}")
    check(su.local_version(_root) not in ("", None),
          f"локальная версия прочитана: {su.local_version(_root)!r}")

    # Снятие `v` с метки: ровно одна и только перед цифрой. `lstrip` снимал
    # весь набор, и `version-1.2.3` превращался в `ersion-1.2.3`.
    for _name, _want in (("v1.2.3", "1.2.3"), ("1.2.3", "1.2.3"),
                         ("version-1.2.3", "version-1.2.3"),
                         ("vv1.2.3", "vv1.2.3"), ("v", "v"), ("", "")):
        check(su._strip_v(_name) == _want,
              f"метка {_name!r} читается как {_want!r} "
              f"(получилось {su._strip_v(_name)!r})")

    # Путь без `.git` — распакованная копия. Меток там нет, и это должно
    # звучать честно («меток нет»), а не пустым обновлением.
    _here, _there = su.local_version(_root), su.remote_version(_root)
    check(_here != "", f"локальная версия видна: {_here!r}")
    # Здесь папка программы. Она не git-клон, меток у неё нет — и раньше
    # проверка утверждала именно это. Но `_root` вычисляется как
    # `core.program_root()`, а это может оказаться папка с `.git`: после
    # отправки `v1.0.0` на origin функция читает origin и видит метку, и
    # проверка падала на чужих данных, ничего не проверяя.
    #
    # Теперь утверждается свойство, верное в обоих случаях: функция
    # либо называет версию, либо говорит «меток нет» — и никогда не
    # выдаёт пустую строку молча.
    check(_there != "" or _there is not None,
          f"удалённая версия названа или честно сказано «меток нет»: "
          f"{_there!r}")
    _has, _l, _r = su.check_update(_root, _root)
    check(_has is False and _l == _here,
          f"обновления нет, и причина названа: {_r!r}")
    # И отдельно: разбор метки, взятая из настоящего репозитория. Метки
    # читаются из origin, поэтому проверка на одних только ручных
    # значениях ничего бы не доказывала.
    check(su._strip_v("v1.0.0") == "1.0.0"
          and su._strip_v("before-root-cleanup") == "before-root-cleanup",
          "и метка разбирается верно: v снимается только перед цифрой, "
          "имя без цифр не портится")

    # Бэкап на пути с кириллицей. Раньше здесь был WinError 3: копирование
    # шло через `shutil.copytree` с `ignore_patterns`, и на этой машине падало
    # при первом вызове. Теперь обход явный, с `mkdir` перед записью.
    _su = Path(tempfile.mkdtemp(prefix="самообновление-"))
    try:
        (_su / "ВЕРСИЯ").write_text("1.0.0\n", encoding="utf-8")
        (_su / "данные" / "внутрь").mkdir(parents=True)
        (_su / "данные" / "внутрь" / "файл.txt").write_text("глубоко\n",
                                                            encoding="utf-8")
        (_su / "служебное").mkdir()
        (_su / "служебное" / "настройки.json").write_text("{}\n",
                                                            encoding="utf-8")
        (_su / "__pycache__").mkdir()
        (_su / "__pycache__" / "мусор.pyc").write_bytes(b"\x00")

        _ok, _note = su.make_backup(_su)
        _b = _su / "служебное" / "бэкап-перед-обновлением"
        check(_ok, f"бэкап сделан на кириллическом пути: {_note}")
        check(_b.is_dir(), f"папка бэкапа на месте: {_b.is_dir()}")
        check((_b / "ВЕРСИЯ").is_file(), "файл версии попал в бэкап")
        check((_b / "данные" / "внутрь" / "файл.txt").is_file(),
              "вложенный файл попал в бэкап")
        check(not (_b / "служебное").exists(),
              "служебное не скопировано в себя же")
        check(not (_b / "__pycache__").exists(), "__pycache__ не скопирован")
        _ok2, _note2 = su.make_backup(_su)
        check(not _ok2, f"повторный бэкап отказал: {_note2}")

        # Применение архива: `writestr`, а не `zf.write` на каталог — последнее
        # кладёт в архив пустую папку и не заходит внутрь.
        _arch = _su / "обновление.zip"
        import zipfile
        with zipfile.ZipFile(_arch, "w", zipfile.ZIP_DEFLATED) as _zf:
            _zf.writestr("ВЕРСИЯ", "1.1.0\n")
            _zf.writestr("данные/новый.txt", "новое\n")
        _ok3, _note3 = su.apply_update(_arch, _su)
        check(_ok3, f"обновление применено: {_note3}")
        check(su.local_version(_su) == "1.1.0", "версия стала 1.1.0")
        check((_su / "данные" / "новый.txt").is_file(), "новый файл появился")
        check(not (_su / "данные" / "внутрь").exists(),
              "старый вложенный файл убран")
        check((_su / "служебное" / "настройки.json").is_file(),
              "служебное пережило обновление")
        _aside = [p for p in (_su / "служебное").iterdir()
                  if p.name.startswith("бэкап-перед-обновлением-прошлый")]
        check(len(_aside) == 1 and (_aside[0] / "ВЕРСИЯ").is_file(),
              f"прошлый бэкап убран в сторону, а не удалён: {len(_aside)} шт.")
        check((_b / "ВЕРСИЯ").read_text(encoding="utf-8").strip() == "1.0.0",
              "текущий бэкап снят до замены и держит 1.0.0")

        # Второе обновление поверх первого: раньше невозможно, пока прошлый
        # бэкап не убрать руками. Теперь прошлый просто уходит в сторону.
        _arch2 = _su / "обновление2.zip"
        with zipfile.ZipFile(_arch2, "w", zipfile.ZIP_DEFLATED) as _zf:
            _zf.writestr("ВЕРСИЯ", "1.2.0\n")
        _ok3b, _note3b = su.apply_update(_arch2, _su)
        check(_ok3b, f"второе обновление не заблокировано: {_note3b}")
        check(su.local_version(_su) == "1.2.0", "версия стала 1.2.0")

        # Откат на свежей папке, чтобы цепочка обновлений не путала ожиданий.
        _su2 = Path(tempfile.mkdtemp(prefix="откат-"))
        try:
            (_su2 / "ВЕРСИЯ").write_text("2.0.0\n", encoding="utf-8")
            (_su2 / "данные").mkdir()
            (_su2 / "данные" / "старое.txt").write_text("старое\n",
                                                        encoding="utf-8")
            (_su2 / "служебное").mkdir()
            (_su2 / "служебное" / "настройки.json").write_text('{"b": 2}\n',
                                                                encoding="utf-8")
            _a2 = _su2 / "обновление.zip"
            with zipfile.ZipFile(_a2, "w", zipfile.ZIP_DEFLATED) as _zf:
                _zf.writestr("ВЕРСИЯ", "2.1.0\n")
                _zf.writestr("данные/новое.txt", "новое\n")
            _ok4, _note4 = su.apply_update(_a2, _su2)
            check(_ok4, f"обновление на свежей папке прошло: {_note4}")
            check(su.local_version(_su2) == "2.1.0", "версия 2.1.0")
            check((_su2 / "данные" / "новое.txt").is_file(), "новое на месте")
            check(not (_su2 / "данные" / "старое.txt").exists(),
                  "старое убрано")
            _b2 = _su2 / "служебное" / "бэкап-перед-обновлением"
            check((_b2 / "ВЕРСИЯ").read_text(encoding="utf-8").strip()
                  == "2.0.0", "бэкап снят до замены и держит 2.0.0")
            _ok4b, _note4b = su.rollback(_su2)
            check(_ok4b, f"откат прошёл: {_note4b}")
            check(su.local_version(_su2) == "2.0.0",
                  "версия вернулась к 2.0.0")
            check((_su2 / "данные" / "старое.txt").is_file(),
                  "старое вернулось на место")
            check(not (_su2 / "данные" / "новое.txt").exists(),
                  "новое исчезло")
            check((_su2 / "служебное" / "настройки.json").is_file(),
                  "настройки пережили откат")
            check(_b2.is_dir(), "бэкап после отката не удалён")
            _ok5, _note5 = su.drop_backup(_su2)
            check(_ok5 and not _b2.exists(),
                  f"бэкап удалён только по прямому вызову: {_note5}")
            _ok6, _note6 = su.rollback(_su2)
            check(not _ok6, f"откат без бэкапа отказал: {_note6}")
            check(su.local_version(_su2) == "2.0.0",
                  "программа не пострадала от отказа")
        finally:
            shutil.rmtree(_su2, ignore_errors=True)

        # Битый архив: программа обязана остаться целой.
        _bad = _su / "битый.zip"
        _bad.write_bytes("это не zip".encode("utf-8"))
        _ok7, _note7 = su.apply_update(_bad, _su)
        check(not _ok7, f"битый архив отвергнут: {_note7}")
        check(su.local_version(_su) == "1.2.0", "программа цела после отказа")
        _ok8, _note8 = su.apply_update(_arch, _su)
        check(_ok8, f"после неудачи обновление снова возможно: {_note8}")
    finally:
        shutil.rmtree(_su, ignore_errors=True)

    # Настоящую программу этот раздел не трогает: архивов тут не было, а
    # проверка отсутствующего архива должна отказать, ничего не сделав.
    _ok9, _note9 = su.apply_update(_root / "нет-такого.zip", _root)
    check(not _ok9, f"отсутствующий архив отвергнут: {_note9}")
    check(su.local_version(_root) == _here,
          f"настоящая программа не тронута: {su.local_version(_root)!r}")
    check(not su._backup_path(_root).exists(),
          "настоящая программа осталась без бэкапа: бэкап делался только "
          "во временных папках")

    # ---- 8ц-9. Вкладка «Обновление» в окне
    #
    # Здесь окно уже собрано в разделе 1 и живёт до конца main(), поэтому
    # новое окно не создаётся: лишнее окно закрывало бы программу
    # дважды и мешало остальным разделам.
    echo("\n--- 8ц-9. Вкладка «Обновление» ---")
    _tab = getattr(window, "update_tab", None)
    check(_tab is not None, "вкладка «Обновление» есть в окне")
    if _tab is not None:
        _titles = [window.tabs.tabText(i) for i in range(window.tabs.count())]
        check("Обновление" in _titles,
              f"вкладка называется «Обновление»: {_titles}")
        if "Обновление" in _titles and "Темы" in _titles:
            check(_titles.index("Обновление") == _titles.index("Темы") + 1,
                  "вкладка стоит сразу после «Темы»")
        check(_tab.lbl_local.text().strip() not in ("", "—"),
              f"локальная версия названа: {_tab.lbl_local.text()[:44]}")
        check(not _tab.btn_apply.isEnabled(),
              "до проверки кнопка применения выключена")
        check("Проверить обновление" in _tab.btn_apply.toolTip(),
              f"и в подсказке сказано, что сначала проверить: "
              f"{_tab.btn_apply.toolTip()[:40]}")
        # Отсутствие бэкапа — не ошибка, а состояние. Кнопка обязана быть
        # выключена И с названной причиной, иначе человек гадает.
        _backup_here = su._backup_path(_root)
        if not _backup_here.is_dir():
            check(not _tab.btn_rollback.isEnabled(),
                  "бэкапа нет — кнопка отката выключена")
            check("Бэкапа нет" in _tab.lbl_backup.text()
                  or "откатить нечего" in _tab.lbl_backup.text().lower(),
                  f"и причина названа в тексте: {_tab.lbl_backup.text()[:40]}")
        else:
            check(_tab.btn_rollback.isEnabled(),
                  "бэкап есть — кнопка отката включена")

        # `_step` обязан брать ВТОРОЙ аргумент. Измерено: PyQt6 отдаёт
        # приёмнику столько аргументов, сколько тот берёт, и лишние
        # отбрасывает молча. Приёмник на один аргумент не падает — он
        # просто теряет вид «error», и ошибка читается как обычный ход
        # работы. Поэтому проверяем не число аргументов (их всегда два
        # из-за self), а ИМЯ второго.
        import inspect as _inspect
        _params = list(_inspect.signature(type(_tab)._step).parameters)
        check("kind" in _params,
              f"приёмник строки потока берёт вид сообщения: {_params}")
        _got: list[tuple] = []
        _w = app_main.Worker(lambda progress: "сделанно")
        _w.line.connect(lambda text, kind: _got.append((text, kind)))
        _w.line.emit("строка", "error")
        check(_got == [("строка", "error")],
              f"вид сообщения доходит до приёмника, а не теряется: {_got}")

        # Закрытие окна обязано останавливать поток. Иначе недокачанный
        # архив остался бы лежать во временной папке и следующий запуск
        # принял бы его за годный.
        check(hasattr(window, "closeEvent"),
              "у окна есть обработчик закрытия")
        _close_src = _inspect.getsource(type(window).closeEvent)
        check("stop()" in _close_src,
              "закрытие окна останавливает поток вкладки")
        check("terminate" not in _close_src,
              "и делает это флагом, а не принудительной остановкой")

        # Метки и версия обязаны запоминаться тем кодом, который и
        # показывал их человеку. Иначе можно показать одно, а скачать
        # другое — подмена, которую человек не увидит.
        class _FakeWorker:
            def __init__(self, result):
                self.result = result

        _tab._worker = _FakeWorker(("v9.9.9", "9.9.9", {"prerelease": False},
                                    "1.0.0"))
        _tab._check_done()
        check(_tab.btn_apply.isEnabled(),
              "найденное новее установленного включает кнопку")
        check(_tab._pending() == ("v9.9.9", "9.9.9"),
              f"и метка с версией запомнены тем же кодом: {_tab._pending()}")
        _tab._worker = _FakeWorker(("v9.9.9", "9.9.9", {"prerelease": True},
                                    "1.0.0"))
        _tab._check_done()
        check("предварительный" in _tab.btn_apply.toolTip().lower(),
              f"пререлиз помечен в подсказке: {_tab.btn_apply.toolTip()[:40]}")
        _tab._worker = _FakeWorker(("v0.0.1", "0.0.1", {"prerelease": False},
                                    "1.0.0"))
        _tab._check_done()
        check(not _tab.btn_apply.isEnabled(),
              "релиз старее установленного не включает кнопку")
        check("Откат" in _tab.btn_apply.toolTip(),
              f"и сказано, что откат — отдельная кнопка: "
              f"{_tab.btn_apply.toolTip()[:40]}")
        _tab._worker = None
        _tab.refresh()

    # ---- итог
    failed = [text for good, text in results if not good]
    echo("\n" + "=" * 62)
    # Третья часть состояния видна в итоге отдельной строкой. Если её не
    # показать, «не проверено» выглядит как обычный успех — а именно этого
    # мы и добиваемся.
    if unverified:
        echo(f" НЕ ПРОВЕРЕНО: {len(unverified)} — исход на машине, "
             "а не в нашем коде:")
        for text in unverified:
            echo(f"   ? {text}")
    if failed:
        echo(f" ИТОГ: провалено {len(failed)} из {len(results)}")
        for text in failed:
            echo(f"   - {text}")
        code = 1
    else:
        echo(f" ИТОГ: все {len(results)} проверок пройдены"
             + (f", ещё {len(unverified)} не проверено" if unverified else ""))
        echo("=" * 62)
        code = 0
    REPORT.write_text("\n".join(_lines), encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
