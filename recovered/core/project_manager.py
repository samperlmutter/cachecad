# Faithful bytecode recovery. Compile-only verification; original behavior retained.
import csv
import logging
import os
from pathlib import Path
from PySide6.QtCore import QObject, Signal
from utils.paths import APP_DATA_DIR
from core.signal_manager import signal_manager

class ProjectManager(QObject):
    def __init__(self, working_dir, csv_filename='projects.csv'):
        super().__init__()
        self.csv_path = os.path.join(APP_DATA_DIR, csv_filename)
        self.working_dir = working_dir
        self.projects = []
        self.load_project_list()
        signal_manager.remove_project_clicked.connect(self.remove_project)
        signal_manager.move_project_up_clicked.connect(self.move_project_up)
        signal_manager.move_project_down_clicked.connect(self.move_project_down)

    def create_project(self, name, id, read_only='False'):
        project_tuple = (name, id, read_only)
        self.projects.append(project_tuple)
        success = self.append_project_to_csv(project_tuple)
        if not success:
            print('[Error] Could not append project to CSV')
            return False
        logging.info(f'Created project: {name} with ID: {id}')
        Path(self.working_dir / name).mkdir(parents=True, exist_ok=True)
        return True

    def load_project_list(self):
        if not os.path.exists(self.csv_path):
            try:
                with open(self.csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                    pass
            except IOError as e:
                print(f'[Error] Could not create empty projects CSV: {e}')
                raise
            self.projects = []
            signal_manager.project_list_changed.emit(self.projects)
            return
        try:
            with open(self.csv_path, newline='', encoding='utf-8') as csvfile:
                reader = csv.reader(csvfile)
                self.projects = [(row[0], row[1], row[2] if len(row) > 2 else 'False') for row in reader if len(row) >= 2]
        except IOError as e:
            print(f'[Error] Could not read projects CSV: {e}')
            raise
        signal_manager.project_list_changed.emit(self.projects)

    def save_project_list_to_csv(self):
        try:
            with open(self.csv_path, 'w', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerows(self.projects)
            return True
        except IOError as e:
            print(f'[Error] Could not write projects CSV: {e}')
            return False

    def append_project_to_csv(self, project_tuple):
        try:
            with open(self.csv_path, 'a', newline='', encoding='utf-8') as csvfile:
                writer = csv.writer(csvfile)
                writer.writerow(project_tuple)
            signal_manager.project_list_changed.emit(self.projects)
            return True
        except IOError as e:
            print(f'[Error] Could not append to projects CSV: {e}')
            return False

    def remove_project(self, index):
        logging.info(f'Removing project at index: {index}')
        if 0 <= index < len(self.projects):
            self.projects.pop(index)
            success = self.save_project_list_to_csv()
            if not success:
                print('[Error] Could not save projects CSV after removal')
            signal_manager.project_list_changed.emit(self.projects)

    def move_project_up(self, index):
        if 0 < index < len(self.projects):
            self.projects[index - 1], self.projects[index] = self.projects[index], self.projects[index - 1]
            success = self.save_project_list_to_csv()
            if not success:
                print('[Error] Could not reorder projects CSV')
            else:
                self.load_project_list()
            signal_manager.project_list_changed.emit(self.projects)

    def move_project_down(self, index):
        if 0 <= index < len(self.projects) - 1:
            self.projects[index], self.projects[index + 1] = self.projects[index + 1], self.projects[index]
            success = self.save_project_list_to_csv()
            if not success:
                print('[Error] Could not reorder projects CSV')
            else:
                self.load_project_list()
            signal_manager.project_list_changed.emit(self.projects)

    def get_projects(self):
        return self.projects
