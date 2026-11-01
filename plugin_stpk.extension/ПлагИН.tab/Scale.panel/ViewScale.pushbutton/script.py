# -*- coding: utf-8 -*-
"""«Масштаб видов» - плагин №4 набора plugin_stpk (IronPython 2.7).

Иерархический список: первый уровень - листы проекта, второй - виды,
размещённые на листе (без легенд и спецификаций - только виды).
Виды помечаются чекбоксами, в правой колонке вводится один общий
масштаб (число, например 100 для 1:100, или строка «1:100»), кнопка
«Применить» ставит его всем отмеченным видам. «Очистить выбор» снимает
все галочки.

Сбор и запись масштаба - по эталону плагина №2: очистка от спецификаций
через классы ViewSchedule/ViewLegend, запись через параметр
BuiltInParameter.VIEW_SCALE + IsValidViewScale (1 - 24000).
"""

import re

from pyrevit import revit, forms, script, DB

# WPF-обёртки (Revit уже загрузил PresentationFramework)
from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import (CheckBox, TextBlock, TreeViewItem)

logger = script.get_logger()

doc = revit.doc

if not doc:
    forms.alert(
        "Откройте проект Revit.",
        title="Нет документа",
        exitscript=True,
    )

# BuiltInParameter-фолбэки имён (гарантия против капризов .Name в IronPython)
BP_SHEET_NUMBER = DB.BuiltInParameter.SHEET_NUMBER
BP_SHEET_NAME = DB.BuiltInParameter.SHEET_NAME
BP_VIEW_NAME = DB.BuiltInParameter.VIEW_NAME
BP_VIEW_SCALE = DB.BuiltInParameter.VIEW_SCALE

STANDARD_SCALES = [
    u"1 : 1", u"1 : 2", u"1 : 5", u"1 : 10", u"1 : 20", u"1 : 25",
    u"1 : 50", u"1 : 75", u"1 : 100", u"1 : 150", u"1 : 200",
    u"1 : 250", u"1 : 400", u"1 : 500", u"1 : 750", u"1 : 1000",
    u"1 : 1500", u"1 : 2000", u"1 : 2500", u"1 : 5000", u"1 : 7500",
    u"1 : 10000", u"1 : 15000", u"1 : 20000",
]


def element_name_safe(el, fallback_bps=()):
    """Имя элемента; .Name в IronPython иногда unavailable - фолбэк на
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


def sheet_number_safe(sheet):
    """Номер листа с фолбэком через параметр."""
    try:
        n = sheet.SheetNumber
        if n:
            return n
    except Exception:
        pass
    p = sheet.get_Parameter(BP_SHEET_NUMBER)
    return (p.AsString() or u"?") if p else u"?"


def collect_sheets(doc):
    """Листы проекта: [(подпись «номер - имя», sheet)], натурально."""
    sheets = list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.ViewSheet)
        .WhereElementIsNotElementType()
        .ToElements()
    )
    pairs = []
    for sheet in sheets:
        label = u"{} - {}".format(
            sheet_number_safe(sheet),
            element_name_safe(sheet, (BP_SHEET_NAME,)))
        pairs.append((label, sheet))
    pairs.sort(key=lambda t: natural_key(t[0]))
    return pairs


def collect_sheet_views(doc, sheet):
    """Все виды на листе, кроме легенд и спецификаций.

    [(подпись, view)], натурально по имени вида. Спецификации
    (ViewSchedule) сюда и без того почти не попадают из GetAllPlacedViews,
    но отсеиваем явно вместе с легендами (ViewLegend).
    """
    items = []
    for view_id in sheet.GetAllPlacedViews():
        view = doc.GetElement(view_id)
        if view is None:
            continue
        try:
            if (isinstance(view, DB.ViewSchedule)
                    or isinstance(view, DB.ViewLegend)):
                continue
        except Exception:
            pass
        name = element_name_safe(view, (BP_VIEW_NAME,))
        items.append((name if name else u"(без имени)", view))
    items.sort(key=lambda t: natural_key(t[0]))
    return items


def parse_scale(text):
    """Масштаб из введённой строки: «100» или «1:100» -> 100, иначе None."""
    t = (text or u"").strip()
    if u":" in t:
        t = t.split(u":")[-1].strip()
    try:
        return int(t)
    except (ValueError, TypeError):
        return None


class ScaleForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        self._sheets = collect_sheets(doc)
        # (chk, view, подпись листа) - все чекбоксы видов
        self._checks = []

        self._build_tree()
        self._refresh_counts()

        self.cb_scale.ItemsSource = list(STANDARD_SCALES)
        self.cb_scale.SelectedIndex = (
            STANDARD_SCALES.index(u"1 : 100"))

        self.btn_clear.Click += self._clear_all
        self.btn_apply.Click += self._apply
        self.btn_close.Click += self._close
        self.cb_scale.SelectionChanged += self._on_scale_picked

        self._set_status(u"")

    # ---------- дерево ----------
    def _build_tree(self):
        """Строит дерево «лист - виды с чекбоксами»."""
        self.tv_views.Items.Clear()
        self._checks = []
        for sheet_label, sheet in self._sheets:
            node = TreeViewItem()
            node.Header = sheet_label
            node.IsExpanded = True
            node.Margin = Thickness(0, 2, 0, 2)

            for view_label, view in collect_sheet_views(doc, sheet):
                leaf = TreeViewItem()
                leaf.Margin = Thickness(0)

                chk = CheckBox()
                chk.Content = view_label
                chk.VerticalAlignment = VerticalAlignment.Center
                chk.Checked += self._on_check_changed
                chk.Unchecked += self._on_check_changed

                leaf.Header = chk
                node.Items.Add(leaf)
                self._checks.append((chk, view, sheet_label))

            self.tv_views.Items.Add(node)

    def _on_check_changed(self, sender, args):
        self._refresh_counts()

    def _refresh_counts(self):
        """Подпись «Отмечено видов» и доступность «Применить»."""
        count = len(self._selected_views())
        self.lbl_selection.Text = u"Отмечено видов: {}".format(count)
        self.btn_apply.IsEnabled = (count > 0)

    def _selected_views(self):
        """Отмеченные виды БЕЗ повторов: [(подпись, view)] по уникальному Id
        (один вид может лежать на нескольких листах)."""
        picked = []
        by_id = {}
        for chk, view, sheet_label in self._checks:
            if not bool(chk.IsChecked):
                continue
            key = view.Id.IntegerValue
            if key in by_id:
                continue
            by_id[key] = True
            picked.append((view_label_from(chk), view))
        return picked

    # ---------- масштаб ----------
    def _on_scale_picked(self, sender, args):
        # выбор из списка стандартных масштабов -> в текст (для разбора)
        item = self.cb_scale.SelectedItem
        if item is not None:
            self.cb_scale.Text = unicode(item)

    def _current_scale(self):
        """Распарсенный масштаб или None (с сообщением пользователю)."""
        scale = parse_scale(self.cb_scale.Text)
        if scale is None:
            forms.alert(
                u"Масштаб должен быть целым числом (например, 100 "
                u"для 1:100) или записью вида «1:100».",
                title="Проверка ввода",
                warn_icon=True,
            )
            return None
        if scale < 1 or scale > 24000:
            forms.alert(
                u"Масштаб вне допустимого диапазона (1 - 24000).",
                title="Проверка ввода",
                warn_icon=True,
            )
            return None
        return scale

    # ---------- операции ----------
    def _clear_all(self, sender, args):
        for chk, view, sheet_label in self._checks:
            if bool(chk.IsChecked):
                chk.IsChecked = False
        self._refresh_counts()

    def _apply(self, sender, args):
        try:
            scale = self._current_scale()
            if scale is None:
                return
            picked = self._selected_views()
            if not picked:
                return

            changed = []
            failed = []
            unchanged = 0
            with revit.Transaction(u"ПлагИН: масштаб видов", doc):
                for label, view in picked:
                    try:
                        # VIEW_SCALE - параметр read-only; масштаб пишется
                        # свойством View.Scale (см. док, Revit 2022)
                        old = view.Scale
                        if old is None or int(old) == scale:
                            unchanged += 1
                            continue
                        if not view.IsValidViewScale(scale):
                            failed.append(
                                (label,
                                 u"масштаб 1:{} недопустим для вида".format(
                                     scale)))
                            continue
                        view.Scale = scale
                        changed.append(label)
                    except Exception as exc:
                        failed.append((label, str(exc)))
                        logger.debug("ViewScale: %s: %s", label, exc)

            # отчёт: сначала счёт, затем неудачи построчно
            lines = []
            if changed:
                lines.append(u"Масштаб 1:{} установлен у {} видов".format(
                    scale, len(changed)))
            if unchanged:
                lines.append(u"Уже был таким: {}".format(unchanged))
            if failed:
                lines.append(u"")
                lines.append(u"Не изменено: {}".format(len(failed)))
                for name, why in failed[:5]:
                    lines.append(u"- {}: {}".format(name, why))
                if len(failed) > 5:
                    lines.append(u"- ... и ещё {}".format(len(failed) - 5))
            self._set_status(u"\n".join(lines) if lines
                             else u"Изменений нет")

        except Exception as exc:
            logger.error("ViewScale: ошибка применения масштаба: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _set_status(self, text):
        self.lbl_status.Text = text

    def _close(self, sender, args):
        self.Close()


def view_label_from(chk):
    """Подпись вида из чекбокса (для отчётов)."""
    try:
        return unicode(chk.Content)
    except Exception:
        return u"(вид)"


form = ScaleForm()
form.ShowDialog()

logger.debug("ViewScale: окно закрыто")
