# Municipal Electric Utility AMI Analytics: Engineering & Analytical Objectives

## Overview
This document defines the core data engineering quality audits, reliability benchmarks, and analytical mart objectives for the Advanced Metering Infrastructure (AMI) time-series data pipeline. Data is ingested from raw 15-minute interval telemetry landing batches and processed through staging and dimensional marts in DuckDB.

---

## Core Analytical & Engineering Objectives

### Objective 1: Telemetry Reconciliation & Physical Lineage Audit
* **Domain Context:** AMI meters report both high-frequency interval power demand ($kW$) and an absolute, cumulative dial accumulator register ($kWh$). These two values must align within strict physical laws.
* **Analytical Tasks:**
  * Use window functions (`LAG()`) partitioned by `meter_id` and ordered by `read_timestamp` to compute the discrete register delta:
    $$\Delta \text{Register} = \text{raw\_cumulative\_kwh}_t - \text{raw\_cumulative\_kwh}_{t-1}$$
  * Compare $\Delta \text{Register}$ against the interval energy consumption ($\text{active\_power\_kw} \times 0.25\text{ h}$).
* **Pipeline Deliverable:** 
  * Build an ELT audit mart (`marts.fct_telemetry_audit`) flagging divergence exceeding $\pm 1.0\%$.
  * Categorize anomalies into root causes: clock drift, dropped telemetry packets, register rollover, or current transformer (CT) multiplier configuration bugs.

---

### Objective 2: ANSI C84.1 Power Quality & Spatial Grid Reliability
* **Domain Context:** Electric utilities are legally obligated under ANSI C84.1 Range A to maintain steady-state delivery voltage within $\pm 5\%$ of nominal (228V–252V on 240V residential split-phase; 456V–504V on 480V commercial three-phase).
* **Analytical Tasks:**
  * Evaluate line voltage against upper and lower ANSI thresholds across every 15-minute interval.
  * Aggregate voltage distributions across the physical grid topological hierarchy (`substation_id` $\to$ `feeder_id` $\to$ `meter_id`).
* **Pipeline Deliverable:**
  * Build a diagnostic mart classifying undervoltage events into two operational categories:
    1. **Systemic Grid Outages:** Sags occurring simultaneously across $\ge 80\%$ of meters on a feeder circuit (indicating upstream substation capacitor bank trips or line faults).
    2. **Localized Service Drop Defects:** Isolated meter sags where adjacent meters on the same feeder remain nominal (indicating corroded customer neutral lugs, loose meter sockets, or overloaded service drops—a direct fire hazard).

---

### Objective 3: AC Power Triangle & Apparent Power Vector Integrity
* **Domain Context:** In alternating current (AC) networks, Active Power ($kW$), Reactive Power ($kVAR$), and Apparent Power ($kVA$) form a closed right-triangle relationship governed by physics: $S = \sqrt{P^2 + Q^2}$.
* **Analytical Tasks:**
  * Calculate theoretical apparent power:
    $$S_{\text{calc}} = \sqrt{\text{active\_power\_kw}^2 + \text{reactive\_power\_kvar}^2}$$
  * Derive theoretical displacement power factor:
    $$\text{PF}_{\text{calc}} = \frac{\text{active\_power\_kw}}{S_{\text{calc}}}$$
* **Pipeline Deliverable:**
  * Construct a data-hygiene validation model in staging that audits whether the reported `power_factor` matches $\text{PF}_{\text{calc}}$ within a tolerance of $\pm 0.015$. Mismatches flag firmware memory corruption or internal DSP calculation errors on specific meter models.

---

### Objective 4: Alarm Reconciliation: Hardware Telemetry vs. Derived Physics
* **Domain Context:** Edge device firmware alarms are prone to communication drops and misconfigured duration thresholds, resulting in both missed real events and spurious alarms.
* **Analytical Tasks:**
  * Construct an automated confusion matrix comparing hardware alarm payloads (`status_flag`) against warehouse-derived physical truth flags:
    * **True Positive (TP):** Firmware alarm fired AND physical voltage/power violated tolerance limits.
    * **Silent Failure / False Negative (FN):** Sensor violated physical thresholds, but the meter reported `NORMAL`.
    * **Spurious / False Positive (FP):** Firmware raised an alarm code, but physical 15-minute average metrics remained completely within tolerance (e.g., brief sub-second motor starting inrush).
* **Pipeline Deliverable:**
  * Build an operational reliability mart (`marts.rpt_meter_alarm_fidelity`) scoring meters by alarm reliability and exposing blind spots in the AMI mesh head-end.

---

### Objective 5: C&I Reactive Power & Low Power Factor Penalty Modeling
* **Domain Context:** Industrial induction motors and commercial HVAC compressors draw reactive power ($kVAR$), which loads utility transmission lines without performing useful work. Utilities assess steep billing penalties on non-residential accounts operating below a $0.90\text{ PF}$ threshold.
* **Analytical Tasks:**
  * Calculate 30-day rolling average power factors for `COMMERCIAL` and `INDUSTRIAL` rate classes.
  * Calculate peak reactive demand ($kVAR$) during grid peak intervals.
* **Pipeline Deliverable:**
  * Deliver a financial exposure model (`marts.rpt_power_factor_penalties`) computing unbilled reactive surcharges for accounts failing to maintain power factor compliance.

---

### Objective 6: Asset Health Index (AHI) & Predictive Meter Retirement
* **Domain Context:** Meter replacements should not be dictated strictly by calendar age, but by operational stress, electrical throughput, and telemetry error history.
* **Analytical Tasks:**
  * Calculate total cumulative energy throughput ($MWh$) accumulated across the asset's service life.
  * Count cumulative hours exposed to severe thermal or voltage stress ($V < 216\text{V}$ or $V > 254\text{V}$).
  * Track historical communication packet loss and register roll-over frequencies.
* **Pipeline Deliverable:**
  * Deliver an asset management mart (`marts.dim_meter_health_index`) categorizing field devices into three actionable maintenance tiers:
    * **Tier 1 (Critical Replacement):** Excessive lifetime $MWh$ throughput, severe drift risk, or frequent packet dropouts.
    * **Tier 2 (Monitor):** Moderate thermal stress or approaching register limits.
    * **Tier 3 (Healthy):** Nominal operating conditions.

---

### Objective 7: "Zero-Drop" Telemetry & Unreported Outage Classification
* **Domain Context:** Extended zero-consumption readings can signify either an unoccupied property or a localized blown fuse / downed service line.
* **Analytical Tasks:**
  * Identify meters reporting continuous `active_power_kw = 0.000` for $\ge 24$ consecutive hours (96 intervals).
  * Cross-reference zero power against the concurrent voltage reading.
* **Pipeline Deliverable:**
  * Segment zero-draw meters into two operational classes:
    * **Vacant / Zero-Load Premises:** Active power is zero, but line voltage remains nominal ($240\text{V}$).
    * **Unreported Outage / Open Phase:** Active power is zero AND voltage drops to near-zero ($< 15\text{V}$), signaling a downed customer service drop that failed to emit an outage alarm.

---

### Objective 8: Coincident Peak (CP) Contribution & Load Profiling
* **Domain Context:** Utilities face significant capacity market charges based on their customer demand during the single highest system-wide peak hour of the summer (e.g., 4CP / 5CP periods).
* **Analytical Tasks:**
  * Identify the single 15-minute interval each month that registered the highest total aggregated active power demand ($kW$) across the entire grid.
  * Disaggregate that specific peak interval by customer rate class (`RESIDENTIAL`, `COMMERCIAL`, `INDUSTRIAL`).
* **Pipeline Deliverable:**
  * Produce an executive dashboard mart (`marts.rpt_coincident_peak_allocation`) showing each customer tariff class's percentage contribution to peak demand, supporting municipal rate design and load forecasting.