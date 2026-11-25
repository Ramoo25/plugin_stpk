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

TEXT_SMALL = u"ADSK_Основной текст_2.5"   # основной текст таблицы
TITLE_TYPE = u"ADSK_Основной текст_3.5"   # заголовок/подзаголовок
GAP_COLS_BASE = 60.0   # зазор помещения/приборы при масштабе 1:10
                       # (пропорционально масштабу вида)
ROW_GAP_MM = 0.0   # зазор между строками таблицы (мм на виде)
TITLE_GAP_XV = 40.0   # зазор заголовок/первая строка, ХВ (при 1:10)
TITLE_GAP_HW = 100.0  # зазор заголовок/первая строка, ГВ (при 1:10)
TITLE_TEXT = u"Диаметры трубопроводов холодного водоснабжения"
TITLE_TEXT_HW = u"Диаметры трубопроводов горячего водоснабжения"
TITLE_HW_SUB = u"(в скобках наличие/отсутствие линии рециркуляции)"
VIEWPORT_TYPE = u"Без названия"

# циркуляция ГВ
CIRC_NONE = u"Нет"
CIRC_YES = u"Да"
CIRC_PS = u"ПС"
CIRC_CHOICES = [CIRC_NONE, CIRC_YES, CIRC_PS]
TEE_CIRC_NONE = u"Нет циркуляции ГВ"
TEE_CIRC_PS = u"Полотенцесушитель"
TEE_CIRC_YES = u"Циркуляция ГВ"
TEE_CIRC_CHOICES = [TEE_CIRC_NONE, TEE_CIRC_PS, TEE_CIRC_YES]
HW_TAIL_RECIRC = u"Рециркуляция ∅16"  # диаметр рециркуляции - всегда 16
# приборы без горячей воды: в вид ГВ не попадают
NO_HW_CONSUMERS = [
    u"Унитаз",
    u"Робот пылесос",
    u"Обливное устройство",
    u"Посудомоечная машина",
    u"Стиральная машина",
]

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
    if find_text_type_id(TITLE_TYPE) is None:
        missing.append(u"тип текста «{}»".format(TITLE_TYPE))
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


def collect_viewports(names):
    """Размещения видов на листах: [(sheet_id, XYZ-центр)] для видов
    с именами из names (Viewport знает лист и точку на листе)."""
    targets = set((n or u"").strip() for n in names)
    out = []
    try:
        for vp in DB.FilteredElementCollector(doc).OfClass(
                DB.Viewport).ToElements():
            try:
                v = doc.GetElement(vp.ViewId)
                nm = element_name_safe(v, (DB.BuiltInParameter.VIEW_NAME,))
                if nm.strip() in targets:
                    # BoxCenter - метод GetBoxCenter(), свойства нет
                    out.append((vp.SheetId, vp.GetBoxCenter()))
            except Exception:
                continue
    except Exception:
        pass
    return out


def viewport_type_list(viewport):
    """Типоразмеры вида «Видовой экран»: [(id, имя)].
    Стратегии по очереди:
    1) класс ViewportType, если он есть в API этой версии;
    2) ElementType с тем же FamilyName, что у типа вьюпорта
       (имя семейства берём у самого типа - без хардкода)."""
    out = []
    # 1) прямой класс ViewportType (в части версий API есть)
    try:
        vt = getattr(DB, "ViewportType", None)
        if vt is not None:
            for t in DB.FilteredElementCollector(doc).OfClass(
                    vt).ToElements():
                nm = element_name_safe(
                    t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
                if nm.strip():
                    out.append((t.Id, nm.strip()))
    except Exception as exc:
        logger.debug("SchemeDiameters: ViewportType: %s", exc)
    if out:
        return out
    # 2) ElementType с тем же FamilyName
    try:
        cur = doc.GetElement(viewport.GetTypeId())
        fam = cur.FamilyName
        for t in DB.FilteredElementCollector(doc).OfClass(
                DB.ElementType).ToElements():
            try:
                if t.FamilyName == fam:
                    nm = element_name_safe(
                        t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
                    if nm.strip():
                        out.append((t.Id, nm.strip()))
            except Exception:
                continue
    except Exception as exc:
        logger.debug("SchemeDiameters: FamilyName: %s", exc)
    return out


def replace_on_sheets(view, placements):
    """Разместить вид на листах в сохранённых точках; вернуть
    (число успешных, [созданные вьюпорты])."""
    placed = 0
    created = []
    for sheet_id, center in placements:
        try:
            vp = DB.Viewport.Create(doc, sheet_id, view.Id, center)
            if vp is not None:
                created.append(vp)
                placed += 1
        except Exception as exc:
            logger.debug("SchemeDiameters: возврат на лист: %s", exc)
    return placed, created


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
        # имя вида - своё на проект; восстанавливаем до подписки на
        # TextChanged, чтобы восстановление не сохранило само себя
        saved_name = unicode(self._cfg.get(u"__view_name__") or u"").strip()
        self.tb_view_name.Text = saved_name or u"СД В1"
        self.tb_view_name.TextChanged += self._on_view_name_changed
        saved_hw = unicode(self._cfg.get(u"__view_hw__") or u"").strip()
        self.tb_view_hw.Text = saved_hw or u"СД ГВ"
        self.tb_view_hw.TextChanged += self._on_view_hw_changed
        # подписи помещений на виде: номер пом. / этаж (скобки)
        self._lbl_num = bool(self._cfg.get(u"__lbl_num__", True))
        self._lbl_floor = bool(self._cfg.get(u"__lbl_floor__", True))
        self.cb_lbl_num.IsChecked = self._lbl_num
        self.cb_lbl_floor.IsChecked = self._lbl_floor
        self.cb_lbl_num.Checked += self._on_lbl_changed
        self.cb_lbl_num.Unchecked += self._on_lbl_changed
        self.cb_lbl_floor.Checked += self._on_lbl_changed
        self.cb_lbl_floor.Unchecked += self._on_lbl_changed

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
                circ = raw.get(u"circ")
                consumers.append({
                    u"name": raw.get(u"name") or u"",
                    u"diam": 20 if raw.get(u"diam") == 20 else DEFAULT_DIAM,
                    u"nb": bool(raw.get(u"nb", True)),
                    u"circ": circ if circ in CIRC_CHOICES else CIRC_NONE})
            elif isinstance(raw, (list, tuple)) and len(raw) >= 2:
                consumers.append({
                    u"name": raw[0],
                    u"diam": 20 if raw[1] == 20 else DEFAULT_DIAM,
                    u"nb": True,
                    u"circ": CIRC_NONE})
        circ_tee = item.get(u"circ_tee")
        if circ_tee not in (TEE_CIRC_NONE, TEE_CIRC_PS, TEE_CIRC_YES):
            circ_tee = TEE_CIRC_NONE
        input_d = item.get("input")
        if input_d not in (16, 20):
            input_d = DEFAULT_DIAM
        return {"consumers": consumers, "input": input_d,
                "circ_tee": circ_tee}

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
                                 item[u"nb"], item[u"circ"])
        finally:
            self._updating = False
        # в тройниковой системе - строка циркуляции ГВ в конце списка
        # (у каждого помещения с приборами)
        if not self._collector_mode() and consumers:
            self._add_circ_tee_row(self._room_cfg(
                self._selected_key)[u"circ_tee"])

    def _add_c2_row(self, idx, name, diam, nb=True, circ=CIRC_NONE):
        border = Border()
        border.Padding = Thickness(4, 2, 4, 2)
        border.Background = BG_PLAIN
        row = StackPanel()
        row.Orientation = Orientation.Horizontal

        nb_box = None
        has_hw = name not in NO_HW_CONSUMERS
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

        circ_box = None
        if self._collector_mode() and has_hw:
            # циркуляция у прибора в коллекторной системе: Нет/Да/ПС
            circ_box = ComboBox()
            circ_box.Width = 52
            circ_box.ItemsSource = list(CIRC_CHOICES)
            circ_box.SelectedIndex = (CIRC_CHOICES.index(circ)
                                      if circ in CIRC_CHOICES else 0)
            circ_box.ToolTip = u"Циркуляция у прибора: Нет / Да / ПС (полотенцесушитель)"

        name_box = ComboBox()
        name_box.IsEditable = True
        name_box.Width = 150
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
        if circ_box is not None:
            row.Children.Add(circ_box)
        border.Child = row
        self.pnl_consumers.Children.Add(border)
        self._c_rows.append({"border": border, "name": name_box,
                             "diam": diam_box, "nb": nb_box,
                             "circ": circ_box})

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
        if circ_box is not None:
            circ_box.SelectionChanged += self._on_consumer_changed
        border.MouseLeftButtonUp += (
            lambda s, e, i=idx: self._select_c_row(i))

    def _add_circ_tee_row(self, circ_tee):
        """Строка «Циркуляция ГВ» в конце С2 (тройниковая система)."""
        border = Border()
        border.Padding = Thickness(4, 2, 4, 2)
        row = StackPanel()
        row.Orientation = Orientation.Horizontal
        lbl = TextBlock()
        lbl.Text = u"Циркуляция ГВ:"
        lbl.VerticalAlignment = VerticalAlignment.Center
        lbl.Margin = Thickness(0, 0, 6, 0)
        box = ComboBox()
        box.Width = 130
        box.ItemsSource = list(TEE_CIRC_CHOICES)
        box.SelectedIndex = (TEE_CIRC_CHOICES.index(circ_tee)
                             if circ_tee in TEE_CIRC_CHOICES else 0)
        box.SelectionChanged += self._on_circ_tee_changed
        box.ToolTip = (u"Полотенцесушитель / Рециркуляция (d16) добавляются "
                       u"в конец цепочки помещения на виде ГВ; "
                       u"«Нет циркуляции» - ничего не добавляется")
        row.Children.Add(lbl)
        row.Children.Add(box)
        border.Child = row
        self.pnl_consumers.Children.Add(border)
        self._circ_tee_box = box

    def _on_circ_tee_changed(self, sender, args):
        """Выбран вариант циркуляции ГВ (тройниковая система)."""
        key = self._selected_key
        if key is None:
            return
        try:
            idx = self._circ_tee_box.SelectedIndex
            self._cfg.setdefault(key, {})[u"circ_tee"] = (
                TEE_CIRC_CHOICES[idx] if idx is not None
                and 0 <= idx < len(TEE_CIRC_CHOICES) else TEE_CIRC_NONE)
        except Exception:
            pass
        save_config(self._cfg)

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
            circ = CIRC_NONE
            if row["circ"] is not None:
                try:
                    i = row["circ"].SelectedIndex
                    circ = (CIRC_CHOICES[i] if i is not None
                            and 0 <= i < len(CIRC_CHOICES) else CIRC_NONE)
                except Exception:
                    circ = CIRC_NONE
            consumers.append({u"name": nm, u"diam": dm, u"nb": nb,
                              u"circ": circ})
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
            has20 = any(c[u"diam"] == 20 for c in cfg["consumers"])
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
    def _on_view_name_changed(self, sender, args):
        """Имя чертёжного вида - запоминаем по проекту."""
        self._cfg[u"__view_name__"] = unicode(
            self.tb_view_name.Text or u"").strip()
        save_config(self._cfg)

    def _on_view_hw_changed(self, sender, args):
        """Имя вида ГВ - запоминаем по проекту."""
        self._cfg[u"__view_hw__"] = unicode(
            self.tb_view_hw.Text or u"").strip()
        save_config(self._cfg)

    def _on_lbl_changed(self, sender, args):
        """Флажки подписи помещения на виде (номер пом. / этаж)."""
        try:
            self._lbl_num = bool(self.cb_lbl_num.IsChecked)
            self._lbl_floor = bool(self.cb_lbl_floor.IsChecked)
        except Exception:
            pass
        self._cfg[u"__lbl_num__"] = self._lbl_num
        self._cfg[u"__lbl_floor__"] = self._lbl_floor
        save_config(self._cfg)

    def _room_display(self, room):
        """Подпись помещения на виде по флажкам: «СУ1»,
        «СУ1 (1 эт.)», «СУ1 (1 эт., пом. 8)», «СУ1 (пом. 8)»."""
        parts = []
        if self._lbl_floor and room["floor"]:
            parts.append(room["floor"])
        if self._lbl_num and room["number"]:
            parts.append(u"пом. {}".format(room["number"]))
        if parts:
            return u"{} ({})".format(room["name"], u", ".join(parts))
        return room["name"]

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
                          u"diam": DEFAULT_DIAM, u"nb": True,
                          u"circ": CIRC_NONE})
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

    def _build_rows(self, is_hw):
        """Строки таблицы: [(room_text, cons_str, ranges)].
        ХВ: все приборы. ГВ: без приборов «без горячей воды»,
        маркеры циркуляции (коллектор), хвост циркуляции (тройниковая)."""
        collector = self._collector_mode()
        rows = []
        total_consumers = 0
        for room in self._rooms:
            cfg = self._room_cfg(room_key(room))
            items = []
            for item in cfg["consumers"]:
                nm = unicode(item.get(u"name") or u"").strip()
                if not nm:
                    continue
                if is_hw and nm in NO_HW_CONSUMERS:
                    continue  # у прибора нет горячей воды
                items.append((nm, int(item.get(u"diam") or 16),
                              bool(item.get(u"nb", True)),
                              item.get(u"circ") or CIRC_NONE))
            if not items:
                continue
            parts = []
            ranges = []
            if is_hw:
                if collector:
                    # у каждого прибора своя труба, маркер циркуляции
                    for i, (nm, d, nb, circ) in enumerate(items):
                        if i:
                            parts.append(u"; ")
                        parts.append(u"{} ∅{} ({})".format(nm, d, circ))
                else:
                    # тройниковая: цепочка приборов + хвост циркуляции
                    for i, (nm, d, nb, circ) in enumerate(items):
                        if i:
                            parts.append(u"  -  ")
                        parts.append(u"{} ∅{}".format(nm, d))
                    tail = cfg.get(u"circ_tee") or TEE_CIRC_NONE
                    if tail == TEE_CIRC_PS:
                        parts.append(u"  -  ")
                        parts.append(TEE_CIRC_PS)
                    elif tail == TEE_CIRC_YES:
                        parts.append(u"  -  ")
                        parts.append(HW_TAIL_RECIRC)
                # вид ГВ: помещение без ∅ ввода (по ТЗ 24.11)
                room_text = self._room_display(room)
            elif collector:
                # группы приборов на одной трубе: nb=True начинает
                # новую; группа из 2+ приборов подчёркивается
                groups = []
                for item in items:
                    if not groups or item[2]:
                        groups.append([])
                    groups[-1].append(item)
                for gi, group in enumerate(groups):
                    if gi > 0:
                        parts.append(u"; ")
                    for ii, (nm, d, nb, circ) in enumerate(group):
                        if ii > 0:
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
                room_text = self._room_display(room)
            else:
                for i, (nm, d, nb, circ) in enumerate(items):
                    if i:
                        parts.append(u"  -  ")
                    parts.append(u"{} ∅{}".format(nm, d))
                room_text = u"{} ∅{}".format(self._room_display(room),
                                             cfg["input"])
            total_consumers += len(items)
            rows.append((room_text, u"".join(parts), ranges))
        return rows, total_consumers

    def _draw_one_scheme(self, small_id, title_id, drafting_type_id,
                         scale_n, view_name, title_text, sub_text, rows,
                         total_consumers, warnings,
                         gap_title, gap_rows, gap_cols):
        """Полный цикл одной схемы: перезапись -> рисование -> удаление
        старых видов -> возврат на листы. Возвращает
        (число строк, приборов, размещений, всего размещений)."""
        global _view_scale
        placements = collect_viewports([view_name])

        renamed = []
        with revit.Transaction(u"ПлагИН: перезапись схемы", doc):
            renamed = rename_views_by_name(view_name)
        if renamed:
            warnings.append(u"«{}»: прежний вид переименован в «{}»".format(
                view_name, u"», «".join(renamed)))
        elif views_with_name(view_name):
            warnings.append(
                u"«{}»: не удалось освободить имя".format(view_name))

        with revit.Transaction(u"ПлагИН: рисование схемы диаметров", doc):
            view = DB.ViewDrafting.Create(doc, drafting_type_id)
            try:
                view.Name = view_name
            except Exception as exc:
                warnings.append(u"«{}»: имя вида не записано: {}".format(
                    view_name, exc))
                logger.debug("SchemeDiameters: имя вида: %s", exc)
            try:
                view.Scale = scale_n
                _view_scale = scale_n
            except Exception as exc:
                warnings.append(u"«{}»: масштаб вида не записан".format(
                    view_name))
                logger.debug("SchemeDiameters: масштаб вида: %s", exc)

            for pname, val in (
                    (u"ADSK_Назначение вида", u"Оформленные виды"),
                    (u"ADSK_Штамп Раздел проекта", u"Водоснабжение")):
                try:
                    p = view.LookupParameter(pname)
                except Exception:
                    p = None
                if p is None or p.IsReadOnly:
                    warnings.append(
                        u"«{}»: у вида нет параметра «{}»".format(
                            view_name, pname))
                else:
                    p.Set(val)

            n = len(rows)
            h_text = text_height_mm(small_id)
            # заголовок: над таблицей; для ГВ - подзаголовок под ним
            # о смысле скобок; центр выставим после регенерации
            if sub_text:
                # зазор заголовок/подзаголовок = 1 мм:
                # верх заголовка = (h+20) + h + 1
                title_note = make_text(view, X_RIGHT_ROOM - 300,
                                       h_text * 2 + 21,
                                       title_text, title_id)
                sub_note = make_text(view, X_RIGHT_ROOM - 300,
                                     h_text + 20,
                                     sub_text, title_id)
            else:
                title_note = make_text(view, X_RIGHT_ROOM - 300,
                                       h_text + 70, title_text, title_id)
                sub_note = None
            # записки строк: шаг калибруется по факту - Revit рендерит
            # текст и позиции с множителем масштаба, поэтому создаём
            # две первые строки с пробным шагом 100, замеряем
            # фактическую высоту текста и множитель позиций, затем
            # шаг = высота текста + ROW_GAP_MM (в мм вида)
            shift_jobs = []  # (note, anchor_mm, "right"/"left")

            def _mk_row(i, y, row, x_left):
                room_text, cons_str, ranges = row
                rn = make_text(view, X_RIGHT_ROOM - 400, y,
                               room_text, small_id)
                cn = make_text(view, x_left, y, cons_str, small_id)
                shift_jobs.append((rn, X_RIGHT_ROOM, u"right"))
                shift_jobs.append((cn, x_left, u"left"))
                if not bold_note(rn):
                    warnings.append(
                        u"«{}» без жирного начертания".format(room_text))
                if ranges and not underline_ranges(cn, ranges):
                    warnings.append(u"приборы на общей трубе без "
                                    u"подчёркивания (строка «{}»)".format(
                                        room_text))
                return rn, cn

            probe_raw = 100.0
            x_left0 = X_RIGHT_ROOM + gap_cols  # start column anchor
            note_r0, note_c0 = _mk_row(0, 0.0, rows[0], x_left0)
            s_eff = 1.0
            pitch = h_text  # запасной шаг, если калибровка не удастся
            if len(rows) > 1:
                note_r1, note_c1 = _mk_row(1, -probe_raw, rows[1], x_left0)
                try:
                    doc.Regenerate()
                    bb0 = note_r0.get_BoundingBox(view)
                    bb1 = note_r1.get_BoundingBox(view)
                    if bb0 is None or bb1 is None:
                        raise Exception(u"габариты записок недоступны")
                    h_render = (bb0.Max.Y - bb0.Min.Y) / MM  # высота
                    # строки текста на виде (мм, с учётом масштаба)
                    dist = (bb0.Max.Y - bb1.Max.Y) / MM
                    s_eff = dist / probe_raw if dist > 0 else 1.0
                    pitch = h_render / s_eff + gap_rows / s_eff
                    # строка 1 -> на своё место (кратный шаг)
                    up_raw = probe_raw - pitch
                    if abs(up_raw) > 0.1 * MM:
                        DB.ElementTransformUtils.MoveElement(
                            doc, note_r1.Id, DB.XYZ(0.0, up_raw * MM, 0.0))
                        DB.ElementTransformUtils.MoveElement(
                            doc, note_c1.Id, DB.XYZ(0.0, up_raw * MM, 0.0))
                except Exception as exc:
                    logger.debug(
                        "SchemeDiameters: калибровка шага: %s", exc)
                    warnings.append(
                        u"шаг строк по запасному значению (калибровка "
                        u"не удалась: {})".format(exc))
            # фактический якорь приборов: зазор помещения/приборы -
            # gap_cols мм НА ВИДЕ (масштабонезависимо: после сдвигов
            # позиции якорятся абсолютно)
            x_left = X_RIGHT_ROOM + gap_cols
            for j, (note, anchor, side) in enumerate(shift_jobs):
                if side == u"left":
                    shift_jobs[j] = (note, x_left, side)
            for i in range(2, len(rows)):
                _mk_row(i, -i * pitch, rows[i], x_left)

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

            # заголовок/подзаголовок: ставим по факту - низ заголовка
            # над верхней строкой на TITLE_GAP_MM (мм на виде),
            # подзаголовок - на 1 мм под заголовком; затем центрирование
            try:
                doc.Regenerate()
                bb0 = note_r0.get_BoundingBox(view)
                bb_t = title_note.get_BoundingBox(view)
                if bb0 is None or bb_t is None:
                    raise Exception(u"габариты недоступны")
                need_bottom = bb0.Max.Y + gap_title * MM
                shift_t = need_bottom - bb_t.Min.Y
                if abs(shift_t) > 0.1 * MM:
                    DB.ElementTransformUtils.MoveElement(
                        doc, title_note.Id, DB.XYZ(0.0, shift_t, 0.0))
                if sub_note is not None:
                    bb_t2 = title_note.get_BoundingBox(view)
                    bb_s = sub_note.get_BoundingBox(view)
                    if bb_t2 is not None and bb_s is not None:
                        need_s = bb_t2.Min.Y - 1.0 * MM
                        shift_s = need_s - bb_s.Max.Y
                        if abs(shift_s) > 0.1 * MM:
                            DB.ElementTransformUtils.MoveElement(
                                doc, sub_note.Id,
                                DB.XYZ(0.0, shift_s, 0.0))
            except Exception as exc:
                logger.debug("SchemeDiameters: позиция заголовка: %s", exc)
                warnings.append(u"заголовок не установлен: {}".format(exc))

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
                    if sub_note is not None and tn.Id == sub_note.Id:
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
                        if sub_note is not None:
                            DB.ElementTransformUtils.MoveElement(
                                doc, sub_note.Id, DB.XYZ(shift, 0.0, 0.0))
            except Exception as exc:
                logger.debug("SchemeDiameters: центр заголовка: %s", exc)
                warnings.append(
                    u"заголовок не отцентрован: {}".format(exc))

        # переключить активный вид на новый (Revit API не удаляет
        # АКТИВНЫЙ вид) и убрать старые виды: свежепереименованные +
        # хвосты «…_СТАРАЯ*» прошлых запусков
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

        # возврат нового вида на листы, где лежал старый (те же точки);
        # типоразмер вьюпорта - «Без названия» (категорию берём у самого
        # вьюпорта: угадывание категорий не нужно)
        placed = 0
        if placements:
            missing_type_names = None
            with revit.Transaction(u"ПлагИН: возврат вида на листы", doc):
                placed, created_vps = replace_on_sheets(view, placements)
                if created_vps:
                    try:
                        types = viewport_type_list(created_vps[0])
                        type_id = None
                        for tid, nm in types:
                            if nm == VIEWPORT_TYPE:
                                type_id = tid
                                break
                        if type_id is not None:
                            for vp in created_vps:
                                try:
                                    vp.ChangeTypeId(type_id)
                                except Exception as exc:
                                    warnings.append(
                                        u"тип вьюпорта не изменён: {}".format(
                                            exc))
                        elif types:
                            warnings.append(
                                u"«{}» нет среди {} типоразмеров "
                                u"семейства".format(VIEWPORT_TYPE,
                                                    len(types)))
                            missing_type_names = sorted(
                                nm for _, nm in types)
                        else:
                            warnings.append(
                                u"типоразмеры семейства вьюпорта не "
                                u"читаются (0 шт. по обеим стратегиям)")
                    except Exception as exc:
                        warnings.append(
                            u"типоразмеры семейства не прочитаны: {}".format(
                                exc))
            if missing_type_names is not None:
                if missing_type_names:
                    body = u"\n".join(u"- " + nm
                                      for nm in missing_type_names[:25])
                    if len(missing_type_names) > 25:
                        body += u"\n- ... и ещё {}".format(
                            len(missing_type_names) - 25)
                else:
                    body = u"(не найдено)"
                forms.alert(
                    u"Типоразмер вьюпорта «{}» не найден.\nВьюпорт будет "
                    u"с подписью.\n\nТипоразмеры вьюпортов в проекте:\n"
                    u"{}".format(VIEWPORT_TYPE, body),
                    title="Типоразмер вьюпорта",
                    warn_icon=True)
            if placed < len(placements):
                warnings.append(
                    u"на листы возвращено {} из {} размещений".format(
                        placed, len(placements)))
        return n, total_consumers, placed, len(placements)

    def _draw_impl(self):
        global _view_scale
        view_name = unicode(self.tb_view_name.Text or u"").strip()
        if not view_name:
            forms.alert(u"Укажите название вида ХВ.", warn_icon=True)
            return
        hw_name = unicode(self.tb_view_hw.Text or u"").strip()
        if hw_name and hw_name == view_name:
            forms.alert(
                u"Названия видов ХВ и ГВ должны различаться.",
                warn_icon=True)
            return
        scale_n = parse_scale(unicode(self.tb_scale.Text or u""))
        if scale_n is None:
            forms.alert(
                u"Масштаб в формате 1:N (например, 1:10).",
                warn_icon=True)
            return

        # строки таблиц: ХВ - все приборы; ГВ - без «без горячей воды»,
        # с маркерами циркуляции и хвостом (тройниковая)
        xv_rows, xv_total = self._build_rows(False)
        hw_rows, hw_total = self._build_rows(True)
        schemes = [(view_name, TITLE_TEXT, None, xv_rows, xv_total,
                    TITLE_GAP_XV)]
        if hw_name:
            schemes.append((hw_name, TITLE_TEXT_HW, TITLE_HW_SUB,
                            hw_rows, hw_total, TITLE_GAP_HW))
        if not xv_rows and not hw_rows:
            forms.alert(
                u"Нет ни одного помещения с потребителями.",
                warn_icon=True)
            return
        if not xv_rows:
            forms.alert(
                u"Вид ХВ пропущен: нет приборов; заполните вид ХВ.",
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

        small_id = find_text_type_id(TEXT_SMALL)
        title_id = find_text_type_id(TITLE_TYPE)
        drafting_type_id = find_drafting_type_id()

        # зазор между строками - 0 (константа, калибруется по факту);
        # зазор помещения/приборы: 60 мм при 1:10, пропорционально
        # масштабу; зазор заголовок/строки - свой для ХВ (80) и ГВ (100)
        gap_rows = ROW_GAP_MM
        gap_cols = GAP_COLS_BASE * scale_n / 10.0

        warnings = []
        # где находился пользователь при вызове плагина - вернём туда
        # в конце (если этот вид/лист не был перезаписан)
        orig_active_id = None
        try:
            orig_active_id = revit.uidoc.ActiveView.Id
        except Exception:
            pass
        lines = []
        for view_n, title, sub, rows, total, gap_title_base in schemes:
            gap_title = gap_title_base * scale_n / 10.0
            try:
                n, total_c, placed, placements = self._draw_one_scheme(
                    small_id, title_id, drafting_type_id, scale_n,
                    view_n, title, sub, rows, total, warnings,
                    gap_title, gap_rows, gap_cols)
                lines.append(
                    u"Создан вид «{}» ({}): помещений {}, "
                    u"потребителей {}".format(view_n, scale_n,
                                              n, total_c))
                if placements:
                    lines.append(u"возвращён на листы: {} из {}".format(
                        placed, placements))
            except Exception as exc:
                logger.error("SchemeDiameters: схема «%s»: %s",
                             view_n, exc)
                warnings.append(u"«{}»: ошибка отрисовки: {}".format(
                    view_n, exc))

        # вернуть пользователя туда, где он был (вид/лист на момент
        # вызова), если этот вид не был перезаписан
        try:
            orig = doc.GetElement(orig_active_id) if orig_active_id else None
            if orig is not None:
                revit.uidoc.ActiveView = orig
        except Exception as exc:
            logger.debug("SchemeDiameters: возврат активного вида: %s", exc)

        if warnings:
            lines.append(u"Замечания:")
            lines.extend(u"- " + w for w in warnings)
        self._set_status(u"\n".join(lines))


form = SchemeDiametersForm()
form.ShowDialog()

logger.debug("SchemeDiameters: окно закрыто")
