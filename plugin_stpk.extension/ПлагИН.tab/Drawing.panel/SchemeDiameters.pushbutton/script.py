# -*- coding: utf-8 -*-
"""«СхемаДиаметров» - плагин №6 набора plugin_stpk (IronPython 2.7).

Рисует чертёжный вид со схемой: какими диаметрами тянуть трубы до
потребителей в санузлах (и других помещениях). Помещения берутся из
СВЯЗАННОЙ архитектуры (RevitLinkInstance.GetLinkDocument); конфигурация
потребителей и входных диаметров запоминается по проекту (JSON в
%APPDATA%\\ПлагИН\\SchemeDiameters).

Схема: слева вертикальный прямоугольник основы (текст повёрнут), от
него линии «stpk_ХВС» по помещениям с потребителями; у основы подпись
φ вводного диаметра; помещение - пунктирная рамка «<Невидимые линии>»
с подписью «Имя, пом. N, этаж» (подчёркнутой), внутри потребители
(имя + φ16/φ20) с отводами к линии. Вид с тем же именем перезаписывается.

API-опора (2022, проверено по справочнику):
- ViewDrafting.Create(doc, viewFamilyTypeId) - чертёжный вид;
- TextNote.Create(..., ширина, TextNoteOptions) - перенос двух слов
  делает сам; TextNoteOptions.Rotation - повёрнутый текст;
- TextNote.GetFormattedText + FormattedText.SetUnderlineStatus +
  SetFormattedText - подчёркивание подписей помещений;
- DetailCurve.LineStyle - стиль линии (stpk_ХВС и пунктир).
"""

import io
import json
import math
import os
import re

from pyrevit import revit, forms, script, DB

# WPF-обёртки (Revit уже загрузил PresentationFramework)
from System.Windows import TextWrapping, Thickness, VerticalAlignment, Visibility
from System.Windows.Input import Key, Keyboard
from System.Windows.Media import Color, SolidColorBrush
from System.Windows.Controls import (Border, CheckBox, ComboBox, Orientation,
                                     StackPanel, TextBlock,
                                     TextChangedEventHandler, TextBox)
from System.Windows.Controls.Primitives import TextBoxBase

logger = script.get_logger()

doc = revit.doc

if not doc:
    forms.alert(
        "Откройте проект Revit.",
        title="Нет документа",
        exitscript=True,
    )


# ---------- константы ----------
MM = 1.0 / 304.8  # мм -> футы (внутренние единицы)

# масштаб текущего чертёжного вида: Revit умножает ШИРИНУ обёртки
# записки на масштаб вида (наблюдение 19.11: вид 1:10 - записка
# в 10 раз шире заданной) - компенсируем делением
_view_scale = 1

STANDARD_CONSUMERS = [
    u"Раковина",
    u"Мойка",
    u"Посудомоечная машина",
    u"Стиральная машина",
    u"Унитаз",
    u"Гигиенический душ",
    u"Душ",
    u"Ванная",
    u"Робот пылесос",
    u"Обливное устройство",
]
DEFAULT_CONSUMER = u"Раковина"
DEFAULT_DIAM = 16

FLOOR_MAP = {
    u"ГП0": u"цок. эт.",
    u"ГП1": u"1 эт.",
    u"ГП2": u"2 эт.",
    u"ГП3": u"3 эт.",
}

TEXT_SMALL = u"ADSK_Основной текст_3.5"
TEXT_BIG = u"ADSK_Основной текст_5"
TITLE_TEXT = u"Схема диаметров холодной воды"

# геометрия упрощённой текстовой схемы (мм чертёжного вида)
X_RIGHT_ROOM = 600.0    # правый край «текстпомещение» (выравнивание)
GAP_ROOM_CONS = 60.0    # зазор «текстпомещение»/«текстприборы»
X_LEFT_CONS = X_RIGHT_ROOM + GAP_ROOM_CONS  # левый край «текстприборы»

BG_HAS = None       # заполним после загрузки WPF (кисти)
BG_SELECTED = None
BG_PLAIN = None


# ---------- утилиты ----------
def element_name_safe(el, fallback_bps=()):
    """Имя элемента; .Name в IronPython иногда недоступен - фолбэк на
    BuiltInParameter-параметры (урок плагинов 1-3)."""
    if el is None:
        return u""
    try:
        n = el.Name
        if n:
            return n
    except Exception:
        pass
    for bp in fallback_bps:
        try:
            p = el.get_Parameter(bp)
            if p is not None:
                v = p.AsString()
                if v:
                    return v
        except Exception:
            pass
    return u""


def natural_key(text):
    """Ключ «естественной» сортировки: числа сравниваются как числа."""
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", text)]


def floor_label(level_name):
    """ГП0/ГП(0)/ГП (0)/ГП-0 -> «цок. эт.» и т.д.; вне списка - как есть."""
    m = re.search(u"ГП\\s*[\\(\\-]?\\s*(\\d+)", level_name or u"")
    if m:
        return FLOOR_MAP.get(u"ГП" + m.group(1), u"ГП" + m.group(1))
    return level_name


def floor_sort_key(level_name):
    """Порядок этажей: ГП0..ГП3, остальное - в конец, по имени."""
    m = re.search(u"ГП\\s*[\\(\\-]?\\s*(\\d+)", level_name or u"")
    if m:
        return (0, int(m.group(1)), u"")
    return (1, 0, level_name or u"")


def room_label(room):
    """Строка списка/подписи: «Санузел, пом. 3, 2 эт.»."""
    parts = [room["name"]]
    if room["number"]:
        parts.append(u"пом. {}".format(room["number"]))
    parts.append(room["floor"] or u"?")
    return u", ".join(parts)


def room_key(room):
    """Устойчивый ключ помещения для конфигурации (переживает
    перезагрузку связи: номер + имя + уровень)."""
    return u"{}|{}|{}".format(room["number"], room["name"], room["level"])


def parse_scale(text):
    """«1:12» / «12» -> 12; без вариантов - None."""
    t = (text or u"").strip().replace(u",", u".").replace(u" ", u"")
    m = re.match(u"^1:(\\d+)$", t) or re.match(u"^(\\d+)$", t)
    if not m:
        return None
    try:
        n = int(m.group(1))
    except Exception:
        return None
    return n if n > 0 else None


# ---------- сбор помещений ----------
def collect_rooms():
    """Помещения всех загруженных связей архитектуры, отсортированные:
    этаж (ГП0..ГП3) -> номер -> имя."""
    rooms = []
    seen = set()
    for link in DB.FilteredElementCollector(doc).OfClass(
            DB.RevitLinkInstance).ToElements():
        ldoc = link.GetLinkDocument()
        if ldoc is None:
            continue  # связь не загружена
        for r in DB.FilteredElementCollector(ldoc).OfCategory(
                DB.BuiltInCategory.OST_Rooms).WhereElementIsNotElementType(
                ).ToElements():
            name = element_name_safe(r, (DB.BuiltInParameter.ROOM_NAME,))
            number = element_name_safe(r, (DB.BuiltInParameter.ROOM_NUMBER,))
            if not name and not number:
                continue
            level_name = u""
            try:
                lvl = ldoc.GetElement(r.LevelId)
                if lvl is not None:
                    level_name = element_name_safe(lvl)
            except Exception:
                pass
            room = {"name": name, "number": number,
                    "level": level_name,
                    "floor": floor_label(level_name)}
            key = room_key(room)
            if key in seen:
                continue
            seen.add(key)
            rooms.append(room)
    rooms.sort(key=lambda r: (floor_sort_key(r["level"]),
                              natural_key(r["number"]),
                              natural_key(r["name"])))
    return rooms


# ---------- конфигурация (JSON по проекту) ----------
def config_path():
    title = u"без имени"
    try:
        if doc.Title:
            title = doc.Title
    except Exception:
        pass
    safe = re.sub(r"[^A-Za-zА-Яа-яЁё0-9_-]+", "_", title)
    folder = os.path.join(os.environ.get("APPDATA", "."),
                          u"ПлагИН", u"SchemeDiameters")
    if not os.path.isdir(folder):
        try:
            os.makedirs(folder)
        except Exception:
            pass
    return os.path.join(folder, safe + u".json")


def load_config():
    try:
        with io.open(config_path(), "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def save_config(cfg):
    try:
        with io.open(config_path(), "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=1)
    except Exception as exc:
        logger.debug("SchemeDiameters: не удалось сохранить конфиг: %s", exc)


# ---------- поиск ресурсов проекта ----------
def find_text_type_id(name):
    """Id типа текста по имени (TextNoteType); сравнение без
    лишних пробелов, фолбэк - параметр ALL_MODEL_TYPE_NAME."""
    target = (name or u"").strip()
    try:
        for t in DB.FilteredElementCollector(doc).OfClass(
                DB.TextNoteType).ToElements():
            nm = element_name_safe(
                t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
            if nm.strip() == target:
                return t.Id
    except Exception:
        pass
    return None


def find_drafting_type_id():
    """Id типа вида «Чертёжный» (ViewFamily.Drafting)."""
    try:
        for t in DB.FilteredElementCollector(doc).OfClass(
                DB.ViewFamilyType).ToElements():
            try:
                if t.ViewFamily == DB.ViewFamily.Drafting:
                    return t.Id
            except Exception:
                continue
    except Exception:
        pass
    return None


def missing_assets():
    """Список отсутствующих ресурсов проекта (не рисуем без них)."""
    missing = []
    if find_text_type_id(TEXT_SMALL) is None:
        missing.append(u"тип текста «{}»".format(TEXT_SMALL))
    if find_text_type_id(TEXT_BIG) is None:
        missing.append(u"тип текста «{}»".format(TEXT_BIG))
    if find_drafting_type_id() is None:
        missing.append(u"тип вида «Чертёжный»")
    return missing


def delete_view_by_name(name):
    """Удаление существующих видов с данным именем (перезапись);
    поиск по-элементно устойчив к сбоям чтения имён.
    Возвращает (сколько удалили, текст последней ошибки или None)."""
    count = 0
    last_error = None
    try:
        views = DB.FilteredElementCollector(doc).OfClass(
            DB.View).WhereElementIsNotElementType().ToElements()
    except Exception:
        views = []
    target = (name or u"").strip()
    for v in views:
        try:
            if v.IsTemplate:
                continue
        except Exception:
            continue
        nm = element_name_safe(v, (DB.BuiltInParameter.VIEW_NAME,))
        if nm.strip() != target:
            continue
        try:
            doc.Delete(v.Id)
            count += 1
        except Exception as exc:
            last_error = str(exc)
            logger.debug("SchemeDiameters: не удалить вид «%s»: %s", nm, exc)
    return count, last_error


def views_with_name(name):
    """Сколько видов с данным именем осталось в проекте."""
    target = (name or u"").strip()
    count = 0
    try:
        for v in DB.FilteredElementCollector(doc).OfClass(
                DB.View).WhereElementIsNotElementType().ToElements():
            nm = element_name_safe(v, (DB.BuiltInParameter.VIEW_NAME,))
            if not v.IsTemplate and nm.strip() == target:
                count += 1
    except Exception:
        pass
    return count


def stale_view_names(prefix):
    """Имена видов-«хвостов» прошлых запусков: начинаются с
    «prefix_СТАРАЯ» (например, «СД В1_СТАРАЯ», «СД В1_СТАРАЯ2»)."""
    names = set()
    tag = (prefix or u"").strip() + u"_СТАРАЯ"
    try:
        for v in DB.FilteredElementCollector(doc).OfClass(
                DB.View).WhereElementIsNotElementType().ToElements():
            try:
                if v.IsTemplate:
                    continue
            except Exception:
                continue
            nm = element_name_safe(v, (DB.BuiltInParameter.VIEW_NAME,))
            if nm.strip().startswith(tag):
                names.add(nm.strip())
    except Exception:
        pass
    return sorted(names)


def rename_views_by_name(name):
    """Переименовать виды с данным именем в «<имя>_СТАРАЯ», «…_СТАРАЯ2»
    и т.д., освобождая имя для нового вида; вернуть список новых имён.
    Переименование срабатывает даже на открытом виде (удаление - нет)."""
    renamed = []
    target = (name or u"").strip()
    try:
        views = DB.FilteredElementCollector(doc).OfClass(
            DB.View).WhereElementIsNotElementType().ToElements()
    except Exception:
        views = []
    for v in views:
        try:
            if v.IsTemplate:
                continue
        except Exception:
            continue
        nm = element_name_safe(v, (DB.BuiltInParameter.VIEW_NAME,))
        if nm.strip() != target:
            continue
        new_name = target + u"_СТАРАЯ"
        k = 1
        while views_with_name(new_name):
            k += 1
            new_name = u"{}_СТАРАЯ{}".format(target, k)
        try:
            v.Name = new_name
            renamed.append(new_name)
        except Exception as exc:
            logger.debug("SchemeDiameters: не переименовать вид: %s", exc)
    return renamed


# ---------- примитивы схемы ----------
def make_text(view, x_mm, y_mm, text, type_id, width_mm=None):
    """Текст в чертёжном виде; xy - левый ВЕРХНИЙ угол (мм);
    width_mm - ширина обёртки (None - без обёртки); Revit умножает
    ширину на масштаб вида - компенсируем делением."""
    opts = DB.TextNoteOptions()
    opts.TypeId = type_id
    pos = DB.XYZ(x_mm * MM, y_mm * MM, 0.0)
    if width_mm:
        w_ft = width_mm * MM / float(_view_scale or 1)
        return DB.TextNote.Create(doc, view.Id, pos, w_ft, text, opts)
    return DB.TextNote.Create(doc, view.Id, pos, text, opts)


def text_height_mm(type_id, fallback=65.0):
    """Высота текста типа (мм): BuiltInParameter TEXT_SIZE /
    TEXT_HEIGHT; не прочиталась - fallback."""
    try:
        t = doc.GetElement(type_id)
        for bp in (DB.BuiltInParameter.TEXT_SIZE,
                   DB.BuiltInParameter.TEXT_HEIGHT):
            p = t.get_Parameter(bp)
            if p is not None:
                h = p.AsDouble()
                if h:
                    return h / MM
    except Exception:
        pass
    return fallback


def room_short(room):
    """Короткая подпись помещения для схемы: «Санузел (цок. эт.)»."""
    if room["floor"]:
        return u"{} ({})".format(room["name"], room["floor"])
    return room["name"]


def bold_note(note):
    """Жирное начертание всего текста записки; True - получилось."""
    try:
        ft = note.GetFormattedText()
        rng = DB.TextRange(0, len(note.Text))
        ft.SetBoldStatus(rng, True)
        note.SetFormattedText(ft)
        return True
    except Exception as exc:
        logger.debug("SchemeDiameters: жирный шрифт не удался: %s", exc)
        return False


def underline_ranges(note, ranges):
    """Подчеркнуть в записке диапазоны символов [(начало, конец)];
    TextRange ждёт (начало, ДЛИНА) - переводим; True - получилось."""
    try:
        ft = note.GetFormattedText()
        for start, end in ranges:
            length = max(0, int(end) - int(start))
            ft.SetUnderlineStatus(DB.TextRange(int(start), length), True)
        note.SetFormattedText(ft)
        return True
    except Exception as exc:
        logger.debug("SchemeDiameters: подчёркивание не удалось: %s", exc)
        return False


class SchemeDiametersForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        global BG_HAS, BG_SELECTED, BG_PLAIN
        # «слегка голубой» фон помещения с потребителями
        BG_HAS = SolidColorBrush(Color.FromArgb(0x30, 0x87, 0xCE, 0xEB))
        BG_SELECTED = SolidColorBrush(Color.FromArgb(0x70, 0x87, 0xCE, 0xEB))
        BG_PLAIN = SolidColorBrush(Color.FromArgb(0x00, 0xFF, 0xFF, 0xFF))

        self._updating = False          # защита от пересобытий
        self._rooms = collect_rooms()
        self._cfg = load_config()
        self._selected_key = None       # выбранное помещение (ключ)
        self._c_selected = None         # отмеченный потребитель (индекс)
        self._hide_empty = False        # чекбокс «Скрывать без приборов»

        self.cb_hide_empty.Checked += self._on_hide_empty_changed
        self.cb_hide_empty.Unchecked += self._on_hide_empty_changed
        self.rb_sys_tee.Checked += self._on_system_changed
        self.rb_sys_col.Checked += self._on_system_changed
        self.rb_in16.Checked += self._on_input_diam_changed
        self.rb_in20.Checked += self._on_input_diam_changed
        self.btn_add.Click += self._add_consumer
        self.btn_remove.Click += self._remove_consumer
        self.btn_up.Click += self._move_up
        self.btn_down.Click += self._move_down
        self.btn_draw.Click += self._draw
        self.btn_close.Click += self._close
        self.PreviewKeyDown += self._on_preview_key

        # восстановить режим «Система» из конфигурации проекта
        if self._cfg.get(u"__system__") == u"col":
            self.rb_sys_col.IsChecked = True
        self._apply_system_mode()

        self._build_room_list()
        self._set_status(u"")

    # ---------- статус ----------
    def _set_status(self, text):
        self.lbl_status.Text = text

    # ---------- С1: помещения ----------
    def _shown_rooms(self):
        """Помещения для списка: все или только с приборами."""
        if not self._hide_empty:
            return self._rooms
        return [r for r in self._rooms
                if self._room_cfg(room_key(r))["consumers"]]

    def _on_hide_empty_changed(self, sender, args):
        try:
            self._hide_empty = bool(self.cb_hide_empty.IsChecked)
        except Exception:
            self._hide_empty = False
        prev_key = self._selected_key
        self._build_room_list()
        # выбор сохраняем, если помещение осталось в списке
        keys = [room_key(r) for r in self._shown_rooms()]
        if prev_key in keys:
            self._select_room(prev_key)

    def _build_room_list(self):
        self.pnl_rooms.Children.Clear()
        self._selected_key = None
        shown = self._shown_rooms()
        if not self._rooms:
            lbl = TextBlock()
            lbl.Text = (u"Помещения не найдены:\n"
                        u"- нет загруженной связанной архитектуры, или\n"
                        u"- в связях нет помещений.")
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(6, 4, 6, 4)
            self.pnl_rooms.Children.Add(lbl)
            self._update_c2_area()
            return
        if not shown:
            lbl = TextBlock()
            lbl.Text = u"Нет помещений с приборами."
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(6, 4, 6, 4)
            self.pnl_rooms.Children.Add(lbl)
            self._update_c2_area()
            return
        for room in shown:
            key = room_key(room)
            border = Border()
            border.Padding = Thickness(6, 3, 6, 3)
            border.Background = BG_PLAIN  # без фона клики не ловятся
            lbl = TextBlock()
            lbl.Text = room_label(room)
            border.Child = lbl
            border.MouseLeftButtonUp += (
                lambda s, e, k=key: self._select_room(k))
            self.pnl_rooms.Children.Add(border)
        self._update_room_row_colors()
        self._update_c2_area()

    def _update_room_row_colors(self):
        """Голубой фон у помещений с потребителями; выделение - ярче."""
        idx = 0
        for child in self.pnl_rooms.Children:
            if idx >= len(self._shown_rooms()):
                break
            key = room_key(self._shown_rooms()[idx])
            cfg = self._room_cfg(key)
            if key == self._selected_key:
                child.Background = BG_SELECTED
            elif cfg["consumers"]:
                child.Background = BG_HAS
            else:
                child.Background = BG_PLAIN
            idx += 1

    def _select_room(self, key):
        self._selected_key = key
        self._update_room_row_colors()
        self._update_c2_area()

    def _room_cfg(self, key):
        """Конфигурация помещения (без создания записи); потребители
        нормализуются в словари {name, diam, nb}; старый формат
        [имя, диаметр] мигрирует (каждый прибор - своя труба)."""
        item = self._cfg.get(key)
        if not isinstance(item, dict):
            return {"consumers": [], "input": DEFAULT_DIAM}
        consumers = []
        for raw in (item.get("consumers") or []):
            if isinstance(raw, dict):
                consumers.append({
                    u"name": raw.get(u"name") or u"",
                    u"diam": 20 if raw.get(u"diam") == 20 else DEFAULT_DIAM,
                    u"nb": bool(raw.get(u"nb", True))})
            elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
                consumers.append({
                    u"name": raw[0],
                    u"diam": 20 if raw[1] == 20 else DEFAULT_DIAM,
                    u"nb": True})
        input_d = item.get("input")
        if input_d not in (16, 20):
            input_d = DEFAULT_DIAM
        return {"consumers": consumers, "input": input_d}

    # ---------- С2: потребители ----------
    def _update_c2_area(self):
        """Пересборка списка потребителей выбранного помещения."""
        self.pnl_consumers.Children.Clear()
        self._c_rows = []
        self._c_selected = None
        self._apply_diam_lock()
        if self._selected_key is None:
            lbl = TextBlock()
            lbl.Text = u"Выберите помещение в списке выше."
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(6, 4, 6, 4)
            self.pnl_consumers.Children.Add(lbl)
            return
        consumers = self._room_cfg(self._selected_key)["consumers"]
        if not consumers:
            lbl = TextBlock()
            lbl.Text = u"Потребителей нет - добавьте кнопкой «Добавить»."
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(6, 4, 6, 4)
            self.pnl_consumers.Children.Add(lbl)
            return
        self._updating = True
        try:
            for idx, item in enumerate(consumers):
                self._add_c2_row(idx, item[u"name"], item[u"diam"],
                                 item[u"nb"])
        finally:
            self._updating = False

    def _add_c2_row(self, idx, name, diam, nb=True):
        border = Border()
        border.Padding = Thickness(4, 2, 4, 2)
        border.Background = BG_PLAIN
        row = StackPanel()
        row.Orientation = Orientation.Horizontal

        nb_box = None
        if self._collector_mode():
            # флажок «с новой трубы»: прибор начинает свою трубу;
            # снятая галочка - прибор висит на предыдущей трубе
            nb_box = CheckBox()
            nb_box.Content = u"с новой трубы"
            nb_box.VerticalAlignment = VerticalAlignment.Center
            nb_box.Margin = Thickness(0, 0, 6, 0)
            nb_box.IsChecked = nb
            if idx == 0:
                nb_box.IsChecked = True  # первый прибор - всегда новая
                nb_box.IsEnabled = False

        name_box = ComboBox()
        name_box.IsEditable = True
        name_box.Width = 170
        name_box.ItemsSource = list(STANDARD_CONSUMERS)
        try:
            name_box.Text = name
        except Exception:
            pass
        diam_box = ComboBox()
        diam_box.Width = 64
        diam_box.ItemsSource = [u"16 мм", u"20 мм"]
        diam_box.SelectedIndex = 1 if diam == 20 else 0

        if nb_box is not None:
            row.Children.Add(nb_box)
        row.Children.Add(name_box)
        row.Children.Add(diam_box)
        border.Child = row
        self.pnl_consumers.Children.Add(border)
        self._c_rows.append({"border": border, "name": name_box,
                             "diam": diam_box, "nb": nb_box})

        # у ComboBox нет TextChanged - ловим всплывающее событие
        # TextChanged внутреннего текстового поля (редактируемый режим);
        # AddHandler требует явного делегата
        name_box.AddHandler(
            TextBoxBase.TextChangedEvent,
            TextChangedEventHandler(self._on_consumer_changed))
        name_box.SelectionChanged += self._on_consumer_changed
        diam_box.SelectionChanged += self._on_consumer_changed
        if nb_box is not None:
            nb_box.Checked += self._on_consumer_changed
            nb_box.Unchecked += self._on_consumer_changed
        border.MouseLeftButtonUp += (
            lambda s, e, i=idx: self._select_c_row(i))

    def _select_c_row(self, idx):
        self._c_selected = idx
        for i, row in enumerate(self._c_rows):
            row["border"].Background = (
                BG_SELECTED if i == idx else BG_PLAIN)

    def _on_consumer_changed(self, sender, args):
        if self._updating:
            return
        self._sync_from_c2()

    def _sync_from_c2(self):
        """Строки С2 -> конфигурация помещения; правило вводного φ
        действует только в тройниковой системе."""
        key = self._selected_key
        if key is None:
            return
        consumers = []
        for row in self._c_rows:
            try:
                nm = unicode(row["name"].Text or u"").strip()
            except Exception:
                nm = u""
            dm = 20 if row["diam"].SelectedIndex == 1 else 16
            nb = bool(row["nb"].IsChecked) if row["nb"] is not None else True
            consumers.append({u"name": nm, u"diam": dm, u"nb": nb})
        if consumers:
            consumers[0][u"nb"] = True  # первый прибор - всегда новая труба
        item = self._cfg.setdefault(key, {})
        item["consumers"] = consumers
        if not self._collector_mode():
            if any(c[u"diam"] == 20 for c in consumers):
                item["input"] = 20  # авто-переключение вводной на 20
            elif item.get("input") not in (16, 20):
                item["input"] = DEFAULT_DIAM
        save_config(self._cfg)
        self._update_room_row_colors()
        self._apply_diam_lock()

    def _apply_diam_lock(self):
        """Радио «Диаметр вводной трубы» - по выбранному помещению
        (только тройниковая система); есть потребитель 20 мм ->
        16 заблокировано. В коллекторной системе блок скрыт."""
        key = self._selected_key
        self._updating = True
        try:
            if self._collector_mode():
                self.rb_in16.IsEnabled = False
                self.rb_in20.IsEnabled = False
                self.rb_in16.IsChecked = False
                self.rb_in20.IsChecked = False
                return
            if key is None:
                self.rb_in16.IsEnabled = False
                self.rb_in20.IsEnabled = False
                self.rb_in16.IsChecked = False
                self.rb_in20.IsChecked = False
                return
            cfg = self._room_cfg(key)
            has20 = any(d == 20 for nm, d in cfg["consumers"])
            self.rb_in16.IsEnabled = not has20
            self.rb_in20.IsEnabled = True
            if has20:
                self.rb_in20.IsChecked = True
            else:
                (self.rb_in16 if cfg["input"] == 16
                 else self.rb_in20).IsChecked = True
        finally:
            self._updating = False

    # ---------- радио ----------
    def _collector_mode(self):
        """True - коллекторная система (радио «Система»)."""
        try:
            return bool(self.rb_sys_col.IsChecked)
        except Exception:
            return False

    def _on_system_changed(self, sender, args):
        if self._updating:
            return
        self._cfg[u"__system__"] = (
            u"col" if self._collector_mode() else u"tee")
        save_config(self._cfg)
        self._apply_system_mode()

    def _apply_system_mode(self):
        """Режим «Система»: блок вводного диаметра гаснет в коллекторной
        системе; список С2 перестраивается (появляются флажки
        «с новой трубы»)."""
        collector = self._collector_mode()
        self.gb_input.Visibility = (Visibility.Collapsed if collector
                                    else Visibility.Visible)
        self._update_c2_area()

    def _on_input_diam_changed(self, sender, args):
        if self._updating:
            return
        key = self._selected_key
        if key is None:
            return
        self._cfg.setdefault(key, {})["input"] = (
            20 if self.rb_in20.IsChecked else 16)
        save_config(self._cfg)

    # ---------- кнопки С2 ----------
    def _rebuild_c2(self, select_index=None):
        self._update_c2_area()
        if select_index is not None and self._c_rows:
            self._select_c_row(min(select_index, len(self._c_rows) - 1))

    def _add_consumer(self, sender, args):
        key = self._selected_key
        if key is None:
            self._set_status(u"Сначала выберите помещение.")
            return
        item = self._cfg.setdefault(key, {})
        consumers = item.setdefault("consumers", [])
        consumers.append({u"name": DEFAULT_CONSUMER,
                          u"diam": DEFAULT_DIAM, u"nb": True})
        save_config(self._cfg)
        self._update_room_row_colors()
        self._rebuild_c2(select_index=len(consumers) - 1)

    def _remove_consumer(self, sender, args):
        key = self._selected_key
        if key is None or self._c_selected is None:
            self._set_status(u"Отметьте потребителя в списке.")
            return
        consumers = self._room_cfg(key)["consumers"]
        if 0 <= self._c_selected < len(consumers):
            del consumers[self._c_selected]
            if consumers:
                consumers[0][u"nb"] = True
            self._cfg.setdefault(key, {})["consumers"] = consumers
            save_config(self._cfg)
            self._update_room_row_colors()
            self._rebuild_c2()

    def _move(self, delta):
        key = self._selected_key
        if key is None or self._c_selected is None:
            self._set_status(u"Отметьте потребителя в списке.")
            return
        consumers = self._room_cfg(key)["consumers"]
        i = self._c_selected
        j = i + delta
        if not (0 <= j < len(consumers)):
            return
        consumers[i], consumers[j] = consumers[j], consumers[i]
        if consumers:
            consumers[0][u"nb"] = True
        self._cfg.setdefault(key, {})["consumers"] = consumers
        save_config(self._cfg)
        self._rebuild_c2(select_index=j)

    def _move_up(self, sender, args):
        self._move(-1)

    def _move_down(self, sender, args):
        self._move(+1)

    def _focus_in_editor(self):
        """Фокус в поле ввода (TextBox / ComboBox) - стрелки и Del
        работают штатно, не двигаем список."""
        try:
            el = Keyboard.FocusedElement
            return isinstance(el, TextBox) or isinstance(el, ComboBox)
        except Exception:
            return False

    def _on_preview_key(self, sender, args):
        """Del - удалить отмеченного потребителя; стрелки Вверх/Вниз -
        переместить его по списку (как одноимённые кнопки). Пока фокус
        в текстовом поле, клавиши работают как обычно."""
        try:
            if self._focus_in_editor():
                return
            key = args.Key
            if key == Key.Delete and self._c_selected is not None:
                args.Handled = True
                self._remove_consumer(sender, args)
            elif key == Key.Up and self._c_selected is not None:
                args.Handled = True
                self._move(-1)
            elif key == Key.Down and self._c_selected is not None:
                args.Handled = True
                self._move(+1)
        except Exception as exc:
            logger.debug("SchemeDiameters: клавиатура: %s", exc)

    def _close(self, sender, args):
        self.Close()

    # ---------- отрисовка ----------
    def _draw(self, sender, args):
        try:
            self._draw_impl()
        except Exception as exc:
            logger.error("SchemeDiameters: ошибка отрисовки: %s", exc)
            self._set_status(u"(ошибка: см. лог pyRevit)")

    def _draw_impl(self):
        global _view_scale
        view_name = unicode(self.tb_view_name.Text or u"").strip()
        if not view_name:
            forms.alert(u"Укажите название вида.", warn_icon=True)
            return
        scale_n = parse_scale(unicode(self.tb_scale.Text or u""))
        if scale_n is None:
            forms.alert(
                u"Масштаб в формате 1:N (например, 1:10).",
                warn_icon=True)
            return

        collector = self._collector_mode()
        drawing = []
        total_consumers = 0
        for room in self._rooms:
            cfg = self._room_cfg(room_key(room))
            consumers = []
            for item in cfg["consumers"]:
                nm = unicode(item.get(u"name") or u"").strip()
                if nm:
                    consumers.append((nm, int(item.get(u"diam") or 16),
                                      bool(item.get(u"nb", True))))
            if not consumers:
                continue
            # группы приборов на одной трубе: nb=True начинает новую;
            # внутри группы (2+ приборов) в коллекторной системе
            # подчёркиваем - видно исключения
            groups = []
            for item in consumers:
                if not groups or item[2]:
                    groups.append([])
                groups[-1].append(item)
            parts = []
            ranges = []  # (начало, конец) приборов на общей трубе
            for gi, group in enumerate(groups):
                if gi > 0:
                    parts.append(u"; ")
                for ii, (nm, d, nb) in enumerate(group):
                    if ii > 0:
                        # разделитель внутри общей трубы подчёркиваем
                        # вместе с приборами
                        sep_start = sum(len(p) for p in parts)
                        sep = u"  -  "
                        parts.append(sep)
                        if len(group) > 1:
                            ranges.append((sep_start,
                                           sep_start + len(sep)))
                    start = sum(len(p) for p in parts)
                    text = u"{} ∅{}".format(nm, d)
                    parts.append(text)
                    if len(group) > 1:
                        ranges.append((start, start + len(text)))
            cons_str = u"".join(parts)
            # «текстпомещение»: в коллекторной системе без диаметра -
            # каждый прибор несёт свой ∅
            room_text = (room_short(room) if collector
                         else u"{} ∅{}".format(room_short(room),
                                               cfg["input"]))
            total_consumers += len(consumers)
            drawing.append((room_text, cons_str, ranges))
        if not drawing:
            forms.alert(
                u"Нет ни одного помещения с потребителями.",
                warn_icon=True)
            return

        missing = missing_assets()
        if missing:
            # подсказка: какие типы текста реально есть в проекте
            avail = []
            try:
                for t in DB.FilteredElementCollector(doc).OfClass(
                        DB.TextNoteType).ToElements():
                    nm = element_name_safe(
                        t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
                    if nm.strip():
                        avail.append(nm.strip())
            except Exception:
                pass
            avail.sort()
            hint = u""
            if avail:
                hint = (u"\n\nТипы текста в проекте:\n"
                        + u"\n".join(u"- " + a for a in avail[:25]))
                if len(avail) > 25:
                    hint += u"\n- ... и ещё {}".format(len(avail) - 25)
            forms.alert(
                u"В проекте не найдено:\n" + u"\n".join(missing) + hint,
                title="Не хватает ресурсов проекта",
                warn_icon=True)
            return

        pipe_id = None  # линии в упрощённой схеме не рисуются
        small_id = find_text_type_id(TEXT_SMALL)
        big_id = find_text_type_id(TEXT_BIG)
        drafting_type_id = find_drafting_type_id()

        warnings = []

        # перезапись: старый вид переименовывается в «<имя>_СТАРАЯ» -
        # имя освобождается даже если вид открыт в Revit (удаление
        # открытого вида API запрещает, переименование - нет)
        renamed = []
        with revit.Transaction(u"ПлагИН: перезапись схемы", doc):
            renamed = rename_views_by_name(view_name)
        if renamed:
            warnings.append(u"прежний вид переименован в «{}»".format(
                u"», «".join(renamed)))
        elif views_with_name(view_name):
            warnings.append(
                u"не удалось освободить имя «{}»".format(view_name))

        with revit.Transaction(u"ПлагИН: рисование схемы диаметров", doc):
            view = DB.ViewDrafting.Create(doc, drafting_type_id)
            try:
                view.Name = view_name
            except Exception as exc:
                warnings.append(u"имя вида не записано: {}".format(exc))
                logger.debug("SchemeDiameters: имя вида: %s", exc)
            try:
                view.Scale = scale_n
                _view_scale = scale_n
            except Exception as exc:
                warnings.append(u"масштаб вида не записан")
                logger.debug("SchemeDiameters: масштаб вида: %s", exc)

            for pname, val in (
                    (u"ADSK_Назначение вида", u"Оформленные виды"),
                    (u"ADSK_Штамп Раздел проекта", u"Водоснабжение")):
                try:
                    p = view.LookupParameter(pname)
                except Exception:
                    p = None
                if p is None or p.IsReadOnly:
                    warnings.append(u"у вида нет параметра «{}»".format(pname))
                else:
                    p.Set(val)

            n = len(drawing)
            h_text = text_height_mm(small_id)
            # заголовок: над таблицей, тип «_5»; центр выставим после
            # регенерации по фактическим габаритам таблицы
            title_note = make_text(view, X_RIGHT_ROOM - 300,
                                   h_text + 70, TITLE_TEXT, big_id)
            # записки без обёртки (обнимают текст); позицию правим
            # сдвигом по фактическим габаритам после регенерации
            shift_jobs = []  # (note, anchor_mm, "right"/"left")
            for i, (room_text, cons_str, ranges) in enumerate(drawing):
                # зазор между строками - 10 мм (19.11е)
                y = -i * (h_text + 10.0)

                # «текстпомещение» (в коллекторной системе - без ∅,
                # каждый прибор несёт свой): правый край - X_RIGHT_ROOM
                room_note = make_text(
                    view, X_RIGHT_ROOM - 400, y, room_text, small_id)
                shift_jobs.append((room_note, X_RIGHT_ROOM, u"right"))
                if not bold_note(room_note):
                    warnings.append(
                        u"«{}» без жирного начертания".format(room_text))

                # «текстприборы»: левый край - на X_LEFT_CONS (зазор
                # одинаковый)
                cons_note = make_text(view, X_LEFT_CONS, y,
                                      cons_str, small_id)
                shift_jobs.append((cons_note, X_LEFT_CONS, u"left"))
                if ranges and not underline_ranges(cons_note, ranges):
                    warnings.append(u"приборы на общей трубе без "
                                    u"подчёркивания (строка «{}»)".format(
                                        room_text))

            # одна регенерация, затем выставление краёв записок
            try:
                doc.Regenerate()
            except Exception as exc:
                logger.debug("SchemeDiameters: регенерация: %s", exc)
            for note, anchor, side in shift_jobs:
                try:
                    bb = note.get_BoundingBox(view)
                    if bb is None:
                        continue
                    if side == u"right":
                        shift = anchor * MM - bb.Max.X
                    else:
                        shift = anchor * MM - bb.Min.X
                    if abs(shift) > 0.1 * MM:
                        DB.ElementTransformUtils.MoveElement(
                            doc, note.Id, DB.XYZ(shift, 0.0, 0.0))
                except Exception as exc:
                    logger.debug(
                        "SchemeDiameters: позиция записки: %s", exc)

            # заголовок - по центру фактических габаритов таблицы
            try:
                bb_t = title_note.get_BoundingBox(view)
                if bb_t is None:
                    raise Exception(u"габариты заголовка недоступны")
                left = None
                right = None
                for tn in DB.FilteredElementCollector(
                        doc, view.Id).OfClass(DB.TextNote).ToElements():
                    if tn.Id == title_note.Id:
                        continue
                    bb = tn.get_BoundingBox(view)
                    if bb is None:
                        continue
                    left = (bb.Min.X if left is None
                            else min(left, bb.Min.X))
                    right = (bb.Max.X if right is None
                             else max(right, bb.Max.X))
                if left is not None and right > left:
                    shift = ((left + right) / 2.0
                             - (bb_t.Min.X + bb_t.Max.X) / 2.0)
                    if abs(shift) > 0.1 * MM:
                        DB.ElementTransformUtils.MoveElement(
                            doc, title_note.Id, DB.XYZ(shift, 0.0, 0.0))
            except Exception as exc:
                logger.debug("SchemeDiameters: центр заголовка: %s", exc)
                warnings.append(
                    u"заголовок не отцентрован: {}".format(exc))

            # старые (переименованные) виды удаляются отдельным шагом
            # ПОСЛЕ транзакции отрисовки (см. ниже): сначала активным
            # становится новый вид, затем «_СТАРАЯ» удаляется

        # переключить активный вид на новый (Revit API не удаляет
        # АКТИВНЫЙ вид: "ElementId cannot be deleted") и убрать старые
        # виды: свежепереименованные + хвосты «…_СТАРАЯ*» прошлых
        # запусков
        if renamed:
            try:
                revit.uidoc.ActiveView = view
            except Exception as exc:
                warnings.append(u"активный вид не переключён: {}".format(exc))
        stale = stale_view_names(view_name)
        targets = sorted(set(renamed) | set(stale))
        if targets:
            with revit.Transaction(u"ПлагИН: удаление прежних видов", doc):
                for old in targets:
                    cnt, err = delete_view_by_name(old)
                    if cnt or not views_with_name(old):
                        continue
                    if err and u"cannot be deleted" in (err or u"").lower():
                        try:
                            revit.uidoc.ActiveView = view
                            cnt2, err2 = delete_view_by_name(old)
                            if cnt2:
                                warnings.append(
                                    u"прежний вид «{}» удалён (активный "
                                    u"вид переключён на новый)".format(old))
                            else:
                                warnings.append(
                                    u"не удалить прежний вид «{}»: {}. "
                                    u"Удалите вручную.".format(old, err2 or
                                                               u"причина "
                                                               u"неизвестна"))
                        except Exception as exc2:
                            warnings.append(
                                u"не удалить прежний вид «{}»: {}; активный "
                                u"вид не переключить: {}. Удалите "
                                u"вручную.".format(old, err, exc2))
                    else:
                        warnings.append(
                            u"не удалить прежний вид «{}»: {}. "
                            u"Удалите вручную.".format(old, err or u"причина "
                                                        u"неизвестна"))

        lines = [u"Создан вид «{}» ({}):".format(view_name, scale_n),
                 u"помещений {}, потребителей {}".format(
                     n, total_consumers)]
        if warnings:
            lines.append(u"Замечания:")
            lines.extend(u"- " + w for w in warnings)
        self._set_status(u"\n".join(lines))


form = SchemeDiametersForm()
form.ShowDialog()

logger.debug("SchemeDiameters: окно закрыто")
