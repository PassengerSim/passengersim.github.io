from pathlib import Path

import pandas as pd

from passengersim import SummaryTables


def load(experiment_n: int):
    def read_csv(name):
        filename = Path(__file__).parent.joinpath(f"{experiment_n:02d}", name)
        if filename.exists():
            return pd.read_csv(filename)
        else:
            return None

    return SummaryTables(
        bookings_by_timeframe=read_csv("bookings_by_timeframe.csv"),
        load_factors=read_csv("load_factors.csv"),
        fare_class_mix=read_csv("fare_class_mix.csv"),
    )
