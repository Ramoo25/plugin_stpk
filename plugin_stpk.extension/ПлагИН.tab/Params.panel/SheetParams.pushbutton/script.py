# -*- coding: utf-8 -*-
"""Параметры листов и видов (plugin_stpk, IronPython 2.7).

Первый плагин набора с ЗАПИСЬЮ в модель: просмотр и редактирование
  - лист: «Номер листа» (SHEET_NUMBER), «Имя листа» (SHEET_NAME),
    «ADSK_Штамп титульный_Наименование документа» (shared, на листе);
  - вид на листе: «Имя вида» (view.Name), «Заголовок на листе»
    (VIEW_DESCRIPTION).
Запись - через pyRevit-транзакцию (обёртка revit.Transaction).
Список видов на листе - как в v1.0 «Листы и виды»: все размещённые
(натурально отсортированы) + спецификации с префиксом «СП ».
"""

import re

from pyrevit import revit, forms, script, DB

logger = script.get_logger()

doc = revit.doc

if not doc:
    forms.alert(
        "Откройте проект Revit.",
        title="Нет документа",
        exitscript=True,
    )


ADSK_PARAM_NAME = u"ADSK_Штамп титульный_Наименование документа"


def natural_key(text):
    """Ключ «естественной» сортировки: числа сравниваются как числа.

    "В-10" разбирается как ["В-", 10, ""], поэтому В-1 < В-2 < В-10,
    а не В-1 < В-10 < В-2, как при обычной сортировке строк.
    """
    return [int(part) if part.isdigit() else part
            for part in re.split(r"(\d+)", text)]


def collect_sheets(doc):
    """Все листы проекта: [(подпись, ViewSheet)], натурально отсортированы.

    Подпись: «номер - название»; параметры только через BuiltInParameter.
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
        num = p_num.AsString() if p_num else u""
        name = p_name.AsString() if p_name else u""
        label = u"{} - {}".format(num, name) if name else num
        pairs.append((num or u"", label, sheet))

    pairs.sort(key=lambda t: natural_key(t[0]))
    return [(label, sheet) for num, label, sheet in pairs]


def collect_sheet_views(doc, sheet):
    """Сущности на листе: [(подпись, view)], натурально отсортированы.

    Все виды из GetAllPlacedViews + спецификации через ScheduleSheetInstance
    (префикс «СП », внутренние таблицы «<...>» отбрасываются).
    """
    items = []

    for view_id in sheet.GetAllPlacedViews():
        view = doc.GetElement(view_id)
        if view is not None:
            items.append((view.Name, view))

    for ssi in DB.FilteredElementCollector(doc).OfClass(
            DB.ScheduleSheetInstance).WhereElementIsNotElementType().ToElements():
        if ssi.OwnerViewId == sheet.Id:
            if (ssi.Name or u"").startswith(u"<"):
                continue  # внутренняя таблица листа (ведомость изменений и пр.)
            sched = doc.GetElement(ssi.ScheduleId)
            if sched is not None:
                items.append((u"СП " + sched.Name, sched))

    items.sort(key=lambda it: natural_key(it[0]))
    return items


class SheetParamsForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        self._sheets = collect_sheets(doc)
        self._sheet = None
        self._views = []
        self._view = None

        self.lst_sheets.ItemsSource = [p[0] for p in self._sheets]
        self.lst_views.ItemsSource = []

        self.lst_sheets.SelectionChanged += self._on_sheet_changed
        self.lst_views.SelectionChanged += self._on_view_changed
        self.btn_save_sheet.Click += self._save_sheet
        self.btn_save_view.Click += self._save_view
        self.btn_close.Click += self._close

        if not self._sheets:
            forms.alert(
                "В проекте нет ни одного листа.",
                title="Листы не найдены",
                exitscript=True,
            )
            return

        # сразу показать первый лист
        self.lst_sheets.SelectedIndex = 0

    # ---------- статус ----------
    def _set_status(self, text):
        self.lbl_status.Text = text

    # ---------- лист ----------
    def _on_sheet_changed(self, sender, args):
        """Выбор листа -> заполнить поля листа и список видов."""
        try:
            label = self.lst_sheets.SelectedItem
            self._set_status(u"")
            self._sheet = None
            self.btn_save_sheet.IsEnabled = False
            self.txt_sheet_num.Text = u""
            self.txt_sheet_name.Text = u""

            sheet = self._find_sheet(label) if label else None

            if sheet is None:
                return

            self._sheet = sheet

            p_num = sheet.get_Parameter(DB.BuiltInParameter.SHEET_NUMBER)
            p_name = sheet.get_Parameter(DB.BuiltInParameter.SHEET_NAME)
            self.txt_sheet_num.Text = p_num.AsString() if p_num else u""
            self.txt_sheet_name.Text = p_name.AsString() if p_name else u""

            adsk_p = sheet.LookupParameter(ADSK_PARAM_NAME)
            if adsk_p is None:
                self.txt_sheet_adsk.Text = u"(ADSK-параметра нет)"
                self.txt_sheet_adsk.IsEnabled = False
            else:
                self.txt_sheet_adsk.IsEnabled = True
                self.txt_sheet_adsk.Text = adsk_p.AsString() or u""

            self.btn_save_sheet.IsEnabled = True

            # список видов, авто-выбор первого
            self._views = collect_sheet_views(doc, sheet)
            self._view_by_label = dict(self._views)
            self.lst_views.ItemsSource = [v[0] for v in self._views]
            if self._views:
                self.lst_views.SelectedIndex = 0

        except Exception as exc:
            logger.error("SheetParams: ошибка выбора листа: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _find_sheet(self, label):
        for lbl, sheet in self._sheets:
            if lbl == label:
                return sheet
        return None

    def _save_sheet(self, sender, args):
        """Сохранить параметры листа (транзакция)."""
        try:
            sheet = self._sheet
            if sheet is None:
                return

            num = self.txt_sheet_num.Text or u""
            name = self.txt_sheet_name.Text or u""
            if not num.strip():
                forms.alert(
                    "Номер листа не может быть пустым.",
                    title="Проверка ввода",
                    warn_icon=True,
                )
                return

            # лишняя пустая транзакция не нужна, если ничего не поменяли
            adsk_p = sheet.LookupParameter(ADSK_PARAM_NAME)
            old_num = (sheet.get_Parameter(DB.BuiltInParameter.SHEET_NUMBER)
                       .AsString() or u"")
            old_name = (sheet.get_Parameter(DB.BuiltInParameter.SHEET_NAME)
                        .AsString() or u"")
            old_adsk = (adsk_p.AsString() or u"")
            new_adsk = (self.txt_sheet_adsk.Text or u"")
            if old_num == num and old_name == name and old_adsk == new_adsk:
                self._set_status(u"Изменений нет - сохранять нечего")
                return

            with revit.Transaction(u"ПлагИН: правка параметров листа", doc):
                if old_num != num:
                    sheet.get_Parameter(DB.BuiltInParameter.SHEET_NUMBER).Set(num)
                if old_name != name:
                    sheet.get_Parameter(DB.BuiltInParameter.SHEET_NAME).Set(name)
                if adsk_p is not None and self.txt_sheet_adsk.IsEnabled:
                    if old_adsk != new_adsk:
                        adsk_p.Set(new_adsk)

            # пересобрать список листов (номер/имя могли измениться),
            # выделение вернуть по Id
            self._sheets = collect_sheets(doc)
            self.lst_sheets.ItemsSource = [p[0] for p in self._sheets]
            idx = self._index_by_id(self._sheets, sheet.Id)
            if idx is not None:
                self.lst_sheets.SelectedIndex = idx  # вызовет _on_sheet_changed

            msg = u"Сохранено"
            dups = self._count_duplicate_numbers(num, sheet.Id)
            if dups:
                msg += u" ВНИМАНИЕ: ещё {} лист(ов) с номером «{}»".format(dups, num)
            self._set_status(msg)

        except Exception as exc:
            logger.error("SheetParams: ошибка сохранения листа: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _count_duplicate_numbers(self, num, sheet_id):
        count = 0
        for lbl, sheet in self._sheets:
            p_num = sheet.get_Parameter(DB.BuiltInParameter.SHEET_NUMBER)
            cur = p_num.AsString() if p_num else u""
            if cur == num and sheet.Id.IntegerValue != sheet_id.IntegerValue:
                count += 1
        return count

    def _index_by_id(self, pairs, element_id):
        for i, (lbl, el) in enumerate(pairs):
            if el.Id.IntegerValue == element_id.IntegerValue:
                return i
        return None

    # ---------- вид ----------
    def _on_view_changed(self, sender, args):
        """Выбор вида -> заполнить поля вида."""
        try:
            label = self.lst_views.SelectedItem
            self._view = None
            self.btn_save_view.IsEnabled = False
            self.txt_view_name.Text = u""
            self.txt_view_title.Text = u""

            if not label or not self._views:
                return

            view = self._view_by_label.get(label)
            if view is None:
                return

            self._view = view
            self.txt_view_name.Text = view.Name

            p_title = view.get_Parameter(DB.BuiltInParameter.VIEW_DESCRIPTION)
            self.txt_view_title.Text = p_title.AsString() if p_title else u""

            # масштаб есть не у всех видов (спецификации, легенды - без него)
            p_scale = view.get_Parameter(DB.BuiltInParameter.VIEW_SCALE)
            if p_scale is None:
                self.txt_view_scale.Text = u"(нет масштаба)"
                self.txt_view_scale.IsEnabled = False
            else:
                self.txt_view_scale.IsEnabled = True
                self.txt_view_scale.Text = str(p_scale.AsInteger())

            self.btn_save_view.IsEnabled = True

        except Exception as exc:
            logger.error("SheetParams: ошибка выбора вида: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _save_view(self, sender, args):
        """Сохранить параметры вида (транзакция)."""
        try:
            view = self._view
            if view is None:
                return

            name = self.txt_view_name.Text or u""
            title = self.txt_view_title.Text or u""
            if not name.strip():
                forms.alert(
                    "Имя вида не может быть пустым.",
                    title="Проверка ввода",
                    warn_icon=True,
                )
                return

            old_title_p = view.get_Parameter(DB.BuiltInParameter.VIEW_DESCRIPTION)
            old_title = old_title_p.AsString() if old_title_p else u""
            old_name = view.Name or u""

            # масштаб (не у всех видов есть)
            old_scale_p = view.get_Parameter(DB.BuiltInParameter.VIEW_SCALE)
            old_scale = old_scale_p.AsInteger() if old_scale_p else None
            new_scale = None
            if self.txt_view_scale.IsEnabled and old_scale_p is not None:
                scale_text = (self.txt_view_scale.Text or u"").strip()
                try:
                    new_scale = int(scale_text)
                except ValueError:
                    forms.alert(
                        u"Масштаб должен быть целым числом "
                        "(например, 100 для 1:100).",
                        title="Проверка ввода",
                        warn_icon=True,
                    )
                    return
                if not view.IsValidViewScale(new_scale):
                    forms.alert(
                        u"Масштаб вне допустимого диапазона (1 - 24000).",
                        title="Проверка ввода",
                        warn_icon=True,
                    )
                    return

            if (old_name == name and old_title == title
                    and old_scale == new_scale):
                self._set_status(u"Изменений нет - сохранять нечего")
                return

            with revit.Transaction(u"ПлагИН: правка параметров вида", doc):
                if old_title != title and old_title_p is not None:
                    old_title_p.Set(title)
                if old_name != name:
                    view.Name = name
                if new_scale is not None and old_scale != new_scale:
                    # VIEW_SCALE - read-only; масштаб пишется свойством
                    # View.Scale (проверено в №4, Revit 2022)
                    view.Scale = new_scale

            # пересобрать список видов, выделение вернуть по Id
            self._views = collect_sheet_views(doc, self._sheet)
            self._view_by_label = dict(self._views)
            self.lst_views.ItemsSource = [v[0] for v in self._views]
            idx = self._index_by_id(self._views, view.Id)
            if idx is not None:
                self.lst_views.SelectedIndex = idx  # вызовет _on_view_changed

            self._set_status(u"Сохранено")

        except Exception as exc:
            logger.error("SheetParams: ошибка сохранения вида: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _close(self, sender, args):
        self.Close()


form = SheetParamsForm()
form.ShowDialog()

logger.debug("SheetParams: окно закрыто")
