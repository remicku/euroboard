import time
import timescaledb_model as tsdb

TSDB = tsdb.TimescaleStockMarketModel
DATADIR = "/mnt/data/"  # contains bourso/ and euronext/ subdirectories


def timer_decorator(func):
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = func(*args, **kwargs)
        end_time = time.time()
        print(f"{func.__name__} run in {(end_time - start_time):.2f} seconds.")
        return result

    return wrapper


@timer_decorator
def store_files(start: str, end: str, website: str, db: TSDB):
    raise NotImplementedError("The store_files function is not implemented")
