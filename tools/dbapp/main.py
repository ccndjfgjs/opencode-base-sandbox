"""Окно управления базой: создать новую или подключить существующую.

Запуск:
    python tools/dbapp/main.py
"""

from __future__ import annotations

import getpass
import json
import os
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import core  # type: ignore[import-not-found]
    import ui  # type: ignore[import-not-found]
    import mcp_registry  # type: ignore[import-not-found]
    import opencode_caps  # type: ignore[import-not-found]
    import android_studio  # type: ignore[import-not-found]
    import bridges  # type: ignore[import-not-found]
    import dbhub  # type: ignore[import-not-found]
    import program_cards  # type: ignore[import-not-found]
    import winget_install  # type: ignore[import-not-found]
else:  # запуск как модуль
    from . import (core, ui, mcp_registry, opencode_caps, android_studio, bridges,
                   dbhub, program_cards, winget_install)

from PyQt6.QtCore import Qt, QThread, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)


# ---------------------------------------------------------------- рабочий поток


#: Высота строки списка навыков в пикселях и запас на рамку. Строки у нас
#: ровно две — название и короткое описание, — поэтому высота постоянная.
#: Константа, а не `sizeHintForRow`: спрашивать у виджета смысла нет, когда
#: высота строки известна и не меняется.
SKILL_ROW_PX = 26
SKILL_LIST_PAD_PX = 10


def fit_skills_height(lst) -> None:
    """Растянуть список навыков так, чтобы все были видны сразу.

    Подпись под списком говорит «Отмечены все: 32», а сам список показывал
    14 — и выглядело так, будто двух третей навыков в нём нет. Внутренняя
    прокрутка списка вводила в заблуждение: человек видел часть и считал,
    что остальных не дали. Теперь прокручивается страница целиком, а
    список показывает все навыки разом.

    Функция на уровне модуля, а не метод класса: списки живут в разных
    вкладках — `CreateTab` и `CapsTab`, — и метод одного из них из другого
    был бы недоступен.
    """
    count = lst.count()
    if count == 0:
        return
    needed = SKILL_ROW_PX * count + SKILL_LIST_PAD_PX
    target = min(needed, 1000)
    lst.setMinimumHeight(target)
    lst.setMaximumHeight(target)


class ScrollPage(QScrollArea):
    """Вкладка с прокруткой.

    Без неё, если содержимое не помещается в окно, Qt сжимает элементы
    и накладывает их друг на друга — получается «каша» из подписей.
    Прокрутка это решает: содержимое держит свои размеры, а лишнее
    уходит вниз под колесо мыши.
    """

    def __init__(self, inner: QWidget, parent=None) -> None:
        super().__init__(parent)
        self.setWidget(inner)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        # колесо мыши должно крутить содержимое, а не «залипать» на краях
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        inner.setAutoFillBackground(False)
        # страницу запрещаем сжимать: её высота — это её настоящая высота.
        # Иначе Qt подгоняет её под окно и рисует элементы друг поверх друга.
        inner.setMinimumHeight(inner.sizeHint().height())
        inner.setSizePolicy(
            QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Minimum
        )
        self._inner = inner

    def showEvent(self, event) -> None:  # noqa: D102
        """Пересчитываем нужную высоту при каждом показе вкладки."""
        super().showEvent(event)
        self.refresh_height()

    def resizeEvent(self, event) -> None:  # noqa: D102
        super().resizeEvent(event)
        self.refresh_height()

    def refresh_height(self) -> None:
        """Держит минимальную высоту страницы по её содержимому."""
        layout = self._inner.layout()
        if layout is None:
            return
        need = layout.minimumSize().height()
        hint = layout.sizeHint().height()
        value = max(need, hint)
        if self._inner.minimumHeight() != value:
            self._inner.setMinimumHeight(value)


class Worker(QThread):
    """Выполняет длинную работу в стороне, чтобы окно не замирало."""

    line = pyqtSignal(str, str)

    def __init__(self, func, parent=None) -> None:
        super().__init__(parent)
        self._func = func
        self.result = None

    def run(self) -> None:  # noqa: D102
        try:
            self.result = self._func(self._progress)
        except Exception as exc:  # показываем причину, а не падаем
            self.line.emit(f"Ошибка: {exc}", "error")
            self.result = exc

    def _progress(self, text: str) -> None:
        self.line.emit(text, "info")


# ---------------------------------------------------------------- подсказки


def подсказка_где_база(folder: Path) -> str:
    """Объясняет, почему папка не подошла и где искать настоящую базу.

    Пустая папка — самая частая причина. Тогда ищем базы рядом:
    может, выбрана папка-родитель, а база лежит внутри.
    """
    try:
        внутри = [d for d in folder.iterdir() if d.is_dir()]
    except OSError:
        внутри = []

    найдены = []
    for d in внутри[:40]:
        try:
            if (d / core.REQUIRED_FILES[0]).is_file():
                найдены.append(d.name)
        except OSError:
            continue

    если_пусто = not any(folder.iterdir()) if folder.is_dir() else False
    хвост = ""
    if len(найдены) == 1:
        хвост = (
            f"\n\nПохоже, база лежит внутри этой папки — "
            f"выберите вложенную папку «{найдены[0]}»."
        )
    elif найдены:
        хвост = (
            "\n\nВнутри этой папки есть готовые базы: "
            + ", ".join(f"«{n}»" for n in найдены[:5])
            + ". Выберите одну из них."
        )
    elif если_пусто:
        хвост = (
            "\n\nПапка пустая. Нужно выбрать ту папку, которую создала "
            "программа: в ней должны лежать файлы "
            + ", ".join(core.REQUIRED_FILES)
            + "."
        )
    else:
        хвост = (
            "\n\nГотовых баз внутри нет. Проверьте, что выбран именно тот "
            "путь, который был указан при создании: в базе обязаны лежать "
            "файлы " + ", ".join(core.REQUIRED_FILES) + "."
        )
    return f"Не похоже на базу: нет файла {core.REQUIRED_FILES[0]}." + хвост


# ---------------------------------------------------------------- вкладка 1


class CreateTab(ScrollPage):
    """Создание новой пустой базы и подключение её к программе."""

    base_ready = pyqtSignal(str)

    def __init__(self, parent=None) -> None:
        self._plan: core.CreationPlan | None = None
        self._worker: Worker | None = None
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    # ---- сборка окна

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(
            ui.label(
                "Новая база создаётся как обычная папка с файлами — "
                "так же, как та, с которой вы работаете сейчас.",
                kind="dim",
                wrap=True,
            )
        )

        # --- шаг 1: где создать
        box_path = QGroupBox("1. Где создать базу")
        path_layout = QVBoxLayout(box_path)

        self.parent_edit = QLineEdit()
        self.parent_edit.setPlaceholderText(
            "Папка, внутри которой появится новая база"
        )
        # По умолчанию — та самая папка DataBases, которую программа создаёт
        # при первом запуске. Раньше здесь стояли «Документы», из-за чего
        # базы уезжали мимо папки, которую же программа и рекомендует.
        # Если создать не вышло (нет прав) — берём домашнюю папку.
        try:
            default_parent = core.ensure_data_bases_folder()
        except OSError:
            default_parent = Path.home()
        if not default_parent.is_dir():
            default_parent = Path.home()
        self.parent_edit.setText(str(default_parent))
        btn_pick_parent = QPushButton("Обзор…")
        btn_pick_parent.clicked.connect(self._pick_parent)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Папка-родитель:"))
        row1.addWidget(self.parent_edit, 1)
        row1.addWidget(btn_pick_parent)
        path_layout.addLayout(row1)

        # Решение человека о чувствительных данных. По умолчанию
        # выключено: без прямой просьбы секретное не пишется.
        self.chk_sensitive = QCheckBox(
            "Разрешить в этой базе пароли и ключи "
            "(иначе нейросеть откажется их записывать)"
        )
        self.chk_sensitive.setToolTip(
            "Сними галочку — нейросеть не будет записывать пароли, ключи "
            "и токены даже по просьбе, а предложит хранилище. "
            "По умолчанию выключено."
        )
        path_layout.addWidget(self.chk_sensitive)

        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("например: Моя-база")
        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Имя новой базы:  "))
        row2.addWidget(self.name_edit, 1)
        path_layout.addLayout(row2)

        self.name_hint = ui.label(
            "Имя станет именем папки. Пробелы и буквы любого языка разрешены; "
            "нельзя только  < > : \" / \\ | ? *",
            kind="dim",
            wrap=True,
        )
        path_layout.addWidget(self.name_hint)

        self.path_preview = ui.label("", kind="dim", wrap=True)
        path_layout.addWidget(self.path_preview)
        outer.addWidget(box_path)

        # --- шаг 2: чем наполнить
        box_content = QGroupBox("2. Чем наполнить новую базу")
        content_layout = QVBoxLayout(box_content)

        self.radio_blank = QRadioButton("Пустая база")
        self.radio_template = QRadioButton("Скопировать содержимое существующей")
        self.radio_blank.setChecked(True)

        content_layout.addWidget(self.radio_blank)
        self.blank_hint = ui.label(
            "Личные файлы будут пустыми. Знания, скиллы, конфиг и обход блокировок придут автоматически из главной базы.",
            kind="dim",
            wrap=True,
        )
        content_layout.addWidget(self.blank_hint)
        content_layout.addWidget(self.radio_template)

        self.template_edit = QLineEdit()
        self.template_edit.setPlaceholderText("Папка существующей базы с файлами")
        self.template_edit.setEnabled(False)
        self.btn_pick_template = QPushButton("Обзор…")
        self.btn_pick_template.setEnabled(False)
        self.btn_pick_template.clicked.connect(self._pick_template)

        row3 = QHBoxLayout()
        row3.addWidget(QLabel("Откуда взять:"))
        row3.addWidget(self.template_edit, 1)
        row3.addWidget(self.btn_pick_template)
        content_layout.addLayout(row3)

        self.template_hint = ui.label("", kind="dim", wrap=True)
        content_layout.addWidget(self.template_hint)

        self.copy_skills = QCheckBox("Перенести скиллы из образца")
        self.copy_skills.setChecked(True)
        self.copy_skills.setEnabled(False)
        content_layout.addWidget(self.copy_skills)
        outer.addWidget(box_content)

        # --- шаг 3: куда подключить
        box_attach = QGroupBox("3. Подключить к OpenCode")
        attach_layout = QVBoxLayout(box_attach)

        self.attach_check = QCheckBox("Подключить базу к программе сразу")
        self.attach_check.setChecked(True)
        attach_layout.addWidget(self.attach_check)

        self.program_list = QListWidget()
        self.program_list.setMinimumHeight(150)
        attach_layout.addWidget(self.program_list)

        self.program_hint = ui.label("", kind="dim", wrap=True)
        attach_layout.addWidget(self.program_hint)

        note = ui.label(
            "Конфликтов с уже открытыми проектами не будет: "
            "база копируется в папку настроек программы, "
            "папки ваших проектов не затрагиваются, "
            "а прежние файлы памяти уходят в _previous-version.\n"
            "Для OpenCode дополнительно ставится плагин памяти и настройки — "
            "именно они заставляют программу читать базу и её скиллы.",
            kind="dim",
            wrap=True,
        )
        attach_layout.addWidget(note)
        outer.addWidget(box_attach)

        # --- шаг 4: ярлык
        box_link = QGroupBox("4. Ярлык на новую базу")
        link_layout = QVBoxLayout(box_link)

        self.link_check = QCheckBox("Создать ярлык, открывающий эту базу")
        self.link_check.setChecked(True)
        link_layout.addWidget(self.link_check)

        self.link_place = QComboBox()
        for ident, title in core.SHORTCUT_PLACES:
            self.link_place.addItem(title, ident)
        row4 = QHBoxLayout()
        row4.addWidget(QLabel("Где разместить:"))
        row4.addWidget(self.link_place, 1)
        link_layout.addLayout(row4)

        self.link_custom_label = QLabel("Своя папка:   ")
        self.link_custom_edit = QLineEdit()
        self.link_custom_edit.setPlaceholderText("Своя папка для ярлыка")
        self.link_custom_edit.setVisible(False)
        self.btn_pick_link = QPushButton("Обзор…")
        self.btn_pick_link.setVisible(False)
        self.link_custom_label.setVisible(False)
        self.btn_pick_link.clicked.connect(self._pick_link_folder)

        row5 = QHBoxLayout()
        row5.addWidget(self.link_custom_label)
        row5.addWidget(self.link_custom_edit, 1)
        row5.addWidget(self.btn_pick_link)
        link_layout.addLayout(row5)

        self.link_hint = ui.label("", kind="dim", wrap=True)
        link_layout.addWidget(self.link_hint)
        outer.addWidget(box_link)

        # --- кнопка и отчёт
        buttons = QHBoxLayout()
        self.btn_preview = QPushButton("Проверить")
        self.btn_preview.clicked.connect(self._preview)
        self.btn_create = QPushButton("Создать базу")
        self.btn_create.setObjectName("primary")
        self.btn_create.setEnabled(False)
        self.btn_create.clicked.connect(self._create)
        self.btn_open = QPushButton("Открыть папку")
        self.btn_open.setEnabled(False)
        self.btn_open.clicked.connect(self._open_result)
        buttons.addWidget(self.btn_preview)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_open)
        buttons.addWidget(self.btn_create)
        outer.addLayout(buttons)

        self.log = ui.LogView()
        self.log.setMinimumHeight(170)
        outer.addWidget(self.log)

        self._created: Path | None = None

        # Слежение за полями подключаем только здесь — когда созданы все
        # элементы, на которые реагируют обработчики. Если подключить
        # раньше, обработчик сработает на ещё не созданном поле и уронит
        # программу без всякого сообщения.
        self.name_edit.textChanged.connect(self._name_changed)
        self.parent_edit.textChanged.connect(self._name_changed)
        self.radio_blank.toggled.connect(self._mode_changed)
        self.template_edit.textChanged.connect(self._check_template)
        self.attach_check.toggled.connect(self._attach_toggled)
        self.program_list.itemSelectionChanged.connect(self._program_changed)
        self.link_check.toggled.connect(self._link_toggled)
        self.link_place.currentIndexChanged.connect(self._link_place_changed)
        self._fill_programs()
        self._mode_changed()
        self._link_toggled()

        # раскладываем и фиксируем настоящую высоту страницы — иначе
        # Qt сожмёт её и элементы наедут друг на друга
        outer.activate()
        self.refresh_height()

    def _fill_programs(self) -> None:
        from PyQt6.QtGui import QColor

        for program in core.PROGRAMS:
            installed = program.is_installed()
            mark = "установлена" if installed else "не найдена"
            if not program.can_attach():
                mark += ", базу не читает"
            text = f"{program.title} — {program.hint}  [{mark}]"
            item = QListWidgetItem(text)
            item.setData(1000, program.ident)
            if not installed:
                item.setForeground(QColor(ui.TEXT_DIM))
            self.program_list.addItem(item)
        # выбираем первую установленную
        for index in range(self.program_list.count()):
            ident = self.program_list.item(index).data(1000)
            if core.PROGRAMS_BY_ID[ident].is_installed():
                self.program_list.setCurrentRow(index)
                break

    # ---- реакции

    def _program_changed(self) -> None:
        """Показывает, куда попадёт база и увидит ли её программа."""
        program = self._selected_program()
        if program is None:
            self.program_hint.setText("")
            return
        if not program.can_attach():
            self.program_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.program_hint.setText(
                f"{program.title}: {program.ability_note()} "
                "Подключить базу к этой программе нельзя — файлы туда "
                "не переносятся."
            )
            return
        text = f"Куда: {program.config_dir()}. {program.ability_note()}"
        if program.supports_skills:
            self.program_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
        else:
            self.program_hint.setStyleSheet(f"color: {ui.WARN};")
            text += " Навыки не переносятся: программа их не читает."
        self.program_hint.setText(text)

    def _pick_parent(self) -> None:
        start = self.parent_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Где создать базу", start)
        if chosen:
            self.parent_edit.setText(chosen)

    def _pick_template(self) -> None:
        start = self.template_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Выберите существующую базу", start
        )
        if chosen:
            self.template_edit.setText(chosen)
            self._check_template()

    def _mode_changed(self) -> None:
        use_template = self.radio_template.isChecked()
        self.template_edit.setEnabled(use_template)
        self.btn_pick_template.setEnabled(use_template)
        self.copy_skills.setEnabled(use_template)
        if use_template:
            self._check_template()
        else:
            self.template_hint.setText("")
        self._name_changed(self.name_edit.text())

    def _check_template(self) -> None:
        raw = self.template_edit.text().strip()
        if not raw:
            self.template_hint.setText("")
            return
        folder = Path(raw)
        if not folder.is_dir():
            self.template_hint.setStyleSheet(f"color: {ui.WARN};")
            self.template_hint.setText("Такой папки нет. Проверьте путь.")
            return
        info = core.base_info(folder)
        if info["is_base"]:
            self.template_hint.setStyleSheet(f"color: {ui.OK};")
            self.template_hint.setText(
                f"Подходит: скиллов {info['skills']}, "
                f"размер {core.human_size(int(info['size']))}. "
                "Новая база будет такой же, плюс ваши имя и путь."
            )
        else:
            self.template_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.template_hint.setText(_подсказка_где_база(folder))

    def _attach_toggled(self) -> None:
        on = self.attach_check.isChecked()
        self.program_list.setEnabled(on)
        self.program_hint.setEnabled(on)

    # ---- ярлык

    def _link_toggled(self) -> None:
        on = self.link_check.isChecked()
        self.link_place.setEnabled(on)
        self._link_place_changed()

    def _link_place_changed(self) -> None:
        """Своя папка нужна только для пункта «Своя папка…»."""
        on = self.link_check.isChecked()
        own = on and self.link_place.currentData() == "custom"
        # поле показываем только когда оно нужно — иначе оно путает
        self.link_custom_label.setVisible(own)
        self.link_custom_edit.setVisible(own)
        self.btn_pick_link.setVisible(own)

        if not on:
            self.link_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            self.link_hint.setText("Ярлык создаваться не будет.")
            return

        where = self._link_folder()
        if where is None:
            self.link_hint.setStyleSheet(f"color: {ui.WARN};")
            self.link_hint.setText("Выберите папку для ярлыка.")
            return

        name = self.name_edit.text().strip() or "имя базы"
        self.link_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
        self.link_hint.setText(
            f"Появится файл «{core.safe_link_name(name)}.lnk» в папке:\n{where}\n"
            "Двойной щелчок по нему открывает папку базы в Проводнике."
        )

    def _link_folder(self) -> Path | None:
        """Папка для ярлыка или None, если выбрать нельзя."""
        base = Path(self.parent_edit.text().strip() or ".") / (
            self.name_edit.text().strip() or "база"
        )
        try:
            return core.resolve_shortcut_folder(
                self.link_place.currentData() or "",
                base,
                self.link_custom_edit.text(),
            )
        except core.NameError_:
            return None

    def _pick_link_folder(self) -> None:
        start = self.link_custom_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Где разместить ярлык", start
        )
        if chosen:
            self.link_custom_edit.setText(chosen)
            self._link_place_changed()

    def _name_changed(self, text: str) -> None:
        parent = self.parent_edit.text().strip()
        try:
            name = core.validate_name(text)
        except core.NameError_ as exc:
            self.name_edit.setProperty("bad", "true" if text.strip() else "false")
            self.name_edit.style().unpolish(self.name_edit)
            self.name_edit.style().polish(self.name_edit)
            self.name_hint.setText(str(exc).replace("\n", " "))
            self.name_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.path_preview.setText("")
            self.btn_create.setEnabled(False)
            self._plan = None
            return

        self.name_edit.setProperty("bad", "false")
        self.name_edit.style().unpolish(self.name_edit)
        self.name_edit.style().polish(self.name_edit)
        self.name_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
        self.name_hint.setText(
            "Имя станет именем папки. Пробелы и буквы любого языка разрешены; "
            "нельзя только  < > : \" / \\ | ? *"
        )
        self.path_preview.setText(f"База появится здесь:  {Path(parent) / name}")
        self.btn_create.setEnabled(True)
        self._plan = None

    # ---- действия

    def _selected_program(self) -> core.Program | None:
        item = self.program_list.currentItem()
        if item is None:
            return None
        return core.PROGRAMS_BY_ID.get(item.data(1000))

    def _make_plan(self, *, quiet: bool = False) -> core.CreationPlan | None:
        parent = Path(self.parent_edit.text().strip() or str(Path.home()))
        template = None
        if self.radio_template.isChecked():
            raw = self.template_edit.text().strip()
            if not raw:
                if not quiet:
                    self._warn("Не выбрана папка-образец.")
                return None
            template = Path(raw)
            if not (template / core.REQUIRED_FILES[0]).is_file():
                if not quiet:
                    self._warn(
                        f"В папке-образце нет файла {core.REQUIRED_FILES[0]}."
                    )
                return None
        try:
            if not parent.is_dir():
                parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            if not quiet:
                self._warn(f"Папка-родитель недоступна:\n{exc}")
            return None
        try:
            return core.build_plan(
                parent, self.name_edit.text(), template,
                allow_sensitive=self.chk_sensitive.isChecked(),
            )
        except core.NameError_ as exc:
            if not quiet:
                self._warn(str(exc))
            return None
        except OSError as exc:
            if not quiet:
                self._warn(f"Не удалось проверить место:\n{exc}")
            return None

    def _preview(self) -> None:
        plan = self._make_plan()
        if not plan:
            return
        self._plan = plan
        self.log.clear_log()
        self.log.add("Проверка пройдена. Будет создано:", "ok")
        self.log.add(f"  папка: {plan.target}", "dim")
        for folder in plan.dirs:
            self.log.add(f"  + {folder}", "dim")
        for name in plan.files:
            self.log.add(f"  файл: {name}", "dim")
        for warning in plan.warnings:
            self.log.add(f"Внимание: {warning}", "warn")

        if self.link_check.isChecked():
            where = self._link_folder()
            if where is None:
                self.log.add(
                    "Ярлык: папка не выбрана — он не будет создан.", "warn"
                )
            else:
                label = core.safe_link_name(plan.name)
                self.log.add(f"Ярлык: {where / (label + '.lnk')}", "info")
        else:
            self.log.add("Ярлык создаваться не будет.", "dim")

        if self.attach_check.isChecked():
            program = self._selected_program()
            if program:
                self.log.add(
                    f"Затем база будет подключена к «{program.title}» "
                    f"({program.config_dir()}).",
                    "info",
                )
                if not program.supports_skills:
                    self.log.add(
                        f"{program.title} скиллы не читает — они останутся "
                        "только в папке базы.",
                        "warn",
                    )
            else:
                self.log.add("Программа не выбрана — подключение пропустится.", "warn")

    def _warn(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Не получилось")
        box.setText(text)
        box.exec()

    def _create(self) -> None:
        plan = self._make_plan()
        if not plan:
            return
        self._plan = plan

        attach = self.attach_check.isChecked()
        program = self._selected_program() if attach else None
        if attach and program is None:
            self._warn("Программа для подключения не выбрана.")
            return

        # второй шанс отказаться, если папка уже занята посторонним
        if plan.warnings:
            answer = QMessageBox.question(
                self,
                "Папка не пустая",
                "\n\n".join(plan.warnings) + "\n\nПродолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.log.clear_log()
        self.btn_create.setEnabled(False)
        self.btn_preview.setEnabled(False)
        self.btn_open.setEnabled(False)

        # запоминаем выбор про ярлык до запуска работы: во время работы
        # поля окна читать нельзя
        link_on = self.link_check.isChecked()
        link_place = self.link_place.currentData() or ""
        link_custom = self.link_custom_edit.text()
        link_name = plan.name

        def job(progress):
            core.create_base(plan, progress)
            attached = None
            if attach and program is not None:
                progress(f"Подключение к «{program.title}»…")
                attached = core.attach_base(plan.target, program, progress)
                # запоминаем, к какой программе подключили — пригодится
                # при повторном подключении этой же базы
                try:
                    core.remember_base(plan.target, program.ident)
                except Exception:
                    pass

            if link_on:
                progress("Создание ярлыка…")
                link = core.create_base_shortcut(
                    plan.target, link_place, link_custom, link_name
                )
                if link.ok:
                    progress(f"Ярлык создан: {link.link}")
                else:
                    progress(f"Ярлык не создан: {link.error}")
                if attached is None:
                    return core.AttachResult(
                        ok=link.ok,
                        messages=[],
                        errors=[] if link.ok else [link.error],
                    )
                if not link.ok:
                    attached.errors.append(link.error)
                    attached.ok = False
                return attached
            return attached

        self._worker = Worker(job, self)
        self._worker.line.connect(self._on_line)
        self._worker.finished.connect(self._on_done)
        self._worker.start()

    def _on_line(self, text: str, tag: str) -> None:
        self.log.add(text, tag)

    def _on_done(self) -> None:
        self.btn_preview.setEnabled(True)
        self.btn_create.setEnabled(True)
        result = self._worker.result if self._worker else None
        plan = self._plan

        if isinstance(result, Exception):
            self.log.add(f"Прервано: {result}", "error")
            self.btn_open.setEnabled(False)
            self._warn(f"Создание прервано:\n{result}")
            return

        if plan is None:
            return
        self._created = plan.target
        self.btn_open.setEnabled(True)
        self.base_ready.emit(str(plan.target))

        if isinstance(result, core.AttachResult):
            if result.errors:
                self.log.add("Готово, но с замечаниями:", "warn")
                for error in result.errors:
                    self.log.add(f"  {error}", "error")
                self._warn(
                    "База создана, но подключить удалось не всё:\n\n"
                    + "\n".join(result.errors)
                )
            else:
                self.log.add("Готово: база создана и подключена.", "ok")
                self._inform_done(plan.target, result)
        else:
            self.log.add("Готово: база создана.", "ok")
            self._inform_done(plan.target, None)

    def _inform_done(self, target: Path, result: core.AttachResult | None) -> None:
        text = f"База создана:\n{target}"
        if self.link_check.isChecked():
            where = self._link_folder()
            if where is not None:
                label = core.safe_link_name(target.name)
                link = where / f"{label}.lnk"
                if link.is_file():
                    text += (
                        f"\n\nЯрлык на рабочем месте:\n{link}"
                        "\nДвойной щелчок по нему откроет папку базы."
                    )
                else:
                    text += "\n\nЯрлык создать не удалось."
        if result and result.skills_dir:
            text += f"\n\nСкиллы подключены в:\n{result.skills_dir}"
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("База создана")
        box.setText(text)
        box.exec()

    def _open_result(self) -> None:
        if self._created and self._created.is_dir():
            core.open_in_explorer(self._created)


# ---------------------------------------------------------------- вкладка 2


class ImportTab(ScrollPage):
    """Подключение уже существующей базы к программе."""

    def __init__(self, parent=None) -> None:
        self._worker: Worker | None = None
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)
        outer.addWidget(
            ui.label(
                "Ваша база остаётся на месте. Её содержимое копируется "
                "в папку настроек программы; прежние файлы памяти "
                "сохраняются в _previous-version.",
                kind="dim",
                wrap=True,
            )
        )

        box_source = QGroupBox("1. Какую базу подключить")
        source_layout = QVBoxLayout(box_source)

        # --- список баз, созданных этой программой
        self.mine_list = QListWidget()
        self.mine_list.setMinimumHeight(120)
        self.mine_list.setMaximumHeight(160)
        source_layout.addWidget(
            ui.label("Созданные вами базы — выберите из списка:", kind="dim")
        )
        source_layout.addWidget(self.mine_list)

        row_mine = QHBoxLayout()
        self.btn_scan = QPushButton("Сканировать")
        self.btn_scan.clicked.connect(self._scan_bases)
        self.btn_refresh_mine = QPushButton("Обновить список")
        self.btn_refresh_mine.clicked.connect(self._fill_mine)
        self.btn_forget_mine = QPushButton("Убрать из список")
        self.btn_forget_mine.setEnabled(False)
        self.btn_forget_mine.clicked.connect(self._forget_mine)
        self.btn_delete_mine = QPushButton("Удалить базу")
        self.btn_delete_mine.setEnabled(False)
        self.btn_delete_mine.setObjectName("danger")
        self.btn_delete_mine.clicked.connect(self._delete_mine)
        row_mine.addWidget(self.btn_scan)
        row_mine.addWidget(self.btn_refresh_mine)
        row_mine.addWidget(self.btn_forget_mine)
        row_mine.addWidget(self.btn_delete_mine)
        row_mine.addStretch(1)
        source_layout.addLayout(row_mine)

        self.mine_hint = ui.label("", kind="dim", wrap=True)
        source_layout.addWidget(self.mine_hint)

        source_layout.addWidget(
            ui.label("…или укажите папку вручную:", kind="dim")
        )
        self.source_edit = QLineEdit()
        self.source_edit.setPlaceholderText("Папка с файлами базы")
        btn_browse = QPushButton("Обзор…")
        btn_browse.clicked.connect(self._pick_source)
        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Папка базы:"))
        row1.addWidget(self.source_edit, 1)
        row1.addWidget(btn_browse)
        source_layout.addLayout(row1)
        self.source_hint = ui.label("", kind="dim", wrap=True)
        source_layout.addWidget(self.source_hint)
        outer.addWidget(box_source)
        # подключаем слежение за полем только после того, как созданы
        # и поле, и подсказка, и кнопка — иначе обработчик сработает
        # раньше времени и уронит программу
        self.source_edit.textChanged.connect(self._check_source)

        box_target = QGroupBox("2. Подключение к OpenCode")
        target_layout = QVBoxLayout(box_target)
        self.target_list = QListWidget()
        self.target_list.setMinimumHeight(170)
        target_layout.addWidget(self.target_list)
        self.target_hint = ui.label("", kind="dim", wrap=True)
        target_layout.addWidget(self.target_hint)
        outer.addWidget(box_target)

        # --- шаг 3: какие навыки (скиллы) взять в работу
        self.box_skills = QGroupBox("3. Какие навыки подключить")
        skills_layout = QVBoxLayout(self.box_skills)
        skills_layout.addWidget(
            ui.label(
                "Навык — это умение, которое ассистент подхватывает "
                "автоматически. Отметьте нужные. Неотмеченные уберутся "
                "из программы, но сохранятся в _previous-version — "
                "выбор всегда можно переиграть.",
                kind="dim",
                wrap=True,
            )
        )
        self.skills_list = QListWidget()
        # Переносы, как и у списка навыков для opencode, вставляет
        # core.skill_item_text, а не Qt: со сворачиванием длинного описания
        # высота строки заранее неизвестна.
        self.skills_list.setWordWrap(False)
        self.skills_list.setUniformItemSizes(False)
        self.skills_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Запас на случай пустого списка: fit_skills_height выставит свою
        # высоту по фактическому числу навыков.
        self.skills_list.setMinimumHeight(300)
        self.skills_list.setMaximumHeight(600)
        skills_layout.addWidget(self.skills_list)

        row_skills = QHBoxLayout()
        self.btn_skills_all = QPushButton("Отметить все")
        self.btn_skills_all.clicked.connect(lambda: self._set_all_skills(True))
        self.btn_skills_none = QPushButton("Снять все")
        self.btn_skills_none.clicked.connect(lambda: self._set_all_skills(False))
        row_skills.addWidget(self.btn_skills_all)
        row_skills.addWidget(self.btn_skills_none)
        row_skills.addStretch(1)
        skills_layout.addLayout(row_skills)

        self.skills_hint = ui.label("", kind="dim", wrap=True)
        skills_layout.addWidget(self.skills_hint)
        outer.addWidget(self.box_skills)

        buttons = QHBoxLayout()
        self.btn_run = QPushButton("Подключить")
        self.btn_run.setObjectName("primary")
        self.btn_run.setEnabled(False)
        self.btn_run.clicked.connect(self._run)
        self.btn_disconnect = QPushButton("Отключить от opencode")
        self.btn_disconnect.setEnabled(False)
        self.btn_disconnect.clicked.connect(self._disconnect)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_disconnect)
        buttons.addWidget(self.btn_run)
        outer.addLayout(buttons)

        self.log = ui.LogView()
        self.log.setMinimumHeight(170)
        outer.addWidget(self.log)

        # Слежение за списком подключаем здесь, когда созданы и список,
        # и подсказка, и кнопка. Если подключить раньше, обработчик
        # обратится к ещё не созданной кнопке и уронит программу молча.
        self.target_list.itemSelectionChanged.connect(self._target_changed)
        self._fill_targets()

        # список созданных баз — тоже только здесь: обработчик трогает
        # и кнопку «убрать», и поле пути
        self.mine_list.itemSelectionChanged.connect(self._mine_chosen)
        self.btn_refresh_mine.clicked.connect(self._fill_mine)
        self.btn_forget_mine.clicked.connect(self._forget_mine)
        self._fill_mine()

        # список навыков: отметки меняют подсказку и доступность кнопки
        self.skills_list.itemChanged.connect(self._skills_changed)
        self._fill_skills()

        # фиксируем настоящую высоту страницы, чтобы Qt её не сжимал
        outer.activate()
        self.refresh_height()

    def _fill_targets(self) -> None:
        from PyQt6.QtGui import QColor

        for program in core.PROGRAMS:
            installed = program.is_installed()
            mark = "установлена" if installed else "не найдена"
            if not program.can_attach():
                mark += ", базу не читает"
            text = (
                f"{program.title} — {program.hint}  "
                f"[{mark}]"
            )
            item = QListWidgetItem(text)
            item.setData(1000, program.ident)
            if not installed:
                item.setForeground(QColor(ui.TEXT_DIM))
            self.target_list.addItem(item)
        for index in range(self.target_list.count()):
            ident = self.target_list.item(index).data(1000)
            if core.PROGRAMS_BY_ID[ident].is_installed():
                self.target_list.setCurrentRow(index)
                break
        self._target_changed()

    def _scan_bases(self) -> None:
        """Сканирует рабочий стол и подпапки в поисках баз по файлу-идентификатору."""
        from PyQt6.QtGui import QColor

        self.mine_list.clear()
        found = core.scan_bases()
        for base in found:
            name = base.name
            marker = base / ".opencode-base.json"
            if marker.is_file():
                try:
                    import json
                    data = json.loads(marker.read_text(encoding="utf-8"))
                    name = str(data.get("name") or base.name)
                except (OSError, ValueError):
                    pass
            text = f"{name}  —  {base}"
            row = QListWidgetItem(text)
            row.setData(1000, str(base))
            row.setToolTip(str(base))
            self.mine_list.addItem(row)

        if not found:
            self.mine_hint.setText(
                "Базы не найдены. Создайте базу на первой вкладке — она появится "
                "здесь сама."
            )
        else:
            self.mine_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            self.mine_hint.setText(f"Найдено баз: {len(found)}")

    def _fill_mine(self) -> None:
        """Показывает базы, созданные этой программой."""
        from PyQt6.QtGui import QColor

        keep = self.source_edit.text().strip()
        self.mine_list.clear()
        main_base = core.current_base("opencode")
        main_key = str(main_base).lower() if main_base is not None else None
        entries = core.read_bases()
        for item in entries:
            folder = Path(str(item.get("path", "")))
            name = str(item.get("name") or folder.name)
            when = str(item.get("when", ""))[:10]
            text = f"{name}  —  {folder}"
            if when:
                text += f"   (создана {when})"
            if main_key and str(folder).lower() == main_key:
                text += "   ← основная"
            row = QListWidgetItem(text)
            row.setData(1000, str(folder))
            row.setToolTip(str(folder))
            self.mine_list.addItem(row)

        if not entries:
            self.mine_hint.setText(
                "Пока пусто. Создайте базу на первой вкладке — она появится "
                "здесь сама, и путь подставится без поисков."
            )
        else:
            self.mine_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            main_txt = f" Основная сейчас: {main_base.name}." if main_base else ""
            self.mine_hint.setText(
                f"Найдено баз: {len(entries)}.{main_txt} Выберите одну — путь "
                "подставится сам. Отмеченная «← основная» — действующая база."
            )

        # возвращаем прежний выбор, если он был; иначе — основную базу,
        # чтобы подключение не уводило программу в сторону
        restored = False
        for index in range(self.mine_list.count()):
            if self.mine_list.item(index).data(1000) == keep:
                self.mine_list.setCurrentRow(index)
                restored = True
                break
        if not restored and main_key:
            for index in range(self.mine_list.count()):
                if str(self.mine_list.item(index).data(1000)).lower() == main_key:
                    self.mine_list.setCurrentRow(index)
                    break
        has_item = self.mine_list.currentItem() is not None
        self.btn_forget_mine.setEnabled(has_item)
        self.btn_delete_mine.setEnabled(has_item)

    def _mine_chosen(self) -> None:
        """Подставляет путь выбранной базы в поле."""
        item = self.mine_list.currentItem()
        has_item = item is not None
        self.btn_forget_mine.setEnabled(has_item)
        self.btn_delete_mine.setEnabled(has_item)
        if item is None:
            return
        chosen = str(item.data(1000) or "")
        if chosen and chosen != self.source_edit.text().strip():
            self.source_edit.setText(chosen)
        # переносим фокус на список программ — следующий шаг по порядку
        if self.target_list.count():
            self.target_list.setFocus()

    def _forget_mine(self) -> None:
        """Убирает базу из списка. Сама папка не трогается."""
        item = self.mine_list.currentItem()
        if item is None:
            return
        folder = Path(str(item.data(1000) or ""))
        if not folder.name:
            return
        answer = QMessageBox.question(
            self,
            "Убрать из списка",
            f"Убрать «{folder.name}» из списка?\n\n"
            f"Сама папка останется на месте:\n{folder}\n"
            "Ничего не удаляется — база просто исчезнет из подсказок.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        core.forget_base(folder)
        self._fill_mine()

    def _delete_mine(self) -> None:
        """Полностью удаляет базу с диска и из списка."""
        item = self.mine_list.currentItem()
        if item is None:
            return
        folder = Path(str(item.data(1000) or ""))
        if not folder.name:
            return
        answer = QMessageBox.question(
            self,
            "Удалить базу",
            f"Удалить базу «{folder.name}»?\n\n"
            f"С диска удалится вся папка:\n{folder}\n\n"
            "Вместе с ней пропадут её файлы: профиль, факты, проекты, "
            "библиотека и наработки.\n\n"
            "Продолжить?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        ok, message = core.delete_base(folder, confirm=True)
        if ok:
            self.log.add(message, "ok")
        else:
            self.log.add(message, "error")
        self._fill_mine()

    def _disconnect(self) -> None:
        """Полностью отключает выбранную базу от opencode."""
        folder_text = self.source_edit.text().strip()
        if not folder_text:
            self._warn("Сначала выберите базу в поле пути.")
            return
        folder = Path(folder_text)
        if not folder.is_dir():
            self._warn(f"Такой папки нет:\n{folder}")
            return

        # Проверяем, что это действительно база
        info = core.base_info(folder)
        if not info["is_base"]:
            self._warn("Выбранная папка не является базой.")
            return

        # Проверяем, что она сейчас основная
        main_base = core.current_base("opencode")
        if main_base is None or str(main_base).lower() != str(folder).lower():
            answer = QMessageBox.question(
                self,
                "База не основная",
                f"База «{folder.name}» сейчас не подключена к opencode.\n\n"
                "Отключать нечего. Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
        else:
            answer = QMessageBox.question(
                self,
                "Отключить базу от opencode",
                f"Это полностью отключит базу «{folder.name}» от opencode:\n\n"
                "• Удалятся instructions, мосты ncp/pc и права из opencode.jsonc\n"
                "• Удалится memory-base-path.txt\n"
                "• Удалятся скиллы этой базы из ~/.config/opencode/skills/\n"
                "• Конфиг NCP-моста сбросится (если ведёт на эту базу)\n\n"
                "Сама папка базы на диске НЕ ТРОНУТА.\n\n"
                "Продолжить?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.log.add(f"Отключение базы: {folder}")
        self.btn_disconnect.setEnabled(False)
        self.btn_run.setEnabled(False)

        def job(progress):
            progress(f"Отключаем {folder.name} от opencode…")
            return core.disconnect_base(folder, "opencode")

        self._worker = Worker(job, self)
        self._worker.line.connect(self.log.add)
        self._worker.finished.connect(self._on_disconnected)
        self._worker.start()

    def _on_disconnected(self) -> None:
        self.btn_disconnect.setEnabled(True)
        self.btn_run.setEnabled(True)
        result = self._worker.result if self._worker else None
        if isinstance(result, Exception):
            self.log.add(f"Прервано: {result}", "error")
            return
        if not isinstance(result, tuple) or len(result) != 2:
            self.log.add("Отключить не удалось.", "error")
            return
        messages, errors = result
        for m in messages:
            self.log.add(m, "ok")
        for e in errors:
            self.log.add(e, "error")
        if not errors:
            self.log.add("База отключена. Перезапустите opencode.", "ok")
        else:
            self.log.add("Есть ошибки — проверьте вручную.", "warn")
        # Обновляем состояние подсказки
        self._check_source()
        self._refresh_run()

    def _pick_source(self) -> None:
        start = self.source_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "Выберите базу", start)
        if chosen:
            self.source_edit.setText(chosen)
            self._check_source()

    def _check_source(self) -> None:
        raw = self.source_edit.text().strip()
        if not raw:
            self.source_hint.setText("")
            self.btn_run.setEnabled(False)
            return
        folder = Path(raw)
        if not folder.is_dir():
            self.source_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.source_hint.setText("Такой папки нет.")
            self.btn_run.setEnabled(False)
            return
        info = core.base_info(folder)
        if not info["is_base"]:
            self.source_hint.setStyleSheet(f"color: {ui.ERROR};")
            подсказка = self._подсказка_где_база(folder)
            self.source_hint.setText(подсказка)
            self.btn_run.setEnabled(False)
            return
        files = ", ".join(str(f) for f in info["files"])
        marker = info["marker"] or {}
        created = f" Создана: {marker['created'][:10]}." if marker.get("created") else ""
        main_base = core.current_base("opencode")
        main_note = ""
        if main_base is not None and str(main_base).lower() == str(folder).lower():
            main_note = "\nЭта база сейчас основная."
        self.source_hint.setStyleSheet(f"color: {ui.OK};")
        self.source_hint.setText(
            f"База найдена. Скиллов: {info['skills']}. "
            f"Размер: {core.human_size(int(info['size']))}.{created}{main_note}\n"
            f"Файлы: {files}"
        )
        # сменилась база — перечитываем её навыки в шаге 3
        if hasattr(self, "skills_list"):
            self._fill_skills()
        self._refresh_run()

    def _подсказка_где_база(self, folder: Path) -> str:
        return подсказка_где_база(folder)

    # ---- шаг 3: навыки

    def _fill_skills(self) -> None:
        """Наполняет список навыками из выбранной базы.

        Все отмечены по умолчанию: обычный случай — перенести весь набор.
        Снимать нужно только то, что не нужно.

        Выбор помнится, но только внутри одной базы: при смене базы
        набор навыков другой, и переносить прежние галочки было бы
        неверно. Поэтому запоминаем не «галочки», а путь базы, для
        которой они поставлены.
        """
        from PyQt6.QtGui import QColor

        raw = self.source_edit.text().strip()
        folder = Path(raw) if raw else None

        # Для новой базы — всё отмечено заново; для той же самой
        # сохраняем то, что пользователь уже расставил.
        same_base = (
            getattr(self, "_skills_base", None) is not None
            and folder is not None
            and str(folder) == self._skills_base
        )
        chosen_before = set(self._chosen_skills()) if same_base else None

        self.skills_list.blockSignals(True)
        self.skills_list.clear()
        self._skills_data = []
        self._skills_base = str(folder) if folder else None

        entries = core.list_skills(folder) if folder and folder.is_dir() else []

        for skill in entries:
            title = skill["title"]
            desc = skill["description"]
            text = core.skill_item_text(skill)
            row = QListWidgetItem(text)
            row.setData(1000, skill["name"])
            row.setToolTip(desc or title)
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            # тот же набор, что был, — возвращаем прежние отметки;
            # новая база — отмечаем всё
            keep = True if chosen_before is None else (skill["name"] in chosen_before)
            row.setCheckState(
                Qt.CheckState.Checked if keep else Qt.CheckState.Unchecked
            )
            if not desc:
                row.setForeground(QColor(ui.TEXT_DIM))
            self.skills_list.addItem(row)
            self._skills_data.append(skill)

        self.skills_list.blockSignals(False)
        self._skills_changed()
        fit_skills_height(self.skills_list)

    def _chosen_skills(self) -> list[str]:
        names: list[str] = []
        if not hasattr(self, "skills_list"):
            return names
        for index in range(self.skills_list.count()):
            row = self.skills_list.item(index)
            if row.checkState() == Qt.CheckState.Checked:
                names.append(str(row.data(1000)))
        return names

    def _set_all_skills(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.skills_list.blockSignals(True)
        for index in range(self.skills_list.count()):
            self.skills_list.item(index).setCheckState(state)
        self.skills_list.blockSignals(False)
        self._skills_changed()

    def _skills_changed(self) -> None:
        """Обновляет подсказку под списком навыков и доступность кнопки."""
        total = self.skills_list.count()
        chosen = len(self._chosen_skills())

        if total == 0:
            raw = self.source_edit.text().strip()
            folder = Path(raw) if raw else None
            if not raw:
                self.skills_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
                self.skills_hint.setText(
                    "Сначала выберите базу в шаге 1 — здесь появятся "
                    "её навыки."
                )
            elif folder and (folder / "skills").is_dir():
                self.skills_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
                self.skills_hint.setText(
                    "В этой базе навыков нет — наполните папку skills, "
                    "и они появятся здесь."
                )
            else:
                self.skills_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
                self.skills_hint.setText("Навыков не найдено.")
        elif chosen == total:
            self.skills_hint.setStyleSheet(f"color: {ui.OK};")
            self.skills_hint.setText(f"Отмечены все {total} — перенесутся все.")
        elif chosen == 0:
            self.skills_hint.setStyleSheet(f"color: {ui.WARN};")
            self.skills_hint.setText(
                f"Не отмечено ни одного из {total}. Ничего не перенесётся, "
                "а прежние навыки уйдут в _previous-version."
            )
        else:
            self.skills_hint.setStyleSheet(f"color: {ui.OK};")
            self.skills_hint.setText(
                f"Отмечено {chosen} из {total}. Остальные уберутся "
                "из программы."
            )
        self._refresh_run()

    def _target_changed(self) -> None:
        item = self.target_list.currentItem()
        if item is None:
            self.target_hint.setText("")
            self._refresh_run()
            return
        program = core.PROGRAMS_BY_ID.get(item.data(1000))
        if program is None:
            self._refresh_run()
            return
        if not program.can_attach():
            self.target_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.target_hint.setText(
                f"{program.title}: {program.ability_note()} "
                "Подключить базу к этой программе нельзя — файлы туда "
                "не переносятся. Выберите другую программу."
            )
            self._refresh_run()
            return
        text = f"Куда: {program.config_dir()}. {program.ability_note()}"
        if program.supports_skills:
            self.target_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
        else:
            self.target_hint.setStyleSheet(f"color: {ui.WARN};")
            text += " Навыки не переносятся: программа их не читает."
        self.target_hint.setText(text)
        self._refresh_run()

    def _refresh_run(self) -> None:
        """Кнопка «Подключить» доступна, когда выбран путь и программа.

        Навыки намеренно не блокируют кнопку: «ни одного навыка» —
        это допустимый выбор, программа предупредит об этом отдельно.
        А программа, которая файлы с диска не читает, кнопку выключает:
        подключать к ней нечего, база всё равно останется незамеченной.

        Кнопка «Отключить от opencode» доступна, когда выбранная база
        сейчас является основной для opencode.
        """
        item = self.target_list.currentItem()
        program = core.PROGRAMS_BY_ID.get(item.data(1000)) if item else None
        ready = (
            program is not None
            and program.can_attach()
            and bool(self.source_edit.text().strip())
        )
        self.btn_run.setEnabled(ready)

        # Кнопка отключения — только если выбранная база сейчас основная для opencode
        folder_text = self.source_edit.text().strip()
        disconnect_ready = False
        if folder_text and program and program.ident == "opencode":
            main_base = core.current_base("opencode")
            if main_base is not None and str(main_base).lower() == str(Path(folder_text)).lower():
                disconnect_ready = True
        self.btn_disconnect.setEnabled(disconnect_ready)

    def _run(self) -> None:
        item = self.target_list.currentItem()
        if item is None:
            return
        program = core.PROGRAMS_BY_ID.get(item.data(1000))
        base = Path(self.source_edit.text().strip())
        if program is None or not base.is_dir():
            return

        chosen = self._chosen_skills()

        # Если навыков нет вовсе, а в базе они есть — предупреждаем,
        # что подключение уберёт прежние. Спрашиваем один раз.
        if program.supports_skills and self.skills_list.count() and not chosen:
            answer = QMessageBox.question(
                self,
                "Ни одного навыка",
                "Не отмечен ни один навык.\n\n"
                "Ничего не перенесётся, а прежние навыки уйдут "
                "в _previous-version (восстановить можно).\n\n"
                "Продолжить подключение?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                return

        self.log.clear_log()
        self.btn_run.setEnabled(False)

        def job(progress):
            return core.attach_base(base, program, progress, skills=chosen)

        self._worker = Worker(job, self)
        self._worker.line.connect(self.log.add)
        self._worker.finished.connect(self._done)
        self._worker.start()

    def _done(self) -> None:
        self.btn_run.setEnabled(True)
        result = self._worker.result if self._worker else None
        if isinstance(result, Exception):
            self.log.add(f"Прервано: {result}", "error")
            return
        if not isinstance(result, core.AttachResult):
            return
        if result.errors:
            self.log.add("Готово, но с замечаниями:", "warn")
            for error in result.errors:
                self.log.add(f"  {error}", "error")
        else:
            self.log.add("Готово: база подключена.", "ok")


# ---------------------------------------------------------------- вкладка 3


class BridgeTab(ScrollPage):
    """Мост NCP — маленькая программа, через которую нейросеть получает
    доступ к библиотеке: спросить состояние, найти запись, сохранить.

    Место, куда его положить, выбирает человек. Ни одного вписанного
    пути здесь нет и быть не должно: иначе мост не заработает на другом
    компьютере, а ради этого он и делается.
    """

    def __init__(self, parent=None) -> None:
        self._worker: Worker | None = None
        self._what = ""
        self._result: core.BridgeResult | None = None
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    # ---- сборка окна

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(
            ui.label(
                "Здесь создаётся ОТДЕЛЬНАЯ копия моста в выбранной папке. "
                "Для opencode это обычно не нужно: мост уже лежит в базе, "
                "а подключается он галочкой «Подключить мост памяти» "
                "на вкладке «opencode».",
                kind="dim",
                wrap=True,
            )
        )

        # --- шаг 1: куда положить
        box_where = QGroupBox("1. Куда положить мост")
        where_layout = QVBoxLayout(box_where)

        self.folder_edit = QLineEdit()
        self.folder_edit.setPlaceholderText("Папка, в которой появится мост")
        self.folder_edit.setText(str(core.bridge_default_dir()))
        btn_pick_folder = QPushButton("Обзор…")
        btn_pick_folder.clicked.connect(self._pick_folder)

        row1 = QHBoxLayout()
        row1.addWidget(QLabel("Папка моста:   "))
        row1.addWidget(self.folder_edit, 1)
        row1.addWidget(btn_pick_folder)
        where_layout.addLayout(row1)

        where_layout.addWidget(
            ui.label(
                "Место любое: файлы моста не привязаны к пути. Если папки "
                "нет — она будет создана. В чужую непустую папку программа "
                "писать не станет.",
                kind="dim",
                wrap=True,
            )
        )

        self.where_hint = ui.label("", kind="dim", wrap=True)
        where_layout.addWidget(self.where_hint)
        outer.addWidget(box_where)

        # --- шаг 2: к какой библиотеке
        box_lib = QGroupBox("2. К какой библиотеке подключить")
        lib_layout = QVBoxLayout(box_lib)

        self.library_edit = QLineEdit()
        self.library_edit.setPlaceholderText("Папка базы или папка библиотеки")
        self.library_edit.setText(str(core.library_dir(core.app_root())))
        btn_pick_lib = QPushButton("Обзор…")
        btn_pick_lib.clicked.connect(self._pick_library)

        row2 = QHBoxLayout()
        row2.addWidget(QLabel("Библиотека:   "))
        row2.addWidget(self.library_edit, 1)
        row2.addWidget(btn_pick_lib)
        lib_layout.addLayout(row2)

        self.library_hint = ui.label("", kind="dim", wrap=True)
        lib_layout.addWidget(self.library_hint)
        outer.addWidget(box_lib)

        # --- шаг 3: куда подключить
        box_prog = QGroupBox("3. Куда подключить")
        prog_layout = QVBoxLayout(box_prog)

        prog_layout.addWidget(
            ui.label(
                "Мост один и тот же. Для opencode — готовые кнопки "
                "на вкладке «opencode». Для Харнеса — YAML-оверлей ниже: "
                "сохрани его и примени как dsh --patch <файл>.",
                kind="dim",
                wrap=True,
            )
        )

        self.radio_open = QRadioButton("Для opencode (JSON)")
        self.radio_harness = QRadioButton("Для Харнеса (YAML-оверлей)")
        self.radio_open.setChecked(True)
        row_prog = QHBoxLayout()
        row_prog.addWidget(QLabel("Показать настройки:"))
        row_prog.addWidget(self.radio_open)
        row_prog.addWidget(self.radio_harness)
        row_prog.addStretch(1)
        prog_layout.addLayout(row_prog)

        self.program_hint = ui.label("", kind="dim", wrap=True)
        prog_layout.addWidget(self.program_hint)

        prog_layout.addWidget(QLabel("Настройки моста — их можно скопировать:"))
        self.snippet = ui.LogView()
        self.snippet.setMinimumHeight(110)
        prog_layout.addWidget(self.snippet)

        row_save = QHBoxLayout()
        self.btn_save_overlay = QPushButton("Сохранить YAML для Харнеса")
        self.btn_save_overlay.clicked.connect(self._save_overlay)
        row_save.addStretch(1)
        row_save.addWidget(self.btn_save_overlay)
        prog_layout.addLayout(row_save)
        outer.addWidget(box_prog)

        # --- кнопки
        buttons = QHBoxLayout()
        self.btn_create = QPushButton("Создать мост")
        self.btn_create.setObjectName("primary")
        self.btn_create.clicked.connect(self._create)
        self.btn_check = QPushButton("Проверить")
        self.btn_check.clicked.connect(self._check)
        self.btn_open = QPushButton("Открыть папку")
        self.btn_open.clicked.connect(self._open)
        buttons.addWidget(self.btn_create)
        buttons.addWidget(self.btn_check)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_open)
        outer.addLayout(buttons)

        self.log = ui.LogView()
        self.log.setMinimumHeight(170)
        outer.addWidget(self.log)

        # Связи — только здесь, когда все элементы уже созданы. Если
        # подключить раньше, обработчик сработает на несуществующем поле
        # и уронит программу без всякого сообщения.
        self.folder_edit.textChanged.connect(self._refresh)
        self.library_edit.textChanged.connect(self._refresh)
        self.radio_open.toggled.connect(self._bridge_target_changed)
        self._refresh()

        outer.activate()
        self.refresh_height()

    # ---- подсказки и состояние

    def _refresh(self) -> None:
        """Показывает, что уже есть на месте, а чего не хватает."""
        folder_text = self.folder_edit.text().strip()
        if folder_text:
            folder = Path(folder_text)
            status = core.bridge_status(folder)
            if status.exists:
                text = "В этой папке мост уже есть — файлы будут обновлены."
                if status.library:
                    text += f"\nОн подключён к библиотеке:\n{status.library}"
                if status.problem:
                    text += f"\nЗамечание: {status.problem}"
                self.where_hint.setStyleSheet(f"color: {ui.OK};")
            elif status.problem:
                self.where_hint.setStyleSheet(f"color: {ui.WARN};")
                text = status.problem
            else:
                self.where_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
                text = "Папки пока нет — она будет создана при первом создании моста."
            self.where_hint.setText(text)
        else:
            self.where_hint.setText("")

        lib_text = self.library_edit.text().strip()
        if lib_text:
            library = core.resolve_library(Path(lib_text))
            if core.library_ready(library.parent):
                self.library_hint.setStyleSheet(f"color: {ui.OK};")
                self.library_hint.setText(
                    f"Библиотека найдена:\n{library}\n"
                    "Записи, индекс и журнал — здесь."
                )
            else:
                self.library_hint.setStyleSheet(f"color: {ui.ERROR};")
                self.library_hint.setText(
                    "Библиотеки по этому пути нет.\n"
                    "Проверьте путь или выберите базу заново."
                )
        else:
            self.library_hint.setText("")

        python = core.find_python()
        if python is None:
            self.program_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.program_hint.setText(
                "Python не найден. Мост создастся, но запускать его будет "
                "нечем. Поставьте Python 3.8 или новее с сайта python.org — "
                "при установке отметьте галочку «Add Python to PATH»."
            )
        else:
            self.program_hint.setStyleSheet(f"color: {ui.OK};")
            self.program_hint.setText(
                f"Python для запуска моста:\n{python}\n"
                "Подключение к opencode — готовыми кнопками на вкладке "
                "«opencode»."
            )

    def _bridge_target_changed(self) -> None:
        """Переключает текст настроек между opencode и Харнесом."""
        result = getattr(self, "_result", None)
        if result is not None and result.python and result.server_py:
            self._show_snippet(result.python, result.server_py)

    def _show_snippet(self, python, server_py) -> None:
        if self.radio_harness.isChecked():
            self.snippet.setPlainText(
                core.harness_overlay_snippet(python, server_py)
            )
        else:
            self.snippet.setPlainText(
                core.mcp_snippet(python, server_py)
            )

    def _save_overlay(self) -> None:
        """Сохраняет YAML-оверлей для Харнеса рядом с мостом."""
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            self._warn("Сначала выбери папку моста.")
            return
        folder = Path(folder_text)
        server_py = folder / core.BRIDGE_SERVER
        if not server_py.is_file():
            self._warn("В этой папке моста пока нет — сначала нажми «Создать мост».")
            return
        python = core.find_python()
        if python is None:
            self._warn("Python не найден — нечем заполнить команду запуска.")
            return
        target = folder / "ncp-cordis-overlay.yml"
        try:
            target.write_text(
                core.harness_overlay_snippet(python, server_py), encoding="utf-8"
            )
        except OSError as exc:
            self._warn(f"Не записался файл:\n{exc}")
            return
        self.snippet.setPlainText(
            core.harness_overlay_snippet(python, server_py)
        )
        self.log.add(f"Оверлей сохранён: {target}", "ok")
        self.log.add("Примени как: dsh --patch " + str(target), "info")

    # ---- выбор папок

    def _pick_folder(self) -> None:
        start = self.folder_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Куда положить мост", start
        )
        if chosen:
            self.folder_edit.setText(chosen)

    def _pick_library(self) -> None:
        start = self.library_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Выберите базу или папку библиотеки", start
        )
        if chosen:
            self.library_edit.setText(chosen)

    # ---- подготовка извне (после создания базы)

    def prepare(self, library: Path | None = None) -> None:
        """Подставляет путь к только что созданной базе."""
        if library is not None:
            self.library_edit.setText(str(library))
        found = core.find_bridges()
        if found:
            self.folder_edit.setText(str(found[0]))
        self._refresh()

    # ---- запуск работы

    def _set_busy(self, busy: bool) -> None:
        for button in (
            self.btn_create,
            self.btn_check,
            self.btn_open,
        ):
            button.setEnabled(not busy)

    def _start(self, job, what: str) -> None:
        self._what = what
        self.log.clear_log()
        self._set_busy(True)
        self._worker = Worker(job, self)
        self._worker.line.connect(self.log.add)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _finished(self) -> None:
        self._set_busy(False)
        result = self._worker.result if self._worker else None
        if isinstance(result, Exception):
            self.log.add(f"Прервано: {result}", "error")
            return
        if self._what == "create":
            self._on_created(result)
        elif self._what == "check":
            self._on_checked(result)
        self._refresh()

    # ---- создание

    def _create(self) -> None:
        folder_text = self.folder_edit.text().strip()
        library_text = self.library_edit.text().strip()
        if not folder_text:
            self._warn("Не выбрана папка, куда положить мост.")
            return
        if not library_text:
            self._warn("Не выбрана библиотека, к которой подключать мост.")
            return

        folder = Path(folder_text)
        library = core.resolve_library(Path(library_text))

        def job(progress):
            return core.create_bridge(folder, library, progress=progress)

        self._start(job, "create")

    def _on_created(self, result) -> None:
        if not isinstance(result, core.BridgeResult):
            self.log.add("Работа завершилась непонятным итогом.", "error")
            return
        self._result = result
        if result.python and result.server_py:
            self._show_snippet(result.python, result.server_py)
        if result.errors:
            self.log.add("Готово, но с замечаниями:", "warn")
            for error in result.errors:
                self.log.add(f"  {error}", "error")
            self._warn("Мост создан, но не всё удалось:\n\n" + "\n\n".join(result.errors))
            return
        self.log.add("Готово: мост создан и проверен.", "ok")
        if result.folder:
            self.log.add(f"Папка моста:\n{result.folder}", "info")
        self._inform(result)

    def _inform(self, result: core.BridgeResult) -> None:
        text = f"Мост создан и проверен:\n{result.folder}"
        if result.python:
            text += f"\n\nPython для запуска:\n{result.python}"
        if self.radio_harness.isChecked():
            text += (
                "\n\nДля Харнеса: сохрани YAML кнопкой ниже и примени как "
                "dsh --patch <файл>."
            )
        else:
            text += (
                "\n\nПодключить мост к opencode — готовыми кнопками "
                "на вкладке «opencode»."
            )
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Мост создан")
        box.setText(text)
        box.exec()

    # ---- проверка

    def _check(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            self._warn("Сначала выберите папку моста.")
            return
        folder = Path(folder_text)

        def job(progress):
            progress("Проверяем мост на временной копии библиотеки…")
            return core.bridge_selftest(folder)

        self._start(job, "check")

    def _on_checked(self, result) -> None:
        if not isinstance(result, tuple):
            self.log.add("Проверить не удалось.", "error")
            return
        ok, output = result
        for line in output.splitlines():
            self.log.add(line, "ok" if ok else "error")
        self.log.add(
            "Проверка пройдена." if ok else "Проверка не прошла.",
            "ok" if ok else "error",
        )

    # ---- прочее

    def _open(self) -> None:
        folder_text = self.folder_edit.text().strip()
        if not folder_text:
            self._warn("Сначала выберите папку моста.")
            return
        folder = Path(folder_text)
        if not folder.is_dir():
            self._warn(f"Такой папки пока нет:\n{folder}")
            return
        core.open_in_explorer(folder)

    def _warn(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Не получилось")
        box.setText(text)
        box.exec()


class ManualConfigDialog(QDialog):
    """Окно вставки конфигурации, которую выдаёт чужая программа.

    Нужно для серверов вроде Android Studio: программа не может
    включить сервер внутри студии, это действие в её окне. Что она
    может - принять конфигурацию, которую человек скопировал, и
    сохранить её рядом с настройками opencode.

    Смысл окна в том, что человек видит текст, который попадёт в
    настройки. Ничего не подставляется молча.
    """

    def __init__(self, server: mcp_registry.Server, parent=None) -> None:
        super().__init__(parent)
        self.server = server
        self.saved_note = ""
        self.setWindowTitle(f"Включить {server.name}")
        self.setMinimumWidth(620)

        box = QVBoxLayout(self)
        box.setSpacing(8)

        known_url = str(
            (server.raw.get("connection") or {}).get("url") or ""
        ).strip()
        if known_url:
            intro = ui.label(
                f"Проще нажать «Настроить автоматически» в блоке серверов: "
                f"программа сама поставит плагин в {server.name}, включит "
                "сервер и проверит его. Это окно нужно, только если плагин "
                "уже стоит руками и осталось вписать адрес.",
                wrap=True,
            )
        else:
            intro = ui.label(
                f"{server.name} включается у себя, внутри своей программы. "
                "Отсюда включить его нельзя — это действие в её окне. "
                "Но конфигурацию принять можно: скопируй её там и вставь сюда.",
                wrap=True,
            )
        box.addWidget(intro)

        steps = server.setup_steps
        if steps:
            box.addWidget(ui.label("Как настроить:", kind="title"))
            for index, text in enumerate(steps, start=1):
                box.addWidget(ui.label(f"  {index}. {text}", wrap=True))

        box.addSpacing(4)
        # Адрес известен программе заранее, а у этого сервера токена нет
        # вовсе. Поэтому поле подставляем сами, а не просим человека
        # идти за конфигурацией, которой он не может достать: прежний
        # текст звал кнопку Copy Config, которой в студии 2026.1 нет.
        if known_url:
            box.addWidget(
                ui.label(
                    "Адрес сервера известен программе, он уже подставлен ниже. "
                    "Ничего копировать из студии не нужно. Проверь глазами и "
                    "нажми «Записать»:",
                    wrap=True,
                )
            )
        else:
            box.addWidget(
                ui.label(
                    "Вставь сюда конфигурацию целиком — вместе с адресом:",
                    wrap=True,
                )
            )

        self.text = QPlainTextEdit()
        if known_url:
            self.text.setPlainText(
                json.dumps(
                    {"mcpServers": {self.server.id: {"url": known_url}}},
                    ensure_ascii=False, indent=2,
                )
            )
        self.text.setPlaceholderText(
            '{"mcpServers": {"' + self.server.id + '": {"url": "http://localhost:.../api/mcp"}}}'
        )
        self.text.setMinimumHeight(110)
        # Моноширинный шрифт, как у окошка лога: конфигурация - это JSON,
        # и в пропорциональном шрифте её структура плохо читается.
        _font = QFont("Consolas")
        _font.setStyleHint(QFont.StyleHint.Monospace)
        self.text.setFont(_font)
        box.addWidget(self.text)

        # Подсказка — про то, что происходит с полем, поэтому стоит под
        # ним. Наверху она читалась как ещё один абзац инструкции.
        self.status = ui.label("", wrap=True)
        self.status.setObjectName("hint")
        box.addWidget(self.status)

        row = QHBoxLayout()
        self.btn_paste = QPushButton("Вставить из буфера обмена")
        self.btn_paste.clicked.connect(self._paste)
        self.btn_clear = QPushButton("Очистить")
        self.btn_clear.clicked.connect(self.text.clear)
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Записать")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        row.addWidget(self.btn_paste)
        row.addWidget(self.btn_clear)
        row.addStretch(1)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        box.addLayout(row)

        # Подсказка зависит от того, подставили мы адрес сами.
        if known_url:
            self.status.setText(
                "Адрес подставлен программой. Если сервер ещё не отвечает, "
                "нажми «Настроить автоматически» в блоке серверов — "
                "поставить плагин и включить его проще."
            )
            return
        # Сразу подсказываем про буфер, если там есть что вставить.
        clipboard = QApplication.clipboard()
        if clipboard is not None and self._looks_like_config(clipboard.text()):
            self.status.setText(
                "В буфере обмена похоже на конфигурацию этого сервера. "
                "Нажми «Вставить из буфера обмена»."
            )
        else:
            self.status.setText(
                "Поле пустое — так честнее, чем подставлять наугад. "
                "Нажми «Вставить из буфера обмена», если конфигурация "
                "уже скопирована."
            )

    def _looks_like_config(self, text: str) -> bool:
        """Похож ли текст на конфигурацию этого сервера.

        Грубая, но достаточная проверка: без неё кнопка предлагала бы
        вставить что угодно, а с ней - хотя бы не пустоту.
        """
        raw = (text or "").strip()
        if not raw or len(raw) > 20000:
            return False
        return "mcp" in raw.lower() and ("url" in raw.lower()
                                        or "http" in raw.lower())

    def _paste(self) -> None:
        clipboard = QApplication.clipboard()
        text = clipboard.text() if clipboard is not None else ""
        if not text.strip():
            self.status.setText("В буфере обмена пусто. Скопируй конфигурацию.")
            return
        self.text.setPlainText(text)
        self.status.setText(
            "Вставлено. Посмотри глазами, что это конфигурация, и нажми "
            "«Записать»."
        )

    def config_text(self) -> str:
        return self.text.toPlainText()


class ObsPasswordDialog(QDialog):
    """Окно пароля WebSocket-сервера OBS.

    Отдельное окно, а не строчка в таблице, потому что здесь есть
    решение, которое нельзя принять за человека: пароль либо уже есть,
    либо его нужно выпустить. Показываем и то, и другое, и третье —
    вписать свой.

    Куда сохранять копию — необязательно. Не выбрал путь, значит пароль
    всё равно лежит рядом с настройками, оттуда его и возьмёт лаунчер.
    Просто тогда человек не найдёт его глазами, поэтому предлагаем.
    """

    def __init__(self, current: str, parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Пароль WebSocket-сервера OBS")
        self.setMinimumWidth(560)
        self._path: Path | None = None

        box = QVBoxLayout(self)
        box.setSpacing(8)

        box.addWidget(ui.label(
            "Мост OBS подключается к OBS по паролю. Программа умеет "
            "выпустить его сама, но ты можешь вписать свой. Пароль не "
            "попадёт в opencode.jsonc: он лежит отдельным файлом, "
            "потому что конфиг открыт и переносится между компьютерами.",
            wrap=True,
        ))

        row = QHBoxLayout()
        self.field = QLineEdit()
        self.field.setEchoMode(QLineEdit.EchoMode.Password)
        self.field.setPlaceholderText(
            "Пароль, 8–20 символов, с буквами и цифрами"
        )
        if current:
            self.field.setText(current)
        row.addWidget(self.field, 1)
        self.btn_gen = QPushButton("Сгенерировать")
        self.btn_gen.clicked.connect(self._generate)
        self.btn_show = QPushButton("Показать")
        self.btn_show.setCheckable(True)
        self.btn_show.toggled.connect(self._toggle)
        row.addWidget(self.btn_gen)
        row.addWidget(self.btn_show)
        box.addLayout(row)

        self.status = ui.label("", wrap=True)
        self.status.setObjectName("hint")
        box.addWidget(self.status)

        row2 = QHBoxLayout()
        self.btn_save = QPushButton("Сохранить копию пароля…")
        self.btn_save.clicked.connect(self._choose_path)
        row2.addWidget(self.btn_save)
        self.path_label = ui.label("", kind="dim", wrap=True)
        row2.addWidget(self.path_label, 1)
        box.addLayout(row2)

        row3 = QHBoxLayout()
        row3.addStretch(1)
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Дальше")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self.accept)
        row3.addWidget(self.btn_cancel)
        row3.addWidget(self.btn_ok)
        box.addLayout(row3)

        self._refresh_status()

    def _generate(self) -> None:
        self.field.setText(bridges.generate_password())
        self.btn_show.setChecked(True)
        self._refresh_status()

    def _toggle(self, show: bool) -> None:
        mode = (QLineEdit.EchoMode.Normal if show
                else QLineEdit.EchoMode.Password)
        self.field.setEchoMode(mode)

    def _refresh_status(self) -> None:
        value = self.field.text()
        if not value:
            self.status.setText(
                "Пусто. Нажми «Сгенерировать» — это надёжнее, чем придумывать."
            )
        elif bridges.password_is_valid(value):
            self.status.setText("Пароль годный.")
        else:
            self.status.setText(
                f"Нужен от {bridges.OBS_PASSWORD_MIN} до "
                f"{bridges.OBS_PASSWORD_MAX} символов, со строчной буквой, "
                "заглавной и цифрой."
            )

    def _choose_path(self) -> None:
        path, _ = QFileDialog.getSaveFileName(
            self, "Куда сохранить пароль", "",
            "Текстовый файл (*.txt);;Все файлы (*)",
        )
        if not path:
            return
        self._path = Path(path)
        self.path_label.setText(f"Сохраню в: {self._path}")

    def password(self) -> str:
        return self.field.text().strip()

    def save_path(self) -> Path | None:
        return self._path


class DbhubConnectionDialog(QDialog):
    """Окно подключения к базе для DBHub.

    Спрашиваем только то, без чего мост нечего запускать: как звать
    подключение, к какой базе и по какой строке. Отдельного поля под
    пароль нет намеренно: пароль берётся из строки подключения и уезжает
    в свой файл, а в конфиг попадает ссылка на переменную. Так секрет не
    остаётся в файле, который открыт и переносится между компьютерами.

    Файл подключений ведёт программа: он лежит рядом с настройками
    opencode и в репозиторий не попадает. Дописать второй источник можно
    тем же окном — список уже добавленных виден наверху.
    """

    def __init__(self, dest: Path, parent=None) -> None:
        super().__init__(parent)
        self.dest = Path(dest)
        self.messages: list[str] = []
        self.setWindowTitle("Подключение к базе для DBHub")
        self.setMinimumWidth(640)

        box = QVBoxLayout(self)
        box.setSpacing(8)
        box.addWidget(ui.label(
            "DBHub умеет PostgreSQL, MySQL, MariaDB, SQL Server, Oracle и "
            "SQLite. Строку подключения возьми у себя: у каждой базы она "
            "своя. Пароль, если он есть в строке, программа вынет и "
            "положит отдельным файлом рядом с настройками — в конфиге "
            "останется только ссылка на него.",
            wrap=True,
        ))

        old = dbhub.read_sources(self.dest)
        if old:
            names = ", ".join(f"{s.id} ({s.db_type})" for s in old)
            box.addWidget(ui.label(f"Уже добавлено: {names}", kind="dim", wrap=True))

        form = QVBoxLayout()
        form.setSpacing(4)

        row_name = QHBoxLayout()
        row_name.addWidget(ui.label("Имя подключения:", kind="title"))
        self.name = QLineEdit()
        self.name.setPlaceholderText("латиницей, без пробелов: sklad, main-db")
        row_name.addWidget(self.name, 1)
        form.addLayout(row_name)

        row_type = QHBoxLayout()
        row_type.addWidget(ui.label("Тип базы:", kind="title"))
        self.db_type = QComboBox()
        for key, title, _schemes in dbhub.DB_TYPES:
            self.db_type.addItem(title, key)
        self.db_type.currentIndexChanged.connect(self._refresh_example)
        row_type.addWidget(self.db_type, 1)
        form.addLayout(row_type)

        row_dsn = QHBoxLayout()
        row_dsn.addWidget(ui.label("Строка подключения:", kind="title"))
        self.dsn = QLineEdit()
        row_dsn.addWidget(self.dsn, 1)
        form.addLayout(row_dsn)
        box.addLayout(form)

        row_limits = QHBoxLayout()
        self.readonly = QCheckBox("Только чтение")
        self.readonly.setChecked(True)
        self.readonly.setToolTip(
            "Запросы на изменение данных будут отклонены. Снять галочку — "
            "значит разрешить серверу менять базу"
        )
        row_limits.addWidget(self.readonly)
        row_limits.addSpacing(16)
        row_limits.addWidget(ui.label("Строк в ответе:", kind="title"))
        self.max_rows = QSpinBox()
        self.max_rows.setRange(10, 100000)
        self.max_rows.setValue(dbhub.DEFAULT_MAX_ROWS)
        self.max_rows.setSingleStep(100)
        self.max_rows.setToolTip(
            "Сколько строк сервер отдаст в одном ответе. Ограничение "
            "касается execute_sql: без него один неосторожный запрос "
            "вернёт таблицу целиком"
        )
        row_limits.addWidget(self.max_rows)
        row_limits.addStretch(1)
        box.addLayout(row_limits)

        self.example = ui.label("", kind="dim", wrap=True)
        box.addWidget(self.example)

        self.status = ui.label("", wrap=True)
        box.addWidget(self.status)

        row = QHBoxLayout()
        row.addStretch(1)
        self.btn_cancel = QPushButton("Отмена")
        self.btn_cancel.clicked.connect(self.reject)
        self.btn_ok = QPushButton("Записать")
        self.btn_ok.setObjectName("primary")
        self.btn_ok.setDefault(True)
        self.btn_ok.clicked.connect(self._save)
        row.addWidget(self.btn_cancel)
        row.addWidget(self.btn_ok)
        box.addLayout(row)

        self._refresh_example()

    def _current_type(self) -> str:
        return str(self.db_type.currentData() or "")

    def _refresh_example(self) -> None:
        key = self._current_type()
        example = dbhub.DSN_EXAMPLES.get(key, "")
        self.dsn.setPlaceholderText(example)
        self.example.setText(
            f"Пример для этой базы: {example}. Такой адрес — образец формы, "
            "а не настоящий: подставь свой хост, базу и пользователя."
            if example else ""
        )

    def _save(self) -> None:
        messages, errors = dbhub.add_source(
            self.dest, self.name.text(), self._current_type(),
            self.dsn.text(), self.readonly.isChecked(), self.max_rows.value(),
        )
        if errors:
            # Окно не закрывается: человек должен видеть, что именно не так,
            # и поправить хотя бы то, что поправимо с клавиатуры.
            self.status.setText("Не записано. " + " ".join(errors))
            return
        self.messages = messages
        self.accept()


class CapsTab(ScrollPage):
    """Возможности базы для opencode — установка по выбору.

    Ставит в настройки opencode команду /голос, мост ПК, мост памяти
    и 12 агентов. Убранная галочка при нажатии «Убрать» снимает
    только наше, чужое не трогает.
    """

    #: Подписи двойного назначения: и для галочек (берутся из
    #: opencode_caps.CAPS_CHOICES), и для строки «уже стоит» (из caps_status,
    #: где мосты тоже есть). Поэтому подписи мостов остаются, хотя самих
    #: галочек у них больше нет.
    TITLES = {
        "voice": "Команда /голос — говорить в микрофон",
        "pc": "Мост ПК — файлы, программы, скриншоты (всё через спрос)",
        "ncp": "Мост NCP — память, библиотека, 7 инструментов",
        "agents": "12 агентов — поиск, план, код, проверка и другие",
        "antiblock": "Обход блокировок — запуск OpenCode через прокси, пул обновляется сам",
    }

    def __init__(self, parent=None) -> None:
        self._worker: Worker | None = None
        self._what = ""
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    # ---- сборка окна

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(
            ui.label(
                "Ставит возможности этой базы прямо в opencode — только "
                "отмеченное. Мосты ПК и NCP сюда не входят: они едут с базой "
                "и включаются кнопкой «Подключить базу». Настоящие настройки "
                "лежат в папке opencode, перед правкой делается копия.",
                kind="dim",
                wrap=True,
            )
        )

        # --- шаг 1: что поставить
        box_what = QGroupBox("1. Что поставить в opencode")
        what_layout = QVBoxLayout(box_what)
        self.checks: dict[str, QCheckBox] = {}
        for name, _title in opencode_caps.CAPS_CHOICES:
            box = QCheckBox(self.TITLES.get(name, name))
            box.setChecked(True)
            what_layout.addWidget(box)
            self.checks[name] = box
        try:
            nagents = len(list((core.app_root() / "tools" / "agents").glob("*.md")))
            if nagents:
                self.checks["agents"].setText(
                    f"{nagents} агентов — поиск, план, код, проверка и другие"
                )
        except OSError:
            pass
        outer.addWidget(box_what)

        # --- шаг 2: куда
        box_where = QGroupBox("2. Куда ставится")
        where_layout = QVBoxLayout(box_where)
        self.dest_edit = QLineEdit()
        self.dest_edit.setPlaceholderText("Папка настроек opencode")
        try:
            self.dest_edit.setText(str(opencode_caps.opencode_dir()))
        except Exception:
            self.dest_edit.setText("")
        btn_pick_dest = QPushButton("Обзор…")
        btn_pick_dest.clicked.connect(self._pick_dest)
        row = QHBoxLayout()
        row.addWidget(QLabel("Папка opencode:   "))
        row.addWidget(self.dest_edit, 1)
        row.addWidget(btn_pick_dest)
        where_layout.addLayout(row)
        self.state_hint = ui.label("", kind="dim", wrap=True)
        where_layout.addWidget(self.state_hint)
        outer.addWidget(box_where)

        # --- шаг 3: провайдеры моделей (новых пресетов)
        box_prov = QGroupBox("3. Провайдеры моделей (новые пресеты)")
        prov_layout = QVBoxLayout(box_prov)
        prov_layout.addWidget(
            ui.label(
                "Провайдеры, которых нет среди встроенных в opencode. "
                "Ключи не спрашиваем и не храним: задай их сам через "
                "/connect в opencode или переменными окружения.",
                kind="dim",
                wrap=True,
            )
        )
        self.pchecks: dict[str, QCheckBox] = {}
        for name, (title, _key, _block) in opencode_caps.PROVIDER_PRESETS.items():
            box = QCheckBox(title)
            box.setChecked(False)
            prov_layout.addWidget(box)
            self.pchecks[name] = box
        outer.addWidget(box_prov)

        # --- шаг 4: обход блокировок — состав набора
        box_ab = QGroupBox("4. Обход блокировок — что именно поставить")
        ab_layout = QVBoxLayout(box_ab)
        ab_layout.addWidget(
            ui.label(
                "Работает только вместе с галочкой «Обход блокировок» выше. "
                "Прокси отдаётся только запущенному через ярлык OpenCode, "
                "остальные программы идут напрямую.",
                kind="dim",
                wrap=True,
            )
        )
        import antiblock as _ab  # noqa: PLC0415 — рядом лежит, круга нет

        self.achecks: dict[str, QCheckBox] = {}
        for key, title in (
            ("facade",
             f"Переводчик и запуск (фасад 127.0.0.1:{_ab.facade_port()} "
             "+ ярлык-запуск)"),
            ("lists", "Бесплатные списки (SOCKS5-пул + VLESS-подписки + автообновление раз в сутки)"),
            ("dns", "Защищённый DNS (DoH: Google/Cloudflare/Quad9/AdGuard) — запасной способ обхода"),
            ("command", "Команда /обход внутри OpenCode"),
            ("shortcut", "Ярлык «OpenCode (обход)» с иконкой программы на рабочий стол"),
        ):
            box = QCheckBox(title)
            box.setChecked(True)
            ab_layout.addWidget(box)
            self.achecks[key] = box
        row_ab = QHBoxLayout()
        self.btn_ab_check = QPushButton("Проверить подключение")
        self.btn_ab_dns = QPushButton("Проверить DNS")
        row_ab.addWidget(self.btn_ab_check)
        row_ab.addWidget(self.btn_ab_dns)
        row_ab.addStretch(1)
        ab_layout.addLayout(row_ab)
        # Строка состояния: своим каналом, запасным или не работает.
        # §20.6 плана требует говорить прямо, когда включён запасной:
        # снаружи оба канала выглядят одинаково, а на деле трафик идёт
        # через чужие машины. Молчание тут врёт.
        self.lbl_ab_state = ui.label("Обход: проверяю...", kind="dim", wrap=True)
        ab_layout.addWidget(self.lbl_ab_state)
        row_port = QHBoxLayout()
        row_port.addWidget(ui.label("Порт фасада:", kind="dim"))
        self.spin_ab_port = QSpinBox()
        self.spin_ab_port.setRange(_ab.PORT_MIN, _ab.PORT_MAX)
        self.spin_ab_port.setValue(_ab.facade_port())
        self.spin_ab_port.setToolTip(
            "Порт, на котором фасад принимает соединения. Меняйте, если "
            f"дефолтный {_ab.FACADE_PORT} занят. Стартер прочитает это сам."
        )
        row_port.addWidget(self.spin_ab_port)
        self.btn_ab_save = QPushButton("Сохранить")
        row_port.addWidget(self.btn_ab_save)
        row_port.addStretch(1)
        ab_layout.addLayout(row_port)
        self._refresh_ab_state()
        outer.addWidget(box_ab)

        # --- шаг 5: навыки поштучно
        box_skills = QGroupBox("5. Навыки — какие поставить в opencode")
        skills_layout = QVBoxLayout(box_skills)
        skills_layout.addWidget(
            ui.label(
                "Отметь нужные: отмеченные скопируются целиком, неотмеченные "
                "уйдут в _previous-version — выбор всегда можно переиграть. "
                "После установки перезапусти opencode.",
                kind="dim",
                wrap=True,
            )
        )
        self.caps_skills_list = QListWidget()
        # Перенос делает core.skill_item_text, вставляя переносы в текст
        # явно: сворачивание длинного описания в одну строку Qt здесь
        # делает непредсказуемо, а с явными переносами высота строки
        # известна заранее и список растёт равномерно.
        self.caps_skills_list.setWordWrap(False)
        self.caps_skills_list.setUniformItemSizes(False)
        self.caps_skills_list.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        # Запас на случай пустого списка: fit_skills_height выставит свою
        # высоту по фактическому числу навыков.
        self.caps_skills_list.setMinimumHeight(300)
        self.caps_skills_list.setMaximumHeight(600)
        skills_layout.addWidget(self.caps_skills_list)

        row_skills = QHBoxLayout()
        self.btn_skills_all = QPushButton("Отметить все")
        self.btn_skills_none = QPushButton("Снять все")
        self.btn_skills_reload = QPushButton("Обновить список")
        self.btn_skills_put = QPushButton("Поставить отмеченные")
        self.btn_skills_drop = QPushButton("Убрать отмеченные")
        row_skills.addWidget(self.btn_skills_all)
        row_skills.addWidget(self.btn_skills_none)
        row_skills.addWidget(self.btn_skills_reload)
        row_skills.addStretch(1)
        row_skills.addWidget(self.btn_skills_put)
        row_skills.addWidget(self.btn_skills_drop)
        skills_layout.addLayout(row_skills)

        self.caps_skills_hint = ui.label("", kind="dim", wrap=True)
        skills_layout.addWidget(self.caps_skills_hint)
        outer.addWidget(box_skills)

        # --- шаг 6. Серверы MCP из реестра
        box_mcp = QGroupBox("6. Серверы MCP — что можно включить")
        mcp_layout = QVBoxLayout(box_mcp)
        mcp_layout.addWidget(
            ui.label(
                "Это не мосты: мосты едут с базой и включены всегда. Здесь "
                "сторонние серверы из реестра. Программа проверяет требования "
                "живьём и вписывает блок в настройки opencode — сама ничего "
                "не скачивает и подписки не покупает. После включения "
                "перезапусти opencode.",
                kind="dim",
                wrap=True,
            )
        )

        self.reg_servers: list[mcp_registry.Server] = []
        self.reg_table = QTableWidget(0, 4)
        # Заголовок «Инструментов» не помещался в свою колонку: та
        # получает остаток места, а не своё, и на снимке обрезалась
        # с двух сторон — «-трумен…». Короче и понятнее: сколько
        # инструментов даёт сервер.
        self.reg_table.setHorizontalHeaderLabels(
            ["Сервер", "Состояние", "Лицензия", "Инструм."]
        )
        self.reg_table.verticalHeader().setVisible(False)
        self.reg_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.reg_table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self.reg_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.reg_table.setMinimumHeight(150)
        self.reg_table.setMaximumHeight(190)
        # Ширины заданы явно, потому что Qt по умолчанию распределяет
        # место неудачно: снимок показал обрезанные до «Windo…» имена и
        # обрезанное до «нужно: …» состояние. Имя должно помещаться
        # целиком - по нему человека ищет в списке, - а лишнее место
        # отдаётся состоянию, потому что оно длиннее всех.
        header = self.reg_table.horizontalHeader()
        header.setStretchLastSection(False)
        header.setSectionResizeMode(header.ResizeMode.Fixed)
        # Ширины считаются по содержимому при заполнении таблицы,
        # см. _reg_fit_columns. Здесь только минимумы: пока таблица
        # пустая, измерять нечего.
        for col, minimum in ((0, 150), (1, 200), (2, 110), (3, 90)):
            self.reg_table.setColumnWidth(col, minimum)
        # Горизонтальная прокрутка в этом блоке не нужна: таблица
        # узкая, и полоса под ней только мешает.
        self.reg_table.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        mcp_layout.addWidget(self.reg_table)

        self.reg_detail = QPlainTextEdit()
        self.reg_detail.setReadOnly(True)
        self.reg_detail.setMinimumHeight(120)
        mcp_layout.addWidget(self.reg_detail)

        row_mcp = QHBoxLayout()
        self.btn_reg_check = QPushButton("Проверить всё")
        self.btn_reg_on = QPushButton("Включить")
        self.btn_reg_auto = QPushButton("Настроить автоматически")
        self.btn_reg_auto.setToolTip(
            "Android Studio: поставить плагин из набора программы, включить "
            "сервер в студии, проверить его и вписать в настройки opencode. "
            "DBHub: проверить связь с базой живым запросом и вписать сервер"
        )
        self.btn_reg_auto.setEnabled(False)
        self.btn_reg_off = QPushButton("Выключить")
        self.btn_reg_src = QPushButton("Открыть источник")
        # Подключения DBHub. Кнопки стоят в общем ряду, но живут только
        # для своей строки: у остальных серверов подключений к базам нет.
        self.btn_db_add = QPushButton("Добавить подключение")
        self.btn_db_add.setToolTip(
            "DBHub: вписать базу — файл mcp-dbhub.toml рядом с настройками "
            "opencode. Пароль из строки подключения программа сохранит "
            "отдельным файлом"
        )
        self.btn_db_add.setEnabled(False)
        self.btn_db_check = QPushButton("Проверить соединение")
        self.btn_db_check.setToolTip(
            "DBHub: поднять сервер и спросить у него протоколом, отвечает ли "
            "база. Ничего не записывает — только говорит, жива ли связь"
        )
        self.btn_db_check.setEnabled(False)
        row_mcp.addWidget(self.btn_reg_check)
        row_mcp.addWidget(self.btn_reg_on)
        row_mcp.addWidget(self.btn_reg_auto)
        row_mcp.addWidget(self.btn_reg_off)
        row_mcp.addWidget(self.btn_reg_src)
        row_mcp.addWidget(self.btn_db_add)
        row_mcp.addWidget(self.btn_db_check)
        row_mcp.addStretch(1)
        mcp_layout.addLayout(row_mcp)

        self.reg_hint = ui.label("", kind="dim", wrap=True)
        mcp_layout.addWidget(self.reg_hint)
        outer.addWidget(box_mcp)

        # --- кнопки
        buttons = QHBoxLayout()
        self.btn_install = QPushButton("Поставить отмеченное")
        self.btn_install.setObjectName("primary")
        self.btn_install.clicked.connect(self._install)
        self.btn_remove = QPushButton("Убрать отмеченное")
        self.btn_remove.clicked.connect(self._remove)
        self.btn_refresh = QPushButton("Обновить состояние")
        self.btn_refresh.clicked.connect(self._refresh)
        buttons.addWidget(self.btn_install)
        buttons.addWidget(self.btn_remove)
        buttons.addStretch(1)
        buttons.addWidget(self.btn_refresh)
        outer.addLayout(buttons)

        self.log = ui.LogView()
        self.log.setMinimumHeight(170)
        outer.addWidget(self.log)

        # Связи — только здесь, когда все элементы уже созданы.
        self.dest_edit.textChanged.connect(self._refresh)
        self.btn_ab_check.clicked.connect(self._check_antiblock)
        self.btn_ab_dns.clicked.connect(self._check_dns)
        self.btn_ab_save.clicked.connect(self._save_ab_port)
        self.btn_skills_all.clicked.connect(lambda: self._set_all_caps_skills(True))
        self.btn_skills_none.clicked.connect(lambda: self._set_all_caps_skills(False))
        self.btn_skills_reload.clicked.connect(lambda: self._fill_caps_skills())
        self.btn_skills_put.clicked.connect(self._install_skills)
        self.btn_skills_drop.clicked.connect(self._remove_skills)
        self.caps_skills_list.itemChanged.connect(self._caps_skills_changed)
        self.btn_reg_check.clicked.connect(self._reg_check_all)
        self.btn_reg_on.clicked.connect(self._reg_enable)
        self.btn_reg_auto.clicked.connect(self._reg_auto)
        self.btn_reg_off.clicked.connect(self._reg_disable)
        self.btn_reg_src.clicked.connect(self._reg_open_source)
        self.btn_db_add.clicked.connect(self._db_add_source)
        self.btn_db_check.clicked.connect(self._db_check)
        self.reg_table.currentCellChanged.connect(
            lambda *_: self._reg_show_detail()
        )
        self._fill_caps_skills()
        self._reg_load()
        self._refresh()

        outer.activate()
        self.refresh_height()

    # ---- серверы MCP из реестра

    def _reg_base(self) -> Path:
        return self._base()

    def _reg_dest(self) -> Path | None:
        dest = self._dest()
        return dest

    def _reg_load(self) -> None:
        """Читает реестр и показывает серверы без проверки требований.

        Требования здесь не проверяются: каждая проверка запускает
        внешнюю программу. Пока пользователь не нажал «Проверить всё»,
        состояние показано как «не проверено» — это честнее, чем молча
        показывать пустые галочки.
        """
        base = self._reg_base()
        data = mcp_registry.load_registry(base)
        self.reg_servers = []
        for spec in data.get("servers") or []:
            if not isinstance(spec, dict):
                continue
            conn = spec.get("connection")
            self.reg_servers.append(
                mcp_registry.Server(
                    id=str(spec.get("id") or ""),
                    name=str(spec.get("name") or ""),
                    raw=spec,
                    has_connection=isinstance(conn, dict) and bool(
                        conn.get("command") or conn.get("url")
                        or conn.get("manual_config")
                    ),
                )
            )
        self._reg_paint()

    def _reg_config_saved(self, server: mcp_registry.Server) -> bool:
        """Вставлена ли конфигурация для сервера, который настраивают руками.

        Из трёх ручных требований это единственное, которое программа
        может проверить сама: конфигурацию вставляет она же и хранит
        рядом с настройками. Остальные два - включён ли сервер в студии
        и запущена ли студия - проверяются только человеком.
        """
        if not server.manual_setup:
            return True
        dest = self._reg_dest()
        if dest is None:
            return False
        return mcp_registry.load_manual_config(dest, server.id) is not None

    # Серверы, которые программа умеет настроить целиком сама.
    AUTO_SERVERS = ("android-studio", "obs", "android-emulator", "dbhub")

    def _reg_auto_possible(self, server: mcp_registry.Server | None) -> tuple[bool, str]:
        """Можно ли настроить автоматически и что этому мешает.

        Автонастройка умеет ровно четыре вещи: Android Studio, OBS,
        эмулятор Android и DBHub. Остальные серверы запускаются командой,
        и «Настроить автоматически» для них был бы кнопкой вроде
        работающей.
        """
        if server is None:
            return False, (
                "Выберите строку в списке: автонастройка есть у Android "
                "Studio, OBS, Android-эмулятора и DBHub. У LDPlayer она не "
                "нужна — ему достаточно кнопки «Включить»."
            )
        if server.id not in self.AUTO_SERVERS:
            return False, (
                f"Автонастройки для «{server.name}» нет: сервер запускается "
                "командой, достаточно кнопки «Включить»."
            )
        return True, ""

    def _reg_state_text(self, server: mcp_registry.Server) -> str:
        """Состояние для колонки. Короткое - иначе таблица разъезжается.

        Полный список требований человек видит в подробностях под
        таблицей. Здесь достаточно понять, что мешает, и то, что
        остальное не упёрлось в ту же строку понасметку.
        """
        if server.installed:
            return "включён"
        if not server.requirements:
            return "не проверено"
        if not server.has_connection:
            return "нет команды"
        if server.missing:
            first = server.missing[0]
            rest = len(server.missing) - 1
            tail = f" и ещё {rest}" if rest > 0 else ""
            return f"не хватает: {first.what}{tail}"
        if server.manual_setup:
            # Конфигурацию программа в состоянии проверить может, иначе
            # строка врала бы "нужно" даже после того, как человек всё
            # вставил. А вот то, работает ли студия, - не может.
            if not self._reg_config_saved(server):
                return "нужно: конфигурация из студии"
            return "можно включить, толк не гарантирован"
        first = server.blocking_manual
        if first:
            rest = len(first) - 1
            tail = f" и ещё {rest}" if rest > 0 else ""
            return f"нужно: {first[0].what}{tail}"
        return "можно включить"

    def _reg_fit_columns(self) -> None:
        """Раздаёт ширины колонок по тому, что в них реально лежит.

        Зачем. Трижды ширины двигали вручную, и каждый раз находился
        либо обрезанный текст, либо лишняя полоса прокрутки: сумма
        ширин упирается в ширину таблицы, а строки в реестре меняются.
        Поэтому измеряем то, что написано, и выдаём каждой колонке
        ровно столько, сколько нужно, а остаток отдаём последней.

        Текст известен на этот момент - ячейки уже заполнены, поэтому
        измерять можно точно, а не гадать по длине строки.
        """
        table = self.reg_table
        if table.rowCount() <= 0:
            return
        # Последняя колонка - число инструментов. Её текст всегда
        # короткий, поэтому меряем её последней и с тем же запасом:
        # иначе при нехватке места Qt отдаёт ей остаток, и она
        # схлопывается в ноль - колонка пропадает совсем.
        raw: list[int] = []
        for col in range(table.columnCount()):
            need = 0
            for row in range(table.rowCount()):
                item = table.item(row, col)
                if item is None:
                    continue
                need = max(need, QFontMetrics(item.font())
                           .horizontalAdvance(item.text()))
            header_item = table.horizontalHeaderItem(col)
            if header_item is not None:
                need = max(need, QFontMetrics(header_item.font())
                           .horizontalAdvance(header_item.text()))
            raw.append(need)

        # Отступы делятся, а не задаются с потолка. Меряю от того,
        # что действительно есть, а не от того, что предполагаю:
        # viewport не равен ширине таблицы. На снимке получилось так,
        # что ширины в сумме 621, таблица 592 - и Qt подрезал правую
        # колонку, потому что вертикальной полосы в блоке не было,
        # а я её вычитал. Значит считать надо от реальной ширины
        # минус настоящая полоса, если она показана.
        shown = table.verticalScrollBar().isVisible()
        available = table.viewport().width()
        if shown:
            available -= table.verticalScrollBar().width()
        columns = max(1, len(raw))
        spare = available - sum(raw)
        if spare < 0:
            # Не помещается - режем отступы, но не текст: обрезанное
            # состояние хуже, чем тесные края.
            padding = 2
        else:
            padding = min(28, max(8, spare // columns))
        for col, need in enumerate(raw):
            table.setColumnWidth(col, need + padding)
        # Минимум ширины разрывает круг. Qt считает ширину таблицы как
        # сумму ширин колонок, а я считаю колонки от ширины таблицы.
        # Без этого minima Qt сжимает таблицу под блок, сумма ширин
        # остаётся прежней, проверки проходят - а последняя колонка
        # молча уезжает за край. На снимке это выглядело как
        # обрезанный заголовок «Инструм.». Проверено: с минимумом
        # таблица рисуется целиком, без него - обрезается.
        table.setMinimumWidth(sum(table.columnWidth(c)
                                  for c in range(table.columnCount())) + 4)

    def _reg_paint(self) -> None:
        servers = self.reg_servers
        self.reg_table.setRowCount(len(servers))
        for row, server in enumerate(servers):
            self.reg_table.setItem(row, 0, QTableWidgetItem(server.name))
            self.reg_table.setItem(row, 1, QTableWidgetItem(self._reg_state_text(server)))
            self.reg_table.setItem(row, 2, QTableWidgetItem(server.license))
            self.reg_table.setItem(row, 3, QTableWidgetItem(server.tools_count))
        self._reg_fit_columns()
        if servers and self.reg_table.currentRow() < 0:
            self.reg_table.setCurrentCell(0, 0)
        self._reg_show_detail()

    def _reg_current(self) -> mcp_registry.Server | None:
        row = self.reg_table.currentRow()
        if 0 <= row < len(self.reg_servers):
            return self.reg_servers[row]
        return None

    def _reg_show_detail(self) -> None:
        server = self._reg_current()
        # Кнопка автонастройки жива только для Android Studio. Для
        # остальных серверов её нажатие было бы враньём, поэтому гасим
        # её, а не ждём ошибки по нажатию.
        possible, _ = self._reg_auto_possible(server)
        self.btn_reg_auto.setEnabled(possible)
        # Кнопки подключений — только у DBHub: больше ни один сервер
        # не ходит в базы, и «Добавить подключение» у него означало бы
        # пустую кнопку.
        is_db = server is not None and server.id == "dbhub"
        self.btn_db_add.setEnabled(is_db)
        self.btn_db_check.setEnabled(
            is_db and bool(dbhub.read_sources(self._reg_dest() or Path()))
        )
        if is_db:
            sources = dbhub.read_sources(self._reg_dest() or Path())
            if sources:
                names = ", ".join(s.id for s in sources)
                self.reg_hint.setText(
                    f"Подключения DBHub: {names}. «Проверить соединение» "
                    "поднимает сервер и спрашивает протоколом, отвечает ли "
                    "база. «Настроить автоматически» делает то же и вписывает "
                    "сервер в настройки opencode."
                )
            else:
                self.reg_hint.setText(
                    "Подключений к базам пока нет: у DBHub без них нечего "
                    "проверять и нечего включать. Начни с «Добавить подключение»."
                )
        else:
            self.reg_hint.clear()
        if server is None:
            self.reg_detail.setPlainText("Выберите сервер, чтобы увидеть подробности.")
            return
        raw = server.raw
        lines = [f"{server.name} — {server.license}"]
        conn = raw.get("connection") or {}
        target = conn.get("url") or " ".join(conn.get("command") or [])
        if target:
            lines.append(f"Команда: {target}")
        if raw.get("why"):
            lines.append("")
            lines.append(str(raw["why"]))
        for key, title in (
            ("exclusive_access", "Особенность"),
            ("telemetry", "Телеметрия"),
            ("auth", "Вход"),
        ):
            if raw.get(key):
                lines.append("")
                lines.append(f"{title}: {raw[key]}")
        if raw.get("requires"):
            lines.append("")
            lines.append("Требуется:")
            for req in raw["requires"]:
                lines.append(f"  • {req.get('what')}"
                             + (f" — {req['note']}" if req.get("note") else ""))
        if raw.get("safety"):
            lines.append("")
            lines.append(f"Безопасность: {raw['safety']}")

        if server.manual_setup:
            steps = server.setup_steps
            if steps:
                lines.append("")
                lines.append("Как настроить — по шагам:")
                for index, step in enumerate(steps, start=1):
                    lines.append(f"  {index}. {step}")
            if raw.get("only_while_running"):
                lines.append("")
                lines.append(
                    f"Пока студия закрыта: {raw['only_while_running']}"
                )
            if raw.get("ready_here_note") and not server.ready_here:
                lines.append("")
                lines.append(f"Сейчас: {raw['ready_here_note']}")

        if raw.get("verdict"):
            lines.append("")
            lines.append(f"Итог: {raw['verdict']}")
        lines.append("")
        lines.append(f"Источник: {server.source}")
        self.reg_detail.setPlainText("\n".join(lines))

    def _reg_check_all(self) -> None:
        """Живая проверка требований. Идёт в фоне: запускает программы."""
        base = self._reg_base()
        dest = self._reg_dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return

        def job(progress):
            servers = mcp_registry.load_servers(base)
            mcp_registry.mark_installed(servers, dest)
            self.reg_servers = servers
            self._reg_paint()
            messages: list[str] = []
            for server in servers:
                state = self._reg_state_text(server)
                messages.append(f"{server.name}: {state}")
            return messages, []

        self._start(job, "registry")

    def _reg_enable(self) -> None:
        dest = self._reg_dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        server = self._reg_current()
        if server is None:
            self._warn("Выберите сервер в списке.")
            return

        # Сервер, который включается внутри другой программы, сначала
        # спрашивает конфигурацию. Без неё запись в настройки была бы
        # враньём: блок без токена заведомо не подключится.
        if server.manual_setup:
            self._reg_ask_config(server, dest)
            return

        # DBHub без подключения к базе — блок, которому нечего отдавать,
        # а с нерабочей базой — блок, который opencode будет ждать 30
        # секунд и покажет «Не удалось». Поэтому у него «Включить» идёт
        # тем же путём, что автонастройка: сначала живая проверка связи,
        # и только потом запись.
        if server.id == "dbhub":
            self._reg_auto()
            return

        def job(progress):
            return mcp_registry.enable(dest, server, progress=progress)

        self._start(job, "registry")

    def _db_add_source(self) -> None:
        """Вписывает подключение к базе в файл DBHub.

        Подключение — не настройка opencode, а отдельный файл рядом с
        ними. Поэтому он живёт не в кнопке «Включить», а в своей:
        вписать базу можно и до включения сервера, и для второй базы.
        """
        dest = self._reg_dest()
        server = self._reg_current()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        if server is None or server.id != "dbhub":
            self._warn("Подключения к базам есть только у DBHub. "
                       "Выберите его строку в списке.")
            return

        dialog = DbhubConnectionDialog(dest, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        for line in dialog.messages:
            self.log.add(line, "ok")
        self.log.add(
            "Дальше — «Проверить соединение»: живая проверка скажет, "
            "отвечает ли база, до записи в настройки opencode.", "ok"
        )
        self._reg_show_detail()

    def _db_check(self) -> None:
        """Живая проверка связи с базами DBHub.

        Настоящий разговор с сервером по протоколу MCP: сервер поднимает
        сама программа, отвечает он или нет — видно по ответу, а не по
        тому, что порт занят. Ничего не записывает.
        """
        dest = self._reg_dest()
        server = self._reg_current()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        if server is None or server.id != "dbhub":
            self._warn("Проверять связь с базой умеет только DBHub. "
                       "Выберите его строку в списке.")
            return
        sources = dbhub.read_sources(dest)
        if not sources:
            self._warn("Подключений нет. Нажми «Добавить подключение» "
                       "и впиши базу.")
            return

        def job(progress):
            return dbhub.check_connection(dest, sources, progress=progress)

        self._start(job, "dbhub")

    def _reg_auto(self) -> None:
        """Настраивает сервер целиком, без конфигураций вручную.

        Автонастройка умеет Android Studio, OBS, эмулятор и DBHub.
        У DBHub она упирается в подключение: без вписанной базы проверять
        нечего, поэтому сначала предлагается «Добавить подключение».
        """
        dest = self._reg_dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        server = self._reg_current()
        possible, why = self._reg_auto_possible(server)
        if not possible or server is None:
            self._warn(why)
            return

        # Ключ реестра с путём установки OBS. Без него студия не знает,
        # где установлена, и падает с ошибкой про языковой файл, хотя
        # файл на месте. Запись в реестр — внешнее действие, поэтому
        # спрашиваем отдельно и по умолчанию отвечаем «нет».
        fix_install_path = False
        # У OBS пароль спрашивается отдельно: его негде взять, кроме
        # самого человека или генератора программы.
        password = ""
        if server.id == "obs":
            trouble = bridges.obs_install_problem()
            if trouble:
                answer = QMessageBox.question(
                    self,
                    "OBS не знает, где установлена",
                    trouble
                    + "\n\nСоздать ключ реестра HKCU\\Software\\OBSStudio "
                      "с путём установки? Запись делается только для "
                      "текущего пользователя и ничего не удаляет.",
                    QMessageBox.StandardButton.Yes
                    | QMessageBox.StandardButton.No,
                    QMessageBox.StandardButton.No,
                )
                if answer != QMessageBox.StandardButton.Yes:
                    self._warn(trouble)
                    return
                fix_install_path = True
            password = self._ask_obs_password(dest)
            if password is None:
                return

        def job(progress):
            if server.id == "android-studio":
                return android_studio.auto_setup(dest, server, progress=progress)
            if server.id == "obs":
                return bridges.auto_setup_obs(
                    dest, server, password, progress=progress,
                    allow_install_path_fix=fix_install_path)
            if server.id == "dbhub":
                return dbhub.auto_setup(dest, server, progress=progress)
            return bridges.auto_setup_emulator(dest, server, progress=progress)

        self._start(job, "registry")

    def _ask_obs_password(self, dest: Path) -> str | None:
        """Спрашивает пароль OBS: показать, сгенерировать, вписать свой.

        None означает «человек закрыл окно» — тогда ничего не делаем.
        """
        current = bridges.read_password(dest)
        dialog = ObsPasswordDialog(current, self)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        password = dialog.password()
        if not password:
            self._warn("Пароль пустой. Мост без него не подключится.")
            return None
        if dialog.save_path():
            ok, note = bridges.copy_password_to(dest, dialog.save_path())
            if ok:
                self.log.add(note, "ok")
            else:
                self._warn(note)
                return None
        return password

    def _reg_ask_config(self, server: mcp_registry.Server, dest: Path) -> None:
        """Спрашивает конфигурацию и, если её дали, включает сервер."""
        import json

        saved = mcp_registry.load_manual_config(dest, server.id)
        dialog = ManualConfigDialog(server, self)
        if saved:
            # Показать прошлую, чтобы человек мог её поправить, а не
            # искать заново. Токен в поле виден целиком, и это правильно:
            # человек должен видеть, что именно попадёт в настройки.
            # Спрятать его было бы удобнее программе и хуже человеку.
            text = json.dumps(
                {"mcpServers": {server.id: saved}}, ensure_ascii=False, indent=2
            )
            dialog.text.setPlainText(text)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        ok, note = mcp_registry.save_manual_config(
            dest, server.id, dialog.config_text()
        )
        if not ok:
            self._warn(note)
            return

        def job(progress):
            return mcp_registry.enable(dest, server, progress=progress)

        self._start(job, "registry")

    def _reg_disable(self) -> None:
        dest = self._reg_dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        server = self._reg_current()
        if server is None:
            self._warn("Выберите сервер в списке.")
            return

        def job(progress):
            return mcp_registry.disable(dest, server)

        self._start(job, "registry")

    def _reg_open_source(self) -> None:
        server = self._reg_current()
        if server is None or not server.source:
            self._warn("У этого сервера нет источника в реестре.")
            return
        # Ссылка из реестра, а не пользовательский ввод: всё равно
        # показываем, что открываем, и ждём подтверждения.
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Открыть источник?")
        box.setText(f"Открыть в браузере?\n\n{server.source}")
        box.setStandardButtons(
            QMessageBox.StandardButton.Open | QMessageBox.StandardButton.Cancel
        )
        if box.exec() != QMessageBox.StandardButton.Open:
            return
        import webbrowser  # локальный импорт: нужен только здесь

        try:
            webbrowser.open(server.source)
        except Exception as exc:  # noqa: BLE001
            self._warn(f"Не открылось: {exc}")

    # ---- состояние

    def _selection(self) -> set[str]:
        # Мосты ПК и NCP — часть базы, а не расширение: у них нет галочки
        # и они подставляются всегда. Иначе можно было бы снять память,
        # не понимая, что происходит.
        picked = {name for name, box in self.checks.items() if box.isChecked()}
        return picked | set(opencode_caps.CAPS_ALWAYS)

    def _pselection(self) -> set[str]:
        return {name for name, box in self.pchecks.items() if box.isChecked()}

    def _antiblock_opts(self) -> dict[str, bool]:
        """Галочки шага 4 — состав набора обхода блокировок."""
        return {key: box.isChecked() for key, box in self.achecks.items()}

    def _refresh_ab_state(self) -> None:
        """Строка состояния обхода: свой канал, запасной пул или тишина.

        Обновляется при открытии секции и после кнопок проверки. Две
        проверки локальных портов стоят почти ноль: закрытый порт падает
        с отказом сразу, а не по таймауту, поэтому ждать не приходится и
        поток не нужен — так же сделано и для кнопки DNS.
        """
        item = getattr(self, "lbl_ab_state", None)
        if item is None:
            return
        try:
            import antiblock  # noqa: PLC0415 — рядом лежит

            state, text = antiblock.channel_state()
            color = {
                antiblock.CHANNEL_OWN: ui.OK,
                antiblock.CHANNEL_FALLBACK: ui.WARN,
            }.get(state, ui.ERROR)
        except Exception as exc:  # noqa: BLE001 — строка не должна ронять окно
            text, color = f"проверка не запустилась: {exc}", ui.ERROR
        item.setText(f"Обход: {text}")
        item.setStyleSheet(f"color: {color};")

    def _save_ab_port(self) -> None:
        """Кнопка «Сохранить»: записать порт фасада.

        Занятый порт не сохраняем. Иначе настройка останется в файле и
        обнаружится только при следующем запуске обхода — когда фасад не
        поднимется. Здесь человек ещё может выбрать другой.
        """
        import antiblock as _ab  # noqa: PLC0415 — рядом лежит

        value = self.spin_ab_port.value()
        why = _ab.port_error(value)
        if why:
            self.log.add(f"Порт не сохранён: {why}.", "warn")
            return
        if _ab._port_listening(_ab.FACADE_HOST, value):
            owner = _ab.who_listens(value) or "неизвестным процессом"
            free = _ab.suggest_free_port(value)
            tip = (f"Свободный рядом: {free}. Впишите его и сохраните ещё раз."
                   if free else "Свободного порта рядом не нашлось.")
            self.log.add(
                f"Порт {value} занят ({owner}). Не сохранён. {tip}", "warn")
            return
        previous = _ab.facade_port()
        ok, msg = _ab.set_facade_port(value)
        self.log.add(msg, "ok" if ok else "warn")
        if ok and previous != value and _ab._port_listening(
                _ab.FACADE_HOST, previous):
            self.log.add(
                f"Фасад сейчас работает на {previous}. Новый порт вступит в "
                "силу после перезапуска обхода.", "warn")
        self.spin_ab_port.setValue(_ab.facade_port())
        self._refresh_ab_state()

    def _check_antiblock(self) -> None:
        """Кнопка «Проверить подключение»: слушает ли фасад свой порт."""
        try:
            import antiblock  # noqa: PLC0415 — рядом лежит

            ok, text = antiblock.check_connection()
        except Exception as exc:
            ok, text = False, f"Проверка не запустилась: {exc}"
        self.log.add(text, "ok" if ok else "warn")
        self._refresh_ab_state()

    def _check_dns(self) -> None:
        """Кнопка «Проверить DNS»: резолвит домен через защищённый DNS."""
        self.btn_ab_dns.setEnabled(False)
        try:
            import antiblock  # noqa: PLC0415 — рядом лежит

            ok, text = antiblock.check_dns(proxy_url="")
        except Exception as exc:
            ok, text = False, f"Проверка DNS не запустилась: {exc}"
        finally:
            self.btn_ab_dns.setEnabled(True)
        self.log.add(text, "ok" if ok else "warn")
        self._refresh_ab_state()

    # ---- навыки поштучно

    def _fill_caps_skills(self) -> None:
        """Список навыков базы: все отмечены, как при подключении."""
        self.caps_skills_list.blockSignals(True)
        self.caps_skills_list.clear()
        for skill in core.list_skills(self._base()):
            desc = skill["description"]
            text = core.skill_item_text(skill)
            row = QListWidgetItem(text)
            row.setData(1000, skill["name"])
            row.setToolTip(desc or skill["title"])
            row.setFlags(row.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            row.setCheckState(Qt.CheckState.Checked)
            self.caps_skills_list.addItem(row)
        self.caps_skills_list.blockSignals(False)
        self._caps_skills_changed()
        fit_skills_height(self.caps_skills_list)

    def _chosen_caps_skills(self) -> list[str]:
        names: list[str] = []
        for index in range(self.caps_skills_list.count()):
            row = self.caps_skills_list.item(index)
            if row.checkState() == Qt.CheckState.Checked:
                names.append(str(row.data(1000)))
        return names

    def _set_all_caps_skills(self, on: bool) -> None:
        state = Qt.CheckState.Checked if on else Qt.CheckState.Unchecked
        self.caps_skills_list.blockSignals(True)
        for index in range(self.caps_skills_list.count()):
            self.caps_skills_list.item(index).setCheckState(state)
        self.caps_skills_list.blockSignals(False)
        self._caps_skills_changed()

    def _caps_skills_changed(self) -> None:
        total = self.caps_skills_list.count()
        chosen = len(self._chosen_caps_skills())
        if total == 0:
            self.caps_skills_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            self.caps_skills_hint.setText("В базе навыков нет — нечего ставить.")
        elif chosen == total:
            self.caps_skills_hint.setStyleSheet(f"color: {ui.OK};")
            self.caps_skills_hint.setText(f"Отмечены все: {chosen}.")
        elif chosen == 0:
            self.caps_skills_hint.setStyleSheet(f"color: {ui.WARN};")
            self.caps_skills_hint.setText(
                "Ничего не отмечено: «Поставить» переносить нечего, "
                "«Убрать» спрячет все навыки программы в _previous-version."
            )
        else:
            self.caps_skills_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            self.caps_skills_hint.setText(f"Отмечено: {chosen} из {total}.")

    def _install_skills(self) -> None:
        dest = self._dest()
        if dest is None:
            self.log.add("Не выбрана папка opencode.", "error")
            return
        chosen = self._chosen_caps_skills()
        base = self._base()

        def job(progress):
            return core.sync_selected_skills(base, dest, chosen, progress)

        self._start(job, "skills")

    def _remove_skills(self) -> None:
        dest = self._dest()
        if dest is None:
            self.log.add("Не выбрана папка opencode.", "error")
            return
        chosen = self._chosen_caps_skills()
        base = self._base()

        def job(progress):
            # Убрать = поставить пустой набор: всё уедет в _previous-version.
            kept = [n for n in self._all_caps_skill_names() if n not in set(chosen)]
            return core.sync_selected_skills(base, dest, kept, progress)

        self._start(job, "skills")

    def _all_caps_skill_names(self) -> list[str]:
        return [s["name"] for s in core.list_skills(self._base())]

    def _dest(self) -> Path | None:
        text = self.dest_edit.text().strip()
        if not text:
            return None
        return Path(text)

    def _refresh(self) -> None:
        dest = self._dest()
        if dest is None or not dest.is_dir():
            self.state_hint.setStyleSheet(f"color: {ui.WARN};")
            self.state_hint.setText("Папки пока нет — при установке будет создана.")
            return
        try:
            status = opencode_caps.caps_status(dest)
            pstatus = opencode_caps.providers_status(dest)
        except Exception as exc:
            self.state_hint.setStyleSheet(f"color: {ui.ERROR};")
            self.state_hint.setText(f"Не прочиталось: {exc}")
            return
        inside = [self.TITLES[n] for n in status if status[n]]
        inside += [title for name, (title, _k, _b) in opencode_caps.PROVIDER_PRESETS.items()
                   if pstatus.get(name)]
        if inside:
            self.state_hint.setStyleSheet(f"color: {ui.OK};")
            self.state_hint.setText("Уже стоит:\n- " + "\n- ".join(inside))
        else:
            self.state_hint.setStyleSheet(f"color: {ui.TEXT_DIM};")
            self.state_hint.setText("Наших возможностей здесь пока нет.")

    # ---- выбор папки

    def _pick_dest(self) -> None:
        start = self.dest_edit.text().strip() or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(
            self, "Папка настроек opencode", start
        )
        if chosen:
            self.dest_edit.setText(chosen)

    # ---- запуск работы

    def _set_busy(self, busy: bool) -> None:
        for button in (self.btn_install, self.btn_remove, self.btn_refresh,
                       self.btn_skills_put, self.btn_skills_drop,
                       self.btn_skills_reload):
            button.setEnabled(not busy)

    def _start(self, job, what: str) -> None:
        self._what = what
        self.log.clear_log()
        self._set_busy(True)
        self._worker = Worker(job, self)
        self._worker.line.connect(self.log.add)
        self._worker.finished.connect(self._finished)
        self._worker.start()

    def _finished(self) -> None:
        self._set_busy(False)
        result = self._worker.result if self._worker else None
        if isinstance(result, Exception):
            self.log.add(f"Прервано: {result}", "error")
            return
        messages, errors = result
        for line in messages:
            self.log.add(line, "ok")
        if errors:
            self.log.add("Готово, но с замечаниями:", "warn")
            for line in errors:
                self.log.add(f"  {line}", "error")
        else:
            self.log.add("Готово.", "ok")
        self._refresh()
        # Включение и выключение сервера меняют его состояние в таблице.
        # Перечитываем здесь, в главном потоке: из рабочего потока трогать
        # виджеты нельзя, а признак «включён» надо ставить по факту
        # записанного блока, а не по факту нажатия кнопки.
        if self._what == "registry":
            reg_dest = self._reg_dest()
            if reg_dest is not None:
                mcp_registry.mark_installed(self.reg_servers, reg_dest)
                self._reg_paint()

    def _base(self) -> Path:
        return core.app_root()

    def _install(self) -> None:
        dest = self._dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        selection = self._selection()
        pselection = self._pselection()
        if not selection and not pselection:
            self._warn("Ничего не отмечено — отметьте хотя бы одну галочку.")
            return
        base = self._base()

        def job(progress):
            m1, e1 = ([], [])
            m2, e2 = ([], [])
            if selection:
                m1, e1 = opencode_caps.install_caps(
                    base, dest, selection, progress=progress,
                    antiblock_opts=self._antiblock_opts(),
                )
            if pselection:
                m2, e2 = opencode_caps.install_providers(dest, pselection, progress=progress)
            return (m1 + m2, e1 + e2)

        self._start(job, "install")

    def _remove(self) -> None:
        dest = self._dest()
        if dest is None:
            self._warn("Не выбрана папка настроек opencode.")
            return
        selection = self._selection()
        pselection = self._pselection()
        if not selection and not pselection:
            self._warn("Ничего не отмечено — отметьте хотя бы одну галочку.")
            return

        def job(progress):
            m1, e1 = ([], [])
            m2, e2 = ([], [])
            if selection:
                m1, e1 = opencode_caps.remove_caps(dest, selection, progress=progress)
            if pselection:
                m2, e2 = opencode_caps.remove_providers(dest, pselection, progress=progress)
            return (m1 + m2, e1 + e2)

        self._start(job, "remove")

    def _warn(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Не получилось")
        box.setText(text)
        box.exec()


# ------------------------------------------------------------- вкладка «Программы»


#: Цвет заголовка состояния по самому состоянию. Три состояния — три цвета,
#: и четвёртого не бывает: программа либо стоит, либо нет, либо «нечем
#: проверять». Придумывать оттенок для четвёртого состояния нельзя, его
#: просто не существует.
_STATE_COLOR = {
    program_cards.STATE_OK: ui.OK,
    program_cards.STATE_MISSING: ui.WARN,
    program_cards.STATE_UNKNOWN: ui.TEXT_DIM,
}


class ProgramsTab(ScrollPage):
    """Программы, без которых мосты не работают: карточки, кнопки, состояние.

    Этап 5 раздела 7 плана. Вкладка **ничего не знает сама**: карточки
    приходят из `program_cards`, который читает реестр, а установку
    запускает `winget_install`. Здесь только вид и реакция на кнопки.

    У кнопки установки три честных ограничения, все три видны человеку:

    * **прав администратора вкладка не берёт сама.** Реестр помечает
      Blender и Adobe как `needs_admin`, но окно UAC не должно всплывать
      неожиданно (раздел 14.5 плана). Если winget упрётся в права, кнопка
      скажет об этом и даст готовую команду с `--scope machine`;
    * **сверка подписи не блокирует установку.** Файла установщика у нас
      нет, сверять нечего. Пакет ставит winget: он сверяет **хеш**
      установщика с манифестом своего источника, но подписанта
      (`expected_signer`) не сверяет — такого механизма у него нет. Наша
      сверка нужна, когда файл попадает к нам, то есть позже. Требовать
      её сейчас значило бы заблокировать кнопку навсегда;
    * **после установки состояние перечитывается по-настоящему**, а не
      считается успехом по коду возврата. winget местами возвращает 0,
      не сделав ничего.
    """

    def __init__(self, parent=None) -> None:
        self._worker: Worker | None = None
        self._cards: dict[str, program_cards.Card] = {}
        self._rows: dict[str, dict] = {}
        # Что сказать в строке результата после перерисовки. Без этого
        # итог установки пропал бы: перерисовка идёт сразу после неё и
        # стирает всё написанное, а человек увидел бы пустую строку
        # там, где должен узнать, что произошло.
        self._messages: dict[str, str] = {}
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)
        self.reload()

    # ---- сборка окна

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(ui.label("Программы", kind="title"))
        outer.addWidget(
            ui.label(
                "Здесь то, без чего мосты работать не будут. Карточки строятся "
                "из реестра: отдельного списка программ нет, иначе он сразу "
                "разошёлся бы с тем, что на самом деле стоит.",
                kind="dim",
                wrap=True,
            )
        )

        # Полоса общих замечаний. Сюда попадает и «данные поехали», и
        # «winget на машине нет» — обе вещи человек обязан увидеть до
        # того, как начнёт нажимать кнопки.
        self.notice = ui.label("", kind="dim", wrap=True)
        outer.addWidget(self.notice)

        self.btn_reload = QPushButton("Перепроверить всё")
        self.btn_reload.clicked.connect(self.reload)
        outer.addWidget(self.btn_reload)

        self.cards_box = QVBoxLayout()
        self.cards_box.setSpacing(10)
        outer.addLayout(self.cards_box)
        outer.addStretch(1)

    def _base(self) -> Path:
        """Папка программы, а не папка базы.

        Реестр `mcp-registry.json` лежит в папке `данные/` программы.
        `app_root()` возвращает базу пользователя
        (`DataBases/OpenCode_Base`), и вкладка читала бы несуществующий
        файл: карточки выходили бы именами серверов, без раздела
        «нужно мостам» и без единой кнопки установки. Отличие молчаливое —
        вкладка выглядела бы рабочей, показывая пустоту, — поэтому здесь
        написано прямо.
        """
        return core.program_root()

    # ---- наполнение

    def reload(self) -> None:
        """Перечитать реестр и перерисовать карточки.

        Порядок именно такой: сначала убрать старые карточки, потом
            нарисовать новые. Иначе при повторной проверке карточки
        накапливались бы и человек видел бы два одинаковых списка.
        """
        while self.cards_box.count():
            item = self.cards_box.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()
        self._rows.clear()

        base = self._base()
        problems = program_cards.section_problem(base)
        notes: list[str] = []
        kinds: list[str] = []
        if problems:
            notes.append("Данные о программах поехали: " + problems)
            kinds.append("error")
        if not winget_install.available():
            notes.append(
                "winget на этой машине нет, поэтому кнопки установки ничего "
                "не сделают. Он входит в Windows 10 версии 1809 и новее."
            )
            kinds.append("warn")
        text = "   ".join(notes)
        self.notice.setText(text)
        if kinds:
            self.notice.setStyleSheet(
                f"color: {ui.ERROR if 'error' in kinds else ui.WARN}")

        cards = program_cards.cards(base)
        self._cards = {card.key: card for card in cards}
        # Текст исчезнувших предметов не должен копиться: ключ мог
        # остаться от прежней версии реестра.
        alive = set(self._cards)
        for gone in [k for k in self._messages if k not in alive]:
            del self._messages[gone]
        names = program_cards.servers_by_name(base)
        if not cards:
            self.cards_box.addWidget(
                ui.label("Ни одной карточки. Реестр прочитан, но предметов "
                         "в нём нет — скажи об этом разработчику, "
                         "программа тут ни при чём.",
                         kind="warn", wrap=True)
            )
            return
        for card in cards:
            self.cards_box.addWidget(self._build_card(card, names))

    def _build_card(self, card: program_cards.Card, names: dict[str, str]) -> QWidget:
        """Одна карточка. Виджеты запоминаются: их надо гасить на время
        установки и заполнять заново по ходу дела."""
        box = QGroupBox(card.name)
        layout = QVBoxLayout(box)
        layout.setSpacing(6)

        head = ui.row()
        head.layout().addWidget(ui.label(card.name, kind="title"))
        self.status = ui.label(card.status)
        self.status.setStyleSheet(f"color: {_STATE_COLOR.get(card.state, ui.TEXT_DIM)}")
        head.layout().addWidget(self.status)
        head.layout().addStretch(1)
        layout.addWidget(head)

        needed = program_cards.needed_by_text(card, names)
        if needed:
            layout.addWidget(ui.label(needed, kind="dim", wrap=True))

        layout.addWidget(ui.label(card.state_text, wrap=True))

        pending = card.pending_text()
        if pending:
            layout.addWidget(ui.label(pending, kind="dim", wrap=True))

        # Совместимость версии — только строка. Ни state, ни can_install,
        # ни ready от неё не зависят: номер версии это сообщение человеку,
        # а не основание объявить программу неустановленной. Проверка на
        # это стоит в селфтесте отдельным пунктом.
        if card.compat.declared and card.compat.text:
            layout.addWidget(ui.label(card.compat.text,
                                      kind="warn" if card.compat.warning
                                      else "dim", wrap=True))

        if card.bridge.bundled:
            layout.addWidget(ui.label("Мост лежит внутри программы — отдельно "
                                      "ставить не нужно.", kind="dim", wrap=True))

        if card.alternatives:
            layout.addWidget(self._build_alternatives(card))

        layout.addWidget(ui.label(self._verify_text(card), kind="dim", wrap=True))

        if card.install.instructions:
            layout.addWidget(
                ui.label("Как это ставится: " + card.install.instructions,
                         kind="dim", wrap=True)
            )

        buttons = ui.row()
        row: dict[str, QPushButton] = {}
        for code, label, hint in card.buttons():
            btn = QPushButton(label)
            btn.setToolTip(hint)
            if code == program_cards.BTN_CHECK:
                btn.clicked.connect(lambda _=False, k=card.key: self._check_one(k))
            elif code == program_cards.BTN_INSTALL:
                btn.clicked.connect(lambda _=False, c=card: self._install_card(c))
                if not card.can_install:
                    btn.setEnabled(False)
            elif code == program_cards.BTN_FETCH:
                btn.clicked.connect(lambda _=False, c=card: self._fetch_card(c))
                if not card.can_fetch:
                    btn.setEnabled(False)
            elif code == program_cards.BTN_UPDATE:
                btn.clicked.connect(
                    lambda _=False, c=card: self._update_card(c))
                if not card.can_update:
                    btn.setEnabled(False)
            elif code == program_cards.BTN_ROLLBACK:
                btn.clicked.connect(
                    lambda _=False, c=card: self._rollback_card(c))
                if not card.can_rollback:
                    btn.setEnabled(False)
            elif code == program_cards.BTN_FOLDER:
                btn.clicked.connect(lambda _=False, p=card.exe_path: self._open_folder(p))
            elif code == program_cards.BTN_PAGE:
                btn.clicked.connect(
                    lambda _=False, u=card.install.official_url: self._open_page(u))
            row[code] = btn
            buttons.layout().addWidget(btn)
        buttons.layout().addStretch(1)
        layout.addWidget(buttons)

        # Строка результата: сюда пишется ход установки и итог. Пустая
        # строка — не «ошибок нет», а «здесь ещё ничего не делали».
        result = ui.label("")
        result.setWordWrap(True)
        result.setStyleSheet(f"color: {ui.TEXT_DIM}")
        layout.addWidget(result)

        self._rows[card.key] = {
            "box": box, "status": self.status, "result": result, "buttons": row,
        }
        remembered = self._messages.get(card.key)
        if remembered:
            result.setText(remembered)
        return box

    def _build_alternatives(self, card: program_cards.Card) -> QWidget:
        """Варианты программы — как их записал реестр.

        Своя кнопка установки есть только у варианта с методом winget, и
        кнопка честно пишет, что поставит, — потому что у BlueStacks и
        MEmu разный путь, и молчаливый общий список их смешал бы.
        """
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        layout.addWidget(ui.label("Варианты:", kind="dim"))
        for alt in card.alternatives:
            name = str(alt.get("program") or "?")
            method = str(alt.get("method") or "")
            winget_id = str(alt.get("winget_id") or "")
            note = str(alt.get("note") or "")
            if method == program_cards.METHOD_WINGET:
                text = f"{name} — ставится через winget: {winget_id}"
            elif method == "manual":
                text = f"{name} — кнопки нет, {note}".rstrip(", ")
            else:
                text = f"{name} — в реестре написано: {method or 'ничего'}"
            layout.addWidget(ui.label(text, kind="dim", wrap=True))
        return wrap

    @staticmethod
    def _verify_text(card: program_cards.Card) -> str:
        """Строка про подпись. Правду говорит только она.

        Подписант есть у одной программы — у Node.js. У остальных молчать
        нельзя: «проверено» без проверки хуже, чем «проверять не с чем».
        """
        verify = card.verify
        if not verify.can_check:
            if verify.expected_publisher:
                return (f"Подпись: сверять не с чем — имя подписанта неизвестно. "
                        f"Издатель из каталога winget — {verify.expected_publisher} — "
                        f"это другое поле. Имя появится, когда файл будет скачан.")
            return ("Подпись: сверять не с чем — имя подписанта неизвестно, "
                    "а файл установщика ещё не скачивали.")
        if verify.checked and verify.verified:
            return f"Подпись сходится: подписал {verify.expected_signer}."
        if verify.checked:
            return f"Подпись: {verify.detail}"
        return (f"Подпись: ждём подписанта {verify.expected_signer}, но файл "
                f"ещё не скачан — сверка не запускалась.")

    # ---- действия

    def _open_folder(self, exe_path: str) -> None:
        path = Path(exe_path)
        folder = path if path.is_dir() else path.parent
        if not folder.exists():
            QMessageBox.warning(self, "Папки нет",
                                f"Папки {folder} на машине нет.")
            return
        core.open_in_explorer(folder)

    def _open_page(self, url: str) -> None:
        core.open_url(url)

    def _check_one(self, key: str) -> None:
        """Перепроверить одну карточку.

        Карточки перерисовываются целиком: собирать разницу по одному
        полю — значит держать в виджетах состояние, которое тут же
        устареет. Одна карточка стоит дешевле, чем рассинхрон.
        """
        card = self._cards.get(key)
        if card is None:
            return
        self._set_result(key, "Перепроверяю…")
        self.reload()
        self._set_result(key, "Перепроверено")

    def _install_card(self, card: program_cards.Card) -> None:
        if not card.install.winget_id:
            self._set_result(card.key, "В реестре нет идентификатора winget — "
                                       "ставить нечем.")
            return
        if self._worker is not None and self._worker.isRunning():
            self._set_result(card.key, "Уже идёт установка — дождись её.")
            return
        if not self._ask_install(card):
            self._set_result(card.key, "Отказался — ничего не ставлю.")
            return

        self._set_busy(card.key, True)
        self._set_result(card.key, "Запускаю winget…")
        worker = Worker(
            lambda progress: winget_install.install(card.install.winget_id, progress),
            self,
        )
        worker.line.connect(
            lambda text, _kind, k=card.key: self._set_result(k, text))
        worker.finished.connect(lambda k=card.key, w=worker: self._install_done(k, w))
        self._worker = worker
        worker.start()

    def _fetch_card(self, card: program_cards.Card) -> None:
        """Докачать плагин моста. Молчание тут недопустимо.

        Качается файл из сети и раскладывается в папку чужой программы,
        поэтому человек должен знать до нажатия три вещи: что именно
        скачается, сколько это весит и куда попадёт. Все три — в вопросе.
        """
        path = bridges.obs_installed()
        if path is None:
            self._set_result(card.key,
                             "OBS не установлена — докачивать некуда.")
            return
        if self._worker is not None and self._worker.isRunning():
            self._set_result(card.key, "Уже идёт работа — дождись её.")
            return
        if not self._ask_fetch(card):
            self._set_result(card.key, "Отказался — ничего не качаю.")
            return
        self._set_busy(card.key, True)
        self._set_result(card.key, "Запускаю…")
        worker = Worker(
            lambda progress: bridges.ensure_plugin(Path(path), progress=progress),
            self,
        )
        worker.line.connect(
            lambda text, _kind, k=card.key: self._set_result(k, text))
        worker.finished.connect(lambda k=card.key, w=worker: self._fetch_done(k, w))
        self._worker = worker
        worker.start()

    def _ask_fetch(self, card: program_cards.Card) -> bool:
        """Спросить перед докачкой. Всегда, даже если кнопка активна."""
        lines = [f"Докачать плагин «{card.plugin.name}»?",
                 f"Как это будет: {card.plugin.hint}",
                 "Плагин кладётся в папку плагинов самой программы. "
                 "Ничего извне не ставится и ничего не удаляется."]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Докачать плагин")
        box.setText("\n\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _fetch_done(self, key: str, worker: Worker) -> None:
        result = worker.result
        if isinstance(result, tuple) and len(result) == 2:
            text = str(result[1])
        else:
            text = f"Докачка не закончилась: {result if result else 'неизвестно'}"
        self.reload()
        self._set_result(key, text)

    def _ask_install(self, card: program_cards.Card) -> bool:
        """Спросить перед установкой. Всегда, а не только при правах.

        Права администратора — не единственное, о чём человек должен
        знать заранее: после установки Adobe нужен вход в учётную
        запись, и об этом тоже лучше сказать до, чем после.
        """
        lines = [f"Поставить {card.install.program or card.name}?",
                 f"Идентификатор: {card.install.winget_id}"]
        if card.needs_admin:
            lines.append(
                "Установщик может запросить права администратора — появится "
                "окно Windows с вопросом. Программа сама эти права не берёт.")
        if card.hand_over:
            lines.append("После установки нужно будет войти в учётную запись "
                         "своими руками.")
        lines.append(
            "Подпись установщика я не сверял: файла у меня нет, пакет качает "
            "winget. Он сверяет хеш с манифестом источника, но подписанта "
            "по реестру не сверяет — этого он не умеет.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Установить программу")
        box.setText("\n\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _install_done(self, key: str, worker: Worker) -> None:
        result = worker.result
        if not isinstance(result, winget_install.InstallResult):
            self._set_busy(key, False)
            self._set_result(key, "Установка не закончилась: "
                                  f"{result if result else 'неизвестно'}")
            self.reload()
            return
        text = result.describe()
        card = self._cards.get(key)
        if result.needs_rights and card is not None and card.install.winget_id:
            text += ("\n winget упёрся в права. Запусти сам, от администратора:"
                     "\n " + winget_install.command_text(
                         card.install.winget_id, machine=True))
        self._set_busy(key, False)
        self._set_result(key, text)
        # Состояние перечитывается по-настоящему: успех кода возврата не
        # доказывает, что программа появилась.
        self.reload()

    # ------------------------------------------------- обновление и откат

    def _record_before_update(self, card: program_cards.Card) -> str:
        """Запомнить текущую версию ДО обновления.

        Вынесено отдельно, чтобы порядок был виден: вызов обязан стоять
        выше `worker.start()`, и это проверяется сравнением номеров строк в
        селфтесте. Возвращает пустую строку, если записать нечего.
        """
        base = self._base()
        current = card.update.installed
        if not base or not current or not card.servers:
            return ""
        said: list[str] = []
        for server_id in card.servers:
            ok, note = mcp_registry.remember_version(base, server_id, current)
            if not ok:
                said.append(note)
        return " ".join(said)

    def _update_card(self, card: program_cards.Card) -> None:
        """Кнопка «Обновить». Сначала живой вопрос источнику."""
        if not card.install.winget_id:
            self._set_result(card.key, "В реестре нет идентификатора winget — "
                                       "обновлять нечем.")
            return
        if self._worker is not None and self._worker.isRunning():
            self._set_result(card.key, "Уже идёт работа — дождись её.")
            return

        self._set_busy(card.key, True)
        self._set_result(card.key, "Спрашиваю источник…")
        state = winget_install.remote_state(card.install.winget_id)
        self._set_busy(card.key, False)
        if not state.seen:
            self._set_result(card.key, "Обновить не вышло: "
                                       + (state.note or "источник не ответил"))
            return
        if not state.update_available:
            if state.installed and not state.available:
                self._set_result(card.key, f"Обновлений нет: стоит "
                                           f"{state.installed}")
            elif state.installed and state.available:
                self._set_result(card.key,
                                 f"Обновлений нет: стоит {state.installed}, "
                                 f"в источнике {state.available}")
            else:
                self._set_result(card.key, "Обновлений нет, но версия "
                                           "источником не названа")
            return
        if not self._ask_update(card, state):
            self._set_result(card.key, "Отказался — ничего не обновляю.")
            return

        self._set_busy(card.key, True)
        # ПОРЯДОК ВАЖЕН: запись прежней версии идёт ДО запуска обновления.
        warning = self._record_before_update(card)
        if warning:
            self._set_result(card.key, "Прежнюю версию записать не удалось: "
                                       + warning + ". Обновляю, но возврат "
                                       "может не получиться.")
        else:
            self._set_result(card.key, f"Запомнил версию {state.installed}. "
                                       "Обновляю…")
        worker = Worker(
            lambda progress, pkg=card.install.winget_id:
                winget_install.upgrade(pkg, progress),
            self,
        )
        worker.line.connect(
            lambda text, _kind, k=card.key: self._set_result(k, text))
        worker.finished.connect(
            lambda k=card.key, w=worker, s=state: self._update_done(k, w, s))
        self._worker = worker
        worker.start()

    def _ask_update(self, card: program_cards.Card,
                    state: winget_install.RemoteState) -> bool:
        """Спросить перед обновлением. Всегда, даже если кнопка активна."""
        lines = [f"Обновить {card.install.program or card.name}?",
                 f"Идентификатор: {card.install.winget_id}",
                 f"Сейчас стоит: {state.installed}",
                 f"Источник предлагает: {state.available}"]
        if card.needs_admin:
            lines.append("Установщик может запросить права администратора — "
                         "появится окно Windows с вопросом.")
        lines.append("Прежняя версия будет записана ДО обновления, чтобы "
                     "возврат к ней был возможен.")
        lines.append("Если после обновления мост перестанет отвечать, верни "
                     "прежнюю версию кнопкой «Вернуть» в этой же карточке.")
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Обновить программу")
        box.setText("\n\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _update_done(self, key: str, worker: Worker,
                     state: winget_install.RemoteState) -> None:
        """Итог обновления. Версия записывается ПОСЕ — отдельной функцией."""
        result = worker.result
        self._set_busy(key, False)
        if not isinstance(result, winget_install.InstallResult):
            self._set_result(key, "Обновление не закончилось: "
                                  f"{result if result else 'неизвестно'}")
            self.reload()
            return
        text = result.describe()
        if result.needs_rights:
            text += ("\n winget упёрся в права. Запусти сам, от "
                     "администратора.")
        if result.ok and not result.already and state.available:
            # Что стоит ПОСЛЕ — проверяется живым запросом, а не берётся из
            # того, что обещал источник до обновления.
            after = winget_install.remote_state(self._winget_id_of(key))
            base = self._base()
            if base and after.seen and after.installed:
                ok, note = mcp_registry.set_installed_version(
                    base, self._server_id_of(key), after.installed,
                    note="снято живым `winget list` после обновления")
                text += f"\n {note}" if ok else f"\n версию записать не вышло: {note}"
            else:
                text += "\n новую версию снять не удалось — нажми «Проверить»"
        self._set_result(key, text)
        self.reload()

    def _rollback_card(self, card: program_cards.Card) -> None:
        """Кнопка «Вернуть прежнюю версию»."""
        target = card.rollback_target
        if not target:
            self._set_result(card.key, "Откатываться нечем: прежняя версия "
                                       "не записана.")
            return
        if not card.install.winget_id:
            self._set_result(card.key, "В реестре нет идентификатора winget — "
                                       "возвращаться некуда.")
            return
        if self._worker is not None and self._worker.isRunning():
            self._set_result(card.key, "Уже идёт работа — дождись её.")
            return
        self._set_busy(card.key, True)
        self._set_result(card.key, "Спрашиваю источник, есть ли эта версия…")
        offered = winget_install.version_offered(card.install.winget_id, target)
        self._set_busy(card.key, False)
        if not offered:
            self._set_result(card.key, f"Версии {target} больше нет в "
                                       "источнике: вернуться к ней не "
                                       "получится. Нажми «Проверить», чтобы "
                                       "увидеть, что доступно.")
            return
        if not self._ask_rollback(card, target):
            self._set_result(card.key, "Отказался — ничего не меняю.")
            return
        self._set_busy(card.key, True)
        self._set_result(card.key, f"Возвращаю {target}…")
        worker = Worker(
            lambda progress, pkg=card.install.winget_id, want=target:
                winget_install.install_version(pkg, want, progress),
            self,
        )
        worker.line.connect(
            lambda text, _kind, k=card.key: self._set_result(k, text))
        worker.finished.connect(
            lambda k=card.key, w=worker, want=target: self._rollback_done(k, w,
                                                                           want))
        self._worker = worker
        worker.start()

    def _ask_rollback(self, card: program_cards.Card, target: str) -> bool:
        """Спросить перед возвратом к прежней версии."""
        lines = [f"Вернуть {card.install.program or card.name} "
                 f"к версии {target}?",
                 f"Идентификатор: {card.install.winget_id}",
                 f"Сейчас стоит: {card.update.installed or 'не записана'}",
                 f"Источник эту версию предлагает — проверено только что.",
                 "Установщик может запросить права администратора."]
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Question)
        box.setWindowTitle("Вернуть прежнюю версию")
        box.setText("\n\n".join(lines))
        box.setStandardButtons(QMessageBox.StandardButton.Yes
                               | QMessageBox.StandardButton.No)
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def _rollback_done(self, key: str, worker: Worker, target: str) -> None:
        """Итог возврата. Версия записывается ПОСЕ."""
        result = worker.result
        self._set_busy(key, False)
        if not isinstance(result, winget_install.InstallResult):
            self._set_result(key, "Возврат не закончился: "
                                  f"{result if result else 'неизвестно'}")
            self.reload()
            return
        text = result.describe()
        if result.ok and not result.already:
            base = self._base()
            ok, note = mcp_registry.set_installed_version(
                base, self._server_id_of(key), target,
                note=f"записано после возврата к версии {target}")
            text += f"\n {note}" if ok else f"\n версию записать не вышл: {note}"
        self._set_result(key, text)
        self.reload()

    def _server_id_of(self, key: str) -> str:
        """Первый сервер карточки. Версии пишутся ему — по одному."""
        card = self._cards.get(key)
        return card.servers[0] if card and card.servers else ""

    def _winget_id_of(self, key: str) -> str:
        card = self._cards.get(key)
        return card.install.winget_id if card else ""

    def _set_busy(self, key: str, busy: bool) -> None:
        """Гасить кнопки на время работы.

        Пока идёт установка, карточка не должна принимать вторую команду:
        две установки одного пакета winget запускать нельзя, и человек
        получит ошибку вместо результата. По той же причине гаснут все
        кнопки карточки, а не только «Установить» — иначе человек успеет
        нажать «Проверить» посреди установки и увидит «не установлена».
        """
        row = self._rows.get(key)
        if row is None:
            return
        card = self._cards.get(key)
        for code, btn in row["buttons"].items():
            if busy:
                btn.setEnabled(False)
            elif code == program_cards.BTN_INSTALL:
                btn.setEnabled(bool(card and card.can_install))
            elif code == program_cards.BTN_FETCH:
                # Возврат после работы — по решению карточки, а не всегда
                # «включено»: кнопка докачки выключена там, где папка
                # закрыта, и после работы её включить нельзя.
                btn.setEnabled(bool(card and card.can_fetch))
            else:
                btn.setEnabled(True)

    def _set_result(self, key: str, text: str) -> None:
        """Написать в строку результата и запомнить на будущее.

        Запоминание обязательно: карточки перерисовываются целиком, и
        написанное здесь иначе пропадало бы при первой же перерисовке —
        то есть сразу после установки.
        """
        self._messages[key] = text
        row = self._rows.get(key)
        if row is None:
            return
        row["result"].setText(text)
        self.refresh_height()

    # ---- показ своего состояния

    def showEvent(self, event) -> None:  # noqa: D102
        super().showEvent(event)
        self.refresh_height()


# ---------------------------------------------------------------- вкладка «Инструкция»


class HelpTab(ScrollPage):
    """Детальная инструкция: как пользоваться программой."""

    def __init__(self, parent=None) -> None:
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    @staticmethod
    def _section(text: str) -> QLabel:
        label = ui.label(text, kind="title")
        label.setStyleSheet(f"font-size: 14px; font-weight: 600; color: {ui.ACCENT};")
        return label

    @staticmethod
    def _step(text: str) -> QLabel:
        label = QLabel(text)
        label.setWordWrap(True)
        label.setStyleSheet(f"color: {ui.TEXT};")
        return label

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(8)

        outer.addWidget(ui.label("Как пользоваться программой", kind="title"))

        outer.addWidget(self._section("1. Что такое эта программа"))
        outer.addWidget(self._step(
            "Это конструктор баз данных для OpenCode. Она создаёт новые базы по "
            "встроенному шаблону, подключает их к OpenCode и помогает управлять "
            "обходом блокировок. Сама базы не хранит — они лежат отдельно на "
            "вашем компьютере (папка DataBases на рабочем столе)."
        ))

        outer.addWidget(self._section("2. Первый запуск"))
        outer.addWidget(self._step(
            "При первом запуске программа создаёт папку DataBases на рабочем "
            "столе и показывает окно-приветствие. Дальше новые базы можно "
            "складывать туда же — программа сама будет находить их кнопкой "
            "«Сканировать»."
        ))

        outer.addWidget(self._section("3. Создать новую базу"))
        outer.addWidget(self._step(
            "Первая вкладка «Создать новую базу»:\n"
            "1) выберите папку, где создать базу (по умолчанию — DataBases);\n"
            "2) введите имя базы (латиницей или русским);\n"
            "3) нажмите «Создать» — программа развернёт базу со всей "
            "файловой структурой, шаблонами, скиллами и инструкциями."
        ))
        outer.addWidget(self._step(
            "База — это папка с текстовыми файлами: память, библиотека знаний, "
            "скиллы, инструкции. Скиллы при создании берутся из встроенного "
            "шаблона, вместе с индексом сценариев (skills-index.json) — "
            "нейросеть читает его и сама выбирает нужный скилл по задаче."
        ))

        outer.addWidget(self._section("4. Подключить существующую базу"))
        outer.addWidget(self._step(
            "Вторая вкладка «Подключить существующую»:\n"
            "1) нажмите «Сканировать» — программа найдёт все базы с файлом-"
            "идентификатором .opencode-base.json на рабочем столе и в DataBases;\n"
            "2) выбазу из списка или укажите папку вручную через «Обзор…»;\n"
            "3) нажмите «Подключить» — файлы базы скопируются в настройки "
            "OpenCode, прежние файлы уйдут в _previous-version."
        ))

        outer.addWidget(self._section("5. Мост NCP"))
        outer.addWidget(self._step(
            "Третья вкладка «Мост NCP — создать»: создаёт переводчик между "
            "OpenCode и библиотекой знаний NCP. Мост умеет 7 операций: "
            "ncp_status (состояние), ncp_search (поиск), ncp_save (сохранить "
            "запись), ncp_read (прочитать), ncp_update (изменить), "
            "ncp_checkpoint (контрольная точка), ncp_reindex (пересобрать "
            "индекс). Путь к библиотеке подставляется автоматически."
        ))

        outer.addWidget(self._section("6. Возможности для OpenCode (вкладка «opencode»)"))
        outer.addWidget(self._step(
            "Здесь ставятся в настройки OpenCode только отмеченные возможности:\n"
            "— команда /голос (голосовой ввод);\n"
            "— мост ПК (файлы, скриншоты, список программ);\n"
            "— мост памяти NCP (7 инструментов);\n"
            "— 12 агентов (поиск, план, код, проверка и другие);\n"
            "— обход блокировок (запуск OpenCode через прокси)."
        ))

        outer.addWidget(self._section("7. Обход блокировок"))
        outer.addWidget(self._step(
            "Обход работает только для запущенного через ярлык OpenCode.\n"
            "Каналы по приоритету:\n"
            "1) V2Ray — свой Xray (узлы из встроенных VLESS-подписок, выбирается "
            "быстрейший);\n"
            "2) публичные прокси (список обновляется автоматически, источников 6);\n"
            "3) защищённый DNS (DoH: Google, Cloudflare, Quad9, AdGuard, Yandex, "
            "NextDNS, CleanBrowsing, Mullvad).\n"
            "Фасад сам выбирает канал по скорости и пишет подробный лог в окно "
            "запуска: видно, через что идёт трафик и что отвалилось."
        ))

        outer.addWidget(self._section("8. Темы (вкладка «Темы»)"))
        outer.addWidget(self._step(
            "Папка themes/ в конструкторе и в базе. Заготовка темы OpenCode — "
            "Markdown-файл с полями: цвета, шрифт, размер. Скопируйте заготовку, "
            "заполните своими значениями и попросите ассистента применить тему."
        ))

        outer.addWidget(self._section("9. Перенос на другой компьютер"))
        outer.addWidget(self._step(
            "Скопируйте папку базы целиком, установите Python с PyQt6, запустите "
            "«Управление-базой.cmd». Программа сама найдёт базу, подключит мосты "
            "и настроит OpenCode. Ничего вручную прописывать не нужно."
        ))

        outer.addWidget(self._section("10. Если что-то не работает"))
        outer.addWidget(self._step(
            "— OpenCode не видит базу → проверьте memory-base-path.txt и "
            "перезапустите OpenCode;\n"
            "— обход не подключается → запустите ярлык «OpenCode (обход)», "
            "посмотрите лог в окне;\n"
            "— мост NCP не отвечает → проверьте library_path в "
            "tools/ncp-bridge/config.json;\n"
            "— программа не открывается → нужен Python 3 с пакетом PyQt6."
        ))


# ---------------------------------------------------------------- вкладка «Темы»


class ThemesTab(ScrollPage):
    """Темы оформления OpenCode — папка в корне главной базы.

    Показывает файлы тем (Markdown-заготовки) из папки themes/ главной
    базы и даёт открыть её в проводнике. Сами темы сюда не применяются:
    вкладка — это просмотр и место, откуда удобно взять заготовку.
    """

    def __init__(self, parent=None) -> None:
        inner = QWidget()
        super().__init__(inner, parent)
        self._page = inner
        self._build(inner)

    @staticmethod
    def themes_dir() -> Path:
        """Папка тем в корне главной базы."""
        return core.program_root() / "themes"

    def _build(self, page: QWidget) -> None:
        outer = QVBoxLayout(page)
        outer.setContentsMargins(14, 14, 14, 14)
        outer.setSpacing(10)

        outer.addWidget(
            ui.label(
                "Темы оформления OpenCode. Выберите тему и нажмите «Применить» — "
                "она скопируется в настройки OpenCode и вступит в силу после "
                "перезапуска.",
                kind="dim",
                wrap=True,
            )
        )

        box = QGroupBox("Доступные темы")
        box_layout = QVBoxLayout(box)
        self.list_ = QListWidget()
        box_layout.addWidget(self.list_, 1)
        hint = ui.label(
            "Встроенные темы — из конструктора. Свои — из папки themes/ базы.",
            kind="dim",
            wrap=True,
        )
        box_layout.addWidget(hint)
        outer.addWidget(box, 1)

        row = QHBoxLayout()
        self.btn_apply = QPushButton("Применить")
        self.btn_apply.clicked.connect(self._apply_theme)
        self.btn_open = QPushButton("Открыть папку тем")
        self.btn_open.clicked.connect(self._open_folder)
        self.btn_refresh = QPushButton("Обновить список")
        self.btn_refresh.clicked.connect(self.refresh)
        row.addWidget(self.btn_apply)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_refresh)
        row.addStretch(1)
        outer.addLayout(row)

        self.refresh()

    def refresh(self) -> None:
        """Обновляет список тем: встроенные (конструктор) + свои (база)."""
        self.list_.clear()
        seen: set[str] = set()
        for folder in (core.program_root() / "themes", core.app_root() / "themes"):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob("*.json")):
                if path.stem in seen:
                    continue
                seen.add(path.stem)
                try:
                    import json
                    data = json.loads(path.read_text(encoding="utf-8"))
                    # Новый формат: имеет defs и theme
                    if "defs" in data and "theme" in data:
                        name = path.stem
                        mode = "dark"  # новый формат поддерживает и dark, и light
                    # Старый формат: имеет name, mode, colors
                    else:
                        name = str(data.get("name") or path.stem)
                        mode = str(data.get("mode") or "dark")
                except (OSError, ValueError):
                    name, mode = path.stem, "dark"
                item = QListWidgetItem(f"{name}  ({mode})")
                item.setData(1000, str(path))
                item.setToolTip(str(path))
                self.list_.addItem(item)
        if not self.list_.count():
            self.list_.addItem("(тем нет — добавьте JSON-файл в папку themes)")

    def _apply_theme(self) -> None:
        item = self.list_.currentItem()
        if item is None:
            self._warn("Сначала выберите тему из списка.")
            return
        path = Path(item.data(1000))
        if not path.is_file():
            self._warn(f"Файл темы не найден:\n{path}")
            return
        ok, message = core.apply_theme(path)
        if ok:
            QMessageBox.information(self, "Тема применена", message)
        else:
            self._warn(message)

    def _open_folder(self) -> None:
        folder = self.themes_dir()
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        if not folder.is_dir():
            self._warn(f"Папку тем не удалось создать:\n{folder}")
            return
        core.open_in_explorer(folder)

    def _warn(self, text: str) -> None:
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Не получилось")
        box.setText(text)
        box.exec()


# ---------------------------------------------------------------- окно


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("Управление базой")
        self.setMinimumSize(720, 560)
        self.resize(880, 900)

        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(10)
        layout.addWidget(ui.label("Управление базой", kind="title"))
        layout.addWidget(
            ui.label(
                "База — это папка с текстовыми файлами памяти, правил и скиллов.",
                kind="dim",
            )
        )

        tabs = QTabWidget()
        self.tabs = tabs
        self.create_tab = CreateTab()
        self.import_tab = ImportTab()
        self.bridge_tab = BridgeTab()
        self.caps_tab = CapsTab()
        self.programs_tab = ProgramsTab()
        self.themes_tab = ThemesTab()

        # Импорт вкладки «Обновление» — здесь, а не вверху файла. Вкладка
        # берёт `ScrollPage` и `Worker` из этого модуля, и импорт наверху
        # дал бы круг: main не успел бы дойти до определения класса,
        # а update_tab уже просил бы его. Измеренная ошибка была ровно
        # такой: «cannot import name 'ScrollPage' from partially
        # initialized module 'main'». Внутри метода main уже загружен
        # целиком, и круг разрывается.
        from update_tab import UpdateTab  # noqa: PLC0415

        self.update_tab = UpdateTab()

        self.help_tab = HelpTab()
        tabs.addTab(self.create_tab, "Создать новую базу")
        tabs.addTab(self.import_tab, "Подключить существующую")
        tabs.addTab(self.bridge_tab, "Мост NCP — создать")
        tabs.addTab(self.caps_tab, "opencode")
        tabs.addTab(self.programs_tab, "Программы")
        tabs.addTab(self.themes_tab, "Темы")
        tabs.addTab(self.update_tab, "Обновление")
        tabs.addTab(self.help_tab, "Инструкция")
        layout.addWidget(tabs, 1)

        self.create_tab.base_ready.connect(self._suggest_import)
        self.create_tab.base_ready.connect(self._suggest_bridge)

    def closeEvent(self, event) -> None:  # noqa: N802 — имя из Qt
        """Окно закрывает человек, а поток скачивания живёт отдельно.

        Здесь выставляется флаг отмены, а поток сам доходит до безопасной
        точки и убирает недокачанный файл. Принудительная остановка
        потока здесь не годилась бы: она прервала бы запись посреди
        файла, и следующий запуск принял бы его за годный архив — он
        лежит во временной папке под тем же именем.
        """
        tab = getattr(self, "update_tab", None)
        if tab is not None and hasattr(tab, "stop"):
            tab.stop()
        super().closeEvent(event)

    def _suggest_bridge(self, path: str) -> None:
        """После создания базы предлагает завести мост NCP.

        Смысл: без моста программа библиотеку не видит — она умеет
        работать только со своими файлами. Лучше сказать об этом сразу,
        пока человек ещё здесь, чем потом искать причину.
        """
        if not path:
            return
        base = Path(path)
        if not core.library_ready(base):
            return

        # путь к новой библиотеке подставляем в любом случае: пригодится,
        # даже если сейчас человек откажется
        self.bridge_tab.prepare(core.library_dir(base))

        if core.find_bridges():
            # мост уже есть — молча готовим вкладку, спрашивать не о чем
            return

        answer = QMessageBox.question(
            self,
            "Нужен мост NCP",
            "База создана.\n\n"
            "Чтобы нейросеть увидела библиотеку, нужен мост NCP — "
            "маленькая программа-переводчик между программой и папкой "
            "с записями. Без него библиотека остаётся просто файлами, "
            "которые никто не читает.\n\n"
            "Создать мост сейчас?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.Yes,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self.tabs.setCurrentIndex(2)

    def _suggest_import(self, path: str) -> None:
        """После создания предлагаем сразу подключить эту же базу.

        Смысл всей затеи: создали базу на первой вкладке — она сразу
        подставлена на второй, искать путь руками не нужно.
        """
        if not path:
            return
        folder = Path(path)
        if not folder.is_dir():
            return

        # обновляем список созданных баз и выделяем в нём новую
        self.import_tab._fill_mine()
        chosen = None
        for index in range(self.import_tab.mine_list.count()):
            item = self.import_tab.mine_list.item(index)
            stored = str(item.data(1000) or "")
            if not stored:
                continue
            try:
                same = Path(stored).resolve() == folder.resolve()
            except OSError:
                same = stored.lower() == str(folder).lower()
            if same:
                chosen = index
                break
        if chosen is not None:
            self.import_tab.mine_list.setCurrentRow(chosen)

        # путь всё равно подставляем: список мог оказаться пустым
        if self.import_tab.source_edit.text().strip() != str(folder):
            self.import_tab.source_edit.setText(str(folder))
        self.import_tab._check_source()

        self.tabs.setCurrentIndex(1)
        self.import_tab.mine_hint.setStyleSheet(f"color: {ui.OK};")
        self.import_tab.mine_hint.setText(
            "База создана. Она уже выбрана — осталось отметить программу "
            "и нажать «Подключить»."
        )


# ------------------------------------------------- единственный экземпляр

#: Имя локального сокета, которым программа занимает место единственного
#: экземпляра.
APP_SOCKET_NAME = "opencode-base-dbapp"

#: Сервер держим в переменной модуля: если он попадёт в сборщик мусора,
#: сокет закроется и защита перестанет работать.
_SINGLE_SERVER = None
_SINGLE_WINDOW = None


def _socket_name() -> str:
    """Имя сокета с учётом пользователя.

    На одном компьютере могут работать два разных человека, и общее
    имя заставило бы их мешать друг другу.
    """
    try:
        user = os.environ.get("USERNAME") or getpass.getuser()
    except Exception:  # noqa: BLE001 - имя пользователя не повод не открыться
        user = "user"
    return f"{APP_SOCKET_NAME}-{user or 'user'}"


def _lock_path() -> Path:
    """Путь файла-метки «занято»."""
    import tempfile

    name = _socket_name()
    return Path(tempfile.gettempdir()) / f"{name}.lock"


def _pid_alive(pid: int) -> bool:
    """Жив ли процесс с таким номером.

    os.kill(pid, 0) на Windows вызывает TerminateProcess, а это
    убийство, поэтому здесь спрашиваем у системы через OpenProcess.
    """
    if pid <= 0:
        return False
    if os.name != "nt":
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True
    import ctypes

    query = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
    still_active = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(query, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        if kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return code.value == still_active
        return True
    except Exception:  # noqa: BLE001
        return True
    finally:
        kernel32.CloseHandle(handle)


def _acquire_instance_lock() -> bool:
    """Занять метку экземпляра. True — место свободно, оно наше.

    Метка создаётся одной операцией O_EXCL: выиграть может ровно один,
    даже если десять процессов стартуют в одну миллисекунду. Один
    локальный сокет такой гарантии не даёт — три копии успевали
    создать слушатель по очереди и все три считали себя первыми.
    """
    path = _lock_path()
    for _ in range(3):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                holder = int(path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                holder = 0
            if holder and holder != os.getpid() and _pid_alive(holder):
                return False
            # метка осталась от аварийно убитого запуска — забираем
            try:
                path.unlink()
            except OSError:
                return False
            continue
        except OSError:
            return True  # метку создать нельзя — не блокируем запуск
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(str(os.getpid()))
        return True
    return False


def _raise_window() -> None:
    """Показать уже открытое окно."""
    window = _SINGLE_WINDOW
    if window is None:
        return
    try:
        window.showNormal()
        window.raise_()
        window.activateWindow()
    except RuntimeError:
        # окно уже закрыто, а сокет ещё жив — просто молча выходим
        pass


def claim_single_instance() -> bool:
    """Занять место единственного экземпляра.

    True — мы первые и должны открыть окно. False — программа уже
    запущена: её окно показано, а этот процесс тихо завершается.

    Зачем. Окружение на этой машине удваивает запуск любого Python:
    один и тот же процесс появляется дважды, и повторяется это даже
    для пустого скрипта, который только спит. Программа с этим ничего
    не может поделать — зато может не показывать лишние окна. Без
    защиты один щелчок по ярлыку открывал три-четыре окна.
    """
    global _SINGLE_SERVER, _SINGLE_WINDOW
    # Метка проверяется первой и решает всё: если место занято, второй
    # экземпляр должен показать чужое окно и выйти.
    if not _acquire_instance_lock():
        return False
    try:
        from PyQt6.QtNetwork import QLocalServer, QLocalSocket
    except ImportError:
        # Qt без сети: метку уже взяли, окно всё равно откроется
        return True

    name = _socket_name()
    probe = QLocalSocket()
    probe.connectToServer(name)
    if probe.waitForConnected(400):
        probe.write(b"show")
        probe.flush()
        probe.waitForBytesWritten(400)
        probe.disconnectFromServer()
        return False

    # Сокета нет: значит мы первые. Остатки от аварийно убитого
    # прежнего запуска убираем, иначе сервер не поднимется.
    QLocalServer.removeServer(name)
    server = QLocalServer()
    server.setSocketOptions(
        QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(name):
        # Занять не вышло: лучше открыться, чем молча не показаться
        return True

    def _on_connection() -> None:
        conn = server.nextPendingConnection()
        if conn is None:
            return
        conn.readyRead.connect(_raise_window)
        conn.disconnected.connect(conn.deleteLater)

    server.newConnection.connect(_on_connection)
    _SINGLE_SERVER = server
    return True


def run() -> int:
    app = QApplication(sys.argv)
    app.setApplicationName("Управление базой")
    ui.apply_dark_theme(app)

    if not claim_single_instance():
        return 0

    if not core.first_run_done():
        folder = core.ensure_data_bases_folder()
        from ui_first_run import FirstRunDialog

        dlg = FirstRunDialog(folder)
        dlg.exec()
        core.mark_first_run_done()

    global _SINGLE_WINDOW
    window = MainWindow()
    _SINGLE_WINDOW = window
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(run())
