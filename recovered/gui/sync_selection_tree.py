# Reconstructed from the original Python 3.12 code object; comments and formatting are not recovered.
from PySide6.QtWidgets import (
    QTreeView, QStyledItemDelegate, QLabel, QAbstractItemView, QMenu, QStyle,
)
from PySide6.QtGui import QStandardItem, QStandardItemModel, QColor, QPixmap, QAction
from PySide6.QtCore import Qt, QRect, QItemSelectionModel, QModelIndex
import logging
from pathlib import Path
from core.signal_manager import signal_manager
from core.models import SyncAction, SyncActionType
from utils.timer import tic, toc
from utils.styles import SYNC_SELECTION_TREE_STYLESHEET


class SyncSelectionTreeView(QTreeView):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.icon_list = []
        self.setItemDelegate(FullRowProgressBarDelegate(self))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        signal_manager.check_all.connect(self.toggle_all_checkboxes)
        self.model = QStandardItemModel()
        self.setModel(self.model)
        self.model.setHorizontalHeaderLabels([
            'Select', '', 'File', 'Path', 'Size', 'Last Modified By', 'Modified Time',
        ])
        header = self.header()
        header.setMinimumSectionSize(30)
        self.model.dataChanged.connect(self.on_data_changed)
        self.setStyleSheet(SYNC_SELECTION_TREE_STYLESHEET)
        signal_manager.update_sync_selection_treeview.connect(self.update_model)
        signal_manager.file_progress.connect(self.update_progress)

    def update_model(self, action_list):
        tic()
        self.model.removeRows(0, self.model.rowCount())
        self.model.blockSignals(True)
        icon_list = []
        for file in action_list:
            row = []
            row.append(CheckBoxItem(file))
            icon_list.append(IconItem(file))
            row.append(QStandardItem(''))
            tic()
            for data in file.get_treeview_data():
                if data is None:
                    data = ''
                item = QStandardItem(str(data))
                item.setEditable(False)
                row.append(item)
            self.model.appendRow(row)
        logging.info('Treeview model updated with ' + str(len(action_list)) + ' items.')
        for idx, icon in enumerate(icon_list):
            self.setIndexWidget(self.model.index(idx, 1), icon.label)
        self.set_column_sizes()
        self.model.blockSignals(False)
        toc()

    def set_column_sizes(self):
        column_size = [50, 30, 180, 180, 50, 110, 30]
        for idx, column in enumerate(column_size):
            self.setColumnWidth(idx, column)

    def update_progress(self, row: int, new_progress: float):
        logging.debug(f'Updating row {str(row)} progress: {new_progress}%')
        row_index = self.model.index(row, 0)
        updated_data = row_index.data(Qt.UserRole) or {}
        updated_data['progress'] = new_progress
        self.model.setData(row_index, updated_data, Qt.UserRole)
        first_col = 0
        last_col = self.model.columnCount() - 1
        top_left = self.model.index(row, first_col)
        bottom_right = self.model.index(row, last_col)
        self.viewport().update()
        self.model.dataChanged.emit(top_left, bottom_right, [Qt.DisplayRole, Qt.ItemDataRole.UserRole])
        logging.debug(f'Emitted dataChanged for row {row} from column {first_col} to {last_col}')
        self.viewport().update()

    def on_data_changed(self, top_left, bottom_right, roles):
        if Qt.CheckStateRole not in roles:
            return
        if top_left.column() != 0 or top_left != bottom_right:
            return
        changed_index = top_left
        changed_item = self.model.itemFromIndex(changed_index)
        new_state = changed_item.checkState()
        selected_indexes = self.selectedIndexes()
        selected_rows = {index.row() for index in selected_indexes if index.column() == 0}
        selected_rows.discard(changed_index.row())
        for row in selected_rows:
            item = self.model.item(row, 0)
            if item is not None and item.isCheckable():
                if item.checkState() != new_state:
                    item.setCheckState(new_state)
        self.selectionModel().clearSelection()

    def show_context_menu(self, pos):
        index = self.indexAt(pos)
        if not index.isValid():
            return
        folder_name = self.get_folder_name_for_index(index)
        if folder_name == '':
            return
        menu = QMenu(self)
        select_from_folder = QAction(f"Select all files in '{folder_name}'", self)
        select_all_from_folder = QAction(f"Select all files in '{folder_name}' and subfolders", self)
        skip_file = QAction('Do not sync this file anymore', self)
        skip_folder = QAction(f"Do not sync files in '{folder_name}' anymore", self)
        skip_recursive = QAction(f"Do not sync files in '{folder_name}' and subfolders anymore", self)
        menu.addAction(select_from_folder)
        menu.addAction(select_all_from_folder)
        menu.addSeparator()
        menu.addAction(skip_file)
        menu.addAction(skip_folder)
        menu.addAction(skip_recursive)
        menu.exec(self.viewport().mapToGlobal(pos))

    def toggle_all_checkboxes(self):
        """Check all checkboxes if any are unchecked, otherwise uncheck all."""
        all_checked = True
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            if item.checkState() != Qt.Checked:
                all_checked = False
                break
        new_state = Qt.Unchecked if all_checked else Qt.Checked
        for row in range(self.model.rowCount()):
            item = self.model.item(row, 0)
            item.setCheckState(new_state)
        self.selectionModel().clearSelection()
        if new_state == Qt.Checked:
            for row in range(self.model.rowCount()):
                index = self.model.index(row, 0)
                self.selectionModel().select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)

    def get_folder_name_for_index(self, index) -> str:
        if not index.isValid():
            return ''
        data = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if data:
            action = data.get('action')
            if action and isinstance(action, SyncAction):
                return str(action.parent_name)
        return ''

    def get_path_name_for_index(self, index) -> str:
        if not index.isValid():
            return ''
        data = index.siblingAtColumn(0).data(Qt.ItemDataRole.UserRole)
        if data:
            path = data.get('path')
            if path:
                return str(path)
        return ''

    def get_selected_actions(self) -> list[SyncAction]:
        checked_items = []
        for row in range(self.model.rowCount()):
            index = self.model.index(row, 0)
            if self.model.itemFromIndex(index).checkState() == Qt.CheckState.Checked:
                data = index.data(Qt.UserRole)
                if data:
                    action = data.get('action')
                    if isinstance(action, SyncAction):
                        checked_items.append((index.row(), action))
        return checked_items


action_icon_map = {
    SyncActionType.UPLOAD_NEW: 'plus',
    SyncActionType.DOWNLOAD_NEW: 'plus',
    SyncActionType.UPLOAD_REVISION: 'revise',
    SyncActionType.DOWNLOAD_REVISION: 'revise',
    SyncActionType.DELETE_LOCAL: 'minus',
    SyncActionType.DELETE_CLOUD: 'minus',
    SyncActionType.UPLOAD_UNKNOWN: 'mismatch',
    SyncActionType.DOWNLOAD_UNKNOWN: 'mismatch',
}
color_map = {
    'plus': QColor('#C8ffC8'),
    'minus': QColor('#FFFFC8'),
    'revise': QColor('#C8C8FF'),
    'mismatch': QColor('#FFFFC8'),
    'nosync': QColor('#C8C8C8'),
}
starting_progress_map = {
    'plus': 0,
    'minus': 100,
    'revise': 0,
    'mismatch': 100,
    'nosync': 100,
}


class CheckBoxItem(QStandardItem):
    def __init__(self, action: SyncAction):
        super().__init__('')
        self.setCheckable(True)
        action_type = action.action
        icon_name = action_icon_map.get(action_type, 'nosync')
        self.setData({
            'color': color_map.get(icon_name, QColor('#C8C8C8')),
            'progress': starting_progress_map.get(icon_name, 0),
            'action': action,
        }, Qt.ItemDataRole.UserRole)
        is_checked = icon_name in ('plus', 'revise', 'minus')
        self.setCheckState(Qt.CheckState.Checked if is_checked else Qt.CheckState.Unchecked)


class IconItem(QStandardItem):
    def __init__(self, action: SyncAction):
        super().__init__()
        self.label = QLabel('')
        action_type = action.action
        self.label.setStyleSheet('background-color: transparent; padding: 0px;')
        icon_name = action_icon_map.get(action_type, 'nosync')
        icon_path = f':/assets/{icon_name}.png'
        if Path(icon_path).exists() or QPixmap(icon_path):
            pix = QPixmap(icon_path)
            self.label.setPixmap(pix)
            self.label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        else:
            logging.warning(f'IconItem given an invalid icon: {icon_name}')


class FullRowProgressBarDelegate(QStyledItemDelegate):
    def __init__(self, view):
        super().__init__()
        self.view = view

    def paint(self, painter, option, index):
        data = index.siblingAtColumn(0).data(Qt.UserRole) or {}
        color = data.get('color') or QColor('#C8C8C8')
        progress = data.get('progress') or 0
        painter.save()
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, QColor('#a0c4ff'))
        else:
            if index.row() % 2 == 0:
                base_color = option.palette.base()
            else:
                base_color = option.palette.alternateBase()
            painter.fillRect(option.rect, base_color)
        total_columns = index.model().columnCount()
        total_width = sum(self.view.columnWidth(c) for c in range(total_columns))
        progress_px = int(progress / 100.0 * total_width)
        cell_start_x = sum(self.view.columnWidth(c) for c in range(index.column()))
        cell_end_x = cell_start_x + self.view.columnWidth(index.column())
        bar_start_in_cell = max(cell_start_x, 0)
        bar_end_in_cell = min(progress_px, cell_end_x)
        bar_width = max(0, bar_end_in_cell - bar_start_in_cell)
        if bar_width > 0:
            x_offset = bar_start_in_cell - cell_start_x
            bar_rect = QRect(option.rect.x() + x_offset, option.rect.y(),
                             bar_width, option.rect.height())
            painter.setPen(Qt.NoPen)
            painter.setBrush(color)
            painter.drawRect(bar_rect)
        painter.restore()
        super().paint(painter, option, index)
