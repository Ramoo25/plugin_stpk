# -*- coding: utf-8 -*-
"""«МаркаПоиск» - плагин №7 набора plugin_stpk (IronPython 2.7).

Поиск видов, на которых выбранный экземпляр семейства отмечен
выбранной маркой (например, «маркой по нескольким категориям»):
- пользователь выделяет элемент в модели ДО запуска плагина;
- в окне выбирает марку (типы марок проекта, поиск «Содержит:»,
  слова через пробел = «И»);
- плагин перебирает марки выбранного типа, у которых среди
  GetTaggedLocalElementIds() есть выделенный экземпляр, и собирает
  OwnerViewId -> список видов/листов («Вид: ...» / «Лист: ...»);
- двойной клик по строке результата открывает вид/лист.
Скрытые марки не анализируются: «марка есть = вид в списке».

API-опора (2022, проверено по справочнику):
- IndependentTag.GetTaggedLocalElementIds() - маркируемые элементы;
- Element.OwnerViewId - вид, которому принадлежит марка;
- тип марки - ElementType; списки типов собираем через категорию
  OST_MultiCategoryTags, запасной путь - типы существующих марок.
"""

import re

from pyrevit import revit, forms, script, DB

# WPF-обёртки (Revit уже загрузил PresentationFramework)
from System.Windows import TextWrapping, Thickness, VerticalAlignment, Visibility
from System.Windows.Media import Color, SolidColorBrush
from System.Windows.Controls import (Border, ComboBox, Orientation,
                                     StackPanel, TextBlock)

logger = script.get_logger()
uidoc = revit.uidoc
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


def collect_tag_types():
    """Типы марок по нескольким категориям: [(имя, type_id)].
    Основной путь - категория OST_MultiCategoryTags; запасной -
    типы, выведенные из существующих марок (урок «Видовой экрана»:
    сбор по категории может не сработать)."""
    out = []
    try:
        for t in DB.FilteredElementCollector(doc).OfCategory(
                DB.BuiltInCategory.OST_MultiCategoryTags
                ).WhereElementIsElementType().ToElements():
            nm = element_name_safe(
                t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
            if nm.strip():
                out.append((nm.strip(), t.Id))
    except Exception as exc:
        logger.debug("TagSearch: типы марок по категории: %s", exc)
    if out:
        out.sort(key=lambda t: natural_key(t[0]))
        return out
    # запасной путь: типы существующих марок
    seen = {}
    for tag in DB.FilteredElementCollector(doc).OfClass(
            DB.IndependentTag).ToElements():
        try:
            t = doc.GetElement(tag.GetTypeId())
            nm = element_name_safe(
                t, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
            if nm.strip() and t.Id.IntegerValue not in seen:
                seen[t.Id.IntegerValue] = (nm.strip(), t.Id)
        except Exception:
            continue
    out = sorted(seen.values(), key=lambda t: natural_key(t[0]))
    return out


def view_label(el_id):
    """Подпись «Вид: ...» / «Лист: ...» по id (OwnerViewId марки)."""
    el = doc.GetElement(el_id)
    if el is None:
        return u"?"
    nm = element_name_safe(el, (DB.BuiltInParameter.VIEW_NAME,))
    try:
        if isinstance(el, DB.ViewSheet):
            num = element_name_safe(
                el, (DB.BuiltInParameter.SHEET_NUMBER,))
            return u"Лист: {} {}".format(num, nm).strip()
    except Exception:
        pass
    return u"Вид: {}".format(nm)


class TagSearchForm(forms.WPFWindow):
    def __init__(self):
        forms.WPFWindow.__init__(self, "ui.xaml")

        global BG_HAS, BG_SELECTED, BG_PLAIN
        BG_HAS = SolidColorBrush(Color.FromArgb(0x30, 0x87, 0xCE, 0xEB))
        BG_SELECTED = SolidColorBrush(
            Color.FromArgb(0x70, 0x87, 0xCE, 0xEB))
        BG_PLAIN = SolidColorBrush(Color.FromArgb(0x00, 0xFF, 0xFF, 0xFF))

        self._element = element
        self._rows = []        # (border, label) результатов
        self._results = []     # [(label, view_id)] до фильтра

        nm = element_name_safe(
            self._element, (DB.BuiltInParameter.ALL_MODEL_TYPE_NAME,))
        self.lbl_element.Text = u"Элемент: {} (id {})".format(
            nm or u"?", self._element.Id.IntegerValue)

        self._tag_types = collect_tag_types()
        names = [t[0] for t in self._tag_types]
        self.cb_tag.ItemsSource = names
        if names:
            self.cb_tag.SelectedIndex = 0

        self.cb_tag.SelectionChanged += self._on_tag_changed
        self.tb_search.TextChanged += self._on_search_changed
        self.btn_close.Click += self._close

        self._find()
        self._set_status(u"")

    # ---------- утилиты ----------
    def _set_status(self, text):
        self.lbl_status.Text = text

    def _query_of(self):
        try:
            return unicode(self.tb_search.Text).strip().lower()
        except Exception:
            return u""

    @staticmethod
    def _matches_all(text, query):
        """True, если ВСЕ части запроса (через пробел, порядок любой)
        встречаются в text."""
        parts = query.split()
        if not parts:
            return True
        low = text.lower()
        return all(part in low for part in parts)

    # ---------- поиск ----------
    def _on_tag_changed(self, sender, args):
        self._find()

    def _on_search_changed(self, sender, args):
        self._apply_filter_visibility()

    def _find(self):
        """Сканирование марок выбранного типа по выделенному элементу."""
        self.pnl_results.Children.Clear()
        self._rows = []
        self._results = []

        idx = self.cb_tag.SelectedIndex
        if idx is None or idx < 0:
            self._set_status(u"Марок по нескольким категориям "
                             u"в проекте не найдено.")
            return
        type_id = self._tag_types[idx][1]
        target = self._element.Id.IntegerValue

        for tag in DB.FilteredElementCollector(doc).OfClass(
                DB.IndependentTag).ToElements():
            try:
                if tag.GetTypeId().IntegerValue != type_id.IntegerValue:
                    continue
                tagged = tag.GetTaggedLocalElementIds()
                hit = any(eid.IntegerValue == target for eid in tagged)
                if not hit:
                    continue
                self._results.append((view_label(tag.OwnerViewId),
                                      tag.OwnerViewId))
            except Exception as exc:
                logger.debug("TagSearch: марка пропущена: %s", exc)

        # уникальные виды (несколько марок одного типа на одном виде)
        uniq = {}
        for lbl, vid in self._results:
            uniq.setdefault(vid.IntegerValue, (lbl, vid))
        ordered = sorted(uniq.values(),
                         key=lambda t: natural_key(t[0]))

        if not ordered:
            lbl = TextBlock()
            lbl.Text = u"Марка на данном элементе не найдена " \
                       u"ни на одном виде."
            lbl.TextWrapping = TextWrapping.Wrap
            lbl.Margin = Thickness(6, 4, 6, 4)
            self.pnl_results.Children.Add(lbl)
            self._set_status(u"Найдено: 0")
            return

        for lbl, vid in ordered:
            self._add_result_row(lbl, vid)
        self._set_status(u"Найдено: {} вид(ов)".format(len(self._rows)))

    def _add_result_row(self, label, vid):
        border = Border()
        border.Padding = Thickness(6, 3, 6, 3)
        border.Background = BG_PLAIN
        lbl = TextBlock()
        lbl.Text = label
        border.Child = lbl
        border.MouseLeftButtonUp += (
            lambda s, e, b=border: self._select_row(b))
        border.MouseDoubleClick += (
            lambda s, e, v=vid: self._open_view(v))
        self.pnl_results.Children.Add(border)
        self._rows.append((border, lbl))

    def _select_row(self, border):
        for b, _ in self._rows:
            b.Background = BG_SELECTED if b is border else BG_PLAIN

    def _apply_filter_visibility(self):
        """Видимость строк по «Содержит:»."""
        query = self._query_of()
        for border, lbl in self._rows:
            visible = self._matches_all(lbl.Text, query)
            border.Visibility = (Visibility.Visible if visible
                                 else Visibility.Collapsed)

    # ---------- операции ----------
    def _open_view(self, vid):
        """Двойной клик: открыть вид/лист."""
        v = doc.GetElement(vid)
        if v is None:
            self._set_status(u"Вид недоступен.")
            return
        try:
            uidoc.ActiveView = v
        except Exception as exc:
            self._set_status(u"Вид не открыт: {}".format(exc))

    def _close(self, sender, args):
        self.Close()


form = TagSearchForm()
form.ShowDialog()

logger.debug("TagSearch: окно закрыто")
