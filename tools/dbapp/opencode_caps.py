# -*- coding: utf-8 -*-
"""Возможности базы для opencode — установка по выбору.

Простыми словами: ставит в настройки opencode команду /голос, мост ПК,
мост памяти, 12 агентов и обход блокировок — только то, что отметил
человек. Убрал галочку — файлы и записи убираются, чужое не трогается.

Правила (как везде в окне):
  * перед правкой opencode.jsonc — копия в _previous-version;
  * чужое не затираем: файлы без нашей метки пропускаем с сообщением;
  * что поставили — записываем в манифест, по нему же удаляем;
  * настоящий ~/.config/opencode трогаем только из окна, проверки —
    всегда во временной папке.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sys
import time
from pathlib import Path

import core  # noqa: PLC0415 — рядом лежит, круга нет

HERE = Path(__file__).resolve().parent
BASE = HERE.parent.parent  # tools/dbapp -> tools -> база

#: Что можно поставить: (имя, подпись для окна).
#: Мосты ncp/pc не управляются окном: они живут внутри базы и подключаются
#: кнопкой «Подключить базу» (attach_base). Галочек у них нет и не должно
#: быть — иначе можно случайно снять мост и остаться без памяти.
CAPS = (
    ("voice", "Команда /голос"),
    ("pc", "Мост ПК"),
    ("ncp", "Мост NCP (авто)"),
    ("agents", "12 агентов"),
    ("antiblock", "Обход блокировок"),
    ("rtk", "rtk — вывод команд короче"),
    ("caveman", "caveman — ответы короче"),
    ("pxpipe", "pxpipe — запросы картинками"),
    ("auto-improve", "auto-improve — улучшение текста"),
    ("greenlight", "greenlight — проверка iOS-перед App Store"),
    ("qwen-review", "Qwen Code — второй ревьюер"),
)

#: Что вкладка «opencode» спрашивает у человека: только расширения.
#: Мостов здесь нет — они едут с базой и включаются всегда.
#: rtk, caveman, pxpipe, auto-improve, greenlight и qwen-review — не
#: MCP-серверы и не мосты: rtk кладёт в настройки плагин, caveman — правила
#: в AGENTS.md, pxpipe — запись провайдера на локальный прокси, auto-improve
#: живёт отметкой в манифесте и копией скрипта, greenlight — собранным из
#: копии автора бинарником, а qwen-review — вторым ревьюером: развёрнутым
#: Qwen Code и выбором модели из провайдеров opencode. Ни один из них
#: в реестр mcp-registry.json не попадает.
CAPS_CHOICES = (
    ("voice", "Команда /голос"),
    ("agents", "12 агентов"),
    ("antiblock", "Обход блокировок"),
    ("rtk", "rtk — вывод команд короче"),
    ("caveman", "caveman — ответы короче"),
    ("pxpipe", "pxpipe — запросы картинками"),
    ("auto-improve", "auto-improve — улучшение текста"),
    ("greenlight", "greenlight — проверка iOS-перед App Store"),
    ("qwen-review", "Qwen Code — второй ревьюер"),
)

#: Галочки, которые при открытии вкладки стоят снятыми: у rtk нужен
#: бинарник в PATH, caveman меняет стиль ответов, pxpipe переписывает
#: запрос картинками, auto-improve тратит токены и коммитит в репозиторий
#: файла, greenlight и вовсе нужен только тем, кто делает приложения для
#: iOS: самому opencode-base (PyQt6, Windows) он не нужен, а второй ревьюер
#: (qwen-review) тратит токены и отправляет код изменений выбранному
#: провайдеру. Включать такое молча, «по умолчанию», нельзя.
CAPS_OFF_BY_DEFAULT = ("rtk", "caveman", "pxpipe", "auto-improve", "greenlight",
                       "qwen-review")

#: Что подставляется всегда, без галочки: мосты — часть базы, а не опция.
CAPS_ALWAYS = ("pc", "ncp")

MANIFEST = ".opencode-base-caps.json"
VOICE_MARK = "{{VOICE_DIR}}"

BEGIN_TPL = "// == OpenCode_Base: {name} =="
END_TPL = "// == OpenCode_Base: конец {name} =="


def opencode_dir() -> Path:
    """Папка настроек opencode (настоящая)."""
    import core  # noqa: PLC0415 — рядом лежит, круга нет

    return core.PROGRAMS_BY_ID["opencode"].config_dir()


def find_python() -> str:
    """Чем запускать мосты: тот же поиск, что у окна, иначе текущий."""
    try:
        import core  # noqa: PLC0415

        found = core.find_python()
        if found is not None:
            return str(found)
    except Exception:
        pass
    return sys.executable


# ------------------------------------------------------------------ JSONC


def strip_jsonc(text: str) -> str:
    """Убирает // и /* */ комментарии, строки в кавычках не трогает."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = False
    escape = False
    while i < n:
        ch = text[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            i += 2
            while i + 1 < n and not (text[i] == "*" and text[i + 1] == "/"):
                i += 1
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def check_jsonc(text: str) -> bool:
    """Файл — целый JSON после снятия комментариев."""
    try:
        json.loads(strip_jsonc(text))
        return True
    except (json.JSONDecodeError, ValueError):
        return False


def find_key_object(text: str, key: str, start: int = 0) -> tuple[int, int] | None:
    """Границы объекта "key": {...} — индексы скобок. Строки уважаем."""
    pattern = re.compile(r'"' + re.escape(key) + r'"\s*:')
    match = pattern.search(text, start)
    if not match:
        return None
    i = match.end()
    while i < len(text) and text[i] not in "{":
        if text[i] in "[]":
            return None
        i += 1
    if i >= len(text):
        return None
    depth = 0
    in_str = False
    escape = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (i, j)
    return None


def has_entry(obj_text: str, name: str) -> bool:
    """Есть ли ключ "name": внутри куска объекта."""
    return re.search(r'"' + re.escape(name) + r'"\s*:', obj_text) is not None


def find_key_array(text: str, key: str, start: int = 0) -> tuple[int, int] | None:
    """Границы массива "key": [...] — индексы [ и ]. Строки уважаем."""
    pattern = re.compile(r'"' + re.escape(key) + r'"\s*:')
    match = pattern.search(text, start)
    if not match:
        return None
    i = match.end()
    while i < len(text) and text[i] != "[":
        if text[i] in "{}":
            return None
        i += 1
    if i >= len(text):
        return None
    depth = 0
    in_str = False
    escape = False
    for j in range(i, len(text)):
        ch = text[j]
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                return (i, j)
    return None


def ensure_object(text: str, key: str) -> str:
    """Гарантирует пустой объект "key": {} в корне (перед последней })."""
    if find_key_object(text, key) is not None:
        return text
    stripped = text.rstrip()
    assert stripped.endswith("}"), "корень opencode.jsonc — объект"
    inner = stripped[:-1].rstrip()
    comma = "," if inner and not inner.endswith("{") and not inner.endswith("[") else ""
    return inner + comma + f'\n  "{key}": {{}}\n}}\n'


def ensure_commas(text: str) -> str:
    """Гарантирует правильные запятые во всех объектах верхнего уровня:
    - между элементами запятая есть
    - после последнего элемента запятой нет"""
    # Сначала убираем висячие запятые
    text = fix_trailing_commas(text)
    
    # Находим все объекты верхнего уровня {...}
    depth = 0
    in_str = False
    escape = False
    objects = []  # (start, end) позиций объектов верхнего уровня
    
    # Собираем все объекты верхнего уровня {...}
    in_str = False
    escape = False
    depth = 0
    obj_start = None
    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                obj_start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                objects.append((obj_start, i))
    
    # Для каждого объекта исправляем запятые внутри
    for start, end in reversed(objects):
        content = text[start + 1 : end]
        fixed = fix_object_commas(content)
        if fixed != content:
            text = text[:start + 1] + fixed + text[end:]
    
    return text


def fix_object_commas(content: str) -> str:
    """Исправляет запятые внутри одного объекта: ставит между элементами, убирает висячие.

    Работает одним проходом по строке. Глубина вложенности и положение
    строк считаются одним состоянием: если их считать отдельно внутри
    цикла и менять depth на походе, внешний обход продолжает с уже
    сдвинутой глубины и начинает дублировать куски текста.
    """
    # Висячие запятые убираем сразу — их легко опознать.
    out: list[str] = []
    depth = 0
    in_str = False
    escape = False
    i = 0
    n = len(content)
    while i < n:
        ch = content[i]
        if in_str:
            out.append(ch)
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            out.append(ch)
            i += 1
            continue
        if ch in "{[":
            depth += 1
            out.append(ch)
            i += 1
            continue
        if ch in "}]":
            depth -= 1
            # Запятая прямо перед закрывающей скобкой — лишняя. Пробелы и
            # перевод строки при этом сохраняем: выбрасывать их нельзя,
            # иначе закрывающая скобка склеится с предыдущей строкой.
            if ch == "}" and depth == 0:
                j = len(out)
                while j > 0 and out[j - 1].isspace():
                    j -= 1
                if j > 0 and out[j - 1] == ",":
                    del out[j - 1]
            out.append(ch)
            i += 1
            continue
        if ch == ",":
            # Запятая на этом уровне: смотрим, есть ли после неё
            # ещё содержимое до закрывающей скобки. Пустое — значит
            # висячая, убираем.
            j = i + 1
            while j < n and content[j].isspace():
                j += 1
            if j < n and content[j] in "}]":
                i += 1
                continue
            out.append(ch)
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def insert_entry(text: str, obj_key: str, name: str, entry: str) -> str:
    """Вставляет запись с метками ПЕРЕД закрывающей скобкой } объекта.

    Порядок важен. Сначала убираем прежнюю запись с таким именем, и
    только потом считаем границы объекта: удаление меняет длину
    текста, и посчитанные раньше границы после него указывают уже
    не туда. Перед вставкой проверяем, есть ли запятая-разделитель
    после последней чужой записи — без неё JSON не читается.
    """
    # Удаляем старую запись с таким именем (и с метками, и без них).
    text = remove_entry(text, obj_key, name)
    text = _remove_entry_by_name(text, obj_key, name)

    # Границы считаем на уже очищенном тексте.
    bounds = find_key_object(text, obj_key)
    if bounds is None:
        raise ValueError(f"нет объекта {obj_key}")
    closing = bounds[1]

    entry_text = entry.rstrip().rstrip(",")

    # Разделитель между чужой записью и нашим блоком. Без него блок
    # приклеится к предыдущей записи и JSON перестанет читаться.
    # Пустой объект ({) и случай, где запятая уже стоит, его не получают.
    #
    # Ставим запятую в начало блока, но ДО отступа: иначе она оказывается
    # внутри наших меток, и следующий проход remove_entry уносит её вместе
    # с блоком — запятая теряется и структура рассыпается.
    head = text[:closing]
    # Разделитель ставим отдельной строкой перед метками: так его
    # не съест remove_entry, который уносит наш блок по меткам.
    # Плата — при каждой реальной перезаписи блока остаётся лишняя
    # пустая строка. Это косметика, и она не вредит: повторные
    # нажатия, когда блок не изменился, до записи не доходят вовсе
    # (ветка-идемпотентность в mcp_registry.enable).
    #
    # Приклеивать запятую к предыдущей строке нельзя: если эта
    # строка — комментарий «//», запятая уедет внутрь него, и файл
    # перестанет читаться. Это проверено на установке возможностей.
    if head.rstrip().endswith("{") or head.rstrip().endswith(","):
        separator = ""
    else:
        separator = ","

    block = (
        separator
        + "\n    " + BEGIN_TPL.format(name=obj_key + "." + name)
        + "\n    " + entry_text
        + "\n    " + END_TPL.format(name=obj_key + "." + name) + "\n"
    )
    return text[:closing] + block + text[closing:]


def remove_entry(text: str, obj_key: str, name: str) -> str:
    """Убирает наш блок с метками; висячие запятые чистит.

    Чистим запятые только если блок действительно нашёлся: иначе
    подчищаем чужой текст, которого трогать не должны.
    """
    begin = BEGIN_TPL.format(name=f"{obj_key}.{name}")
    end = END_TPL.format(name=f"{obj_key}.{name}")
    pattern = re.compile(re.escape(begin) + r".*?" + re.escape(end) + r"\s*,?\s*", re.DOTALL)
    cleaned, count = pattern.subn("", text)
    if not count:
        return text
    # Висячая запятая перед закрывающей скобкой: ",\n  }" -> "\n  }".
    return re.sub(r",(\s*[}\]])", r"\1", cleaned)


def fix_trailing_commas(text: str) -> str:
    """Убирает висячие запятые перед } и ]: наши вставки всегда кончаются
    запятой, а если запись оказалась последней в объекте — это ошибка JSON."""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def merge_caps(
    text: str,
    servers: dict[str, str],
    permissions: dict[str, str],
) -> str:
    """Дописывает наши MCP-серверы и права. Чужое не трогает."""
    if not check_jsonc(text):
        raise ValueError("opencode.jsonc сломан — правим руками, не автоматом")
    text = ensure_object(text, "mcp")
    text = ensure_object(text, "permission")
    for name, entry in servers.items():
        bounds = find_key_object(text, "mcp")
        assert bounds is not None
        if not has_entry(text[bounds[0] : bounds[1]], name):
            text = insert_entry(text, "mcp", name, entry)
    for name, value in permissions.items():
        bounds = find_key_object(text, "permission")
        assert bounds is not None
        if not has_entry(text[bounds[0] : bounds[1]], name):
            text = insert_entry(text, "permission", name, f'"{name}": {value}')
    text = fix_trailing_commas(text)
    if not check_jsonc(text):
        raise ValueError("после вставки файл не читается — откат")
    return text


def unmerge_caps(text: str, names: list[str]) -> str:
    """Убирает наши записи из mcp и permission."""
    for name in names:
        text = remove_entry(text, "mcp", name)
        text = remove_entry(text, "permission", name)
    if not check_jsonc(text):
        raise ValueError("после удаления файл не читается — откат")
    return text


def set_instructions(text: str, base: Path) -> str:
    """Переписывает массив instructions в файле на пути этой базы.

    Всё остальное — провайдеры, права, другие серверы mcp — остаётся
    как было. Возвращает новый текст; если файл не читается или в нём
    нет массива instructions — бросает ValueError.
    """
    if not check_jsonc(text):
        raise ValueError("opencode.jsonc сломан — правим руками, не автоматом")
    import core  # noqa: PLC0415 — рядом лежит

    bounds = find_key_array(text, "instructions")
    if bounds is None:
        raise ValueError("в opencode.jsonc нет массива instructions")
    opening, closing = bounds
    base_posix = str(base).replace("\\", "/")
    lines = ["["]
    for name in core.INSTRUCTION_TARGETS:
        lines.append(f'    "{base_posix}/{name}",')
    lines[-1] = lines[-1][:-1]
    lines.append("  ]")
    fixed = text[:opening] + "\n".join(lines) + text[closing + 1 :]
    if not check_jsonc(fixed):
        raise ValueError("после замены instructions файл не читается — откат")
    return fixed


def update_block(text: str, obj_key: str, name: str, entry: str) -> tuple[str, bool]:
    """Переписывает наш блок в объекте. Чужого не трогает.

    Наши блоки живут между метками. Если меток нет, а запись с таким
    именем уже есть (чужая) — пропускаем и возвращаем False. Если записи
    нет вовсе — вставляем свою. Возвращает (текст, изменено ли).
    """
    begin = BEGIN_TPL.format(name=f"{obj_key}.{name}")
    end = END_TPL.format(name=f"{obj_key}.{name}")
    bounds = find_key_object(text, obj_key)
    if bounds is None:
        text = ensure_object(text, obj_key)
        bounds = find_key_object(text, obj_key)
    assert bounds is not None
    b = text.find(begin)
    if b == -1:
        if has_entry(text[bounds[0] : bounds[1]], name):
            return text, False
        return insert_entry(text, obj_key, name, entry), True
    e = text.find(end, b)
    if e == -1:
        raise ValueError(f"метка {begin} не закрыта — правим руками")
    # Есть ли внутри объекта реальное содержимое после нашего блока.
    rest_after = text[e + len(end) : bounds[1]]
    needs_comma = re.search(r'"[^":\n]+"\s*:', rest_after) is not None
    entry_text = entry.rstrip()
    if entry_text.endswith(","):
        entry_text = entry_text[:-1].rstrip()
    if needs_comma:
        entry_text += ","
    # Сдвигаем начало замены до перевода строки перед меткой — иначе
    # каждая перепрошивка оставляет лишнюю пустую строку перед блоком.
    b0 = text.rfind("\n", 0, b)
    if b0 < 0:
        b0 = 0
    block = f"\n    {begin}\n    {entry_text}\n    {end}"
    fixed = text[:b0] + block + text[e + len(end) :]
    fixed = fix_trailing_commas(fixed)
    if not check_jsonc(fixed):
        raise ValueError("после обновления файл не читается — откат")
    return fixed, True


def _remove_entry_by_name(text: str, obj_key: str, name: str) -> str:
    """Удаляет запись "name": value из объекта obj_key. Работает без меток."""
    bounds = find_key_object(text, obj_key)
    if bounds is None:
        return text
    
    chunk_start = bounds[0]
    chunk_end = bounds[1] + 1
    chunk = text[chunk_start:chunk_end]
    
    pattern = re.compile(r'^\s*"' + re.escape(name) + r'"\s*:\s*', re.MULTILINE)
    match = re.search(pattern, chunk)
    if not match:
        return text
    
    match_start = match.start()
    match_end = match.end()
    
    # Позиция в полном тексте
    abs_start = bounds[0] + match.start()
    match_end_abs = bounds[0] + match.end()
    
    # Смотрим, что после двоеточия - в самом чанке
    after_colon_chunk = chunk[match.end():].lstrip()
    
    if after_colon_chunk.startswith("{"):
        # Объект: ищем закрывающую скобку в чанке
        brace_start_rel = chunk.find("{", match.end())
        if brace_start_rel != -1:
            depth = 0
            end_rel = None
            for i in range(brace_start_rel, len(chunk)):
                if chunk[i] == "{":
                    depth += 1
                elif chunk[i] == "}":
                    depth -= 1
                    if depth == 0:
                        end_rel = i
                        break
            if end_rel is not None:
                # Позиции в полном тексте
                abs_start = bounds[0]
                abs_end = bounds[0] + end_rel + 1
                if abs_end < len(text) and text[abs_end] == ",":
                    abs_end += 1
                # Удаляем запись: пересобираем чанк без неё
                new_chunk = chunk[:match.start()] + chunk[match.end():brace_start_rel] + chunk[end_rel+1:]
                text = text[:bounds[0]] + new_chunk + text[bounds[1]+1:]
            else:
                # Строка: "key": "value"
                pass  # упрощённо
    return text


def rewire_config(dest: Path, base: Path) -> list[str]:
    """Переводит opencode.jsonc в папке настроек на эту базу.

    Меняет массив instructions и блоки мостов ncp/pc с правами, чтобы
    всё указывало на новую базу. Чужие серверы и провайдеры не трогает.
    Возвращает список сообщений. Бросает ValueError, если файл не читается.
    """
    messages: list[str] = []
    cfg = dest / "opencode.jsonc"
    if not cfg.is_file():
        raise ValueError("нет opencode.jsonc — мосты подключать некуда")
    text = cfg.read_text(encoding="utf-8")
    if not check_jsonc(text):
        raise ValueError("opencode.jsonc сломан — правим руками, не автоматом")
    text = set_instructions(text, base)
    # Объекты mcp и permission могут отсутствовать: эталонный opencode.jsonc
    # в базе описывает только instructions, и мостов в нём ещё нет. Без
    # ensure_object вставка падала бы с «нет объекта mcp», и подключение
    # не доходило до конца.
    text = ensure_object(text, "mcp")
    text = ensure_object(text, "permission")
    python = find_python()
    for obj_key, name, entry in (
        ("mcp", "ncp", ncp_server_block(base, python)),
        ("mcp", "pc", pc_server_block(base, python)),
        ("permission", "ncp_*", '"ncp_*": "ask"'),
        ("permission", "pc_*", '"pc_*": "ask"'),
    ):
        # Старую запись (если есть) убирает сам insert_entry — иначе путь
        # к базе не обновится при повторном подключении. fix_trailing_commas
        # здесь не зовём: insert_entry и так ставит разделитель сам, а эта
        # чистка срезала бы его, потому что блок кончается вплотную к }.
        text = insert_entry(text, obj_key, name, entry)
        messages.append(f"В настройки вписан блок: {obj_key}.{name}")
    text = ensure_commas(text)
    cfg.write_text(text, encoding="utf-8")
    messages.append("Пути, мосты ncp/pc и права переведены на эту базу")
    return messages


# ------------------------------------------------------------------ файлы


def file_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def read_manifest(dest: Path) -> dict:
    try:
        data = json.loads((dest / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def write_manifest(dest: Path, data: dict) -> None:
    (dest / MANIFEST).write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def server_entry(python: str, server_py: Path) -> str:
    cmd = json.dumps([python, str(server_py).replace("\\", "/")], ensure_ascii=False)
    return f'"command": {cmd}'


def pc_server_block(base: Path, python: str) -> str:
    return (
        '"pc": {\n'
        '      "type": "local",\n'
        f'      {server_entry(python, base / "tools" / "pc-bridge" / "server.py")},\n'
        '      "enabled": true\n'
        "    },"
    )


def ncp_server_block(base: Path, python: str) -> str:
    return (
        '"ncp": {\n'
        '      "type": "local",\n'
        f'      {server_entry(python, base / "tools" / "ncp-bridge" / "server.py")},\n'
        '      "enabled": true\n'
        "    },"
    )


#: Новые пресеты провайдеров: их нет среди встроенных в opencode,
#: окно дописывает их как custom-провайдеры. Формат — из официальной
#: документации opencode (npm @ai-sdk/openai-compatible).
#: (имя, подпись, ключ окружения или "", текст записи)
PROVIDER_PRESETS: dict[str, tuple[str, str, str]] = {
    "ollama": (
        "Ollama (локально, без ключа)",
        "",
        '"ollama": {\n'
        '      "npm": "@ai-sdk/openai-compatible",\n'
        '      "name": "Ollama (local)",\n'
        '      "options": {\n'
        '        "baseURL": "http://localhost:11434/v1"\n'
        "      },\n"
        '      "models": {\n'
        '        "qwen3": {"name": "Qwen3 (local)"},\n'
        '        "llama3.1": {"name": "Llama 3.1 (local)"}\n'
        "      }\n"
        "    },",
    ),
    "lmstudio": (
        "LM Studio (локально, без ключа)",
        "",
        '"lmstudio": {\n'
        '      "npm": "@ai-sdk/openai-compatible",\n'
        '      "name": "LM Studio (local)",\n'
        '      "options": {\n'
        '        "baseURL": "http://127.0.0.1:1234/v1"\n'
        "      },\n"
        '      "models": {\n'
        '        "local-model": {"name": "Модель из LM Studio (поправь id)"}\n'
        "      }\n"
        "    },",
    ),
    # Формат записи и модели — из документации самого OmniRoute
    # (docs/frameworks/OPENCODE.md): тот же npm-пакет, что у двух пресетов
    # выше, адрес — панель и API на одном порту 20128, `auto` — модель,
    # которую советует сам OmniRoute. Ключ `sk_omniroute` — не секрет, а
    # литерал-заглушка: так пишет собственная команда
    # `omniroute config opencode` для локального режима, где проверка
    # ключа выключена и запросы идут с этой машины. Секретов провайдеров
    # (ключей сервисов) в записи нет: их человек вписывает в панели
    # OmniRoute, и в opencode.jsonc они не попадают.
    "omniroute": (
        "OmniRoute (локальный шлюз, ставится в блоке «Серверы MCP»)",
        "",
        '"omniroute": {\n'
        '      "npm": "@ai-sdk/openai-compatible",\n'
        '      "name": "OmniRoute",\n'
        '      "options": {\n'
        '        "baseURL": "http://localhost:20128/v1",\n'
        '        "apiKey": "sk_omniroute"\n'
        "      },\n"
        '      "models": {\n'
        '        "auto": {"name": "Автовыбор модели OmniRoute"},\n'
        '        "claude-sonnet-4-5-thinking": {"name": "Claude Sonnet 4.5 Thinking"},\n'
        '        "gemini-3-flash": {"name": "Gemini 3 Flash"}\n'
        "      }\n"
        "    },",
    ),
    # Мост LMArena. Статического блока у него нет намеренно: имена моделей
    # называет сама арена, и на каждой машине список свой. Блок собирает
    # lmarena.provider_block() из живого ответа моста и передаётся в
    # install_providers(blocks=...). Пустой третий элемент здесь — не
    # забытая строка, а признак: см. PROVIDER_DYNAMIC.
    "lmarena": (
        "LMArena (мост к моделям арены; модели берутся живьём из моста)",
        "",
        "",
    ),
}

#: Пресеты, чей блок собирается из живого ответа моста, а не лежит в коде.
#: Выдумать список моделей нельзя: у арены он свой и меняется. Такой
#: пресет ставится только с готовым блоком (install_providers(blocks=...)),
#: иначе установка отказывает — пустой провайдер в настройках хуже, чем
#: его отсутствие.
PROVIDER_DYNAMIC: set[str] = {"lmarena"}


def install_providers(
    dest: Path,
    selection: set[str],
    progress=None,
    blocks: dict[str, str] | None = None,
) -> tuple[list[str], list[str]]:
    """Дописывает выбранные пресеты в provider. Ключи не трогает и не просит:
    их человек вводит сам (/connect или переменные окружения).

    `blocks` — готовые блоки для пресетов из PROVIDER_DYNAMIC: их собирает
    тот, кто знает живой ответ моста (у LMArena это список моделей арены).
    Для остальных пресетов блок берётся из PROVIDER_PRESETS, как раньше.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    unknown = set(selection) - set(PROVIDER_PRESETS)
    if unknown:
        errors.append(f"Не знаю таких провайдеров: {', '.join(sorted(unknown))}.")
        return messages, errors
    if not selection:
        return messages, errors

    override = dict(blocks or {})
    need_block = [name for name in sorted(selection)
                  if name in PROVIDER_DYNAMIC and not override.get(name)]
    if need_block:
        errors.append(
            "Блок провайдера «" + "», «".join(need_block) + "» собирается из "
            "живого моста: без его ответа список моделей выдумывать нельзя. "
            "Запусти мост и повтори — или возьми кнопку «Настроить "
            "автоматически» у строки моста."
        )
        return messages, errors

    dest.mkdir(parents=True, exist_ok=True)
    cfg = dest / "opencode.jsonc"
    if not cfg.is_file():
        cfg.write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}\n', encoding="utf-8")
    backup_dir = dest / "_previous-version"
    backup_dir.mkdir(exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    try:
        shutil.copy2(cfg, backup_dir / f"opencode.jsonc-{stamp}")
    except OSError as exc:
        errors.append(f"Не сохранилась копия настроек: {exc}")
        return messages, errors
    try:
        text = cfg.read_text(encoding="utf-8")
        if not check_jsonc(text):
            raise ValueError("opencode.jsonc сломан — правим руками, не автоматом")
        text = ensure_object(text, "provider")
        for name in sorted(selection):
            _title, _key, preset_block = PROVIDER_PRESETS[name]
            block = override.get(name) or preset_block
            bounds = find_key_object(text, "provider")
            assert bounds is not None
            if not has_entry(text[bounds[0] : bounds[1]], name):
                text = insert_entry(text, "provider", name, block)
        text = fix_trailing_commas(text)
        if not check_jsonc(text):
            raise ValueError("после вставки файл не читается — откат")
        cfg.write_text(text, encoding="utf-8")
        manifest = read_manifest(dest)
        have = manifest.get("providers", [])
        for name in sorted(selection):
            if name not in have:
                have.append(name)
        manifest["providers"] = have
        write_manifest(dest, manifest)
        say(f"В provider вписано: {', '.join(sorted(selection))}")
        for name in sorted(selection):
            _title, key, _block = PROVIDER_PRESETS[name]
            if key:
                say(f"{name}: ключ задай сам — переменная {key} (или /connect в opencode)")
            else:
                say(f"{name}: ключа не надо, запусти локальный сервер")
    except (OSError, ValueError) as exc:
        errors.append(f"Настройки не тронуты: {exc}")
    return messages, errors


def remove_providers(
    dest: Path,
    selection: set[str],
    progress=None,
) -> tuple[list[str], list[str]]:
    """Убирает пресеты из provider. Чужие записи не трогает."""
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    cfg = dest / "opencode.jsonc"
    if cfg.is_file() and selection:
        try:
            text = cfg.read_text(encoding="utf-8")
            for name in sorted(selection):
                text = remove_entry(text, "provider", name)
            if not check_jsonc(text):
                raise ValueError("после удаления файл не читается — откат")
            cfg.write_text(text, encoding="utf-8")
            manifest = read_manifest(dest)
            manifest["providers"] = [n for n in manifest.get("providers", []) if n not in selection]
            write_manifest(dest, manifest)
            say(f"Из provider убрано: {', '.join(sorted(selection))}")
        except (OSError, ValueError) as exc:
            errors.append(f"Настройки не тронуты: {exc}")
    return messages, errors


def providers_status(dest: Path) -> dict[str, bool]:
    """Какие пресеты сейчас стоят."""
    status = {name: False for name in PROVIDER_PRESETS}
    try:
        text = (dest / "opencode.jsonc").read_text(encoding="utf-8")
        bounds = find_key_object(text, "provider")
        if bounds is not None:
            chunk = text[bounds[0] : bounds[1]]
            for name in status:
                if has_entry(chunk, name):
                    status[name] = True
    except OSError:
        pass
    return status


# ------------------------------------------------------------------ установка


def install_caps(
    base: Path,
    dest: Path,
    selection: set[str],
    progress=None,
    antiblock_opts: dict[str, bool] | None = None,
    caveman_level: str = "lite",
    pxpipe_provider: str = "",
) -> tuple[list[str], list[str]]:
    """Ставит выбранное в папку настроек opencode. Возвращает (сообщения, ошибки).

    antiblock_opts — галочки раздела «Обход блокировок» (фасад, списки,
    команда, ярлык). Без них обход ставится целиком.

    caveman_level — «лёгкий» (lite) или «полный» (full) уровень правил
    caveman. Уровня у остальных возможностей нет, поэтому он один на вызов.

    pxpipe_provider — имя провайдера opencode, к которому pxpipe пересылает
    запросы и у которого берёт модели. Без живого прокси pxpipe ничего не
    впишет: запись без прокси всё равно не заработает.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    unknown = set(selection) - {name for name, _ in CAPS}
    if unknown:
        errors.append(f"Не знаю таких возможностей: {', '.join(sorted(unknown))}.")
        return messages, errors
    if "agents" in selection and not (core.program_root() / "tools" / "agents").is_dir():
        errors.append("В базе нет папки tools/agents — нечего ставить.")
        return messages, errors

    dest.mkdir(parents=True, exist_ok=True)
    manifest = read_manifest(dest)
    manifest.setdefault("files", {})
    manifest.setdefault("jsonc", [])

    # --- opencode.jsonc: копия, потом вставка.
    # Условие на оба моста, а не только на pc: раньше блок был под "pc",
    # и мост ncp в настройки не попадал вовсе — его статус оставался False,
    # а проверка падала. Серверы и права собираем по тому, что выбрано.
    if "pc" in selection or "ncp" in selection:
        cfg = dest / "opencode.jsonc"
        if not cfg.is_file():
            cfg.write_text('{\n  "$schema": "https://opencode.ai/config.json"\n}\n', encoding="utf-8")
            say("Создан пустой opencode.jsonc (его не было)")
        backup_dir = dest / "_previous-version"
        backup_dir.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(cfg, backup_dir / f"opencode.jsonc-{stamp}")
            say("Копия opencode.jsonc сохранена в _previous-version")
        except OSError as exc:
            errors.append(f"Не сохранилась копия настроек: {exc}")
            return messages, errors
        try:
            text = cfg.read_text(encoding="utf-8")
            servers: dict[str, str] = {}
            python = find_python()
            if "pc" in selection:
                servers["pc"] = pc_server_block(base, python)
            if "ncp" in selection:
                servers["ncp"] = ncp_server_block(base, python)
            permissions = {f"{n}_*": '"ask"' for n in ("pc", "ncp") if n in selection}
            cfg.write_text(merge_caps(text, servers, permissions), encoding="utf-8")
            for name in list(servers) + list(permissions):
                if name not in manifest["jsonc"]:
                    manifest["jsonc"].append(name)
            say(f"В opencode.jsonc вписано: {', '.join(sorted(set(servers) | set(permissions)))}")
        except (OSError, ValueError) as exc:
            errors.append(f"Настройки не тронуты: {exc}")
            return messages, errors

    # --- команда /голос
    if "voice" in selection:
        src = core.program_root() / "tools" / "voice" / "voice.md"
        if not src.is_file():
            errors.append("В программе нет tools/voice/voice.md — команду ставить не из чего.")
        else:
            body = src.read_text(encoding="utf-8").replace(
                VOICE_MARK, str(core.program_root() / "tools" / "voice").replace("\\", "/")
            )
            target = dest / "command" / "voice.md"
            if place_file(target, body, manifest, say, errors, "Команда /голос"):
                say("Команда /голос поставлена (папка command)")

    # --- агенты
    if "agents" in selection:
        agents_dir = dest / "agents"
        agents_dir.mkdir(exist_ok=True)
        put, skipped = 0, 0
        for src in sorted((core.program_root() / "tools" / "agents").glob("*.md")):
            target = agents_dir / src.name
            if place_file(target, src.read_text(encoding="utf-8"), manifest, say, errors,
                           f"Агент {src.stem}", quiet=True):
                put += 1
            else:
                skipped += 1
        say(f"Агентов поставлено: {put}, пропущено (чужие): {skipped}")

    write_manifest(dest, manifest)

    # --- обход блокировок: свой модуль, свой манифест. Вызываем после
    # записи нашего манифеста, чтобы модули не затирали друг друга.
    if "antiblock" in selection:
        try:
            antiblock_path = core.program_root() / "tools" / "antiblock"
            if str(antiblock_path) not in sys.path:
                sys.path.insert(0, str(antiblock_path))
            import antiblock  # noqa: PLC0415 — рядом лежит, круга нет

            m_ab, e_ab = antiblock.install_antiblock(
                core.program_root(), dest, opts=antiblock_opts, progress=progress
            )
            messages += m_ab
            errors += e_ab
        except (OSError, ValueError) as exc:
            errors.append(f"Обход блокировок не поставился: {exc}")

    # --- rtk: свой модуль. Он сам делает живую проверку и при отказе
    # ничего не пишет — поэтому вызывается до общего «перезапустите».
    if "rtk" in selection:
        m_rtk, e_rtk = _rtk_module().install(dest, progress=progress)
        messages += m_rtk
        errors += e_rtk

    # --- caveman: правила в AGENTS.md между нашими метками, уровень — выбор
    # человека. Живой проверки тут не нужно: проверять нечего, кроме самого
    # файла настроек, а он перед правкой копируется в _previous-version.
    if "caveman" in selection:
        m_cav, e_cav = _caveman_module().install(dest, caveman_level, progress=progress)
        messages += m_cav
        errors += e_cav

    # --- pxpipe: свой модуль. Он сам проверяет, что прокси жив и запущен
    # программой именно под выбранный источник, и иначе ничего не пишет.
    if "pxpipe" in selection:
        m_px, e_px = _pxpipe_module().install(dest, pxpipe_provider, progress=progress)
        messages += m_px
        errors += e_px

    # --- auto-improve: свой модуль. Он проверяет окружение целиком (копия
    # скрипта, git, requests у Python, ключ судьи) и при нехватке ничего не
    # записывает: без ключа и git цикл всё равно не пойдёт.
    if "auto-improve" in selection:
        m_ai, e_ai = _auto_improve_module().install(dest, progress=progress)
        messages += m_ai
        errors += e_ai

    # --- greenlight: свой модуль. Живая проверка — собранный бинарник,
    # который отвечает на --version. Не собран или Go нет — в настройки
    # ничего не пишем и говорим об этом прямо.
    if "greenlight" in selection:
        m_gl, e_gl = _greenlight_module().install(dest, progress=progress)
        messages += m_gl
        errors += e_gl

    # --- qwen-review: свой модуль. Живая проверка — отвечающий qwen и
    # выбранная модель; не развёрнут или модель не выбрана — в настройки
    # ничего не пишем и говорим об этом прямо.
    if "qwen-review" in selection:
        m_qw, e_qw = _qwen_review_module().install(dest, progress=progress)
        messages += m_qw
        errors += e_qw

    if (
        "pc" in selection
        or "ncp" in selection
        or "voice" in selection
        or "agents" in selection
        or "antiblock" in selection
        or "rtk" in selection
        or "caveman" in selection
        or "pxpipe" in selection
        or "auto-improve" in selection
        or "greenlight" in selection
        or "qwen-review" in selection
    ):
        say("Перезапустите opencode: настройки читаются при старте.")
    return messages, errors


def _rtk_module():
    """Модуль rtk рядом: отдельной функцией, чтобы не плодить импорты."""
    import rtk  # noqa: PLC0415 — рядом лежит, круга нет

    return rtk


def _caveman_module():
    """Модуль caveman рядом."""
    import caveman  # noqa: PLC0415 — рядом лежит, круга нет

    return caveman


def _pxpipe_module():
    """Модуль pxpipe рядом."""
    import pxpipe  # noqa: PLC0415 — рядом лежит, круга нет

    return pxpipe


def _auto_improve_module():
    """Модуль auto-improve рядом."""
    import auto_improve  # noqa: PLC0415 — рядом лежит, круга нет

    return auto_improve


def _greenlight_module():
    """Модуль greenlight рядом."""
    import greenlight  # noqa: PLC0415 — рядом лежит, круга нет

    return greenlight


def _qwen_review_module():
    """Модуль второго ревьюера рядом."""
    import qwen_review  # noqa: PLC0415 — рядом лежит, круга нет

    return qwen_review


def place_file(
    target: Path,
    body: str,
    manifest: dict,
    say,
    errors: list[str],
    title: str,
    quiet: bool = False,
) -> bool:
    """Кладёт файл, чужой (не наш и не из манифеста) — не трогает.
    Хэш считаем по байтам на диске: Windows пишет \\r\\n, и хэш текста
    с \\n никогда бы не сошёлся при проверке."""
    record = manifest.get("files", {}).get(str(target))
    if target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            current = None
        if current == body:
            manifest.setdefault("files", {})[str(target)] = file_hash(target)
            return True
        if record is None:
            errors.append(f"{title}: файл уже есть и не наш — пропустил ({target.name}).")
            return False
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body, encoding="utf-8")
        manifest.setdefault("files", {})[str(target)] = file_hash(target)
        if not quiet:
            say(f"{title}: записан")
        return True
    except OSError as exc:
        errors.append(f"{title}: не записался ({exc}).")
        return False


def remove_caps(
    dest: Path,
    selection: set[str],
    progress=None,
) -> tuple[list[str], list[str]]:
    """Убирает выбранное. Чужие файлы и записи не трогает."""
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    manifest = read_manifest(dest)
    # Как и при установке, блок открыт на оба моста: выбрали ncp — убираем ncp.
    if "pc" in selection or "ncp" in selection:
        cfg = dest / "opencode.jsonc"
        if cfg.is_file():
            try:
                text = cfg.read_text(encoding="utf-8")
                names: list[str] = []
                for bridge in ("pc", "ncp"):
                    if bridge in selection:
                        names += [bridge, f"{bridge}_*"]
                if names:
                    cfg.write_text(unmerge_caps(text, names), encoding="utf-8")
                    manifest["jsonc"] = [n for n in manifest.get("jsonc", []) if n not in names]
                    say(f"Из opencode.jsonc убрано: {', '.join(names)}")
            except (OSError, ValueError) as exc:
                errors.append(f"Настройки не тронуты: {exc}")

    paths: list[Path] = []
    if "voice" in selection:
        paths.append(dest / "command" / "voice.md")
    if "agents" in selection:
        paths += [dest / "agents" / src.name for src in (core.program_root() / "tools" / "agents").glob("*.md")] if (core.program_root() / "tools" / "agents").is_dir() else []
    for target in paths:
        record = manifest.get("files", {}).get(str(target))
        if not target.is_file():
            manifest.get("files", {}).pop(str(target), None)
            continue
        try:
            if record is not None and file_hash(target) != record:
                errors.append(f"Файл {target.name} меняли вручную — оставил как есть.")
                continue
            if record is None:
                errors.append(f"Файл {target.name} не из манифеста — не трогаю.")
                continue
            target.unlink()
            manifest.get("files", {}).pop(str(target), None)
            say(f"Убран файл {target.name}")
        except OSError as exc:
            errors.append(f"Не убрался {target.name}: {exc}")

    write_manifest(dest, manifest)

    # --- rtk: плагин убирает свой модуль и только свой файл.
    if "rtk" in selection:
        m_rtk, e_rtk = _rtk_module().remove(dest)
        messages += m_rtk
        errors += e_rtk

    # --- caveman: из AGENTS.md убирается только наш блок между метками.
    if "caveman" in selection:
        m_cav, e_cav = _caveman_module().remove(dest)
        messages += m_cav
        errors += e_cav

    # --- pxpipe: убирается только наша запись провайдера. Сам прокси это
    # не останавливает: его гасят кнопкой «Остановить pxpipe».
    if "pxpipe" in selection:
        m_px, e_px = _pxpipe_module().remove(dest, progress=progress)
        messages += m_px
        errors += e_px

    # --- auto-improve: снимается только наша отметка в манифесте. Ключ и
    # папка данных остаются на диске — их убирает человек, если захочет.
    if "auto-improve" in selection:
        m_ai, e_ai = _auto_improve_module().remove(dest, progress=progress)
        messages += m_ai
        errors += e_ai

    # --- greenlight: снимается только отметка. Исходники, собранный
    # бинарник, журнал и отчёты остаются на диске.
    if "greenlight" in selection:
        m_gl, e_gl = _greenlight_module().remove(dest, progress=progress)
        messages += m_gl
        errors += e_gl

    # --- qwen-review: снимается только отметка. Развёрнутый Qwen Code,
    # настройки и ход споров остаются на диске.
    if "qwen-review" in selection:
        m_qw, e_qw = _qwen_review_module().remove(dest, progress=progress)
        messages += m_qw
        errors += e_qw

    # --- обход блокировок: убирает свой модуль сам, в запас, не в корзину.
    if "antiblock" in selection:
        try:
            antiblock_path = core.program_root() / "tools" / "antiblock"
            if str(antiblock_path) not in sys.path:
                sys.path.insert(0, str(antiblock_path))
            import antiblock  # noqa: PLC0415 — рядом лежит, круга нет

            m_ab, e_ab = antiblock.remove_antiblock(dest, progress=progress)
            messages += m_ab
            errors += e_ab
        except (OSError, ValueError) as exc:
            errors.append(f"Обход блокировок не убрался: {exc}")

    return messages, errors


def caps_status(dest: Path) -> dict[str, bool]:
    """Что из возможностей сейчас стоит в папке настроек."""
    manifest = read_manifest(dest)
    status = {name: False for name, _ in CAPS}
    try:
        text = (dest / "opencode.jsonc").read_text(encoding="utf-8")
        for key in ("mcp",):
            bounds = find_key_object(text, key)
            if bounds is None:
                continue
            chunk = text[bounds[0] : bounds[1]]
            if has_entry(chunk, "pc"):
                status["pc"] = True
            if has_entry(chunk, "ncp"):
                status["ncp"] = True
    except OSError:
        pass
    if (dest / "command" / "voice.md").is_file():
        status["voice"] = True
    agents = list((dest / "agents").glob("*.md")) if (dest / "agents").is_dir() else []
    if agents:
        status["agents"] = True
    try:
        antiblock_path = core.program_root() / "tools" / "antiblock"
        if str(antiblock_path) not in sys.path:
            sys.path.insert(0, str(antiblock_path))
        import antiblock  # noqa: PLC0415 — рядом лежит, круга нет

        if antiblock.antiblock_status(dest):
            status["antiblock"] = True
    except Exception:
        pass
    try:
        status["rtk"] = bool(_rtk_module().status(dest).get("installed"))
    except Exception:
        pass
    try:
        status["caveman"] = bool(_caveman_module().status(dest).get("installed"))
    except Exception:
        pass
    try:
        status["pxpipe"] = bool(_pxpipe_module().status(dest).get("installed"))
    except Exception:
        pass
    try:
        status["auto-improve"] = bool(_auto_improve_module().installed(dest))
    except Exception:
        pass
    try:
        status["greenlight"] = bool(_greenlight_module().installed(dest))
    except Exception:
        pass
    try:
        status["qwen-review"] = bool(_qwen_review_module().installed(dest))
    except Exception:
        pass
    _ = manifest
    return status
