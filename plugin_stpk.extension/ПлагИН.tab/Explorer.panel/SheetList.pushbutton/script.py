# -*- coding: utf-8 -*-
"""Листы проекта + сущности на выбранном листе (IronPython 2.7).

Листы собираются через FilteredElementCollector и сортируются "натурально".
Клик по листу собирает всё размещённое на нём:
  - виды (модельные: планы, разрезы, фасады, 3D)     - GetAllPlacedViews
  - чертёжные виды (DraftingView/Detail)             - GetAllPlacedViews
  - легенды                                          - GetAllPlacedViews
  - спецификации (ViewSchedule) с префиксом "СП "    - ScheduleSheetInstance
Внутренние таблицы листа ("<Ведомость изменений>", всегда в угловых
скобках) отбрасываются.
Четыре чекбокса управляют видимостью категорий в нижнем списке.
"""

import re

from pyrevit import revit, forms, script, DB

logger = script.get_logger()
output = script.get_output()

doc = revit.doc

if not doc:
    forms.alert(
        "Откройте проект Revit, чтобы посмотреть листы.",
        title="Нет документа",
        exitscript=True,
    )


def natural_key(text):
    """Ключ "естественной" сортировки: числа сравниваются как числа.

    "В-10" разбирается как ["В-", 10, ""], поэтому В-1 < В-2 < В-10,
    а не В-1 < В-10 < В-2, как при обычной сортировке строк.
    """
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def collect_sheets(doc):
    """Все листы проекта, отсортированные по номеру листа (натурально).

    Возвращает (список подписей, dict: подпись -> ViewSheet).
    Формат подписи: "номер - название". Номера берём только через
    BuiltInParameter (SHEET_NUMBER/SHEET_NAME), иначе сломается на другой локали.
    """
    sheets = list(
        DB.FilteredElementCollector(doc)
        .OfClass(DB.ViewSheet)
        .WhereElementIsNotElementType()
        .ToElements()
    )

    pairs = []
    for sheet in sheets:
        p_num = sheet.get_Parameter(DB.BuiltInParameter.SHEET_NUMBER)
        p_name = sheet.get_Parameter(DB.BuiltInParameter.SHEET_NAME)
        num = p_num.AsString() if p_num else ""
        name = p_name.AsString() if p_name else ""
        label = u"{} - {}".format(num, name) if name else num
        pairs.append((num or u"", label, sheet))

    pairs.sort(key=lambda t: natural_key(t[0]))
    labels = [p[1] for p in pairs]
    by_label = dict((p[1], p[2]) for p in pairs)
    return labels, by_label


def classify_placed_view(view):
    """Категория вида на листе: view / drafting / legend.

    Спецификации сюда не попадают (GetAllPlacedViews их не возвращает).
    Detail относим к чертёжным - это родственные типы (фрагменты/детали узлов).
    """
    vt = view.ViewType
    if vt == DB.ViewType.Legend:
        return "legend"
    if vt == DB.ViewType.DraftingView or vt == DB.ViewType.Detail:
        return "drafting"
    return "view"


def collect_sheet_items(doc, sheet):
    """Все сущности на листе в виде списка (категория, подпись).

    Категории: view / drafting / legend / sched. Спецификации получают
    префикс "СП ". Внутренние таблицы листа (имя в угловых скобках)
    не показываются. Итог сортируется натурально по подписи.
    """
    items = []

    for view_id in sheet.GetAllPlacedViews():
        view = doc.GetElement(view_id)
        if view is None:
            continue
        items.append((classify_placed_view(view), view.Name))

    for ssi in DB.FilteredElementCollector(doc).OfClass(
            DB.ScheduleSheetInstance).WhereElementIsNotElementType().ToElements():
        if ssi.OwnerViewId == sheet.Id:
            if (ssi.Name or u"").startswith(u"<"):
                continue  # внутренняя таблица листа (ведомость изменений и пр.)
            sched = doc.GetElement(ssi.ScheduleId)
            if sched is not None:
                items.append(("sched", u"СП " + sched.Name))

    items.sort(key=lambda it: natural_key(it[1]))
    return items


# Чекбокс формы -> категория сущности
CHECKBOX_KINDS = {
    "chk_views": "view",
    "chk_schedules": "sched",
    "chk_drafting": "drafting",
    "chk_legends": "legend",
}


class SheetExplorerForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        labels, self._by_label = collect_sheets(doc)

        if not labels:
            forms.alert(
                "В проекте нет ни одного листа.",
                title="Листы не найдены",
                exitscript=True,
            )

        self.lst_sheets.ItemsSource = labels
        self._current_items = []
        self.lst_views.ItemsSource = []
        self._refresh_views()

        self.lst_sheets.SelectionChanged += self._on_sheet_changed
        for chk_name, kind in CHECKBOX_KINDS.iteritems():
            chk = getattr(self, chk_name)
            chk.Checked += self._on_checkbox_changed
            chk.Unchecked += self._on_checkbox_changed
        self.btn_close.Click += self._close

    def _refresh_views(self):
        """Собирает нижний список из кэша текущего листа с учётом чекбоксов."""
        dropped = set()
        for chk_name, kind in CHECKBOX_KINDS.iteritems():
            chk = getattr(self, chk_name)
            if bool(chk.IsChecked):
                dropped.add(kind)

        rows = [label for kind, label in self._current_items if kind in dropped]

        if not self._current_items:
            self.lst_views.ItemsSource = [u"(на листе нет сущностей)"]
        elif not rows:
            self.lst_views.ItemsSource = [u"(ничего не выбрано)"]
        else:
            self.lst_views.ItemsSource = rows

    def _close(self, sender, args):
        self.Close()

    def _on_sheet_changed(self, sender, args):
        """Клик по листу в верхнем списке -> собрать сущности листа."""
        try:
            label = self.lst_sheets.SelectedItem
            logger.debug("SheetExplorer: выбран лист %r", label)

            sheet = self._by_label.get(label)
            self._current_items = []

            if sheet is not None:
                self._current_items = collect_sheet_items(doc, sheet)

            self._refresh_views()

        except Exception as exc:
            # исключение в WPF-событии не должно молча гаснуть: пишем в лог,
            # пользователю - короткая подпись в списке (окно не падает)
            logger.error("SheetExplorer: ошибка сбора видов: %s", exc)
            self.lst_views.ItemsSource = [u"(ошибка: см. лог)"]

    def _on_checkbox_changed(self, sender, args):
        self._refresh_views()


form = SheetExplorerForm()
form.ShowDialog()

logger.debug("SheetExplorer: окно закрыто, листов: %s", len(form.lst_sheets.ItemsSource))
