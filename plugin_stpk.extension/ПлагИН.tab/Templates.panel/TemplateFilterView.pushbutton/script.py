# -*- coding: utf-8 -*-
"""«ШаблонФильтрВид» - плагин №5 набора plugin_stpk (IronPython 2.7).

Копирование фильтров с настройками отображения (цвет, прозрачность,
толщина, скрыть/показать) из одного шаблона вида в другой. Фильтры,
уже существующие в приёмнике, перезаписываются настройками источника;
их отсутствие в приёмнике - добавляются.

Сами фильтры (ParameterFilterElement) копировать не нужно - это
общепроектные элементы; переносим только применение: настройки графики
(GetFilterOverrides -> SetFilterOverrides) и флаг видимости
(GetFilterVisibility -> SetFilterVisibility). SetFilterOverrides
в 2022 сам добавляет фильтр к виду, если тот не применён (см. док).
Порядок (приоритет) фильтров не переносится - новый встаёт в конец.
"""

import re

from pyrevit import revit, forms, script, DB

# WPF-обёртки (Revit уже загрузил PresentationFramework)
from System.Windows import Thickness, VerticalAlignment
from System.Windows.Controls import CheckBox, Orientation, StackPanel, TextBlock

logger = script.get_logger()

doc = revit.doc

if not doc:
    forms.alert(
        "Откройте проект Revit.",
        title="Нет документа",
        exitscript=True,
    )


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


def collect_templates(doc):
    """Шаблоны видов проекта: [(подпись, view)], натурально по имени."""
    pairs = []
    for view in DB.FilteredElementCollector(doc).OfClass(
            DB.View).WhereElementIsNotElementType().ToElements():
        try:
            if not view.IsTemplate:
                continue
        except Exception:
            continue
        name = element_name_safe(view, (DB.BuiltInParameter.VIEW_NAME,))
        if name:
            pairs.append((name, view))
    pairs.sort(key=lambda t: natural_key(t[0]))
    return pairs


def collect_filters(doc, view):
    """Фильтры, применённые к шаблону: [(имя, filter_id)], по имени."""
    items = []
    for filter_id in view.GetFilters():
        f = doc.GetElement(filter_id)
        if f is None:
            continue
        items.append((element_name_safe(f), filter_id))
    items.sort(key=lambda t: natural_key(t[0]))
    return items


class TemplateFilterForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        self._templates = collect_templates(doc)

        names = [t[0] for t in self._templates]
        self.cb_source.ItemsSource = names
        self.cb_target.ItemsSource = names

        if len(names) >= 2:
            self.cb_source.SelectedIndex = 0
            self.cb_target.SelectedIndex = 1
        elif len(names) == 1:
            self.cb_source.SelectedIndex = 0
        else:
            forms.alert(
                u"В проекте нет шаблонов видов.\n"
                u"Создайте шаблон вида и повторите.",
                title="Шаблоны не найдены",
                exitscript=True,
            )
            return

        self.cb_source.SelectionChanged += self._on_template_changed
        self.cb_target.SelectionChanged += self._on_template_changed
        self.btn_all.Click += self._check_all
        self.btn_none.Click += self._uncheck_all
        self.btn_copy.Click += self._copy
        self.btn_close.Click += self._close

        self._refresh_filter_list()
        self._set_status(u"")

    # ---------- списки ----------
    def _selected_view(self, combo):
        """View по выбранному имени в списке (или None)."""
        idx = combo.SelectedIndex
        name = combo.SelectedItem
        if idx is None or idx < 0 or not name:
            return None
        for lbl, view in self._templates:
            if lbl == name:
                return view
        return None

    def _filter_ids(self, view):
        """Множество id фильтров шаблона (для пометок «перезапись»)."""
        ids = set()
        if view is None:
            return ids
        for fid in view.GetFilters():
            try:
                ids.add(fid.IntegerValue)
            except Exception:
                pass
        return ids

    def _refresh_filter_list(self, sender=None, args=None):
        """Пересборка списка фильтров источника с пометками."""
        self.pnl_filters.Children.Clear()
        self._rows = []

        source = self._selected_view(self.cb_source)
        target = self._selected_view(self.cb_target)
        target_ids = self._filter_ids(target)

        if source is None:
            self._set_status(u"Выберите шаблон-источник.")
            self._refresh_copy_enabled()
            return

        for name, filter_id in collect_filters(doc, source):
            exists = filter_id.IntegerValue in target_ids
            label = u"{} ({})".format(
                name, u"перезапись" if exists else u"новый")
            hint = u"переписать настройки" if exists else u"добавить в приёмник"

            row = StackPanel()
            row.Orientation = Orientation.Horizontal
            row.Margin = Thickness(0, 2, 4, 2)

            chk = CheckBox()
            chk.VerticalAlignment = VerticalAlignment.Center
            chk.Margin = Thickness(0, 0, 6, 0)
            chk.IsChecked = True
            chk.Checked += self._on_check_changed
            chk.Unchecked += self._on_check_changed

            lbl = TextBlock()
            lbl.Text = label
            lbl.ToolTip = hint
            lbl.VerticalAlignment = VerticalAlignment.Center

            row.Children.Add(chk)
            row.Children.Add(lbl)
            self.pnl_filters.Children.Add(row)

            self._rows.append((chk, filter_id, name, exists))

        self._refresh_copy_enabled()

    def _on_template_changed(self, sender, args):
        self._refresh_filter_list()
        self._set_status(u"")

    def _on_check_changed(self, sender, args):
        self._refresh_copy_enabled()

    def _checked_rows(self):
        return [(fid, name, exists)
                for chk, fid, name, exists in self._rows
                if bool(chk.IsChecked)]

    def _refresh_copy_enabled(self):
        source = self._selected_view(self.cb_source)
        target = self._selected_view(self.cb_target)
        same = (source is not None and target is not None
                and source.Id.IntegerValue == target.Id.IntegerValue)
        ok = (source is not None and not same
              and len(self._checked_rows()) > 0)
        self.btn_copy.IsEnabled = ok

    # ---------- операции ----------
    def _check_all(self, sender, args):
        for chk, fid, name, exists in self._rows:
            chk.IsChecked = True

    def _uncheck_all(self, sender, args):
        for chk, fid, name, exists in self._rows:
            chk.IsChecked = False

    def _copy(self, sender, args):
        """Копирование отмеченных фильтров: настройки + видимость."""
        try:
            source = self._selected_view(self.cb_source)
            target = self._selected_view(self.cb_target)
            if source is None or target is None:
                forms.alert(u"Выберите оба шаблона.", warn_icon=True)
                return
            if source.Id.IntegerValue == target.Id.IntegerValue:
                forms.alert(u"Источник и приёмник совпадают.", warn_icon=True)
                return

            checked = self._checked_rows()
            if not checked:
                return

            new_count = 0
            over_count = 0
            failed = []
            with revit.Transaction(u"ПлагИН: копирование фильтров шаблона",
                                   doc):
                for filter_id, name, exists in checked:
                    try:
                        overrides = source.GetFilterOverrides(filter_id)
                        target.SetFilterOverrides(filter_id, overrides)
                        visibility = source.GetFilterVisibility(filter_id)
                        target.SetFilterVisibility(filter_id, visibility)
                        if exists:
                            over_count += 1
                        else:
                            new_count += 1
                    except Exception as exc:
                        failed.append((name, str(exc)))
                        logger.debug("TemplateFilter: %s: %s", name, exc)

            # свежие пометки (что теперь «перезапись», что «новый»)
            self._refresh_filter_list()

            lines = [u"Скопировано: {} (новых: {}, перезаписано: {})".format(
                new_count + over_count, new_count, over_count)]
            if failed:
                lines.append(u"Не скопировано: {}".format(len(failed)))
                for name, why in failed[:5]:
                    lines.append(u"- {}: {}".format(name, why))
                if len(failed) > 5:
                    lines.append(u"- ... и ещё {}".format(len(failed) - 5))
            self._set_status(u"\n".join(lines))

        except Exception as exc:
            logger.error("TemplateFilter: ошибка копирования: %s", exc)
            self._set_status(u"(ошибка: см. лог)")

    def _set_status(self, text):
        self.lbl_status.Text = text

    def _close(self, sender, args):
        self.Close()


form = TemplateFilterForm()
form.ShowDialog()

logger.debug("TemplateFilter: окно закрыто")
