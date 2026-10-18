from PySide6.QtCore import QThreadPool
import os

cpu_cores = os.cpu_count() or 4
optimal_threads = min(32, cpu_cores * 2)
thread_pool = QThreadPool()
thread_pool.setMaxThreadCount(optimal_threads)

network_pool = QThreadPool()
network_pool.setMaxThreadCount(8)
