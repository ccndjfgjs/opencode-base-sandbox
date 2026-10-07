"""caveman — короткие ответы: правила в AGENTS.md, два уровня на выбор.

Что это. caveman — набор правил автора (JuliusBrussee, лицензия Apache-2.0),
который просит нейросеть отвечать телеграфным стилем: без вступлений,
вежливостей и пересказа, весь технический смысл остаётся, код — дословно.
README автора поддерживает opencode, поэтому правила переносятся не «на
всякий случай», а штатным для базы путём.

Где что лежит:

    tools/thirdparty/caveman/levels/lite.md   правила «лёгкого» уровня (копия
                                              src/rules/caveman-activate.md)
    tools/thirdparty/caveman/levels/full.md   правила «полного» уровня (копия
                                              skills/ultracave/SKILL.md)
    tools/thirdparty/caveman/LICENSE          лицензия автора (Apache-2.0)
    tools/thirdparty/caveman/NOTICE           уведомление автора
    <папка настроек opencode>/AGENTS.md       наши правила между метками

Уровни. «Лёгкий» — базовые правила автора, «полный» — его же `ultracave`,
самая жёсткая ступень. По умолчанию «лёгкий»: полный режим заметно режет
язык и подходит не всем задачам. Выбор уровня меняет только текст между
нашими метками, остальные строки AGENTS.md не трогаются.

Чего программа не делает: не запускает установщик автора
(`node bin/install.js --only opencode`). Он пишет плагин, команды, агентов
и навыки и переписывает `opencode.jsonc` обычным JSON — комментарии в файле
настроек при этом теряются (об этом предупреждает сам установщик, оставляя
`.bak`). Наши настройки с комментариями, и чужой установщик их бы сломал.

Про цифры. 65–75% экономии — заявление автора в описании репозитория,
независимой проверки нет. В собственном README автора числа скромнее:
скилл «caveman» — 3% на его же проверке (в пределах шума), «ultracave» —
35%, JetBrains на 86 задачах — 8.5% без заметной потери качества.
Правила добавляют текст в каждый запрос, и окупаются ли они, зависит от
модели и задачи. Поэтому и написано прямо, а не обещано.
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path

#: Откуда взяты правила и по какому коммиту сверялись их байты.
SOURCE = "https://github.com/JuliusBrussee/caveman"
SOURCE_TAG = "v3.1.0"
SOURCE_COMMIT = "8af1f1b9b1346bca0722a1556f119b4e6675cc96"
SOURCE_NOTE = "коммит от 03.10.2026"
LICENSE = "Apache-2.0"

#: SHA-1 blob каждого файла правил: по нему видно, что копию не правили.
SOURCE_BLOBS = {
    "levels/lite.md": "f4746bc269ebc5bff165f3f9434fccf242910090",
    "levels/full.md": "c9634bba64d7bd8250023fb975e4cb08311d49f6",
}

#: Уровни: ключ → (слово для окна, имя режима автора, файл правил).
LEVELS = {
    "lite": ("лёгкий", "caveman", "levels/lite.md"),
    "full": ("полный", "ultracave", "levels/full.md"),
}
DEFAULT_LEVEL = "lite"

#: Файл настроек opencode, в который вписываются правила, и наши метки.
#: Метки свои: чужой установщик ставит вокруг своих правил `caveman-begin`,
#: и путать их нельзя.
AGENTS_FILE = "AGENTS.md"
BEGIN_MARK = "<!-- == OpenCode_Base: caveman == -->"
END_MARK = "<!-- == OpenCode_Base: конец caveman == -->"

#: Шапка блока: кому принадлежат правила и чьи это цифры.
NOTE = (
    "Правила ниже — чужая работа: JuliusBrussee/caveman (Apache-2.0, файл\n"
    "tools/thirdparty/caveman/{file}). Ставит и убирает их программа базы:\n"
    "вкладка opencode, «caveman». Экономию 65–75% называет автор, независимо\n"
    "она не проверена; его же README даёт 3% у скилла «caveman» и 35% у\n"
    "«ultracave». Правила действуют, пока их не сняли галочкой или словами\n"
    "«stop caveman» / «normal mode»."
)


def program_root() -> Path:
    """Корень программы — папка, в которой лежит `tools`."""
    return Path(__file__).resolve().parent.parent.parent


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


def level_file(level: str) -> Path:
    """Путь к файлу правил уровня в репозитории."""
    key = level if level in LEVELS else DEFAULT_LEVEL
    return program_root() / "tools" / "thirdparty" / "caveman" / LEVELS[key][2]


def strip_frontmatter(text: str) -> str:
    """Убирает служебную шапку `--- … ---` из файла правил.

    У уровня «полный» правила лежат в файле навыка, и шапка там нужна
    opencode, а в AGENTS.md она лишняя: это описание файла, а не правило.
    """
    if not text.startswith("---"):
        return text
    lines = text.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return text
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return "".join(lines[index + 1:]).lstrip("\n")
    return text


def level_rules(level: str) -> str:
    """Текст правил выбранного уровня — как он лежит в репозитории."""
    text = level_file(level).read_text(encoding="utf-8")
    return strip_frontmatter(text).rstrip("\n") + "\n"


def block_text(level: str) -> str:
    """Готовый блок для AGENTS.md вместе с метками."""
    key = level if level in LEVELS else DEFAULT_LEVEL
    word, mode, rel = LEVELS[key]
    header = (
        f"## Ответы короче — caveman, уровень: {word} ({mode})\n\n"
        + NOTE.format(file=rel)
        + "\n\n"
    )
    return f"{BEGIN_MARK}\n{header}{level_rules(key)}{END_MARK}\n"


def _block_pattern() -> re.Pattern[str]:
    return re.compile(
        re.escape(BEGIN_MARK) + r".*?" + re.escape(END_MARK) + r"\n?",
        re.DOTALL,
    )


def _backup(target: Path, name: str) -> tuple[bool, bool, str]:
    """Копия файла настроек перед правкой.

    Возвращает (получилось, копия сделана, текст ошибки). Копии нет, когда
    файла ещё нет: сохранять нечего.
    """
    if not target.is_file():
        return True, False, ""
    backup = target.parent / "_previous-version"
    try:
        backup.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copy2(target, backup / f"{name}-{stamp}")
    except OSError as exc:
        return False, False, f"Не сохранилась копия {name}: {exc}"
    return True, True, ""


def install(dest: Path, level: str = DEFAULT_LEVEL, progress=None) -> tuple[list[str], list[str]]:
    """Вписывает правила выбранного уровня в AGENTS.md настроек opencode.

    Чужие строки файла остаются как есть: меняется только наш блок между
    метками. Повторная установка с тем же уровнем файл не переписывает.
    """
    messages: list[str] = []
    errors: list[str] = []

    def say(text: str) -> None:
        messages.append(text)
        if progress:
            progress(text)

    key = level if level in LEVELS else DEFAULT_LEVEL
    if level not in LEVELS:
        say(f"Уровень «{level}» неизвестен — взял «{LEVELS[key][0]}».")
    block = block_text(key)
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    target = dest / AGENTS_FILE

    try:
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"AGENTS.md не читается: {exc}. Ничего не вписано.")
        return messages, errors

    begin_count = text.count(BEGIN_MARK)
    end_count = text.count(END_MARK)
    if begin_count != end_count or begin_count > 1:
        errors.append(
            "В AGENTS.md метки caveman стоят не парой — файл не тронут. "
            "Убери лишние строки с метками и повтори."
        )
        return messages, errors

    if begin_count == 1:
        current = _block_pattern().search(text)
        if current and current.group(0) == block:
            say(f"Правила caveman уже стоят, уровень «{LEVELS[key][0]}» — файл не менялся.")
            return messages, errors
        next_text = _block_pattern().sub(block, text, count=1)
        action = f"Правила caveman обновлены: уровень «{LEVELS[key][0]}»"
    else:
        next_text = text
        if next_text and not next_text.endswith("\n"):
            next_text += "\n"
        next_text = next_text + ("\n" if next_text else "") + block
        action = f"Правила caveman дописаны: уровень «{LEVELS[key][0]}»"

    ok, copied, note = _backup(target, AGENTS_FILE)
    if not ok:
        errors.append(f"{note}. Ничего не вписано.")
        return messages, errors
    if copied:
        say("Копия AGENTS.md сохранена в _previous-version")

    try:
        target.write_text(next_text, encoding="utf-8")
    except OSError as exc:
        errors.append(f"AGENTS.md не записался: {exc}")
        return messages, errors
    try:
        if BEGIN_MARK not in target.read_text(encoding="utf-8"):
            errors.append("AGENTS.md не читается после записи — проверь файл руками.")
            return messages, errors
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"AGENTS.md не читается после записи: {exc}")
        return messages, errors
    say(action)
    say("Перезапустите opencode: AGENTS.md читается при старте.")
    return messages, errors


def remove(dest: Path) -> tuple[list[str], list[str]]:
    """Убирает только наш блок caveman из AGENTS.md."""
    messages: list[str] = []
    errors: list[str] = []
    dest = Path(dest)
    target = dest / AGENTS_FILE
    try:
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"AGENTS.md не читается: {exc}")
        return messages, errors
    if BEGIN_MARK not in text:
        messages.append("Правил caveman в AGENTS.md нет — убирать нечего.")
        return messages, errors
    if text.count(BEGIN_MARK) != text.count(END_MARK):
        errors.append("В AGENTS.md метки caveman стоят не парой — файл не тронут.")
        return messages, errors

    next_text = _block_pattern().sub("", text, count=1)
    next_text = re.sub(r"\n{3,}", "\n\n", next_text).strip("\n")
    if next_text:
        next_text += "\n"

    ok, _copied, note = _backup(target, AGENTS_FILE)
    if not ok:
        errors.append(f"{note}. Ничего не убрано.")
        return messages, errors
    try:
        target.write_text(next_text, encoding="utf-8")
    except OSError as exc:
        errors.append(f"AGENTS.md не записался: {exc}")
        return messages, errors
    messages.append("Правила caveman убраны из AGENTS.md.")
    return messages, errors


def rescue(dest: Path, old_text: str) -> bool:
    """Возвращает блок caveman в AGENTS.md, если он пропал при перезаписи.

    Файл настроек `AGENTS.md` при повторном «Подключить базу» копируется из
    базы заново — и правила, поставленные галочкой, исчезли бы молча.
    Возвращает True, если блок пришлось вернуть. Чужое не трогает: если в
    новом файле наши метки уже есть, ничего не делает.
    """
    if not old_text or BEGIN_MARK not in old_text:
        return False
    if old_text.count(BEGIN_MARK) != 1 or old_text.count(END_MARK) != 1:
        return False
    found = _block_pattern().search(old_text)
    if not found:
        return False
    block = found.group(0)
    if not block.endswith("\n"):
        block += "\n"
    target = Path(dest) / AGENTS_FILE
    try:
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return False
    if BEGIN_MARK in text:
        return False
    if text and not text.endswith("\n"):
        text += "\n"
    text = text + ("\n" if text else "") + block
    try:
        target.write_text(text, encoding="utf-8")
    except OSError:
        return False
    return True


def status(dest: Path) -> dict[str, object]:
    """Стоит ли наш блок и какой уровень выбран."""
    target = Path(dest) / AGENTS_FILE
    result: dict[str, object] = {"installed": False, "level": "", "word": "", "note": ""}
    try:
        text = target.read_text(encoding="utf-8") if target.is_file() else ""
    except (OSError, UnicodeDecodeError):
        return result
    if text.count(BEGIN_MARK) == 0 and text.count(END_MARK) == 0:
        return result
    if text.count(BEGIN_MARK) != 1 or text.count(END_MARK) != 1:
        result["note"] = "метки caveman стоят не парой"
        return result
    chunk = text.split(BEGIN_MARK, 1)[1].split(END_MARK, 1)[0]
    for key, (word, mode, _file) in LEVELS.items():
        if f"уровень: {word} ({mode})" in chunk:
            result.update({"installed": True, "level": key, "word": word})
            return result
    result["note"] = "блок есть, но уровень в нём не назван"
    return result


def status_text(dest: Path) -> str:
    """Строка состояния словами — для окна и для командной строки."""
    data = status(dest)
    if not data.get("installed"):
        note = str(data.get("note") or "")
        return "Правила caveman: не стоят" + (f" ({note})." if note else ".")
    return f"Правила caveman: стоят, уровень «{data['word']}»."


def _say(text: str) -> None:
    print(text, flush=True)


def _main(argv: list[str]) -> int:
    """Командная строка caveman: то же, что галочка и уровень в окне."""
    action = (argv[0] if argv else "status").lower()
    dest = find_settings_dir()
    if action in ("-h", "--help", "help"):
        _say("caveman.py status | install [lite|full] | remove")
        _say("Переключатель и уровень — на вкладке opencode.")
        return 0
    if action == "status":
        _say(status_text(dest))
        return 0
    if action == "install":
        level = argv[1].lower() if len(argv) > 1 else DEFAULT_LEVEL
        messages, errors = install(dest, level, progress=_say)
    elif action == "remove":
        messages, errors = remove(dest)
    else:
        _say(f"Не знаю такой команды: {action}")
        _say("caveman.py status | install [lite|full] | remove")
        return 2
    for line in messages:
        _say(line)
    for line in errors:
        _say("Ошибка: " + line)
    return 1 if errors else 0


if __name__ == "__main__":
    import sys as _sys

    _sys.exit(_main(_sys.argv[1:]))
