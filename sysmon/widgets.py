"""Single-line terminal tables, with Textual 6 cursor and mouse behavior."""

from rich.cells import set_cell_size
from rich.segment import Segment
from rich.style import Style
from rich.text import Text
from textual.widgets import DataTable


class PlainDataTable(DataTable):
    def _render_cell(self, row_index, column_index, base_style, width, cursor=False, hover=False):
        header = row_index == -1
        label = column_index == -1
        row_key = self._header_row_key if header else self._row_locations.get_key(row_index)
        row_label, cells = self._get_row_renderables(row_index)
        cell = row_label if label else cells[column_index]
        height = self.header_height if header else self.rows[row_key].height
        padding = self.cell_padding
        if (height != 1 or not isinstance(cell, Text) or cell.style or cell.spans
                or cell.justify not in (None, "default", "left")
                or cell.overflow not in (None, "fold", "crop")
                or "\n" in cell.plain or "\t" in cell.plain or width < padding * 2):
            return super()._render_cell(row_index, column_index, base_style, width, cursor, hover)
        fixed = not header and not label and (row_index < self.fixed_rows or column_index < self.fixed_columns)
        # Position preserves click metadata after sorting. The actual text lets
        # unchanged cells survive another cell's update in the same table.
        cache_key = (
            row_index, column_index, cell.plain, width, padding, base_style,
            cursor, hover, fixed, self.show_cursor, self._show_hover_cursor,
            self.cursor_foreground_priority, self.cursor_background_priority,
            self._pseudo_class_state,
        )
        cached = self._cell_render_cache.get(cache_key)
        if cached is not None:
            return cached
        component, post = self._get_styles_to_render_cell(
            header, label, fixed, hover, cursor, self.show_cursor,
            self._show_hover_cursor, self.cursor_foreground_priority == "css",
            self.cursor_background_priority == "css",
        )
        style = base_style + Style.from_meta({"row": row_index, "column": column_index}) + component + post
        text = " " * padding + set_cell_size(cell.plain, width - padding * 2) + " " * padding
        result = [[Segment(text, style)]]
        self._cell_render_cache[cache_key] = result
        return result
