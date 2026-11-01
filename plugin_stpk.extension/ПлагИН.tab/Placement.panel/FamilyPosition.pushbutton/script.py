# -*- coding: utf-8 -*-
"""«Разместить» - плагин №3 набора plugin_stpk (IronPython 2.7).

Слева - строки загруженных семейств: чекбокс + имя + выпадающий список
типоразмеров. Справа - связь (если несколько), уровень проекта и помещение
из связи (помещения принадлежат файлу связи). «Разместить»: по одному
экземпляру каждого отмеченного семейства, случайные точки внутри полигона
помещения; координаты связи пересчитываются в хост; неудачи (семейства,
требующие хоста) - в отчёт, остальные ставятся.

Помещения читаются из связи: точка метки room.Location.Point, контур из
GetBoundarySegments, пересчёт координат RevitLinkInstance.GetTotalTransform.
Контур считается в координатах хоста, поэтому случайные точки попадают
в помещение независимо от общих координат, смещения и поворота связи.
"""

import random
import re

from pyrevit import revit, forms, script, DB

# WPF-обёртки (Revit уже загрузил PresentationFramework)
from System.Windows import (Visibility, Thickness, TextTrimming,
                            VerticalAlignment)
from System.Windows.Controls import (CheckBox, ComboBox, Orientation,
                                     StackPanel, TextBlock)

logger = script.get_logger()

doc = revit.doc

# В Revit 2022 StructuralType лежит в пространстве Autodesk.Revit.DB.Structure,
# отдельные подпространства через ленивый модуль DB резолвятся не всегда
try:
    NON_STRUCTURAL = DB.Structure.StructuralType.NonStructural
except Exception:
    from Autodesk.Revit.DB.Structure import StructuralType as _StructuralType
    NON_STRUCTURAL = _StructuralType.NonStructural

if not doc:
    forms.alert(
        "Откройте проект Revit.",
        title="Нет документа",
        exitscript=True,
    )


# BuiltInParameter-фолбэки для имён (чтобы не зависеть от .Name,
# который в IronPython иногда падает MissingMemberException)
BP_SYMBOL_NAME = DB.BuiltInParameter.SYMBOL_NAME_PARAM       # "Type Name"
BP_FAMILY_NAME = DB.BuiltInParameter.SYMBOL_FAMILY_NAME_PARAM  # "Family Name"
BP_ROOM_NAME = DB.BuiltInParameter.ROOM_NAME                 # "Name"
BP_ROOM_NUMBER = DB.BuiltInParameter.ROOM_NUMBER             # "Number"
BP_DATUM_NAME = DB.BuiltInParameter.DATUM_TEXT               # "Name" (уровни)


def element_name_safe(el, fallback_bps=()):
    """Имя элемента с фолбэком через BuiltInParameter-параметры.

    .Name в IronPython/Revit-модели иногда недоступен (MissingMemberException)
    для некоторых классов (замечено на Family/FamilySymbol) - тогда читаем
    имя строковым параметром.
    """
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


def symbol_display_name(sym):
    """Имя типоразмера с фолбэком через параметр «Type Name»."""
    try:
        n = sym.Name
        if n:
            return n
    except Exception:
        pass
    p = sym.get_Parameter(BP_SYMBOL_NAME)
    return (p.AsString() or u"(без имени)") if p else u"(без имени)"


def collect_families(doc):
    """Группирует загруженные семейства: [(Family, подпись, [FamilySymbol,...])].

    Собираем FamilySymbol напрямую (без .WhereElementIsNotElementType -
    типоразмеры сами являются ElementTypes). Имя семейства берём через
    ElementType.FamilyName, имя типоразмера - через symbol_display_name
    (фолбэк на BuiltInParameter.SYMBOL_NAME_PARAM): обращение .Name
    в IronPython падает MissingMemberException на некоторых типах
    (Family/FamilySymbol), параметр надёжнее.
    """
    groups = {}

    symbols_all = list(
        DB.FilteredElementCollector(doc).OfClass(DB.FamilySymbol).ToElements()
    )
    for sym in symbols_all:
        try:
            fam = sym.Family
        except Exception:
            continue
        key = fam.Id.IntegerValue
        if key not in groups:
            hint = family_host_hint(fam)
            try:
                fam_name = sym.FamilyName or u"?"
            except Exception:
                fam_name = (element_name_safe(sym, (BP_FAMILY_NAME,))
                            or u"?")
            label = fam_name + (u" ({})".format(hint) if hint else u"")
            groups[key] = {"fam": fam, "label": label, "symbols": []}
        groups[key]["symbols"].append((symbol_display_name(sym), sym))

    rows = []
    for g in groups.itervalues():
        g["symbols"].sort(key=lambda t: natural_key(t[0]))
        rows.append((g["fam"], g["label"],
                     [s for display_name, s in g["symbols"]]))
    rows.sort(key=lambda t: natural_key(t[1]))
    return rows


def collect_levels(doc):
    """Уровни проекта: [(имя, Level)], натурально по имени."""
    levels = list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.Level)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    pairs = [(element_name_safe(lvl, (BP_DATUM_NAME,)), lvl)
             for lvl in levels]
    pairs.sort(key=lambda t: natural_key(t[0]))
    return pairs


def collect_link_instances(doc):
    """Все экземпляры связей: [(имя, RevitLinkInstance)], натурально."""
    links = list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.RevitLinkInstance)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    pairs = [
        (element_name_safe(l, (DB.BuiltInParameter.RVT_LINK_INSTANCE_NAME,)), l)
        for l in links
    ]
    pairs.sort(key=lambda t: natural_key(t[0]))
    return pairs


def room_display_number(room):
    """Номер помещения с фолбэком через параметр (на случай глюка .Number)."""
    try:
        n = room.Number
        if n:
            return n
    except Exception:
        pass
    p = room.get_Parameter(BP_ROOM_NUMBER)
    return (p.AsString() or u"?") if p else u"?"


def room_polygon_xy(room, to_host):
    """Контур помещения в координатах хоста: список [(x, y), ...].

    Берём сегменты внешнего контура (первый). Дуги апроксимируем
    Tessellate(). Пустой контур не ошибка - вернём [].
    """
    opts = DB.SpatialElementBoundaryOptions()
    loops = room.GetBoundarySegments(opts)
    if not loops:
        return []
    pts = []
    for seg in loops[0]:
        curve = seg.GetCurve()
        for p in curve.Tessellate():
            ph = to_host.OfPoint(p)
            pts.append((ph.X, ph.Y))
    return pts


def point_in_polygon(x, y, poly):
    """Проверка «точка в полигоне» (алгоритм пересечения лучом, 2D)."""
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y):
            x_int = (xj - xi) * (y - yi) / (yj - yi) + xi
            if x < x_int:
                inside = not inside
        j = i
    return inside


def random_point_in(poly, rng, tries=12):
    """Случайная точка внутри полигона; если контур пуст - None."""
    if not poly:
        return None
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    for i in range(tries):
        x = x0 + rng.random() * (x1 - x0)
        y = y0 + rng.random() * (y1 - y0)
        if point_in_polygon(x, y, poly):
            return DB.XYZ(x, y, 0)
    return None


def family_host_hint(fam):
    """Подсказка по способу размещения семейства (FamilyPlacementType)."""
    try:
        if fam.FamilyPlacementType == DB.FamilyPlacementType.HostBased:
            return u"требует хоста"
        return u""
    except Exception:
        return u""


class PlacementForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        self._families = collect_families(doc)
        self._levels = collect_levels(doc)
        self._links = collect_link_instances(doc)
        self._rooms = []
        self._rows = []

        self._rng = random.Random()

        # если связей нет - дальше идти некуда
        if not self._links:
            forms.alert(
                u"В проекте нет файлов связей (архитектуры).\n"
                u"Без связи невозможно взять помещения.",
                title="Связи не найдены",
                exitscript=True,
            )
            return

        self._build_family_rows()
        self.cb_level.ItemsSource = [l[0] for l in self._levels]
        self.cb_room.ItemsSource = []

        if len(self._links) > 1:
            self.cb_link.ItemsSource = [l[0] for l in self._links]
            self.cb_link.SelectedIndex = 0
        else:
            self.lbl_link.Visibility = Visibility.Collapsed
            self.cb_link.Visibility = Visibility.Collapsed
            self.cb_link.ItemsSource = [self._links[0][0]]
            self.cb_link.SelectedIndex = 0

        self.cb_level.SelectionChanged += self._on_level_changed
        self.cb_room.SelectionChanged += self._refresh_place_enabled
        self.btn_all.Click += self._check_all
        self.btn_none.Click += self._uncheck_all
        self.tb_search.TextChanged += self._on_search_changed
        self.chk_only.Checked += self._on_only_selected_changed
        self.chk_only.Unchecked += self._on_only_selected_changed
        self.btn_place.Click += self._place
        self.btn_close.Click += self._close

        self._set_status(u"")

    # ---------- левая колонка: строки семейств ----------
    def _build_family_rows(self):
        self.pnl_families.Children.Clear()
        self._rows = []
        for fam, fam_label, symbols in self._families:
            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(0, 2, 6, 2)

            chk = CheckBox()
            chk.VerticalAlignment = VerticalAlignment.Center
            chk.Margin = Thickness(0, 0, 6, 0)

            label = TextBlock()
            label.Text = fam_label
            label.VerticalAlignment = VerticalAlignment.Center
            label.Margin = Thickness(0, 0, 8, 0)
            label.Width = 220
            label.TextTrimming = TextTrimming.WordEllipsis
            label.ToolTip = fam_label

            combo = ComboBox()
            combo.Width = 150
            combo.IsEnabled = False
            for sym in symbols:
                combo.Items.Add(symbol_display_name(sym))
            if symbols:
                combo.SelectedIndex = 0

            chk.Checked += self._on_family_toggled
            chk.Unchecked += self._on_family_toggled

            row.Children.Add(chk)
            row.Children.Add(label)
            row.Children.Add(combo)
            self.pnl_families.Children.Add(row)

            self._rows.append(
                (row, chk, label, combo, fam, symbols))

    def _checked_rows(self):
        """[(Family, FamilySymbol или None, подпись)] по отмеченным."""
        result = []
        for row, chk, label, combo, fam, symbols in self._rows:
            if bool(chk.IsChecked):
                idx = combo.SelectedIndex if symbols else -1
                symbol = symbols[idx] if idx >= 0 else None
                result.append((fam, symbol, label))
        return result

    def _check_all(self, sender, args):
        # отмечаем только видимые строки (не скрытые фильтром поиска)
        for row, chk, label, combo, fam, symbols in self._rows:
            if row.Visibility == Visibility.Visible:
                chk.IsChecked = True

    def _uncheck_all(self, sender, args):
        # снимаем со всех, включая скрытые фильтром
        for row, chk, label, combo, fam, symbols in self._rows:
            chk.IsChecked = False

    def _on_family_toggled(self, sender, args):
        # включение/выключение списка типоразмеров у этой строки
        for row, chk, label, combo, fam, symbols in self._rows:
            if chk is sender:
                combo.IsEnabled = bool(chk.IsChecked)
                break
        self._refresh_place_enabled(None, None)

    def _on_search_changed(self, sender, args):
        """Динамический фильтр списка семейств по части имени."""
        self._refresh_family_visibility()

    def _on_only_selected_changed(self, sender, args):
        """Чекбокс «Только выбранные»: строки = отмеченные семейства,
        текстовый фильтр при этом игнорируется и гаснет."""
        only_checked = bool(self.chk_only.IsChecked)
        self.tb_search.IsEnabled = not only_checked
        self._refresh_family_visibility()
        if only_checked and not self._checked_rows():
            self._set_status(
                u"«Только выбранные»: пока нет ни одного отмеченного "
                u"семейства - поставьте галочки в списке.")

    def _refresh_family_visibility(self):
        """Видимость строк: фильтр имени или режим «только выбранные»."""
        only_checked = bool(self.chk_only.IsChecked)
        query = u""
        if only_checked:
            self.tb_search.IsEnabled = False
        else:
            self.tb_search.IsEnabled = True
            try:
                query = unicode(self.tb_search.Text).strip().lower()
            except Exception:
                query = u""
        for row, chk, label, combo, fam, symbols in self._rows:
            try:
                name = unicode(label.Text).lower()
            except Exception:
                name = u""
            if only_checked:
                visible = bool(chk.IsChecked)
            else:
                visible = query in name
            row.Visibility = (Visibility.Visible if visible
                              else Visibility.Collapsed)

    # ---------- правая колонка: место ----------
    def _set_status(self, text):
        self.lbl_status.Text = text

    def _selected_link(self):
        idx = self.cb_link.SelectedIndex
        if idx is None or idx < 0 or idx >= len(self._links):
            return None
        return self._links[idx][1]

    def _on_level_changed(self, sender, args):
        try:
            self._rooms = []
            self.cb_room.ItemsSource = []
            level_name = self.cb_level.SelectedItem
            link = self._selected_link()
            if not level_name or link is None:
                return

            link_doc = link.GetLinkDocument()
            if link_doc is None:
                self._set_status(
                    u"(связь недоступна - покажите её на виде/в Revit)")
                return

            # ищем уровень связи с совпадающим именем
            matched = None
            for ll in DB.FilteredElementCollector(link_doc).OfClass(
                    DB.Level).WhereElementIsNotElementType().ToElements():
                if element_name_safe(ll, (BP_DATUM_NAME,)) == level_name:
                    matched = ll
                    break

            # В Revit 2022 класс Room лежит в Autodesk.Revit.DB.Architecture,
            # поэтому DB.Room недоступен из ленивого модуля DB - собираем
            # по категории помещений, это не зависит от класса
            rooms = []
            for room in DB.FilteredElementCollector(link_doc).OfCategory(
                    DB.BuiltInCategory.OST_Rooms
            ).WhereElementIsNotElementType().ToElements():
                if room.Location is None:
                    continue
                cur_display = element_name_safe(room, (BP_ROOM_NAME,))
                if matched is not None:
                    if room.LevelId != matched.Id:
                        continue
                    label = u"{} - {}".format(room_display_number(room), cur_display)
                else:
                    lvl = link_doc.GetElement(room.LevelId)
                    lvl_name = (element_name_safe(lvl, (BP_DATUM_NAME,))
                                if lvl else u"?")
                    label = u"{} - {} (уровень: {})".format(
                        room_display_number(room), cur_display, lvl_name)
                rooms.append((label, room))
            rooms.sort(key=lambda t: natural_key(t[0]))

            self._rooms = rooms
            self.cb_room.ItemsSource = [r[0] for r in rooms]
            if not rooms:
                self._set_status(u"(на этом уровне помещений в связи нет)")
        except Exception as exc:
            logger.error("Placement: ошибка сбора помещений: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _refresh_place_enabled(self, sender, args):
        ok = (self.cb_room.SelectedItems.Count > 0
              and len(self._checked_rows()) > 0)
        self.btn_place.IsEnabled = ok

    # ---------- размещение ----------
    def _place(self, sender, args):
        try:
            link = self._selected_link()
            if link is None:
                forms.alert("Выберите файл связи.", warn_icon=True)
                return
            # выбранные помещения (мультивыбор)
            picked = set(str(item) for item in self.cb_room.SelectedItems)
            rooms = []
            by_label = {}
            for lbl, r in self._rooms:
                if lbl in picked and lbl not in by_label:
                    by_label[lbl] = r
                    rooms.append((lbl, r))
            if not rooms:
                forms.alert("Выберите хотя бы одно помещение.", warn_icon=True)
                return
            level_name = self.cb_level.SelectedItem
            level = None
            for lbl, lvl in self._levels:
                if lbl == level_name:
                    level = lvl
                    break
            if level is None:
                forms.alert("Выберите уровень.", warn_icon=True)
                return

            selection = self._checked_rows()
            if not selection:
                return

            to_host = link.GetTotalTransform()

            placed = []
            failed = []
            activated = set()
            with revit.Transaction(u"ПлагИН: размещение семейств", doc):
                for room_lbl, room in rooms:
                    poly = room_polygon_xy(room, to_host)
                    label_host = to_host.OfPoint(room.Location.Point)
                    who = room_lbl
                    for fam, symbol, label in selection:
                        try:
                            if symbol is None:
                                failed.append(
                                    (u"{} → {}".format(label, who),
                                     u"нет типоразмера"))
                                continue
                            key = symbol.Id.IntegerValue
                            if key not in activated:
                                symbol.Activate()
                                doc.Regenerate()
                                activated.add(key)
                            # случайная точка внутри этого помещения
                            pt = random_point_in(poly, self._rng)
                            if pt is None:
                                pt = DB.XYZ(label_host.X, label_host.Y, 0)
                            doc.Create.NewFamilyInstance(
                                pt, symbol, level, NON_STRUCTURAL)
                            placed.append(
                                u"{} → {}".format(label, who))
                        except Exception as exc:
                            failed.append(
                                (u"{} → {}".format(label, who), str(exc)))
                            logger.debug("Placement: %s → %s: %s",
                                         label, who, exc)

            status = u"Размещено: {} (помещений: {})".format(
                len(placed), len(rooms))
            if failed:
                status += u"\nНе размещено: {}".format(len(failed))
                for name, why in failed[:5]:
                    status += u"\n• {}: {}".format(name, why)
                if len(failed) > 5:
                    status += u"\n• ... и ещё {}".format(len(failed) - 5)
            self._set_status(status)
            self._refresh_place_enabled(None, None)

        except Exception as exc:
            logger.error("Placement: ошибка размещения: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _close(self, sender, args):
        self.Close()


form = PlacementForm()
form.ShowDialog()

logger.debug("Placement: окно закрыто")
