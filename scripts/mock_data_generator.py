"""
FRAMEWORK TEMPLATE: MOCK UTILITY TELEMETRY GENERATOR
Location: Data-Pipeline-Framework/scripts/mock_data_generator.py

Domain: Electric Utility Advanced Metering Infrastructure (AMI) Time Series
Features: Fixed 500-Meter Population, Continuous Multi-Day Incremental Batching,
          Realistic Date-Stamped Filenames, Decoupled Alarms, Injected Data Defects.
"""

import json
import shutil
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

# --- RUNTIME PATH RESOLUTION ---
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LANDING_ZONE = PROJECT_ROOT / "data" / "raw" / "incoming"
STATE_DIR = PROJECT_ROOT / "data" / "raw" / ".state"
LANDING_ZONE.mkdir(parents=True, exist_ok=True)
STATE_DIR.mkdir(parents=True, exist_ok=True)

STATE_FILE = STATE_DIR / "generator_state.json"

# --- ENGINE CONFIGURATION ---
TOTAL_METERS = 500

PROFILES = {
    "1": {
        "name": "Historical Backfill (30 Days)",
        "num_entities": TOTAL_METERS,
        "intervals": 2880,  # 30 days x 96 intervals/day
        "description": "1,440,000 rows (500 meters x 30 days @ 15m). Baseline history.",
    },
    "2": {
        "name": "Daily Incremental Drop (1 Day)",
        "num_entities": TOTAL_METERS,
        "intervals": 96,  # 1 day x 96 intervals/day
        "description": "48,000 rows (500 meters x 1 day @ 15m). Advances calendar by 24h.",
    },
}


def load_or_initialize_state(fake: Faker) -> tuple[pd.DataFrame, datetime]:
    """Loads existing meter fleet state or initializes a persistent 500-meter catalog."""
    substations = [f"SUB-{name}" for name in ["NORTH", "SOUTH", "WEST", "VALLEY"]]
    feeders = [f"FDR-{num}" for num in ["101", "102", "201", "202", "301", "302"]]
    rate_classes = ["RESIDENTIAL", "COMMERCIAL", "INDUSTRIAL"]
    rate_weights = [0.75, 0.20, 0.05]

    if STATE_FILE.exists():
        try:
            with open(STATE_FILE, "r") as f:
                state_data = json.load(f)

            entities = state_data["entities"]
            if len(entities) == TOTAL_METERS:
                df_entities = pd.DataFrame(entities)
                last_ts = datetime.fromisoformat(state_data["last_timestamp"])
                next_start_ts = last_ts + timedelta(minutes=15)
                return df_entities, next_start_ts
        except Exception:
            pass  # Fall back to fresh initialization on read error

    # Fresh entity fleet initialization
    catalog_records = []
    base_start_ts = datetime(2026, 6, 1, 0, 0, 0)

    for i in range(TOTAL_METERS):
        rate = np.random.choice(rate_classes, p=rate_weights)
        nominal_v = 480.0 if rate == "INDUSTRIAL" else 240.0
        base_demand_kw = {
            "RESIDENTIAL": float(np.random.uniform(1.5, 4.8)),
            "COMMERCIAL": float(np.random.uniform(25.0, 75.0)),
            "INDUSTRIAL": float(np.random.uniform(180.0, 480.0)),
        }[rate]

        # Allocate 3 fixed meters as permanent vacant premises (Obj 7)
        is_vacant = True if i in [5, 42, 108] else False

        catalog_records.append(
            {
                "meter_id": f"MTR-{fake.unique.bothify(text='#####')}",
                "substation_id": str(np.random.choice(substations)),
                "feeder_id": str(np.random.choice(feeders)),
                "rate_class": rate,
                "nominal_voltage": nominal_v,
                "base_demand_kw": base_demand_kw,
                "current_cumulative_kwh": float(np.random.uniform(15000.0, 95000.0)),
                "commission_year": int(
                    np.random.choice([2017, 2018, 2019, 2020, 2021, 2022, 2023])
                ),
                "is_vacant": is_vacant,
            }
        )
    df_entities = pd.DataFrame(catalog_records)
    return df_entities, base_start_ts


def save_state(df_entities: pd.DataFrame, last_timestamp: datetime) -> None:
    """Persists closing cumulative registers and last timestamp to disk."""
    state_data = {
        "last_timestamp": last_timestamp.isoformat(),
        "entities": df_entities.to_dict(orient="records"),
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state_data, f, indent=2)


def generate_dataset(choice: str) -> None:
    cfg = PROFILES[choice]
    n_intervals = cfg["intervals"]

    print(f"\n[RUNNING] Starting {cfg['name']} generation...")
    start_time = time.time()

    fake = Faker()
    run_seed = int(time.time() * 1000) % 2**32
    np.random.seed(run_seed)
    Faker.seed(run_seed)

    # ==========================================
    # SLOT 1: ENTITY FLEET & CUMULATIVE REGISTERS
    # ==========================================
    df_entities, start_timestamp = load_or_initialize_state(fake)

    # ==========================================
    # SLOT 2: TEMPORAL SPINE (15-Minute Cadence)
    # ==========================================
    time_index = pd.date_range(
        start=start_timestamp,
        periods=n_intervals,
        freq="15min",
    )
    df = df_entities.merge(pd.DataFrame({"read_timestamp": time_index}), how="cross")
    df = df.sort_values(by=["meter_id", "read_timestamp"]).reset_index(drop=True)

    # ==========================================
    # SLOT 3: DOMAIN PHYSICS & LOAD PROFILES
    # ==========================================
    dec_hours = (
        df["read_timestamp"].dt.hour.values
        + df["read_timestamp"].dt.minute.values / 60.0
    )

    # 1. Bimodal Residential Curve vs Commercial Business-Hour Plateau (Obj 8)
    res_curve = (
        0.30
        + 0.40 * np.exp(-0.5 * ((dec_hours - 7.5) / 1.8) ** 2)
        + 0.70 * np.exp(-0.5 * ((dec_hours - 19.5) / 2.5) ** 2)
    )
    comm_curve = (
        0.20
        + 0.80 / (1.0 + np.exp(-1.5 * (dec_hours - 7.0)))
        - 0.80 / (1.0 + np.exp(-1.5 * (dec_hours - 18.0)))
    )

    is_res = df["rate_class"] == "RESIDENTIAL"
    diurnal_profile = np.where(is_res, res_curve, comm_curve)

    # Base active power with Gaussian telemetry noise
    kw_noise = np.random.normal(1.0, 0.04, size=len(df))
    df["active_power_kw"] = np.clip(
        df["base_demand_kw"].values * diurnal_profile * kw_noise,
        0.05,
        None,
    )

    # Vacant premise clamp: 0.00 kW draw (Obj 7)
    df.loc[df["is_vacant"], "active_power_kw"] = 0.000

    # 2. Power Factor & Reactive Power (kVAR)
    df["power_factor"] = np.random.uniform(0.94, 0.98, size=len(df))
    pf_ratio = np.sqrt((1.0 - df["power_factor"] ** 2) / (df["power_factor"] ** 2))
    df["reactive_power_kvar"] = df["active_power_kw"] * pf_ratio

    # 3. Line Voltage Drop under Load (I*R line regulation) + Feeder Noise
    load_ratio = df["active_power_kw"].values / (
        df["base_demand_kw"].values * 1.5 + 1e-6
    )
    voltage_noise = np.random.normal(0.0, 0.65, size=len(df))
    df["voltage_v"] = df["nominal_voltage"].values - (load_ratio * 3.8) + voltage_noise

    # Vacant premises maintain healthy nominal delivery voltage (Obj 7)
    df.loc[df["is_vacant"], "voltage_v"] = (
        df.loc[df["is_vacant"], "nominal_voltage"] + voltage_noise[df["is_vacant"]]
    )

    # ==========================================
    # SLOT 4: ANOMALIES, SYSTEMIC CLUSTERING & DEFECTS
    # ==========================================
    total_rows = len(df)
    status_flags = np.full(total_rows, "NORMAL", dtype=object)

    # 1. Systemic Feeder-Wide Voltage Sags (Obj 2)
    # Pick 1 random feeder and 1 specific 2-hour window during the batch
    target_feeder = np.random.choice(df["feeder_id"].unique())
    random_interval_idx = np.random.randint(0, max(1, n_intervals - 8))
    sag_start_ts = time_index[random_interval_idx]
    sag_end_ts = sag_start_ts + timedelta(hours=2)

    systemic_mask = (
        (df["feeder_id"] == target_feeder)
        & (df["read_timestamp"] >= sag_start_ts)
        & (df["read_timestamp"] <= sag_end_ts)
        & (np.random.rand(total_rows) < 0.90)  # 90% of meters on this feeder drop
    )
    df.loc[systemic_mask, "voltage_v"] *= np.random.uniform(
        0.78, 0.86, size=systemic_mask.sum()
    )
    status_flags[systemic_mask] = "VOLTAGE_SAG"

    # 2. Isolated Customer-Level Sags (Obj 2)
    isolated_sag_rate = 0.0012
    isolated_mask = (np.random.rand(total_rows) < isolated_sag_rate) & (~systemic_mask)
    df.loc[isolated_mask, "voltage_v"] *= np.random.uniform(
        0.75, 0.85, size=isolated_mask.sum()
    )

    # Decouple alarms: 30% of isolated sags suffer Silent Failures (Obj 4)
    isolated_reported = isolated_mask & (np.random.rand(total_rows) >= 0.30)
    status_flags[isolated_reported] = "VOLTAGE_SAG"

    # 3. Spurious Voltage Alarms / False Positives (Obj 4)
    spurious_rate = 0.0008
    is_spurious = (
        (np.random.rand(total_rows) < spurious_rate)
        & (~systemic_mask)
        & (~isolated_mask)
    )
    status_flags[is_spurious] = "VOLTAGE_SAG"

    # 4. Reverse Flow / Unauthorized Tamper with Silent Failures (Obj 4)
    tamper_rate = 0.0010
    is_tamper = (
        (np.random.rand(total_rows) < tamper_rate)
        & (~systemic_mask)
        & (~isolated_mask)
        & (~df["is_vacant"])
    )
    df.loc[is_tamper, "active_power_kw"] *= -0.80
    tamper_reported = is_tamper & (np.random.rand(total_rows) >= 0.25)
    status_flags[tamper_reported] = "REVERSE_FLOW_TAMPER"

    # 5. Low Power Factor Penalty Anomaly for C&I Accounts (Obj 5)
    low_pf_rate = 0.0020
    is_low_pf = (
        (np.random.rand(total_rows) < low_pf_rate)
        & (df["rate_class"].isin(["COMMERCIAL", "INDUSTRIAL"]))
        & (~is_tamper)
    )
    df.loc[is_low_pf, "power_factor"] = np.random.uniform(
        0.65, 0.79, size=is_low_pf.sum()
    )
    new_pf_ratio = np.sqrt(
        (1.0 - df.loc[is_low_pf, "power_factor"] ** 2)
        / (df.loc[is_low_pf, "power_factor"] ** 2)
    )
    df.loc[is_low_pf, "reactive_power_kvar"] = (
        df.loc[is_low_pf, "active_power_kw"] * new_pf_ratio
    )
    pf_reported = is_low_pf & (np.random.rand(total_rows) >= 0.20)
    status_flags[pf_reported] = "LOW_POWER_FACTOR"

    # 6. Physical Outage Event: 2.5-hour duration on 4 meters (Obj 7)
    outage_start_idx = np.random.randint(0, max(1, n_intervals - 10))
    outage_start_ts = time_index[outage_start_idx]
    outage_end_ts = outage_start_ts + timedelta(minutes=150)
    outage_meters = (
        df_entities.loc[~df_entities["is_vacant"], "meter_id"].iloc[10:14].values
    )

    outage_mask = (
        (df["meter_id"].isin(outage_meters))
        & (df["read_timestamp"] >= outage_start_ts)
        & (df["read_timestamp"] <= outage_end_ts)
    )
    df.loc[outage_mask, "active_power_kw"] = 0.0
    df.loc[outage_mask, "reactive_power_kvar"] = 0.0
    df.loc[outage_mask, "voltage_v"] = np.random.uniform(
        0.0, 3.5, size=outage_mask.sum()
    )
    status_flags[outage_mask] = "POWER_OUTAGE"

    # 7. Injected Apparent Power Vector Corruption Defects (Obj 3)
    vector_corrupt_rate = 0.0005
    is_corrupt = np.random.rand(total_rows) < vector_corrupt_rate
    df.loc[is_corrupt, "power_factor"] = np.random.uniform(
        0.35, 0.55, size=is_corrupt.sum()
    )

    df["status_flag"] = status_flags

    # 8. RMS Current Calculation
    kva = np.sqrt(df["active_power_kw"] ** 2 + df["reactive_power_kvar"] ** 2)
    safe_v = np.maximum(df["voltage_v"], 1.0)
    df["current_a"] = np.round((kva * 1000.0) / safe_v, 2)

    # 9. Register Accumulation & Injected Lineage Audit Jitter (Obj 1)
    df["interval_kwh"] = np.maximum(df["active_power_kw"], 0.0) * 0.25
    df["incremental_kwh"] = df.groupby("meter_id")["interval_kwh"].cumsum()
    df["raw_cumulative_kwh"] = df["current_cumulative_kwh"] + df["incremental_kwh"]

    # Inject 0.1% audit divergence defects (clock drift / packet jitter)
    jitter_mask = np.random.rand(total_rows) < 0.0010
    df.loc[jitter_mask, "raw_cumulative_kwh"] += np.random.choice(
        [-1.5, 2.25, 4.1], size=jitter_mask.sum()
    )

    # Update state file with closing cumulative register values
    final_registers = df.groupby("meter_id")["raw_cumulative_kwh"].last().to_dict()
    df_entities["current_cumulative_kwh"] = df_entities["meter_id"].map(final_registers)
    last_timestamp_generated = df["read_timestamp"].max()
    save_state(df_entities, last_timestamp_generated)

    # UUIDv4 event IDs
    df["event_id"] = [uuid.uuid4().hex for _ in range(total_rows)]

    # ==========================================
    # SLOT 5: SCHEMA PROJECTION & METADATA LINEAGE
    # ==========================================
    batch_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    df["source_batch_id"] = f"MOCK_{batch_ts}"

    df["raw_cumulative_kwh"] = df["raw_cumulative_kwh"].round(4)
    df["active_power_kw"] = df["active_power_kw"].round(3)
    df["reactive_power_kvar"] = df["reactive_power_kvar"].round(3)
    df["power_factor"] = df["power_factor"].round(4)
    df["voltage_v"] = df["voltage_v"].round(2)
    df["current_a"] = df["current_a"].round(2)

    final_columns = [
        "event_id",
        "meter_id",
        "substation_id",
        "feeder_id",
        "rate_class",
        "commission_year",
        "read_timestamp",
        "raw_cumulative_kwh",
        "active_power_kw",
        "reactive_power_kvar",
        "power_factor",
        "voltage_v",
        "current_a",
        "status_flag",
        "source_batch_id",
    ]
    df = df[final_columns]

    # ==========================================
    # DYNAMIC FILE EXPORT TO LANDING ZONE
    # ==========================================
    start_date_str = df["read_timestamp"].min().strftime("%Y%m%d")
    end_date_str = df["read_timestamp"].max().strftime("%Y%m%d")

    if choice == "1":
        target_filename = f"ami_interval_reads_{start_date_str}_to_{end_date_str}.csv"
    else:
        target_filename = f"ami_interval_reads_{start_date_str}.csv"

    output_path = LANDING_ZONE / target_filename
    df.to_csv(output_path, index=False)

    elapsed_time = time.time() - start_time

    # ==========================================
    # TERMINAL REPORT
    # ==========================================
    print("\n" + "=" * 65)
    print("SUCCESS: Mock dataset generated and staged for ingestion!")
    print("=" * 65)
    print(f" Profile Selected    : {cfg['name']}")
    print(f" Total Rows Staged   : {len(df):,}")
    print(
        f" Telemetry Range     : {df['read_timestamp'].min()} -> {df['read_timestamp'].max()}"
    )
    print(f" Generation Time     : {elapsed_time:.2f} seconds")
    print(f" File Name Staged    : {target_filename}")
    print(f" Target Landing Path : {output_path.resolve()}")
    print("=" * 65 + "\n")


def display_menu() -> str:
    print("\n" + "=" * 65)
    print("     UTILITY PIPELINE MOCK DATA GENERATOR (CLI HARNESS)")
    print("=" * 65)
    print("Select the dataset batch to generate for this project:\n")
    print(f" [1] Historical Backfill : {PROFILES['1']['description']}")
    print(f" [2] Daily Incremental   : {PROFILES['2']['description']}")
    print(" [R] Reset State         : Wipes .state cache to restart from Day 1.")
    print(" [Q] Quit                : Exit without generating data.")
    print("-" * 65)

    while True:
        selection = input("Enter your choice [1, 2, R, or Q]: ").strip()
        if selection in ["1", "2"]:
            return selection
        elif selection.upper() == "R":
            if STATE_DIR.exists():
                shutil.rmtree(STATE_DIR)
                STATE_DIR.mkdir(parents=True, exist_ok=True)
            print(
                "\n[RESET] State cache wiped. Next run will initialize at Day 1 (2026-06-01).\n"
            )
            return display_menu()
        elif selection.lower() == "q":
            print("\nOperation cancelled by user. Exiting.\n")
            sys.exit(0)
        else:
            print("Invalid input. Please enter '1', '2', 'R', or 'Q'.")


if __name__ == "__main__":
    user_choice = display_menu()
    generate_dataset(user_choice)
