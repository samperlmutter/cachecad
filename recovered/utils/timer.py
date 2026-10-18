import logging
import time

_start_time = None

def tic():
    global _start_time
    _start_time = time.perf_counter()

def toc(note: str = 'Time:'):
    global _start_time
    if _start_time is None:
        print('tic() must be called before toc()')
        return
    elapsed = time.perf_counter() - _start_time
    print(f'{note} {elapsed:.4f} seconds')
    logging.info(f'{note} {elapsed:.4f} seconds')
