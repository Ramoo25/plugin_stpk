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
from System.Windows import TextWrapping, Thickness, Visibility
from System.Windows.Media import Colors, SolidColorBrush
from System.Windows.Controls import (Border, ComboBox, Orientation,
                                     StackPanel, TextBlock)

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

STYLE_PIPE = u"stpk_ХВС"
STYLE_DASHED = u"<Невидимые линии>"
STYLE_SOLID = u"<Обычные линии>"
TEXT_BIG = u"ADSK_Основной текст_5"
TEXT_SMALL = u"ADSK_Основной текст_3.5"

# геометрия схемы (мм чертёжного вида)
BASE_W = 30.0       # ширина прямоугольника основы
ROW_PITCH = 36.0    # шаг линий помещений (по вертикали)
BOX_H = 22.0        # высота пунктирной рамки помещения
BOX_GAP = 8.0       # зазор рамки над линией помещения
CELL_W = 38.0       # ширина ячейки потребителя
CELL_PAD = 6.0      # отступ рамки от первого потребителя
FIRST_BOX_X = 55.0  # левый край рамок помещений (от начала координат)

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
    """ГП0 -> «цок. эт.», ГП1 -> «1 эт.» и т.д.; вне списка - как есть."""
    m = re.search(u"ГП(\\d+)", level_name or u"")
    if m:
        return FLOOR_MAP.get(u"ГП" + m.group(1), u"ГП" + m.group(1))
    return level_name


def floor_sort_key(level_name):
    """Порядок этажей: ГП0..ГП3, остальное - в конец, по имени."""
    m = re.search(u"ГП(\\d+)", level_name or u"")
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
def find_style_id(name):
    """Id стиля линий по имени (GraphicsStyle)."""
    try:
        for gs in DB.FilteredElementCollector(doc).OfClass(
                DB.GraphicsStyle).ToElements():
            if element_name_safe(gs) == name:
                return gs.Id
    except Exception:
        pass
    return None


def find_text_type_id(name):
    """Id типа текста по имени (TextNoteType)."""
    try:
        for t in DB.FilteredElementCollector(doc).OfClass(
                DB.TextNoteType).ToElements():
            if element_name_safe(t) == name:
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
    if find_style_id(STYLE_PIPE) is None:
        missing.append(u"стиль линий «{}»".format(STYLE_PIPE))
    if find_style_id(STYLE_DASHED) is None:
        missing.append(u"стиль линий «{}»".format(STYLE_DASHED))
    if find_style_id(STYLE_SOLID) is None:
        missing.append(u"стиль линий «{}»".format(STYLE_SOLID))
    if find_text_type_id(TEXT_BIG) is None:
        missing.append(u"тип текста «{}»".format(TEXT_BIG))
    if find_text_type_id(TEXT_SMALL) is None:
        missing.append(u"тип текста «{}»".format(TEXT_SMALL))
    if find_drafting_type_id() is None:
        missing.append(u"тип вида «Чертёжный»")
    return missing


def delete_view_by_name(name):
    """Удаление существующих видов с данным именем (перезапись)."""
    count = 0
    try:
        victims = [v.Id for v in DB.FilteredElementCollector(doc).OfClass(
            DB.View).WhereElementIsNotElementType().ToElements()
            if not v.IsTemplate and element_name_safe(v) == name]
    except Exception:
        victims = []
    for vid in victims:
        try:
            doc.Delete(vid)
            count += 1
        except Exception as exc:
            logger.debug("SchemeDiameters: не удалить вид: %s", exc)
    return count


# ---------- примитивы схемы ----------
def make_line(view, x1, y1, x2, y2, style_id):
    """Отрезок в чертёжном виде (координаты в мм)."""
    line = DB.Line.CreateBound(
        DB.XYZ(x1 * MM, y1 * MM, 0.0),
        DB.XYZ(x2 * MM, y2 * MM, 0.0))
    dc = doc.Create.NewDetailCurve(view, line)
    try:
        if style_id is not None:
            dc.LineStyle = doc.GetElement(style_id)
    except Exception as exc:
        logger.debug("SchemeDiameters: стиль линии не применён: %s", exc)
    return dc


def make_text(view, x_mm, y_mm, text, type_id, width_mm=None):
    """Текст в чертёжном виде; xy - левый ВЕРХНИЙ угол (мм);
    width_mm - ширина обёртки (None - без обёртки)."""
    opts = DB.TextNoteOptions()
    opts.TypeId = type_id
    pos = DB.XYZ(x_mm * MM, y_mm * MM, 0.0)
    if width_mm:
        return DB.TextNote.Create(doc, view.Id, pos, width_mm * MM,
                                  text, opts)
    return DB.TextNote.Create(doc, view.Id, pos, text, opts)


def underline_note(note):
    """Подчеркнуть весь текст записки; True - получилось."""
    try:
        ft = note.GetFormattedText()
        rng = DB.TextRange(0, len(note.Text))
        ft.SetUnderlineStatus(rng, True)
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
        BG_HAS = SolidColorBrush(Colors.FromArgb(0x30, 0x87, 0xCE, 0xEB))
        BG_SELECTED = SolidColorBrush(Colors.FromArgb(0x70, 0x87, 0xCE, 0xEB))
        BG_PLAIN = SolidColorBrush(Colors.Transparent)

        self._updating = False          # защита от пересобытий
        self._rooms = collect_rooms()
        self._cfg = load_config()
        self._selected_key = None       # выбранное помещение (ключ)
        self._c_selected = None         # отмеченный потребитель (индекс)

        # восстановить выбор «Основы»
        if self._cfg.get(u"__base__") == u"coll":
            self.rb_base_coll.IsChecked = True

        self.rb_base_node.Checked += self._on_base_changed
        self.rb_base_coll.Checked += self._on_base_changed
        self.rb_in16.Checked += self._on_input_diam_changed
        self.rb_in20.Checked += self._on_input_diam_changed
        self.btn_add.Click += self._add_consumer
        self.btn_remove.Click += self._remove_consumer
        self.btn_up.Click += self._move_up
        self.btn_down.Click += self._move_down
        self.btn_draw.Click += self._draw
        self.btn_close.Click += self._close

        self._build_room_list()
        self._set_status(u"")

    # ---------- статус ----------
    def _set_status(self, text):
        self.lbl_status.Text = text

    # ---------- С1: помещения ----------
    def _build_room_list(self):
        self.pnl_rooms.Children.Clear()
        self._selected_key = None
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
        for room in self._rooms:
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
            if idx >= len(self._rooms):
                break
            key = room_key(self._rooms[idx])
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
        """Конфигурация помещения (без создания записи)."""
        item = self._cfg.get(key)
        if not isinstance(item, dict):
            return {"consumers": [], "input": DEFAULT_DIAM}
        consumers = item.get("consumers") or []
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
            for idx, (name, diam) in enumerate(consumers):
                self._add_c2_row(idx, name, diam)
        finally:
            self._updating = False

    def _add_c2_row(self, idx, name, diam):
        border = Border()
        border.Padding = Thickness(4, 2, 4, 2)
        border.Background = BG_PLAIN
        row = StackPanel()
        row.Orientation = Orientation.Horizontal

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

        row.Children.Add(name_box)
        row.Children.Add(diam_box)
        border.Child = row
        self.pnl_consumers.Children.Add(border)
        self._c_rows.append({"border": border,
                             "name": name_box, "diam": diam_box})

        name_box.TextChanged += self._on_consumer_changed
        name_box.SelectionChanged += self._on_consumer_changed
        diam_box.SelectionChanged += self._on_consumer_changed
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
        """Строки С2 -> конфигурация помещения (+правило вводного φ)."""
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
            consumers.append([nm, dm])
        item = self._cfg.setdefault(key, {})
        item["consumers"] = consumers
        if any(d == 20 for nm, d in consumers):
            item["input"] = 20  # авто-переключение вводной на 20
        elif item.get("input") not in (16, 20):
            item["input"] = DEFAULT_DIAM
        save_config(self._cfg)
        self._update_room_row_colors()
        self._apply_diam_lock()

    def _apply_diam_lock(self):
        """Радио «Диаметр вводной трубы» - по выбранному помещению;
        есть потребитель 20 мм -> 16 заблокировано."""
        key = self._selected_key
        self._updating = True
        try:
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
    def _on_base_changed(self, sender, args):
        self._cfg[u"__base__"] = (
            u"coll" if self.rb_base_coll.IsChecked else u"node")
        save_config(self._cfg)

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
        consumers.append([DEFAULT_CONSUMER, DEFAULT_DIAM])
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
        self._cfg.setdefault(key, {})["consumers"] = consumers
        save_config(self._cfg)
        self._rebuild_c2(select_index=j)

    def _move_up(self, sender, args):
        self._move(-1)

    def _move_down(self, sender, args):
        self._move(+1)

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

        drawing = []
        for room in self._rooms:
            cfg = self._room_cfg(room_key(room))
            consumers = []
            for nm, d in cfg["consumers"]:
                nm = unicode(nm or u"").strip()
                if nm:
                    consumers.append((nm, int(d)))
            if consumers:
                drawing.append((room, consumers, cfg["input"]))
        if not drawing:
            forms.alert(
                u"Нет ни одного помещения с потребителями.",
                warn_icon=True)
            return

        missing = missing_assets()
        if missing:
            forms.alert(
                u"В проекте не найдено:\n" + u"\n".join(missing) +
                u"\n\nДобавьте их в проект (или проверьте имена) "
                u"и повторите.",
                title="Не хватает ресурсов проекта",
                warn_icon=True)
            return

        pipe_id = find_style_id(STYLE_PIPE)
        dashed_id = find_style_id(STYLE_DASHED)
        solid_id = find_style_id(STYLE_SOLID)
        big_id = find_text_type_id(TEXT_BIG)
        small_id = find_text_type_id(TEXT_SMALL)
        drafting_type_id = find_drafting_type_id()

        base_text = (u"Коллектор холодного водоснабжения"
                     if self.rb_base_coll.IsChecked else u"Узел ввода воды")

        warnings = []
        underline_ok = True
        replaced = delete_view_by_name(view_name)
        if replaced:
            warnings.append(u"перезаписан прежний вид «{}»".format(view_name))

        with revit.Transaction(u"ПлагИН: рисование схемы диаметров", doc):
            view = DB.ViewDrafting.Create(doc, drafting_type_id)
            try:
                view.Name = view_name
            except Exception as exc:
                warnings.append(u"имя вида не записано")
                logger.debug("SchemeDiameters: имя вида: %s", exc)
            try:
                view.Scale = scale_n
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
            line_ys = [-i * ROW_PITCH for i in range(n)]
            y_top = line_ys[0] + BOX_H + BOX_GAP + 8
            y_bot = line_ys[-1] - 8
            center_y = (y_top + y_bot) / 2.0

            # подпись основы: создаём до прямоугольника, чтобы знать
            # её протяжённость, затем поворачиваем на 90°
            base_note = make_text(view, BASE_W / 2 - 40, center_y + 3,
                                  base_text, big_id, width_mm=80)
            try:
                bbox = base_note.get_BoundingBox(view)
                cx = (bbox.Min.X + bbox.Max.X) / 2.0
                cy = (bbox.Min.Y + bbox.Max.Y) / 2.0
                text_ext = (bbox.Max.X - bbox.Min.X) / MM  # после
                # поворота станет вертикальной протяжённостью
                axis = DB.Line.CreateBound(
                    DB.XYZ(cx, cy, 0.0), DB.XYZ(cx, cy, 1.0))
                DB.ElementTransformUtils.RotateElement(
                    doc, base_note.Id, axis, math.pi / 2)
            except Exception as exc:
                logger.debug("SchemeDiameters: поворот текста: %s", exc)
                warnings.append(u"текст основы без поворота (буквы "
                                u"столбиком недоступны, см. лог)")

            rect_top = max(y_top, cy / MM + text_ext / 2 + 5)
            rect_bot = min(y_bot, cy / MM - text_ext / 2 - 5)
            make_line(view, 0, rect_top, BASE_W, rect_top, solid_id)
            make_line(view, 0, rect_bot, BASE_W, rect_bot, solid_id)
            make_line(view, 0, rect_bot, 0, rect_top, solid_id)
            make_line(view, BASE_W, rect_bot, BASE_W, rect_top, solid_id)

            total_consumers = 0
            for i, (room, consumers, input_d) in enumerate(drawing):
                y = line_ys[i]
                total_consumers += len(consumers)
                box_left = FIRST_BOX_X
                centers = [box_left + CELL_PAD + j * CELL_W + CELL_W / 2
                           for j in range(len(consumers))]
                last_cx = centers[-1]

                # горизонтальная линия помещения + φ вводного
                make_line(view, BASE_W, y, last_cx, y, pipe_id)
                make_text(view, BASE_W + 4, y + 5, u"φ{}".format(input_d),
                          small_id)

                # пунктирная рамка помещения
                box_top = y + BOX_H + BOX_GAP
                box_bottom = y + BOX_GAP
                box_right = last_cx + CELL_W / 2 + CELL_PAD
                make_line(view, box_left, box_top, box_right, box_top,
                          dashed_id)
                make_line(view, box_left, box_bottom, box_right,
                          box_bottom, dashed_id)
                make_line(view, box_left, box_bottom, box_left, box_top,
                          dashed_id)
                make_line(view, box_right, box_bottom, box_right,
                          box_top, dashed_id)

                # подпись помещения (подчёркнутая)
                lbl = make_text(view, box_left, box_top + 2,
                                room_label(room), small_id,
                                width_mm=(box_right - box_left + 20))
                if not underline_note(lbl):
                    underline_ok = False

                # потребители: имя, φ, отвод к линии
                for j, (nm, d) in enumerate(consumers):
                    cx = centers[j]
                    make_text(view, cx - CELL_W / 2 + 1, y + 28, nm,
                              small_id, width_mm=CELL_W - 2)
                    make_text(view, cx - 8, y + 16, u"φ{}".format(d),
                              small_id, width_mm=16)
                    make_line(view, cx, y + 10, cx, y, pipe_id)

        if not underline_ok:
            warnings.append(u"подписи помещений без подчёркивания "
                            u"(Revit не дал форматировать текст)")

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
