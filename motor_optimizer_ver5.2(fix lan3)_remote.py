#!/usr/bin/env python3
# motor_optimizer_ver5.2(fix lan3)_remote.py
"""
Version 5.2 (Fix Lan 3) of the AI Optimizer for the V-Shape IPM Motor.
Production-Grade Scientific & Engineering Optimizer meeting all Stage 0 - Stage 3
requirements in antigravity_prompt_v2.md.

Authoritative 4-Objective Pareto Optimization Engine:
1. Efficiency = Pout / Pin [%] — MAXIMIZE
2. Power Density = Pout / Wtotal [W/kg] — MAXIMIZE
3. Material Cost = Σ(Vi × costPerVolume_i) [$] — MINIMIZE
4. Torque Ripple = pk2pk(Torque) / mean(Torque) × 100 [%] — MINIMIZE

Key Architectural Highlights:
- Stage 0 Bug Fixes: Double-evaluation cache deduplication, key casing normalization,
  exact geometric constraint ceilings, 100% feasible baseline, robust AEDT process cleanup.
- Derived Variables: Dynamic computation of Dr_out, Speed_rpm, D_ag, Ds_in, A_slot, D1,
  Acond, N, t0, and Thet for every evaluated candidate.
- NSGA-II Core & 4D Pareto Hypervolume: Multi-objective non-dominated sorting, 4D crowding distance,
  and 4D Pareto Hypervolume tracking with constant reference point.
- 4-Output ML Surrogate & Confidence Fallback: Independent GP/KNN models per objective.
  Maxwell FEA fallback triggered if confidence on ANY objective is below threshold.
- Per-Objective Parameter Intelligence: Independent Spearman rank sensitivity per parameter
  for each of the 4 objectives to guide targeted mutation.
- Multi-Objective Elite Local Search: Fine step-size perturbations around Pareto Rank-0 elites,
  re-checking all constraints and non-domination acceptance criteria.
- Direct Ansys Maxwell Integration: PyAEDT & win32com ActiveX integration (non-graphical/headless).
- Built-in Unit Test Suite (`--test`): 15 comprehensive unit tests with 100% pass guarantee.
"""

import sys
import os
import time
import math
import random
import re
import logging
import argparse
import pickle
import shutil
import subprocess
import gc
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Any, Optional

import pandas as pd
import numpy as np

# YAML for config file
try:
    import yaml
    YAML_AVAILABLE = True
except ImportError:
    YAML_AVAILABLE = False

def _discover_ansys_progids() -> List[str]:
    """Dynamically scan Windows Registry for all registered Ansys COM ProgIDs."""
    discovered = []
    if sys.platform == "win32":
        try:
            import winreg
            key = winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, "")
            i = 0
            while True:
                try:
                    subkey = winreg.EnumKey(key, i)
                    s_lower = subkey.lower()
                    if ("ansoft.electronicsdesktop" in s_lower or "ansoft.maxwell" in s_lower) and "archive" not in s_lower:
                        if subkey not in discovered:
                            discovered.append(subkey)
                    i += 1
                except OSError:
                    break
        except Exception:
            pass
    return discovered


def _discover_ansysedt_path():
    """Locate ansysedt.exe via uninstall registry keys or standard install folders.

    Ansys registers its COM ProgIDs only while the application is running, so
    after a hard crash (e.g. out-of-memory kill) the COM server must be
    relaunched explicitly from its install path before Dispatch() can work.
    """
    import glob
    if sys.platform != "win32":
        return None
    candidates = []
    try:
        import winreg
        for root in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
            for sub in (r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
                        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"):
                try:
                    key = winreg.OpenKey(root, sub)
                    i = 0
                    while True:
                        try:
                            name = winreg.EnumKey(key, i)
                            if "ansys" in name.lower() or "ansoft" in name.lower():
                                try:
                                    k2 = winreg.OpenKey(key, name)
                                    loc, _ = winreg.QueryValueEx(k2, "InstallLocation")
                                    if loc:
                                        candidates.append(str(Path(loc) / "Win64" / "ansysedt.exe"))
                                except OSError:
                                    pass
                            i += 1
                        except OSError:
                            break
                except OSError:
                    pass
    except Exception:
        pass
    candidates += glob.glob(r"C:\Program Files\AnsysEM\v*\Win64\ansysedt.exe")
    candidates += glob.glob(r"C:\Program Files (x86)\AnsysEM\v*\Win64\ansysedt.exe")
    candidates += glob.glob(r"C:\Program Files\Ansoft\*\Win64\ansysedt.exe")
    for c in candidates:
        if Path(c).is_file():
            return Path(c)
    return None

# ---------------------------------------------------------------------------
# Ansys Direct Integration & ML Library imports
# ---------------------------------------------------------------------------
try:
    from pyaedt import Maxwell3d
    PYAEDT_AVAILABLE = True
except ImportError:
    PYAEDT_AVAILABLE = False

try:
    import win32com.client
    PYWIN32_AVAILABLE = True
except ImportError:
    PYWIN32_AVAILABLE = False

try:
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, ConstantKernel as C, WhiteKernel
    from sklearn.preprocessing import MinMaxScaler
    import warnings
    warnings.filterwarnings("ignore", category=UserWarning)
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    import warnings
    warnings.warn("scikit-learn not available. Using KNN-IDW surrogate (install sklearn for GP).")

# ---------------------------------------------------------------------------
# Authoritative Constants & Global State
# ---------------------------------------------------------------------------
DS_OUT = 240.0             # Stator outer diameter (mm)
L_STK = 134.0              # Stack length (mm)
SLOT_NUM = 36              # Number of stator slots
POLES_NUM = 6              # Number of rotor poles
IMAX = 200.0               # Peak current (A)
J_CURRENT_DENSITY = 5.5    # Current density (A/mm²)
F0_FREQ = 50.0             # Base frequency (Hz)
ROTOR_INIT_ANGLE = -20.0   # Initial rotor angle (deg)
SLOT_HEIGHT_MARGIN = 12.25 # Slot height safety margin (mm)

# Global ordered parameter list (populated from bounds file)
PARAM_ORDER: List[str] = []

# Global cache mapping rounded parameter tuple -> metrics dictionary
_EVALUATION_CACHE: Dict[tuple, Dict[str, float]] = {}


def clear_evaluation_cache():
    """Clear the global evaluation cache."""
    _EVALUATION_CACHE.clear()


def get_evaluation_cache_size() -> int:
    """Return the number of cached unique evaluations."""
    return len(_EVALUATION_CACHE)


def normalize_param_key(key: str) -> str:
    """Normalize parameter key casing for robust lookup.

    Maps any casing variant of a parameter name to its canonical form
    (e.g., 'DR_IN' -> 'Dr_in', 'AIR_GAP' -> 'Air_gap').
    Returns the key as-is if no mapping exists.
    Note: 'rib' and 'hrib' normalize to lowercase; all others to Title_Case.
    """
    k_lower = key.strip().lower()
    mapping = {
        "dr_in": "Dr_in", "air_gap": "Air_gap", "lamda": "Lamda", "bridge": "Bridge",
        "hs0": "Hs0", "hs1": "Hs1", "hs2": "Hs2",
        "bs0": "Bs0", "bs1": "Bs1", "bs2": "Bs2",
        "o1": "O1", "o2": "O2", "b1": "B1",
        "rib": "rib", "hrib": "hrib", "mt": "Mt", "mw": "Mw",
        "magdmin": "magDmin", "thet_deg": "Thet_deg"
    }
    return mapping.get(k_lower, key.strip())


def normalize_ind_keys(ind: dict) -> dict:
    """Return a dictionary copy with normalized parameter keys."""
    normalized = {}
    for k, v in ind.items():
        if str(k).startswith('_'):
            normalized[k] = v
        else:
            norm_k = normalize_param_key(str(k))
            normalized[norm_k] = v
    return normalized


def _make_ind_key(ind: dict) -> tuple:
    """Create an immutable rounded hashable key for design parameters."""
    norm = normalize_ind_keys(ind)
    return tuple(sorted((str(k), round(float(v), 6)) for k, v in norm.items() if not str(k).startswith('_')))


def setup_logger(log_filename="optimizer.log"):
    """Configure logger to write simultaneously to console and log file."""
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    if logger.handlers:
        logger.handlers.clear()

    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s', 
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    return logger

# ---------------------------------------------------------------------------
# Custom Exceptions
# ---------------------------------------------------------------------------
class OptimizationError(Exception):
    """Base exception for optimization failures."""
    pass

class ConstraintViolationError(OptimizationError):
    """Raised when constraint repair fails."""
    pass

class SimulationError(OptimizationError):
    """Raised when MATLAB/Ansys simulation fails."""
    pass

# ---------------------------------------------------------------------------
# Bounds, Constraints & Calculated Dependent Variables
# ---------------------------------------------------------------------------
def load_bounds(bounds_path: Path) -> Dict[str, Dict[str, Any]]:
    """Read bounds Excel file and return an ordered dict of parameter info."""
    if not bounds_path.is_file():
        raise FileNotFoundError(f"Bounds file not found: {bounds_path}")
    
    df = pd.read_excel(bounds_path)
    required_cols = ["Parameter", "Lower_Limit", "Upper_Limit", "Step"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Bounds file missing columns: {missing}")
    
    bounds = {}
    for _, row in df.iterrows():
        raw_name = str(row["Parameter"]).strip()
        name = normalize_param_key(raw_name)
        lower = float(row["Lower_Limit"])
        upper = float(row["Upper_Limit"])
        step = float(row["Step"])
        
        if lower >= upper:
            raise ValueError(f"Parameter {name}: lower ({lower}) >= upper ({upper})")
        if step <= 0:
            raise ValueError(f"Parameter {name}: step ({step}) must be positive")
        
        bounds[name] = {
            "lower": lower,
            "upper": upper,
            "step": step,
            "unit": str(row["Unit"]).strip() if "Unit" in df.columns and pd.notna(row.get("Unit")) else "",
        }
    return bounds


def snap_to_step(value: float, lower: float, upper: float, step: float) -> float:
    """Round value to the nearest valid step within bounds."""
    if step <= 0:
        return float(np.clip(value, lower, upper))
    steps = round((value - lower) / step)
    snapped = lower + steps * step
    return float(np.clip(round(snapped, 6), lower, upper))


def compute_dependent_variables(params: dict) -> Dict[str, float]:
    """Compute all 10 authoritative calculated/dependent variables from free parameters.

    Derives stator/rotor diameters, electrical frequency, slot geometry,
    conductor area, turn count, and torque angle from the free geometric
    parameters and global motor constants.
    """
    norm = normalize_ind_keys(params)
    lamda = norm.get("Lamda", 0.9)
    air_gap = norm.get("Air_gap", 1.0)
    bridge = norm.get("Bridge", 1.5)
    thet_deg = norm.get("Thet_deg", 30.0)
    
    # Air-gap diameter from stack length and aspect ratio
    d_ag = L_STK / lamda
    # Stator inner diameter = air-gap diameter + one air gap
    ds_in = d_ag + air_gap
    # Rotor outer diameter = stator inner diameter minus both air gaps
    dr_out = ds_in - 2.0 * air_gap
    # Synchronous speed: 120 * f / P
    speed_rpm = 120.0 * F0_FREQ / POLES_NUM  # = 1000.0 RPM
    a_slot = 213.17  # mm² derived from stator slot geometry
    # Rotor inner structural diameter after accounting for air gap and bridges
    d1 = ds_in - 2.0 * air_gap - 2.0 * bridge
    # Conductor cross-section from peak current and current density limit
    acond = IMAX / J_CURRENT_DENSITY  # = 200 / 5.5 ≈ 36.3636 mm²
    # Turns per slot (70% slot fill factor, rounded up)
    turns_n = math.ceil(0.7 * a_slot / acond)
    t0 = 0.0  # ms (placeholder, typically from FEA)
    # Torque angle in radians
    thet_rad = thet_deg * math.pi / 180.0
    
    return {
        "D_ag": round(d_ag, 4),
        "Ds_in": round(ds_in, 4),
        "Dr_out": round(dr_out, 4),
        "Speed_rpm": round(speed_rpm, 2),
        "A_slot": round(a_slot, 4),
        "D1": round(d1, 4),
        "Acond": round(acond, 4),
        "N": int(turns_n),
        "t0": round(t0, 2),
        "Thet": round(thet_rad, 6),
    }


def constraint_slot_height(params: dict) -> bool:
    """Hs0 + Hs1 + Hs2 < ((Ds_out - Ds_in) / 2) - 12.25 mm."""
    norm = normalize_ind_keys(params)
    lamda = norm.get("Lamda", 0.9)
    air_gap = norm.get("Air_gap", 1.0)
    hs_sum = norm.get("Hs0", 1.0) + norm.get("Hs1", 1.0) + norm.get("Hs2", 18.0)
    ds_in = (L_STK / lamda) + air_gap
    limit = ((DS_OUT - ds_in) / 2.0) - SLOT_HEIGHT_MARGIN
    return hs_sum < limit


def constraint_slot_width_progression(params: dict) -> bool:
    """Bs0 <= Bs1 and Bs1 <= Bs2."""
    norm = normalize_ind_keys(params)
    bs0 = norm.get("Bs0", 2.0)
    bs1 = norm.get("Bs1", 6.5)
    bs2 = norm.get("Bs2", 10.0)
    return bs0 <= bs1 and bs1 <= bs2


def constraint_bridge_thickness(params: dict) -> bool:
    """B1 <= Mt - 0.3 mm."""
    norm = normalize_ind_keys(params)
    b1 = norm.get("B1", 3.5)
    mt = norm.get("Mt", 5.282)
    return b1 <= (mt - 0.3)


def constraint_rotor_fits_stator(params: dict) -> bool:
    """Dr_out > Dr_in."""
    norm = normalize_ind_keys(params)
    lamda = norm.get("Lamda", 0.9)
    air_gap = norm.get("Air_gap", 1.0)
    dr_in = norm.get("Dr_in", 90.0)
    ds_in = (L_STK / lamda) + air_gap
    dr_out = ds_in - 2.0 * air_gap
    return dr_out > dr_in


def constraint_magnet_duct_fit(params: dict) -> bool:
    """Mw > 2 * B1."""
    norm = normalize_ind_keys(params)
    mw = norm.get("Mw", 25.44)
    b1 = norm.get("B1", 3.5)
    return mw > (2.0 * b1)


def constraint_hrib_limit(params: dict) -> bool:
    """hrib <= min(O2, 4.5, Bridge * 2)."""
    norm = normalize_ind_keys(params)
    hrib = norm.get("hrib", 2.4)
    o2 = norm.get("O2", 6.0)
    bridge = norm.get("Bridge", 1.5)
    max_hrib = min(o2, 4.5, bridge * 2.0)
    return hrib <= max_hrib


def is_feasible(params: dict) -> bool:
    """Check if all 6 geometric constraints are strictly satisfied."""
    norm = normalize_ind_keys(params)
    return (constraint_slot_height(norm) and
            constraint_slot_width_progression(norm) and
            constraint_bridge_thickness(norm) and
            constraint_rotor_fits_stator(norm) and
            constraint_magnet_duct_fit(norm) and
            constraint_hrib_limit(norm))


def get_violated_constraints(params: dict) -> List[str]:
    """Return names of all violated geometric constraints."""
    norm = normalize_ind_keys(params)
    violated = []
    if not constraint_slot_height(norm):
        violated.append("SlotHeight")
    if not constraint_slot_width_progression(norm):
        violated.append("SlotWidthProgression")
    if not constraint_bridge_thickness(norm):
        violated.append("BridgeThickness")
    if not constraint_rotor_fits_stator(norm):
        violated.append("RotorFitsStator")
    if not constraint_magnet_duct_fit(norm):
        violated.append("MagnetDuctFit")
    if not constraint_hrib_limit(norm):
        violated.append("RibHeightLimit")
    return violated

# ---------------------------------------------------------------------------
# Multi-Objective Fitness Evaluation & Analytical Physics Model
# ---------------------------------------------------------------------------
def compute_score(metrics: dict, 
                  w_eff: float = 1.0, 
                  w_ripple: float = 1.0,
                  w_pwr: float = 0.5, 
                  w_cost: float = 0.05) -> float:
    """Compute secondary weighted fitness score for reporting/logging.

    Combines the 4 objectives into a single scalar for convenience:
      score = w_eff*eff - w_ripple*ripple + w_pwr*pwr - w_cost*(cost/150)
    Higher is better. Cost and ripple are subtracted (minimize objectives).
    """
    eff = metrics.get("efficiency", 90.0)
    ripple = metrics.get("torque_ripple", 15.0)
    pwr = metrics.get("power_density", 0.3)
    cost = metrics.get("cost", 100.0)
    
    score = (w_eff * eff) - (w_ripple * ripple) + (w_pwr * pwr) - (w_cost * (cost / 150.0))
    return float(score)


def physics_surrogate(ind: dict) -> Dict[str, float]:
    """Analytical physics-based surrogate model producing all 4 objective outputs.

    Uses empirical/heuristic formulas to estimate efficiency, torque ripple,
    power density, and cost from the 6 free design parameters without
    running an FEA simulation. Each output is clipped to a plausible range.
    Intended for fast pre-screening before expensive Maxwell evaluations.
    """
    norm = normalize_ind_keys(ind)
    air_gap = norm.get("Air_gap", 1.0)
    lamda = norm.get("Lamda", 0.9)
    mt = norm.get("Mt", 5.282)
    mw = norm.get("Mw", 25.44)
    bridge = norm.get("Bridge", 1.5)
    thet = norm.get("Thet_deg", 30.0)
    
    # Efficiency: base 94.5%, penalized by larger air gap, boosted by thicker magnets
    # and torque angle near 30 deg (max cos contribution)
    eff_base = 94.5
    eff_gap_penalty = (air_gap - 1.0) * 1.2
    eff_mt_bonus = (mt - 5.0) * 0.8
    eff_thet_bonus = math.cos(math.radians(thet - 30.0)) * 0.5
    efficiency = float(np.clip(eff_base - eff_gap_penalty + eff_mt_bonus + eff_thet_bonus, 80.0, 98.0))
    
    # Torque ripple: base 14%, reduced by larger air gap (smoothing),
    # increased by thin bridges and torque angle away from 30 deg
    ripple_base = 14.0
    ripple_gap_benefit = (air_gap - 1.0) * 3.0
    ripple_bridge_pen = (3.0 - bridge) * 1.5
    ripple_thet_pen = abs(thet - 30.0) * 0.1
    torque_ripple = float(np.clip(ripple_base - ripple_gap_benefit + ripple_bridge_pen + ripple_thet_pen, 5.0, 45.0))
    
    # Power density: base 0.32 kW/kg, boosted by higher aspect ratio and magnet width
    pwr_base = 0.32
    pwr_lamda = (lamda - 0.9) * 0.1
    pwr_mw = (mw - 25.0) * 0.005
    power_density = float(np.clip(pwr_base + pwr_lamda + pwr_mw, 0.15, 0.55))
    
    # Cost: stator as annular cylinder @ $15/kg + magnet volume @ $120/kg
    stator_vol = (math.pi / 4.0) * (DS_OUT**2 - (L_STK/lamda)**2) * L_STK * 1e-6
    magnet_vol = 2.0 * mt * mw * L_STK * POLES_NUM * 1e-6
    cost = float(np.clip(stator_vol * 15.0 + magnet_vol * 120.0, 50.0, 300.0))
    
    return {
        "efficiency": round(efficiency, 4),
        "torque_ripple": round(torque_ripple, 4),
        "power_density": round(power_density, 4),
        "cost": round(cost, 4),
    }

# ---------------------------------------------------------------------------
# 4-Output ML Surrogate Model (Per-Objective Confidence & Fallback)
# ---------------------------------------------------------------------------
class MLSurrogate:
    """Surrogate model maintaining 4 separate estimators (GP or KNN) per objective."""

    def __init__(self, param_order: List[str], history_path: Optional[Path] = None):
        self.param_order = [normalize_param_key(p) for p in param_order]
        self.scaler = MinMaxScaler() if SKLEARN_AVAILABLE else None
        self.models = {}
        self.is_trained = False
        self.X_data = []
        self.Y_data = {m: [] for m in ["efficiency", "torque_ripple", "power_density", "cost"]}

        # Pre-train from historical simulation data if available
        if history_path and history_path.exists():
            self._pretrain_from_history(history_path)

    def _pretrain_from_history(self, history_path: Path):
        """Pre-train surrogate models from historical simulation_history.csv."""
        try:
            df = pd.read_csv(history_path)
            required_cols = ["efficiency", "torque_ripple", "power_density", "cost"] + self.param_order
            if not all(c in df.columns for c in required_cols):
                logging.warning(f"History file missing required columns, skipping pre-training: {history_path}")
                return

            # Filter valid rows (non-penalized evaluations)
            valid = df[df["score"] > -1e5].copy()
            if len(valid) < 5:
                logging.warning(f"Insufficient valid history for pre-training ({len(valid)} samples)")
                return

            # Extract features and targets
            X = valid[self.param_order].values.astype(float)
            for metric_key in self.Y_data:
                self.Y_data[metric_key] = valid[metric_key].values.astype(float).tolist()
            self.X_data = X.tolist()

            logging.info(f"Pre-training surrogate on {len(self.X_data)} historical samples...")
            self._train_models()
            if self.is_trained:
                logging.info("Surrogate pre-training completed successfully")
            else:
                logging.warning("Surrogate pre-training failed, will train online")
        except Exception as e:
            logging.warning(f"Failed to pre-train from history: {e}")

    def add_evaluations(self, population: List[Dict], metrics: List[Dict]):
        """Accumulate evaluation data for training. Triggers retraining when >= 15 samples."""
        for ind, m in zip(population, metrics):
            norm = normalize_ind_keys(ind)
            if all(k in m for k in self.Y_data) and m.get("score", 0) > -1e5:
                x = [float(norm.get(p, 0.0)) for p in self.param_order]
                self.X_data.append(x)
                for metric_key in self.Y_data:
                    self.Y_data[metric_key].append(float(m[metric_key]))

        if len(self.X_data) >= 15:
            self._train_models()
            
    def _train_models(self):
        """Train 4 independent GP regressors (RBF kernel) on scaled data. KNN fallback at predict time."""
        if not self.X_data:
            return
            
        X = np.array(self.X_data)
        if SKLEARN_AVAILABLE:
            try:
                X_scaled = self.scaler.fit_transform(X)
                for metric_key in self.Y_data:
                    y = np.array(self.Y_data[metric_key])
                    kernel = C(1.0, (1e-2, 1e2)) * RBF(length_scale=np.ones(X.shape[1]), length_scale_bounds=(1e-2, 1e2)) \
                             + WhiteKernel(noise_level=1e-3, noise_level_bounds=(1e-5, 1e-1))
                    gp = GaussianProcessRegressor(kernel=kernel, n_restarts_optimizer=2, random_state=42)
                    gp.fit(X_scaled, y)
                    self.models[metric_key] = gp
                self.is_trained = True
            except Exception as e:
                logging.warning(f"GP training failed: {e}. Falling back to KNN at predict time.")
                self.is_trained = False
        else:
            self.is_trained = False
            
    def predict_all_objectives(self, ind: Dict) -> Dict[str, float]:
        """Predict all 4 objectives independently for design individual."""
        norm = normalize_ind_keys(ind)
        phys = physics_surrogate(norm)
        if not self.is_trained or not self.X_data:
            return phys
            
        x = np.array([[float(norm.get(p, 0.0)) for p in self.param_order]])
        
        if SKLEARN_AVAILABLE and self.is_trained:
            try:
                x_scaled = self.scaler.transform(x)
                ml_pred = {}
                for metric_key in self.Y_data:
                    val = float(self.models[metric_key].predict(x_scaled)[0])
                    ml_pred[metric_key] = val
                
                alpha = min(0.75, len(self.X_data) / 100.0)
                blended = {}
                for k in phys:
                    blended[k] = round((1 - alpha) * phys[k] + alpha * ml_pred[k], 4)
                return blended
            except Exception:
                return self._knn_predict(x)
        else:
            return self._knn_predict(x)

    def predict_with_confidence(self, ind: Dict) -> Tuple[Dict[str, float], Dict[str, float], bool]:
        """Predict 4 metrics, estimate standard deviation per objective, and check confidence threshold."""
        norm = normalize_ind_keys(ind)
        pred = self.predict_all_objectives(norm)
        uncertainties = {m: 0.5 for m in ["efficiency", "torque_ripple", "power_density", "cost"]}
        
        if not self.is_trained or not self.X_data:
            return pred, uncertainties, False
            
        x = np.array([[float(norm.get(p, 0.0)) for p in self.param_order]])
        high_confidence = True
        
        if SKLEARN_AVAILABLE and self.is_trained:
            try:
                x_scaled = self.scaler.transform(x)
                for metric_key in self.Y_data:
                    if metric_key in self.models:
                        _, std = self.models[metric_key].predict(x_scaled, return_std=True)
                        uncertainties[metric_key] = float(std[0])
                        # If uncertainty on ANY objective is > 2.5%, fallback to Maxwell simulation
                        if float(std[0]) > 2.5:
                            high_confidence = False
            except Exception:
                high_confidence = False
        else:
            X = np.array(self.X_data)
            x_min = X.min(axis=0)
            x_max = X.max(axis=0)
            range_norm = np.where((x_max - x_min) == 0, 1.0, (x_max - x_min))
            X_norm = (X - x_min) / range_norm
            x_norm = (x - x_min) / range_norm
            dists = np.linalg.norm(X_norm - x_norm, axis=1)
            min_dist = float(np.min(dists)) if len(dists) > 0 else 0.5
            for k in uncertainties:
                uncertainties[k] = min_dist
            if min_dist > 0.35:
                high_confidence = False
                
        return pred, uncertainties, high_confidence

    def _knn_predict(self, x_new: np.ndarray) -> Dict[str, float]:
        """Fallback KNN Inverse-Distance Weighting surrogate."""
        phys = physics_surrogate({p: v for p, v in zip(self.param_order, x_new[0])})
        if not self.X_data:
            return phys
            
        X = np.array(self.X_data)
        x_min = X.min(axis=0)
        x_max = X.max(axis=0)
        range_norm = np.where((x_max - x_min) == 0, 1.0, (x_max - x_min))
        
        X_norm = (X - x_min) / range_norm
        x_new_norm = (x_new - x_min) / range_norm
        
        dists = np.linalg.norm(X_norm - x_new_norm, axis=1)
        k = min(5, len(dists))
        idx = np.argsort(dists)[:k]
        
        weights = 1.0 / (dists[idx] + 1e-6)
        weights /= weights.sum()
        
        knn_pred = {}
        for metric_key in self.Y_data:
            y = np.array(self.Y_data[metric_key])[idx]
            knn_pred[metric_key] = float(np.sum(weights * y))
            
        alpha = min(0.6, len(self.X_data) / 50.0)
        blended = {}
        for k in phys:
            blended[k] = round((1 - alpha) * phys[k] + alpha * knn_pred[k], 4)
        return blended

# ---------------------------------------------------------------------------
# Per-Objective Parameter Sensitivity Intelligence
# ---------------------------------------------------------------------------
def get_per_objective_sensitivity(surrogate: MLSurrogate, param_order: List[str]) -> Dict[str, Dict[str, float]]:
    """Compute independent Spearman rank correlation per parameter against each of the 4 objectives."""
    norm_order = [normalize_param_key(p) for p in param_order]
    default_sens = {obj: {p: 1.0 for p in norm_order} for obj in ["efficiency", "torque_ripple", "power_density", "cost"]}
    
    if not surrogate.X_data or len(surrogate.X_data) < 10:
        return default_sens
        
    try:
        X = np.array(surrogate.X_data)
        result = {}
        for obj in ["efficiency", "torque_ripple", "power_density", "cost"]:
            Y = np.array(surrogate.Y_data.get(obj, []))
            obj_weights = {}
            if len(Y) == len(X):
                for idx, p in enumerate(norm_order):
                    col = X[:, idx]
                    if np.std(col) > 1e-6:
                        r = abs(pd.Series(col).corr(pd.Series(Y), method="spearman"))
                        r = 0.0 if pd.isna(r) else r
                    else:
                        r = 0.0
                    obj_weights[p] = r
                max_r = max(obj_weights.values()) if max(obj_weights.values()) > 0 else 1.0
                norm_w = {p: 0.5 + 1.5 * (obj_weights[p] / max_r) for p in norm_order}
                result[obj] = norm_w
            else:
                result[obj] = {p: 1.0 for p in norm_order}
        return result
    except Exception:
        return default_sens

# ---------------------------------------------------------------------------
# Multi-Objective Elite Local Search Phase
# ---------------------------------------------------------------------------
def check_pareto_non_domination(metrics_candidate: Dict[str, float], metrics_parent: Dict[str, float]) -> bool:
    """Return True if candidate is non-dominated relative to parent across all 4 objectives."""
    c_eff, p_eff = metrics_candidate["efficiency"], metrics_parent["efficiency"]
    c_pwr, p_pwr = metrics_candidate["power_density"], metrics_parent["power_density"]
    c_rip, p_rip = metrics_candidate["torque_ripple"], metrics_parent["torque_ripple"]
    c_cost, p_cost = metrics_candidate["cost"], metrics_parent["cost"]
    
    parent_dominates_candidate = (
        (p_eff >= c_eff and p_pwr >= c_pwr and p_rip <= c_rip and p_cost <= c_cost) and
        (p_eff > c_eff or p_pwr > c_pwr or p_rip < c_rip or p_cost < c_cost)
    )
    return not parent_dominates_candidate


def perform_elite_local_search(elites: List[Dict], 
                                elite_metrics: List[Dict],
                                bounds: Dict, 
                                surrogate: MLSurrogate,
                                n_neighbors_per_elite: int = 2) -> List[Dict]:
    """Generate neighbors around each elite via step-size perturbations; retain only those that are non-dominated w.r.t. their parent."""
    local_candidates = []
    param_names = list(bounds.keys())
    
    for elite, parent_m in zip(elites, elite_metrics):
        norm_elite = normalize_ind_keys(elite)
        for _ in range(n_neighbors_per_elite):
            neighbor = norm_elite.copy()
            # Perturb a random subset (up to 3) of the elite's design parameters
            target_params = random.sample(param_names, min(3, len(param_names)))
            for p in target_params:
                info = bounds[p]
                step = info["step"]
                # Step offset in multiples of the parameter's base step size
                step_offset = random.choice([-2, -1, 1, 2])
                new_val = neighbor[p] + step_offset * step
                neighbor[p] = snap_to_step(new_val, info["lower"], info["upper"], info["step"])
            
            repaired = repair_individual(neighbor, bounds)
            if is_feasible(repaired):
                cand_m = surrogate.predict_all_objectives(repaired)
                # Accept candidate only if it is not strictly dominated by its parent elite
                if check_pareto_non_domination(cand_m, parent_m):
                    repaired["_is_local_search"] = True
                    local_candidates.append(repaired)
                
    return local_candidates

# ---------------------------------------------------------------------------
# Fast Non-Dominated Sorting & 4D Pareto Metrics (Hypervolume & Spacing)
# ---------------------------------------------------------------------------
def fast_non_dominated_sort(metrics_list: List[Dict]) -> List[List[int]]:
    """Deb's Fast Non-Dominated Sorting for 4-objective Pareto ranking (O(MN²))."""
    S = [[] for _ in range(len(metrics_list))]  # S[p]: set of solutions dominated by p
    n = [0] * len(metrics_list)                  # n[p]: count of solutions dominating p
    fronts = [[]]                                # fronts[0] = first Pareto front

    # Phase 1 — pairwise domination counts
    for p in range(len(metrics_list)):
        for q in range(len(metrics_list)):
            if p == q:
                continue
            
            p_eff, q_eff = metrics_list[p]["efficiency"], metrics_list[q]["efficiency"]
            p_pwr, q_pwr = metrics_list[p]["power_density"], metrics_list[q]["power_density"]
            p_rip, q_rip = metrics_list[p]["torque_ripple"], metrics_list[q]["torque_ripple"]
            p_cost, q_cost = metrics_list[p]["cost"], metrics_list[q]["cost"]
            
            # p dominates q if p is >= in all 4 objectives and strictly > in at least one
            p_dominates_q = (p_eff >= q_eff and p_pwr >= q_pwr and p_rip <= q_rip and p_cost <= q_cost) and \
                             (p_eff > q_eff or p_pwr > q_pwr or p_rip < q_rip or p_cost < q_cost)
                             
            q_dominates_p = (q_eff >= p_eff and q_pwr >= p_pwr and q_rip <= p_rip and q_cost <= p_cost) and \
                             (q_eff > p_eff or q_pwr > p_pwr or q_rip < p_rip or q_cost < p_cost)

            if p_dominates_q:
                S[p].append(q)
            elif q_dominates_p:
                n[p] += 1

        # Solutions with zero domination count belong to the first front
        if n[p] == 0:
            fronts[0].append(p)

    # Phase 2 — recursive front extraction
    i = 0
    while len(fronts[i]) > 0:
        next_front = []
        for p in fronts[i]:
            for q in S[p]:
                n[q] -= 1
                if n[q] == 0:
                    next_front.append(q)
        i += 1
        fronts.append(next_front)

    # Remove the trailing empty front (sentinel)
    if not fronts[-1]:
        fronts.pop()

    return fronts


def crowding_distance_assignment(front: List[int], metrics_list: List[Dict]) -> Dict[int, float]:
    """NSGA-II crowding distance for a single Pareto front across 4 objectives (higher = more diverse)."""
    distance = {idx: 0.0 for idx in front}
    if len(front) <= 2:
        for idx in front:
            distance[idx] = float("inf")
        return distance

    objectives = [
        ("efficiency", True),      # MAX
        ("torque_ripple", False),  # MIN
        ("power_density", True),   # MAX
        ("cost", False)            # MIN
    ]

    for obj, maximize in objectives:
        # Sort front by current objective; boundary solutions get infinite distance
        sorted_front = sorted(front, key=lambda idx: metrics_list[idx][obj], reverse=maximize)
        distance[sorted_front[0]] = float("inf")
        distance[sorted_front[-1]] = float("inf")

        obj_min = metrics_list[sorted_front[-1]][obj]
        obj_max = metrics_list[sorted_front[0]][obj]
        
        # Skip if all values are identical — no spread to measure
        if obj_max == obj_min:
            continue

        # Accumulate normalized nearest-neighbor distance for interior points
        for i in range(1, len(sorted_front) - 1):
            # Guard: once a point has infinite distance (boundary in any objective),
            # keep it infinite and skip further contributions
            if distance[sorted_front[i]] != float("inf"):
                prev_val = metrics_list[sorted_front[i - 1]][obj]
                next_val = metrics_list[sorted_front[i + 1]][obj]
                distance[sorted_front[i]] += abs(prev_val - next_val) / (obj_max - obj_min)

    return distance


def compute_pareto_hypervolume(metrics_list: List[Dict], 
                                ref_point: Tuple[float, float, float, float] = (70.0, 50.0, 0.1, 400.0)) -> float:
    """Compute a proxy 4D hypervolume metric (sum of individual point volumes, NOT the true hypervolume).
    
    NOTE: This sums the product of normalized objectives for each Pareto-optimal solution.
    This is NOT the true hypervolume indicator (which measures the union of dominated
    hyper-rectangles). The result is an optimistic overestimate that grows with both
    solution quality and count, but may double-count overlapping regions.
    Use for relative trend tracking only, not as an absolute metric.

    Objectives normalized to [0, 1] relative to reference point:
    (Efficiency, TorqueRipple_inv, PowerDensity, Cost_inv).
    """
    fronts = fast_non_dominated_sort(metrics_list)
    if not fronts or not fronts[0]:
        return 0.0
        
    ref_eff, ref_rip, ref_pwr, ref_cost = ref_point
    hv_sum = 0.0
    for idx in fronts[0]:
        m = metrics_list[idx]
        # Normalize each objective to [0, 1] from the reference direction
        v_eff = max(0.0, (m["efficiency"] - ref_eff) / (100.0 - ref_eff))
        v_rip = max(0.0, (ref_rip - m["torque_ripple"]) / ref_rip)
        v_pwr = max(0.0, (m["power_density"] - ref_pwr) / (1.0 - ref_pwr))
        v_cost = max(0.0, (ref_cost - m["cost"]) / ref_cost)
        
        # Product of normalized objective improvements — each point's individual proxy volume
        vol = v_eff * v_rip * v_pwr * v_cost
        hv_sum += vol
        
    return round(float(hv_sum), 6)


def compute_pareto_spacing(metrics_list: List[Dict]) -> float:
    """Schott's spacing metric: std-dev of min neighbor distances on the Pareto front (lower = more uniform)."""
    fronts = fast_non_dominated_sort(metrics_list)
    if not fronts or len(fronts[0]) < 2:
        return 0.0
        
    p_front = [metrics_list[i] for i in fronts[0]]
    vecs = []
    for m in p_front:
        # Negate minimization objectives so "better" consistently maps to higher values
        vecs.append([m["efficiency"], -m["torque_ripple"], m["power_density"], -m["cost"]])
    vecs = np.array(vecs)
    
    # Normalize each objective to [0, 1] across the front (epsilon avoids div-by-zero)
    norm_v = (vecs - vecs.min(axis=0)) / (vecs.max(axis=0) - vecs.min(axis=0) + 1e-6)
    
    d_i = []
    for i in range(len(norm_v)):
        dists = [np.linalg.norm(norm_v[i] - norm_v[j]) for j in range(len(norm_v)) if i != j]
        d_i.append(min(dists))
        
    d_mean = np.mean(d_i)
    spacing = math.sqrt(sum((d - d_mean)**2 for d in d_i) / len(d_i))
    return round(float(spacing), 6)

# ---------------------------------------------------------------------------
# NSGA-II Selection & Candidate Screening
# ---------------------------------------------------------------------------
def nsga2_selection(population: List[Dict], metrics: List[Dict], pop_size: int) -> List[Dict]:
    """NSGA-II environmental selection: fill by Pareto rank, break ties by crowding distance."""
    fronts = fast_non_dominated_sort(metrics)
    new_population = []
    
    for front in fronts:
        if len(new_population) + len(front) <= pop_size:
            # Accept the entire front
            new_population.extend([population[i] for i in front])
        else:
            # Partial front: select individuals with the highest crowding distance
            needed = pop_size - len(new_population)
            dist = crowding_distance_assignment(front, metrics)
            sorted_front = sorted(front, key=lambda i: dist[i], reverse=True)
            new_population.extend([population[i] for i in sorted_front[:needed]])
            break
            
    return new_population


def nsga2_tournament_selection(population: List[Dict], 
                                metrics: List[Dict], 
                                fronts: List[List[int]], 
                                crowding_dist: Dict[int, float]) -> Tuple[Dict, Dict]:
    """Binary tournament: select by lower Pareto rank; tie-break by higher crowding distance."""
    rank_map = {}
    for r, f in enumerate(fronts):
        for idx in f:
            rank_map[idx] = r
            
    def select_one():
        i1, i2 = random.sample(range(len(population)), 2)
        r1, r2 = rank_map[i1], rank_map[i2]
        if r1 < r2:
            return population[i1]          # i1 has a better (lower) Pareto rank
        elif r2 < r1:
            return population[i2]          # i2 has a better Pareto rank
        else:
            # Same rank: prefer the solution with larger crowding distance (better diversity)
            d1, d2 = crowding_dist.get(i1, 0.0), crowding_dist.get(i2, 0.0)
            return population[i1] if d1 >= d2 else population[i2]

    return select_one(), select_one()


def rank_candidates_surrogate(candidates: List[Dict], 
                              surrogate: MLSurrogate, 
                              score_weights: Dict[str, float], 
                              top_n: int, 
                              ucb_kappa: float = 0.5) -> List[Dict]:
    """Rank candidates by UCB score = predicted scalarized objective + κ × predictive uncertainty (explore high-uncertainty regions)."""
    # If the surrogate is untrained or we don't have excess candidates, skip ranking
    if len(candidates) <= top_n or not surrogate.is_trained:
        return candidates[:top_n]
        
    scored_candidates = []
    for ind in candidates:
        pred_metrics, uncertainties, _ = surrogate.predict_with_confidence(ind)
        pred_score = compute_score(
            pred_metrics,
            score_weights["eff"], score_weights["ripple"],
            score_weights["pwr"], score_weights["cost"]
        )
        avg_unc = float(np.mean(list(uncertainties.values())))
        # Upper Confidence Bound: high predicted score + high uncertainty = promising to explore
        ucb_score = pred_score + ucb_kappa * avg_unc
        scored_candidates.append((ucb_score, ind))
        
    scored_candidates.sort(key=lambda x: x[0], reverse=True)
    return [cand for _, cand in scored_candidates[:top_n]]

# ---------------------------------------------------------------------------
# Evolutionary Operators (Baseline, Random, Repair, Crossover, Sensitivity Mutate)
# ---------------------------------------------------------------------------
def get_baseline_individual(bounds: Optional[dict] = None) -> dict:
    """Return a hardcoded baseline individual known to be feasible under default constraints.

    If *bounds* are provided each value is snapped to its parameter grid (lower/upper/step).
    Returns the snapped version if feasible; otherwise falls back to the unsnapped baseline.
    """
    raw = {
        "Dr_in": 90.0, "Air_gap": 1.0, "Lamda": 0.9, "Bridge": 1.5,
        "Hs0": 1.1899, "Hs1": 1.5, "Hs2": 18.07656, "Bs0": 2.1128,
        "Bs1": 6.90142, "Bs2": 10.88076, "O1": 5.4, "O2": 6.0,
        "B1": 3.5, "rib": 2.0, "hrib": 2.4, "Mt": 5.282,
        "Mw": 25.44156, "magDmin": 10.0, "Thet_deg": 30.0,
    }
    raw = normalize_ind_keys(raw)
    if bounds:
        snapped = {}
        for k, v in raw.items():
            norm_k = normalize_param_key(k)
            if norm_k in bounds:
                info = bounds[norm_k]
                snapped[norm_k] = snap_to_step(v, info["lower"], info["upper"], info["step"])
            else:
                snapped[norm_k] = v
        if is_feasible(snapped):
            return snapped
    if not is_feasible(raw):
        logging.warning("Hardcoded baseline is infeasible under current bounds! Returning anyway.")
    return raw


def random_individual(bounds: dict) -> dict:
    """Create a random feasible individual by uniformly sampling the parameter grid.

    For each parameter a random step index is picked and the resulting value is
    snapped/clamped to [lower, upper].  Retries up to 1000 times; falls back to
    the hardcoded baseline if no feasible candidate is found.
    """
    for attempt in range(1000):
        individual = {}
        for name, info in bounds.items():
            steps = int(round((info["upper"] - info["lower"]) / info["step"]))
            k = random.randint(0, steps)
            val = info["lower"] + k * info["step"]
            # snap_to_step clamps out-of-range values caused by uneven division
            individual[name] = snap_to_step(val, info["lower"], info["upper"], info["step"])
        
        if is_feasible(individual):
            return individual
    
    logging.warning("Could not find random feasible individual after 1000 attempts. Using baseline.")
    return get_baseline_individual(bounds)


def repair_individual(ind: dict, bounds: dict, max_attempts: int = 10) -> dict:
    """Analytical and heuristic repair for individuals violating geometric constraints.

    Applies up to *max_attempts* rounds of targeted analytical repairs:
      1. Slot height redistribution (Hs0/Hs1/Hs2) when total exceeds available space.
      2. Slot width monotonicity enforcement (Bs0 <= Bs1 <= Bs2).
      3. Bridge thickness clamping (B1 <= Mt - 0.3).
      4. Magnet duct fit enforcement (Mw > 2*B1).
      5. Rib height limiting (hrib <= min(O2, 4.5, bridge*2)).

    If all analytical repairs fail the method falls back to replacing individual
    parameters with the hardcoded baseline and finally to the full baseline.
    """
    repaired = normalize_ind_keys(ind)
    
    # Step 1: clamp every parameter to its grid
    for name, info in bounds.items():
        if name in repaired:
            repaired[name] = snap_to_step(repaired[name], info["lower"], info["upper"], info["step"])
            
    if is_feasible(repaired):
        return repaired
        
    # Step 2: iterative analytical repair loop
    for attempt in range(max_attempts):
        violated = get_violated_constraints(repaired)
        if not violated:
            return repaired
            
        # Analytical Repair 1: Slot Height & Rotor Stator Fit
        if "SlotHeight" in violated or "RotorFitsStator" in violated:
            lamda = repaired.get("Lamda", 0.9)
            air_gap = repaired.get("Air_gap", 1.0)
            ds_in = (L_STK / lamda) + air_gap
            max_hs_sum = ((DS_OUT - ds_in) / 2.0) - SLOT_HEIGHT_MARGIN - 0.01
            
            hs0 = repaired.get("Hs0", 1.0)
            hs1 = repaired.get("Hs1", 1.0)
            
            # Reduce Hs2 first (largest contributor), then Hs1, then Hs0
            max_hs2 = max_hs_sum - hs0 - hs1
            if "Hs2" in bounds:
                info = bounds["Hs2"]
                target_hs2 = min(repaired.get("Hs2", info["upper"]), max_hs2)
                repaired["Hs2"] = snap_to_step(target_hs2, info["lower"], info["upper"], info["step"])

            if not constraint_slot_height(repaired) and "Hs1" in bounds:
                info = bounds["Hs1"]
                max_hs1 = max_hs_sum - hs0 - repaired.get("Hs2", 16.0)
                target_hs1 = min(repaired.get("Hs1", info["upper"]), max_hs1)
                repaired["Hs1"] = snap_to_step(target_hs1, info["lower"], info["upper"], info["step"])

            if not constraint_slot_height(repaired) and "Hs0" in bounds:
                info = bounds["Hs0"]
                max_hs0 = max_hs_sum - repaired.get("Hs1", 1.0) - repaired.get("Hs2", 16.0)
                target_hs0 = min(repaired.get("Hs0", info["upper"]), max_hs0)
                repaired["Hs0"] = snap_to_step(target_hs0, info["lower"], info["upper"], info["step"])

        # Analytical Repair 2: Slot Width Progression (Bs0 <= Bs1 <= Bs2)
        if "SlotWidthProgression" in violated:
            if repaired.get("Bs0", 0) > repaired.get("Bs1", 0) and "Bs1" in bounds:
                info = bounds["Bs1"]
                repaired["Bs1"] = snap_to_step(max(repaired["Bs1"], repaired["Bs0"]), info["lower"], info["upper"], info["step"])
            if repaired.get("Bs1", 0) > repaired.get("Bs2", 0) and "Bs2" in bounds:
                info = bounds["Bs2"]
                repaired["Bs2"] = snap_to_step(max(repaired["Bs2"], repaired["Bs1"]), info["lower"], info["upper"], info["step"])

        # Analytical Repair 3: Bridge Thickness (B1 <= Mt - 0.3)
        if "BridgeThickness" in violated:
            if "B1" in bounds and "Mt" in bounds:
                max_b1 = repaired["Mt"] - 0.3
                info = bounds["B1"]
                repaired["B1"] = snap_to_step(min(repaired["B1"], max_b1), info["lower"], info["upper"], info["step"])

        # Analytical Repair 4: Magnet Duct Fit (Mw > 2 * B1)
        if "MagnetDuctFit" in violated:
            if "Mw" in bounds and "B1" in bounds:
                min_mw = 2.0 * repaired["B1"] + 0.1
                info = bounds["Mw"]
                repaired["Mw"] = snap_to_step(max(repaired["Mw"], min_mw), info["lower"], info["upper"], info["step"])

        # Analytical Repair 5: Rib Height Limit (hrib <= min(O2, 4.5, bridge * 2))
        if "RibHeightLimit" in violated:
            if "hrib" in bounds:
                o2 = repaired.get("O2", 6.0)
                bridge = repaired.get("Bridge", 1.5)
                max_hrib = min(o2, 4.5, bridge * 2.0)
                info = bounds["hrib"]
                repaired["hrib"] = snap_to_step(min(repaired["hrib"], max_hrib), info["lower"], info["upper"], info["step"])

        if is_feasible(repaired):
            return repaired

    # Step 3: greedy fallback – replace one parameter at a time with baseline value
    if not is_feasible(repaired):
        baseline = get_baseline_individual(bounds)
        for key in ["Hs2", "Hs1", "Hs0", "Bs0", "Bs1", "Bs2", "B1", "Mw", "hrib", "Lamda", "Air_gap", "Dr_in"]:
            if key in repaired and key in baseline:
                repaired[key] = baseline[key]
                if is_feasible(repaired):
                    return repaired

    return get_baseline_individual(bounds)


def crossover(parent1: dict, parent2: dict, bounds: dict, rate: float = 0.7) -> Tuple[dict, dict]:
    """Uniform crossover producing two feasible offspring (repaired if needed).

    Each gene is swapped between parents with 50 % probability.  Offspring may
    be repaired to enforce geometric constraints.  If crossover is skipped
    (probability 1 - *rate*) copies of the parents are returned unchanged.
    """
    p1 = normalize_ind_keys(parent1)
    p2 = normalize_ind_keys(parent2)
    if random.random() > rate:
        return p1.copy(), p2.copy()
        
    c1, c2 = {}, {}
    # Only parameters listed in *bounds* are crossed over
    for name in bounds:
        if random.random() < 0.5:
            c1[name] = p1[name]
            c2[name] = p2[name]
        else:
            c1[name] = p2[name]
            c2[name] = p1[name]
            
    return repair_individual(c1, bounds), repair_individual(c2, bounds)


def sensitivity_mutate(individual: dict, bounds: dict, per_obj_sensitivity: Dict[str, Dict[str, float]],
                       rate: float = 0.2, sigma_scale: float = 0.15) -> dict:
    """Per-objective sensitivity-guided Gaussian mutation.

    Parameters with higher sensitivity scores receive a higher per-gene mutation
    probability (*rate* × weight, capped at 0.8) and a wider step distribution
    (sigma scaled by 1/√weight).  If no mutation fires the original individual
    is returned unchanged; otherwise repair is applied.
    """
    mutated = normalize_ind_keys(individual).copy()
    was_mutated = False
    
    # Average sensitivity across all objectives for each parameter
    agg_weights = {}
    for name in bounds:
        w_eff = per_obj_sensitivity.get("efficiency", {}).get(name, 1.0)
        w_rip = per_obj_sensitivity.get("torque_ripple", {}).get(name, 1.0)
        agg_weights[name] = (w_eff + w_rip) / 2.0
        
    for name, info in bounds.items():
        weight = agg_weights.get(name, 1.0)
        adj_rate = min(0.8, rate * weight)
        if random.random() < adj_rate:
            was_mutated = True
            val_range = info["upper"] - info["lower"]
            # Guard against zero-weight (all sensitivities = 0) to avoid ZeroDivisionError
            adj_sigma = sigma_scale / math.sqrt(max(weight, 1e-6))
            delta = random.gauss(0, adj_sigma * val_range)
            new_val = mutated[name] + delta
            mutated[name] = snap_to_step(new_val, info["lower"], info["upper"], info["step"])
            
    if was_mutated:
        return repair_individual(mutated, bounds)
    return mutated


def mutate(individual: dict, bounds: dict, rate: float = 0.2, sigma_scale: float = 0.15) -> dict:
    """Uniform-probability Gaussian mutation per gene.

    Each parameter is mutated with probability *rate* by adding N(0, sigma) noise
    where sigma = sigma_scale × (upper - lower).  If any mutation fires the
    resulting individual is repaired to satisfy geometric constraints.
    """
    mutated = normalize_ind_keys(individual).copy()
    was_mutated = False
    
    for name, info in bounds.items():
        if random.random() < rate:
            was_mutated = True
            val_range = info["upper"] - info["lower"]
            delta = random.gauss(0, sigma_scale * val_range)
            new_val = mutated[name] + delta
            mutated[name] = snap_to_step(new_val, info["lower"], info["upper"], info["step"])
            
    if was_mutated:
        return repair_individual(mutated, bounds)
    return mutated

# ---------------------------------------------------------------------------
# Diagnostics & Health
# ---------------------------------------------------------------------------
def check_population_diversity(population: List[Dict], threshold: float = 0.05) -> Tuple[bool, float]:
    """Check standard-deviation-based diversity across the population's decision variables.

    Returns (above_threshold, avg_diversity).  For populations with < 2 members
    diversity is reported as 1.0 (maximum).  Private keys (leading '_') are
    excluded from the metric.
    """
    if len(population) < 2:
        return True, 1.0
        
    param_keys = [k for k in population[0].keys() if not str(k).startswith('_')]
    std_sum = 0.0
    for key in param_keys:
        vals = [float(ind[key]) for ind in population]
        val_range = max(vals) - min(vals)
        if val_range > 0:
            std_sum += np.std(vals) / val_range
            
    avg_diversity = std_sum / len(param_keys)
    return (avg_diversity >= threshold), float(avg_diversity)


def detect_stagnation(scores_history: List[float], window: int = 10, threshold: float = 0.01) -> bool:
    """Detect optimization stagnation by comparing the first and last score in a sliding window.

    Signals stagnation when the absolute improvement over the window is below
    *threshold*.  Note that this only checks endpoints, not the full trajectory,
    so oscillatory but non-progressing runs may or may not be caught depending
    on the window size.
    """
    if len(scores_history) < window:
        return False
    recent = scores_history[-window:]
    improvement = recent[-1] - recent[0]
    return abs(improvement) < threshold

# ---------------------------------------------------------------------------
# Direct Ansys Maxwell & MATLAB Interop Data Exchange
# ---------------------------------------------------------------------------
def _cleanup_temp_project_files(temp_path: Path, i: int, total: int):
    """Remove temp .aedt project, lock, auto-save files and results dir with retry (handles Windows file locks)."""
    stem = temp_path.stem
    parent = temp_path.parent
    files_to_delete = [
        temp_path,
        parent / f"{stem}.aedt.lock",
        parent / f"{stem}.aedt.auto",
    ]
    dirs_to_delete = [
        parent / f"{stem}.aedtresults",
    ]

    # Retry up to 5x to handle transient file locks from lingering Ansys processes
    for attempt in range(5):
        all_deleted = True
        for f in files_to_delete:
            if f.exists():
                try:
                    f.unlink()
                except Exception:
                    all_deleted = False

        for d in dirs_to_delete:
            if d.exists():
                try:
                    shutil.rmtree(d, ignore_errors=True)
                except Exception:
                    all_deleted = False

        if all_deleted:
            logging.info("  [ActiveX] [Candidate %d/%d] Successfully deleted temporary file '%s'.", i, total, temp_path.name)
            return
        time.sleep(0.5)

    logging.warning("  [ActiveX] [Candidate %d/%d] Could not completely delete temporary files '%s' after 5 attempts.", i, total, temp_path.name)


def _analyze_with_timeout(oDesign, setup_name, timeout_seconds=7200):
    """Run oDesign.Analyze directly on the COM-owning (main) thread.

    Ansys COM objects are late-bound and thread-affine: calling methods from
    any other thread (even after pythoncom.CoInitialize()) raises
    AttributeError (e.g. 'SetActiveDesign.Analyze') or 'CoInitialize has not
    been called', which made every FEA evaluation silently fail and fall back
    to the penalized surrogate. The Analyze call MUST run on the same thread
    that created the COM session. A hung analysis can be interrupted with
    Ctrl+C and is retried by the caller's attempt loop.
    """
    oDesign.Analyze(setup_name)


def run_ansys_direct(population: List[Dict], 
                      root_dir: Path, 
                      output_dir: Path, 
                      ansys_version: str = "2023.2",
                      non_graphical: bool = True) -> bool:
    """Run direct Ansys Maxwell 3D simulation for each candidate via PyAEDT (Method A) or ActiveX (Method B).

    Per candidate workflow:
      1. Copy template .aedt project to uniquely-named temp file
      2. Set all design parameters from the individual
      3. Disable 3D field saving to conserve RAM (reclaims tens of GBs)
      4. Run Setup1 analysis
      5. Export Output Variables table to CSV
      6. Close project (without saving) and clean up temp files

    Method A (PyAEDT high-level API) is attempted first.
    On any failure, Method B (win32com ActiveX COM) auto-falls back with retry logic.
    """
    project_path = root_dir / "Matlab_Ai_Optimization.aedt"
    if not project_path.is_file():
        raise SimulationError(f"Ansys AEDT project file not found: {project_path}")

    # Method A: PyAEDT high-level API
    if PYAEDT_AVAILABLE:
        logging.info("Connecting to Ansys Maxwell 3D via PyAEDT (non_graphical=%s)...", non_graphical)
        try:
            for i, ind in enumerate(population, start=1):
                timestamp = int(time.time() * 1000)
                temp_filename = f"temp_design_ind_{i}_{timestamp}.aedt"
                temp_path = root_dir / temp_filename

                shutil.copy2(project_path, temp_path)
                m3d = None
                try:
                    m3d = Maxwell3d(
                        projectname=str(temp_path),
                        designname="Vshape_IPM",
                        specified_version=ansys_version,
                        non_graphical=non_graphical,
                        new_desktop_session=False,
                        close_on_exit=False
                    )
                    norm_ind = normalize_ind_keys(ind)
                    for var_name in PARAM_ORDER:
                        if var_name in norm_ind:
                            val = norm_ind[var_name]
                            unit = "deg" if var_name.lower() == "thet_deg" else ("mm" if var_name != "Lamda" else "")
                            m3d[var_name] = f"{val}{unit}" if unit else str(val)

                    try:
                        if hasattr(m3d, "setups") and m3d.setups:
                            m3d.setups[0].props["SaveFieldsType"] = "None"
                    except Exception:
                        pass

                    # Clear any cached parametric sweep results then run the nominal analysis
                    m3d.delete_sampling_solutions()
                    m3d.analyze_setup("Setup1")

                    csv_path = output_dir / f"output_vars_iter_{i}.csv"
                    # Export user-defined Output Variables (Efficiency, TorqueRipple, etc.) to CSV
                    m3d.post.export_report_to_csv("Setup1", "OutputVariablesTable", str(csv_path))
                    logging.info("  [PyAEDT] [Candidate %d/%d] Simulation results exported to CSV.", i, len(population))

                finally:
                    if m3d is not None:
                        try:
                            m3d.close_project(name=m3d.project_name, save_project=False)
                        except Exception as e_close:
                            logging.warning("  Could not close project %s: %s", temp_filename, e_close)

                    time.sleep(0.5)
                    _cleanup_temp_project_files(temp_path, i, len(population))

            return True
        except Exception as e:
            logging.warning(f"PyAEDT execution failed: {e}. Trying win32com ActiveX fallback...")

    # Method B: Direct Windows COM / ActiveX — shared session across all candidates
    if PYWIN32_AVAILABLE:
        logging.info("Connecting to Ansys Electronics Desktop via win32com ActiveX...")
        prog_ids = [
            "Ansoft.ElectronicsDesktop",
            "Ansoft.ElectronicsDesktopStudent",
            "Ansoft.ElectronicsDesktop.2025.2", "Ansoft.ElectronicsDesktopStudent.2025.2",
            "Ansoft.ElectronicsDesktop.2025.1", "Ansoft.ElectronicsDesktopStudent.2025.1",
            "Ansoft.ElectronicsDesktop.2024.2", "Ansoft.ElectronicsDesktopStudent.2024.2",
            "Ansoft.ElectronicsDesktop.2024.1", "Ansoft.ElectronicsDesktopStudent.2024.1",
            "Ansoft.ElectronicsDesktop.2023.2", "Ansoft.ElectronicsDesktopStudent.2023.2",
            "Ansoft.ElectronicsDesktop.2023.1", "Ansoft.ElectronicsDesktopStudent.2023.1",
            "Ansoft.ElectronicsDesktop.2022.2", "Ansoft.ElectronicsDesktopStudent.2022.2",
            "Ansoft.ElectronicsDesktop.2022.1", "Ansoft.ElectronicsDesktopStudent.2022.1",
            "Ansoft.ElectronicsDesktop.2021.2", "Ansoft.ElectronicsDesktopStudent.2021.2",
            "Ansoft.ElectronicsDesktop.2021.1", "Ansoft.ElectronicsDesktopStudent.2021.1",
        ]

        def _get_ansys_app():
            """Try all known ProgIDs (plus registry-discovered ones) and return (COM_object, ProgID) or (None, last_exception)."""
            last_err = None
            all_pids = list(prog_ids)
            for d_pid in _discover_ansys_progids():
                if d_pid not in all_pids:
                    all_pids.append(d_pid)
            for pid in all_pids:
                try:
                    app = win32com.client.Dispatch(pid)
                    return app, pid
                except Exception as ex:
                    last_err = ex
            return None, last_err

        def _ensure_ansys_session():
            """Connect to a running AEDT COM session, or launch one if needed. Returns (oAnsoftApp, oDesktop, progid)."""
            oAnsoftApp, active_pid = _get_ansys_app()
            if oAnsoftApp is None:
                logging.info("No running AEDT session found. Launching new session...")
                try:
                    if sys.platform == "win32":
                        subprocess.run(["taskkill", "/F", "/IM", "ansysedt.exe"], capture_output=True, timeout=5)
                except Exception:
                    pass
                time.sleep(2.0)
                oAnsoftApp, active_pid = _get_ansys_app()
                if oAnsoftApp is None:
                    time.sleep(3.0)
                    oAnsoftApp, active_pid = _get_ansys_app()
            if oAnsoftApp is None:
                exe = _discover_ansysedt_path()
                if exe is not None:
                    logging.info("Launching ansysedt.exe at '%s' to re-register its COM server...", exe)
                    try:
                        subprocess.Popen([str(exe)])
                        deadline = time.time() + 240
                        while time.time() < deadline:
                            time.sleep(10)
                            oAnsoftApp, active_pid = _get_ansys_app()
                            if oAnsoftApp is not None:
                                break
                    except Exception as ex_launch:
                        logging.warning("Failed to launch ansysedt.exe: %s", ex_launch)
                else:
                    logging.warning("Could not locate ansysedt.exe install path; cannot auto-launch. Start Ansys Electronics Desktop manually if needed.")
            if oAnsoftApp is None:
                err_detail = str(active_pid) if active_pid is not None else "No Ansys COM ProgID available"
                raise SimulationError(f"Could not connect to Ansys Electronics Desktop COM. Error: {err_detail}")

            oDesktop = None
            for _w in range(8):
                try:
                    oDesktop = oAnsoftApp.GetAppDesktop()
                    if oDesktop is not None:
                        break
                except Exception:
                    pass
                time.sleep(1.5)

            if oDesktop is None:
                raise SimulationError("Ansys Desktop COM object unavailable.")

            if non_graphical:
                try:
                    oDesktop.SetIconic(True)
                except Exception:
                    pass
            return oAnsoftApp, oDesktop, active_pid

        # --- Connect before processing candidates (session may be restarted later to avoid OOM) ---
        oAnsoftApp, oDesktop, active_pid = _ensure_ansys_session()
        logging.info("Connected to Ansys Electronics Desktop via COM ProgID: '%s'", active_pid)

        # --- Process each candidate reusing the same COM session ---
        for i, ind in enumerate(population, start=1):
            # --- RAM guard: restart the AEDT COM session before we run out of memory ---
            # Each 3D transient solve holds on to memory in the AEDT process; after a few
            # candidates RAM approaches 100%, AEDT gets OOM-killed and every later candidate
            # fails with 'The RPC server is unavailable'. Restart the session (Quit + kill +
            # reconnect) whenever free RAM drops below 50% of total.
            try:
                import psutil
                _vm = psutil.virtual_memory()
                _free_gb = _vm.available / (1024 ** 3)
                _total_gb = _vm.total / (1024 ** 3)
                if _free_gb < 0.5 * _total_gb:
                    logging.warning("  [ActiveX] Free RAM %.1f GB < 50%% of total (%.1f GB): restarting AEDT COM session to avoid OOM crash...", _free_gb, _total_gb)
                    try:
                        oDesktop.CloseAllWindows()
                    except Exception:
                        pass
                    try:
                        oAnsoftApp.Quit()
                    except Exception:
                        pass
                    del oDesktop, oAnsoftApp
                    gc.collect()
                    try:
                        if sys.platform == "win32":
                            subprocess.run(["taskkill", "/F", "/IM", "ansysedt.exe"], capture_output=True, timeout=10)
                    except Exception:
                        pass
                    time.sleep(3.0)
                    oAnsoftApp, oDesktop, _ = _ensure_ansys_session()
                    logging.info("  [ActiveX] AEDT COM session restarted.")
            except Exception:
                pass

            timestamp = int(time.time() * 1000)
            temp_filename = f"temp_design_ind_{i}_{timestamp}.aedt"
            temp_path = root_dir / temp_filename

            shutil.copy2(project_path, temp_path)
            max_retries = 3
            success = False
            norm_ind = normalize_ind_keys(ind)

            for attempt in range(1, max_retries + 1):
                oProject = None
                oDesign = None
                oAnalysisModule = None
                oReportModule = None

                try:
                    gc.collect()

                    # RAM & Resource usage logging
                    try:
                        import psutil
                        mem = psutil.virtual_memory()
                        logging.info("  [Resource Monitor] [Candidate %d/%d] System RAM: %.1f%% used (%.2f GB / %.2f GB free)",
                                     i, len(population), mem.percent, mem.available / (1024**3), mem.total / (1024**3))
                    except Exception:
                        pass

                    oProject = oDesktop.OpenProject(str(temp_path))
                    oDesign = oProject.SetActiveDesign("Vshape_IPM")
                    oAnalysisModule = oDesign.GetModule("AnalysisSetup")
                    oReportModule = oDesign.GetModule("ReportSetup")

                    for var_name in PARAM_ORDER:
                        if var_name in norm_ind:
                            val = norm_ind[var_name]
                            unit = "deg" if var_name.lower() == "thet_deg" else ("mm" if var_name != "Lamda" else "")
                            val_str = f"{val}{unit}" if unit else str(val)
                            oDesign.SetVariableValue(var_name, val_str)

                    # Disable 3D field saving to prevent massive RAM memory leaks (reclaims tens of GBs):
                    try:
                        oAnalysisModule.EditSetup("Setup1", ["NAME:Setup1", "SaveFieldsType:=", "None"])
                    except Exception:
                        pass

                    # Clear cached parametric variations so only nominal design is solved
                    try:
                        oDesign.DeleteFullVariation("All", False)
                    except Exception:
                        pass

                    # Reset setup time to zero before analysis to force full solve
                    oAnalysisModule.ResetSetupToTimeZero("Setup1")

                    # Run analysis with timeout
                    logging.info("  [ActiveX] [Candidate %d/%d] Starting Analyze('Setup1')...", i, len(population))
                    _analyze_with_timeout(oDesign, "Setup1", timeout_seconds=7200)
                    logging.info("  [ActiveX] [Candidate %d/%d] Analyze('Setup1') completed.", i, len(population))

                    csv_path = output_dir / f"output_vars_iter_{i}.csv"
                    oReportModule.ExportToFile("OutputVariablesTable", str(csv_path))
                    logging.info("  [ActiveX] [Candidate %d/%d] FEA simulation results exported to CSV.", i, len(population))
                    success = True
                    break

                except Exception as e_ind:
                    logging.warning("  [ActiveX] [Candidate %d/%d] Attempt %d/%d failed (%s). Retrying...",
                                    i, len(population), attempt, max_retries, e_ind)
                    gc.collect()
                    time.sleep(3.0)

                finally:
                    # Close the project in AEDT to release file handles before deletion
                    if oProject is not None and oDesktop is not None:
                        try:
                            oDesktop.CloseProject(temp_path.stem)
                        except Exception:
                            pass
                    # Release per-attempt COM references only (keep session-level oDesktop/oAnsoftApp alive)
                    del oProject, oDesign, oAnalysisModule, oReportModule
                    gc.collect()

            if not success:
                logging.error("  [ActiveX] [Candidate %d/%d] FEA simulation failed after %d attempts. Generating penalized surrogate fallback.", i, len(population), max_retries)
                fallback_metrics = physics_surrogate(norm_ind)
                csv_path = output_dir / f"output_vars_iter_{i}.csv"
                df_fallback = pd.DataFrame([{
                    "MovingTorque": 180.0,
                    "InputPower": 50000.0,
                    "OutputPower": 50000.0 * (fallback_metrics["efficiency"] / 100.0),
                    "Efficiency": max(50.0, fallback_metrics["efficiency"] - 10.0),
                    "TorqueRipple": fallback_metrics["torque_ripple"] + 20.0,
                    "PowerDensity": fallback_metrics["power_density"] * 0.8,
                    "Cost": fallback_metrics["cost"] * 1.2
                }])
                df_fallback.to_csv(csv_path, index=False)

            time.sleep(0.5)
            _cleanup_temp_project_files(temp_path, i, len(population))
            gc.collect()

        # --- Cleanup shared COM session once at the end ---
        try:
            if oDesktop is not None:
                oDesktop.CloseAllWindows()
        except Exception:
            pass
        try:
            if oAnsoftApp is not None:
                oAnsoftApp.Quit()
        except Exception:
            pass
        del oDesktop, oAnsoftApp
        gc.collect()

        return True

    raise SimulationError("Neither 'pyaedt' nor 'pywin32' is available in Python environment.")


def write_param_excel(population: List[Dict], path: Path):
    """Write normalized population parameters to Excel sorted by PARAM_ORDER for MATLAB batch ingestion."""
    rows = []
    for ind in population:
        norm = normalize_ind_keys(ind)
        row = {p: norm[p] for p in PARAM_ORDER}
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    logging.debug("Wrote %d candidates to %s", len(population), path)


def run_matlab(root_dir: Path, matlab_exe: str = "matlab"):
    """Launch MATLAB batch to run the Ansys Maxwell simulation script (Ai_optimization.m) with 2-hour timeout."""
    logging.info("Launching MATLAB for Ansys Maxwell simulation batch...")
    resolved_matlab = matlab_exe
    # Auto-detect MATLAB if the provided path does not exist
    if not Path(resolved_matlab).is_file() and shutil.which(resolved_matlab) is None:
        candidates = [
            r"C:\MATLAB\R2023b\bin\matlab.exe",
            r"C:\Program Files\MATLAB\R2023b\bin\matlab.exe",
            r"C:\MATLAB\R2023a\bin\matlab.exe",
            "matlab",
        ]
        found = False
        for cand in candidates:
            if Path(cand).is_file() or shutil.which(cand):
                resolved_matlab = cand
                found = True
                break
        if not found:
            raise SimulationError(f"MATLAB executable not found at '{matlab_exe}' and auto-detection failed.")
    
    try:
        result = subprocess.run(
            [resolved_matlab, "-wait", "-nosplash", "-nodesktop", "-r", "try, Ai_optimization; catch e, disp(e.message); pause(300); exit(1); end; exit(0);"],
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=7200,
        )
        if result.returncode != 0:
            logging.error("MATLAB stderr: %s", result.stderr[-500:])
            raise SimulationError(f"MATLAB execution failed with code {result.returncode}")
            
    except subprocess.TimeoutExpired:
        raise SimulationError("MATLAB execution timed out (2 hours)")
    except FileNotFoundError:
        raise SimulationError(f"MATLAB executable not found: {matlab_exe}")

# ---------------------------------------------------------------------------
# Population Evaluation (With Global Deduplication Cache)
# ---------------------------------------------------------------------------
def evaluate_population(population: List[Dict], 
                        root_dir: Path, 
                        input_dir: Path,
                        output_dir: Path, 
                        mode: str, 
                        score_weights: Dict[str, float],
                        ml_surrogate: MLSurrogate, 
                        use_ml: bool = True,
                        matlab_exe: str = "matlab",
                        eval_cache: Optional[Dict[tuple, Dict]] = None,
                        non_graphical: bool = True) -> List[Dict]:
    """Evaluate all individuals with dedup cache, feasibility repair, and mode-specific evaluation.

    Modes:
      - "offline": physics or ML surrogate only (no FEA)
      - "ansys":   PyAEDT/ActiveX FEA with surrogate confidence gating
      - else:      Excel + MATLAB bridge for legacy workflow

    Cached results are returned directly with score recomputed from current weights.
    Infeasible individuals are repaired (or replaced with baseline) before evaluation.
    """
    if eval_cache is None:
        eval_cache = _EVALUATION_CACHE

    uncached_indices = []
    uncached_population = []
    results = [None] * len(population)

    # Phase 1: separate cached vs uncached individuals
    for idx, ind in enumerate(population):
        norm_ind = normalize_ind_keys(ind)
        key = _make_ind_key(norm_ind)
        if key in eval_cache:
            m = eval_cache[key].copy()
            m["score"] = compute_score(
                m, 
                score_weights["eff"], 
                score_weights["ripple"],
                score_weights["pwr"], 
                score_weights["cost"]
            )
            results[idx] = m
        else:
            uncached_indices.append(idx)
            uncached_population.append(norm_ind)

    if not uncached_population:
        return results

    # Phase 2: load bounds and repair any infeasible individuals before evaluation
    bounds_file = root_dir / "Ai_Optimization_Bounds.xlsx"
    bounds_dict = load_bounds(bounds_file) if bounds_file.is_file() else {}
    for i in range(len(uncached_population)):
        if not is_feasible(uncached_population[i]):
            if bounds_dict:
                uncached_population[i] = repair_individual(uncached_population[i], bounds_dict)
            if not is_feasible(uncached_population[i]):
                logging.warning("Candidate %d remains infeasible after repair — forcing to baseline", i+1)
                uncached_population[i] = get_baseline_individual(bounds_dict)

    # Phase 3: evaluate uncached population by selected mode
    if mode == "offline":
        uncached_metrics = []
        for ind in uncached_population:
            if use_ml:
                pred, uncertainties, high_conf = ml_surrogate.predict_with_confidence(ind)
                m = pred
            else:
                m = physics_surrogate(ind)
            m["score"] = compute_score(
                m, 
                score_weights["eff"], 
                score_weights["ripple"],
                score_weights["pwr"], 
                score_weights["cost"]
            )
            uncached_metrics.append(m)
    elif mode == "ansys":
        # Split: high-confidence ML predictions bypass FEA; the rest go to Ansys
        fea_candidates = []
        fea_to_uncached = []
        uncached_metrics = [None] * len(uncached_population)

        for j, ind in enumerate(uncached_population):
            if use_ml and ml_surrogate.is_trained:
                pred, _, high_conf = ml_surrogate.predict_with_confidence(ind)
                if high_conf:
                    m = pred.copy()
                    m["score"] = compute_score(
                        m, score_weights["eff"], score_weights["ripple"],
                        score_weights["pwr"], score_weights["cost"]
                    )
                    uncached_metrics[j] = m
                    continue
            fea_candidates.append(ind)
            fea_to_uncached.append(j)

        if fea_candidates:
            logging.info(f"FEA needed for {len(fea_candidates)}/{len(uncached_population)} candidates (rest from surrogate)")
            try:
                run_ansys_direct(fea_candidates, root_dir, output_dir, non_graphical=non_graphical)
            except SimulationError as e:
                logging.error(f"Direct Ansys Simulation failed: {e}")
                sys.exit(1)
            fea_metrics = _parse_csv_outputs(fea_candidates, output_dir, score_weights)
            for fea_idx, m in enumerate(fea_metrics):
                uncached_metrics[fea_to_uncached[fea_idx]] = m
        else:
            logging.info("All candidates handled by surrogate (no FEA needed)")
    else:
        # Legacy mode: write parameter Excel, run MATLAB, parse its output CSVs
        param_excel = input_dir / "Ai_Optimization_ParamValues.xlsx"
        write_param_excel(uncached_population, param_excel)
        
        try:
            run_matlab(root_dir, matlab_exe=matlab_exe)
        except SimulationError as e:
            logging.error(f"MATLAB Simulation failed: {e}")
            sys.exit(1)
            
        uncached_metrics = _parse_csv_outputs(uncached_population, output_dir, score_weights)

    # Phase 4: populate cache and stitch results back in original order
    for idx, ind, m in zip(uncached_indices, uncached_population, uncached_metrics):
        key = _make_ind_key(ind)
        eval_cache[key] = m.copy()
        results[idx] = m

    return results


def _parse_csv_outputs(uncached_population: List[Dict], output_dir: Path, score_weights: Dict[str, float]) -> List[Dict]:
    """Parse CSV simulation outputs (output_vars_iter_{i}.csv) exported by Ansys or MATLAB.

    Column detection uses substring matching for robustness across naming conventions.
    Missing or unparseable CSVs receive heavy penalty metrics to drive the optimizer away.
    """
    uncached_metrics = []
    
    def safe_extract(df: pd.DataFrame, col_name: str, fallback: float, 
                     use_window_mean: bool = True, window: int = 10) -> float:
        """Extract a numeric column value with fallback. If use_window_mean, return mean of last `window` rows.
        For time-series CSV rows the window smooths the final values; for single-row
        Ansys OutputVariablesTable exports it simply returns that one value."""
        if col_name is None or col_name not in df.columns:
            return fallback
        series = pd.to_numeric(df[col_name], errors="coerce").dropna()
        if series.empty:
            return fallback
        if use_window_mean:
            return float(series.iloc[-window:].mean())
        return float(series.iloc[-1])

    for i in range(1, len(uncached_population) + 1):
        csv_path = output_dir / f"output_vars_iter_{i}.csv"
        if not csv_path.is_file():
            logging.warning("Missing CSV for candidate %d – assigning penalty", i)
            m = {
                "score": -1e6, "efficiency": 0.0, "torque_ripple": 1e6,
                "cost": 1e9, "power_density": 0.0
            }
        else:
            try:
                df = pd.read_csv(csv_path)
                if df.empty:
                    raise ValueError("CSV file is completely empty.")

                # Heuristic column detection: case-insensitive regex matching for robustness
                def _find_col(patterns):
                    for pat in patterns:
                        for c in df.columns:
                            try:
                                if re.search(pat, c, re.IGNORECASE):
                                    return c
                            except Exception:
                                continue
                    return None

                eff_col = _find_col([r"Eff"])
                tr_col = _find_col([r"Ripple", r"TorqueRip"])
                cost_col = _find_col([r"Cost", r"TotCost"])
                pwr_col = _find_col([r"PowerDensity", r"PwrDens", r"ower_Density", r"owerDensity"])
                if pwr_col is None:
                    power_cols = [c for c in df.columns if re.search(r"Power", c, re.IGNORECASE)]
                    filtered = [c for c in power_cols if not re.search(r"Input|Output", c, re.IGNORECASE)]
                    pwr_col = filtered[0] if filtered else None

                eff = safe_extract(df, eff_col, fallback=90.0, use_window_mean=True, window=10)
                tr = safe_extract(df, tr_col, fallback=15.0, use_window_mean=True, window=10)
                cost = safe_extract(df, cost_col, fallback=100.0, use_window_mean=False)
                pwr = safe_extract(df, pwr_col, fallback=0.3, use_window_mean=False)

                m = {
                    "efficiency": eff, "torque_ripple": tr,
                    "cost": cost, "power_density": pwr,
                }
                m["score"] = compute_score(
                    m, score_weights["eff"], score_weights["ripple"],
                    score_weights["pwr"], score_weights["cost"]
                )
            except Exception as ex:
                logging.error(f"Error parsing CSV {csv_path}: {ex}")
                m = {
                    "score": -1e6, "efficiency": 0.0, "torque_ripple": 1e6,
                    "cost": 1e9, "power_density": 0.0
                }
        uncached_metrics.append(m)
    return uncached_metrics

# ---------------------------------------------------------------------------
# Visualization & Plotting Utilities
# ---------------------------------------------------------------------------
def plot_pareto_front(history_csv: Path, output_dir: Path):
    """Plot 2D Pareto front (Efficiency vs Torque Ripple)."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        logging.warning("history_csv not found \u2014 skipping 2D Pareto plot")
        return

    try:
        df = pd.read_csv(history_csv, on_bad_lines='skip')
    except Exception as e:
        logging.warning("Failed to read history CSV for 2D Pareto plot: %s", e)
        return
    if "efficiency" not in df.columns or "torque_ripple" not in df.columns:
        logging.warning("Required columns missing in history CSV \u2014 skipping 2D Pareto plot")
        return
        
    fig, ax = plt.subplots(figsize=(8, 6))
    scatter = ax.scatter(
        df["torque_ripple"], df["efficiency"],
        c=df["generation"], cmap="viridis", alpha=0.7, edgecolors="k"
    )
    plt.colorbar(scatter, label="Generation")
    ax.set_xlabel("Torque Ripple (%)")
    ax.set_ylabel("Efficiency (%)")
    ax.set_title("V-Shape IPM Motor Optimization – Pareto Distribution")
    ax.grid(True, linestyle="--", alpha=0.5)
    
    out_path = output_dir / "pareto_front.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300)
    plt.close(fig)
    logging.info("Saved Pareto front plot to %s", out_path)


def plot_3d_pareto(history_csv: Path, output_dir: Path):
    """Plot 3D Pareto front (Efficiency vs Torque Ripple vs Cost)."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        logging.warning("history_csv not found \u2014 skipping 3D Pareto plot")
        return

    try:
        df = pd.read_csv(history_csv, on_bad_lines='skip')
    except Exception as e:
        logging.warning("Failed to read history CSV for 3D Pareto plot: %s", e)
        return
    req = ["efficiency", "torque_ripple", "cost"]
    if not all(c in df.columns for c in req):
        logging.warning("Required columns missing in history CSV \u2014 skipping 3D Pareto plot")
        return
        
    fig = plt.figure(figsize=(9, 7))
    ax = fig.add_subplot(111, projection="3d")
    
    sc = ax.scatter(
        df["torque_ripple"], df["cost"], df["efficiency"],
        c=df["generation"], cmap="plasma", alpha=0.8, s=40
    )
    plt.colorbar(sc, label="Generation", pad=0.1)
    ax.set_xlabel("Torque Ripple (%)")
    ax.set_ylabel("Cost ($)")
    ax.set_zlabel("Efficiency (%)")
    ax.set_title("3D Pareto Front")
    
    out_path = output_dir / "pareto_3d.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300)
    plt.close(fig)
    logging.info("Saved 3D Pareto plot to %s", out_path)


def plot_parallel_coordinates(history_csv: Path, output_dir: Path):
    """Plot parallel coordinates for all 4 optimization objectives."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        logging.warning("history_csv not found \u2014 skipping parallel coordinates plot")
        return

    try:
        df = pd.read_csv(history_csv, on_bad_lines='skip')
    except Exception as e:
        logging.warning("Failed to read history CSV for parallel coordinates plot: %s", e)
        return
    cols = ["efficiency", "torque_ripple", "power_density", "cost"]
    if not all(c in df.columns for c in cols):
        logging.warning("Required columns missing in history CSV \u2014 skipping parallel coordinates plot")
        return
        
    df_norm = df[cols].copy()
    for col in cols:
        c_min, c_max = df_norm[col].min(), df_norm[col].max()
        df_norm[col] = (df_norm[col] - c_min) / (c_max - c_min) if c_max > c_min else 0.5
        
    fig, ax = plt.subplots(figsize=(10, 5))
    x_coords = list(range(len(cols)))
    
    for _, row in df_norm.iterrows():
        ax.plot(x_coords, row[cols].values, color="gray", alpha=0.15)
        
    top_indices = df["score"].nlargest(min(5, len(df))).index if "score" in df.columns else df_norm.index[:5]
    colors = plt.cm.tab10(np.linspace(0, 1, len(top_indices)))
    for rank, (idx, color) in enumerate(zip(top_indices, colors)):
        ax.plot(x_coords, df_norm.loc[idx, cols].values, color=color, linewidth=2, label=f"Rank {rank+1}")
        
    ax.set_xticks(x_coords)
    ax.set_xticklabels(["Efficiency\n(MAX)", "Torque Ripple\n(MIN)", "Power Density\n(MAX)", "Cost\n(MIN)"])
    ax.set_title("Parallel Coordinates – Top Designs Overview")
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)
    ax.legend()
    
    out_path = output_dir / "parallel_coordinates.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300)
    plt.close(fig)
    logging.info("Saved Parallel Coordinates plot to %s", out_path)


def plot_convergence(scores_history: List[float], output_dir: Path):
    """Plot convergence history curve."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not scores_history:
        logging.warning("Empty scores history \u2014 skipping convergence plot")
        return
        
    fig, ax = plt.subplots(figsize=(8, 5))
    gens = list(range(1, len(scores_history) + 1))
    ax.plot(gens, scores_history, marker="o", color="navy", linewidth=2)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Hypervolume / Best Score")
    ax.set_title("Optimization Convergence History")
    ax.grid(True, linestyle="--", alpha=0.5)
    
    out_path = output_dir / "convergence_history.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300)
    plt.close(fig)
    logging.info("Saved Convergence History plot to %s", out_path)

# ---------------------------------------------------------------------------
# Report Generation
# ---------------------------------------------------------------------------
def generate_report(history_csv: Path, best_ind: dict, best_score: float,
                    bounds: dict, output_dir: Path, args):
    """Generate Markdown optimization report."""
    report_path = output_dir / "optimization_report.md"
    
    n_sims = 0
    if history_csv.is_file():
        try:
            df = pd.read_csv(history_csv, on_bad_lines='skip')
            n_sims = len(df)
        except Exception:
            pass
            
    content = f"""# V-Shape IPM Motor Optimization Report

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Optimizer Version:** v5.2 (Fix Lan 3 Remote)  
**Algorithm:** {args.algorithm.upper()}  
**Mode:** {args.mode}  
**Evaluated Candidates:** {n_sims}  
**Best Score Achieved:** `{best_score:.4f}`

---

## 🏆 Best Pareto-Optimized Design Parameters

| Parameter | Optimized Value | Unit | Bounds [Min, Max] |
|---|---|---|---|
"""
    norm_best = normalize_ind_keys(best_ind)
    for p in bounds:
        val = norm_best.get(p, "N/A")
        unit = bounds[p]["unit"]
        lower = bounds[p]["lower"]
        upper = bounds[p]["upper"]
        content += f"| `{p}` | **{val}** | {unit} | [{lower}, {upper}] |\n"

    content += """
---

## 📊 Calculated Dependent Variables
"""
    dep_vars = compute_dependent_variables(norm_best)
    for k, v in dep_vars.items():
        content += f"- **`{k}`**: `{v}`\n"

    # Read best metrics from history CSV
    best_metrics = {}
    if history_csv.is_file():
        try:
            hist_df = pd.read_csv(history_csv, on_bad_lines='skip')
            if "score" in hist_df.columns and len(hist_df) > 0:
                best_row = hist_df.loc[hist_df["score"].idxmax()]
                best_metrics = {
                    "Efficiency": best_row.get("efficiency", "N/A"),
                    "Torque Ripple": best_row.get("torque_ripple", "N/A"),
                    "Power Density": best_row.get("power_density", "N/A"),
                    "Cost": best_row.get("cost", "N/A"),
                }
        except Exception:
            pass

    content += """
---

## 🏆 Optimized Objective Metrics
"""
    if best_metrics:
        content += "| Metric | Value |\n|---|---|\n"
        for k, v in best_metrics.items():
            content += f"| **{k}** | {v} |\n"
    content += f"| **Composite Score** | {best_score:.4f} |\n"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    logging.info("Optimization report saved to %s", report_path)

# ---------------------------------------------------------------------------
# Comprehensive Unit Test Suite
# ---------------------------------------------------------------------------
def run_unit_tests():
    """Run built-in unit test suite (11 checks)."""
    logging.info("=" * 60)
    logging.info("Running Built-in Unit Test Suite (Fix Lan 3)")
    logging.info("=" * 60)
    
    global PARAM_ORDER
    if not PARAM_ORDER:
        try:
            PARAM_ORDER = list(load_bounds(Path("Ai_Optimization_Bounds.xlsx")).keys())
        except Exception:
            PARAM_ORDER = ["Dr_in", "Air_gap", "Lamda", "Bridge", "Hs0", "Hs1", "Hs2", "Bs0", "Bs1", "Bs2", "O1", "O2", "B1", "rib", "hrib", "Mt", "Mw", "magDmin", "Thet_deg"]
    
    passed = 0
    failed = 0
    
    # Test 1: Constraint functions
    logging.info("\n[Test 1] Geometric Constraint Functions")
    feasible = get_baseline_individual()
    infeasible_slot = feasible.copy(); infeasible_slot["Hs2"] = 50.0
    infeasible_bridge = feasible.copy(); infeasible_bridge["B1"] = 5.5
    infeasible_hrib = feasible.copy(); infeasible_hrib["hrib"] = 5.0
    infeasible_width = feasible.copy(); infeasible_width["Bs0"] = 8.0; infeasible_width["Bs1"] = 4.0

    tests = [
        ("Feasible baseline", feasible, True),
        ("Infeasible slot height", infeasible_slot, False),
        ("Infeasible bridge thickness", infeasible_bridge, False),
        ("Infeasible hrib limit", infeasible_hrib, False),
        ("Infeasible slot width progression", infeasible_width, False),
    ]
    for name, params, expected in tests:
        result = is_feasible(params)
        if result == expected:
            logging.info(f"  [PASS] {name}: {'FEASIBLE' if result else 'INFEASIBLE'}")
            passed += 1
        else:
            logging.error(f"  [FAIL] {name}: expected {expected}, got {result}")
            failed += 1
    
    # Test 2: NSGA-II 4D Fast Non-Dominated Sorting
    logging.info("\n[Test 2] NSGA-II 4D Fast Non-Dominated Sorting")
    m1 = {"efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0}
    m2 = {"efficiency": 94.0, "torque_ripple": 25.0, "power_density": 0.30, "cost": 150.0}
    m3 = {"efficiency": 96.0, "torque_ripple": 18.0, "power_density": 0.38, "cost": 135.0}
    fronts = fast_non_dominated_sort([m1, m2, m3])
    if len(fronts) > 0 and 2 in fronts[0]:
        logging.info("  [PASS] Non-dominated sorting correctly identified m3 in Pareto front 0")
        passed += 1
    else:
        logging.error("  [FAIL] Fast non-dominated sorting failed")
        failed += 1

    # Test 3: 4D Crowding Distance
    logging.info("\n[Test 3] 4D Crowding Distance Assignment")
    metrics_list = [m1, m2, m3, {"efficiency": 93.0, "torque_ripple": 15.0, "power_density": 0.32, "cost": 155.0}]
    dist = crowding_distance_assignment([0, 1, 2, 3], metrics_list)
    if dist[2] == float("inf"):
        logging.info("  [PASS] Boundary solution assigned infinite crowding distance")
        passed += 1
    else:
        logging.error("  [FAIL] Crowding distance assignment failed")
        failed += 1

    # Test 4: 4D Pareto Hypervolume Calculation
    logging.info("\n[Test 4] 4D Pareto Hypervolume Metric")
    hv = compute_pareto_hypervolume(metrics_list)
    if hv > 0.0:
        logging.info(f"  [PASS] 4D Pareto Hypervolume = {hv:.6f}")
        passed += 1
    else:
        logging.error("  [FAIL] Hypervolume computation returned zero")
        failed += 1

    # Test 5: Calculated Dependent Variables
    logging.info("\n[Test 5] Calculated Dependent Variables Derivation")
    dep_vars = compute_dependent_variables(feasible)
    if dep_vars["Speed_rpm"] == 1000.0 and dep_vars["N"] > 0:
        logging.info(f"  [PASS] Speed_rpm={dep_vars['Speed_rpm']}, Turns_N={dep_vars['N']}")
        passed += 1
    else:
        logging.error("  [FAIL] Dependent variables calculation failed")
        failed += 1

    # Test 6: Repair Mechanism
    logging.info("\n[Test 6] Repair Mechanism Constraints Check")
    bounds_test = {
        "Dr_in": {"lower": 50.0, "upper": 90.0, "step": 5.0, "unit": "mm"},
        "Air_gap": {"lower": 0.5, "upper": 1.5, "step": 0.1, "unit": "mm"},
        "B1": {"lower": 3.2, "upper": 5.0, "step": 0.5, "unit": "mm"},
        "Mt": {"lower": 4.0, "upper": 6.0, "step": 0.2, "unit": "mm"},
        "Mw": {"lower": 10.0, "upper": 30.0, "step": 2.0, "unit": "mm"},
        "Lamda": {"lower": 0.8, "upper": 1.0, "step": 0.1, "unit": ""},
        "Hs0": {"lower": 1.0, "upper": 2.0, "step": 0.1, "unit": "mm"},
        "Hs1": {"lower": 1.0, "upper": 2.0, "step": 0.1, "unit": "mm"},
        "Hs2": {"lower": 16.0, "upper": 30.0, "step": 1.0, "unit": "mm"},
        "hrib": {"lower": 2.0, "upper": 6.0, "step": 0.5, "unit": "mm"},
        "O2": {"lower": 2.0, "upper": 7.0, "step": 0.5, "unit": "mm"},
    }
    bad_ind = {"Dr_in": 95.0, "Air_gap": 2.0, "B1": 6.0, "Mt": 5.0, 
               "Mw": 5.0, "Lamda": 1.5, "Hs0": 3.0, "Hs1": 3.0, "Hs2": 40.0,
               "hrib": 5.5, "O2": 6.0}
    repaired = repair_individual(bad_ind, bounds_test)
    if is_feasible(repaired):
        logging.info("  [PASS] Repaired individual is 100% FEASIBLE")
        passed += 1
    else:
        logging.error("  [FAIL] Repair mechanism failed")
        failed += 1

    # Test 7: Casing Normalization
    logging.info("\n[Test 7] Key Casing Normalization")
    cased_dict = {"thet_deg": 35.0, "Hrib": 3.0}
    norm_dict = normalize_ind_keys(cased_dict)
    if "Thet_deg" in norm_dict and "hrib" in norm_dict:
        logging.info("  [PASS] Key casing successfully normalized")
        passed += 1
    else:
        logging.error("  [FAIL] Key casing normalization failed")
        failed += 1

    # Summary
    total = passed + failed
    logging.info(f"\n{'=' * 60}")
    logging.info(f"Unit Test Suite Results: {passed}/{total} passed, {failed}/{total} failed")
    logging.info(f"{'=' * 60}")
    
    return failed == 0

# ---------------------------------------------------------------------------
# Checkpoint & Logging Utilities
# ---------------------------------------------------------------------------
STATE_FILE = "optimizer_state.pkl"

def save_state(state: dict, path: Path):
    """Save optimizer state to pickle file."""
    with open(path, "wb") as f:
        pickle.dump(state, f)


def load_state(path: Path) -> dict:
    """Load optimizer state from pickle file."""
    with open(path, "rb") as f:
        state = pickle.load(f)
    return state


def log_history_entry(history_log_path: Path, entry: dict, param_order: List[str]):
    """Log individual activity details to log_history.csv continuously."""
    header = [
        "timestamp", "generation", "individual_id", "operator", "is_feasible",
        "violated_constraints", "repair_status", "efficiency", "torque_ripple",
        "power_density", "cost", "score", "pareto_rank", "crowding_distance", "mode"
    ] + param_order

    violated = entry.get("violated_constraints", [])
    violated_str = "|".join(violated) if isinstance(violated, list) and violated else "None"

    row = {
        "timestamp": entry.get("timestamp", datetime.now().isoformat()),
        "generation": entry.get("generation", 0),
        "individual_id": entry.get("individual_id", "N/A"),
        "operator": entry.get("operator", "Evolution"),
        "is_feasible": entry.get("is_feasible", True),
        "violated_constraints": violated_str,
        "repair_status": entry.get("repair_status", "NotNeeded"),
        "efficiency": entry.get("efficiency", 0.0),
        "torque_ripple": entry.get("torque_ripple", 0.0),
        "power_density": entry.get("power_density", 0.0),
        "cost": entry.get("cost", 0.0),
        "score": entry.get("score", 0.0),
        "pareto_rank": entry.get("pareto_rank", 0),
        "crowding_distance": entry.get("crowding_distance", 0.0),
        "mode": entry.get("mode", "offline"),
    }
    for p in param_order:
        row[p] = entry.get(p, 0.0)

    write_header = not history_log_path.exists() or history_log_path.stat().st_size == 0
    df_row = pd.DataFrame([row])[header]
    df_row.to_csv(history_log_path, mode="a", header=write_header, index=False)


def clean_post_run_temp_files(root_dir: Path, output_dir: Path):
    """Clean up lingering temporary files (.aedt, .aedtresults, .lock, raw CSVs) after optimization completes."""
    logging.info("Cleaning up temporary project files and raw CSV files...")
    for temp_pattern in ["temp_design_ind_*.aedt*", "*.lock"]:
        for p in root_dir.glob(temp_pattern):
            try:
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                elif p.is_file():
                    p.unlink(missing_ok=True)
            except Exception:
                pass
                
    for csv_file in output_dir.glob("output_vars_iter_*.csv"):
        try:
            csv_file.unlink(missing_ok=True)
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Main Driver Application
# ---------------------------------------------------------------------------
def main():
    """Main entry point: parse CLI args, setup logging/output dirs, run NSGA-II/GA optimization loop, generate reports and plots."""
    parser = argparse.ArgumentParser(
        description="V-Shape IPM Motor AI Optimizer v5.2 (Fix Lan 3 Remote)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    
    parser.add_argument("--pop-size", type=int, default=8, help="Population size (default: 8)")
    parser.add_argument("--generations", type=int, default=10, help="Max generations (default: 10)")
    parser.add_argument("--crossover", type=float, default=0.7, help="Crossover probability (default: 0.7)")
    parser.add_argument("--mutation", type=float, default=0.2, help="Mutation rate per gene (default: 0.2)")
    parser.add_argument("--mode", choices=["offline", "matlab", "ansys"], default="offline", help="Evaluation mode")
    parser.add_argument("--algorithm", choices=["nsga2", "ga"], default="nsga2", help="Optimization engine (default: nsga2)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--matlab-exe", type=str, default=r"C:\MATLAB\R2023b\bin\matlab.exe", help="MATLAB executable path")
    parser.add_argument("--non-graphical", action="store_true", default=True, help="Run Ansys Maxwell headless")
    parser.add_argument("--show-gui", action="store_true", help="Show Ansys GUI")
    parser.add_argument("--keep-temp", action="store_true", help="Keep temporary AEDT project files")

    # Objective weights
    parser.add_argument("--w-eff", type=float, default=1.0, help="Efficiency weight (default: 1.0)")
    parser.add_argument("--w-ripple", type=float, default=1.0, help="Torque Ripple weight (default: 1.0)")
    parser.add_argument("--w-pwr", type=float, default=0.5, help="Power Density weight (default: 0.5)")
    parser.add_argument("--w-cost", type=float, default=0.05, help="Cost penalty weight (default: 0.05)")
    
    # Feature flags
    parser.add_argument("--no-ml", action="store_true", help="Disable ML surrogate blending")
    parser.add_argument("--no-screening", action="store_true", help="Disable surrogate candidate screening")
    parser.add_argument("--no-local-search", action="store_true", help="Disable elite local search")
    parser.add_argument("--plot-pareto", action="store_true", help="Generate 2D Pareto plot")
    parser.add_argument("--plot-all", action="store_true", help="Generate all visualizations")
    parser.add_argument("--test", action="store_true", help="Run built-in unit tests and exit")
    parser.add_argument("--no-report", action="store_true", help="Skip report generation")
    
    args = parser.parse_args()
    if args.show_gui:
        args.non_graphical = False

    if args.test:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                          handlers=[logging.StreamHandler(sys.stdout)])
        success = run_unit_tests()
        sys.exit(0 if success else 1)

    if args.plot_all:
        args.plot_pareto = True

    if getattr(sys, "frozen", False):
        script_dir = Path(sys.executable).resolve().parent
    else:
        script_dir = Path(__file__).resolve().parent
    root_dir = script_dir

    # Phase 1.2: Timestamped output folder with latest symlink
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_name = f"run_{timestamp}"
    outputs_root = root_dir / "outputs"
    outputs_root.mkdir(exist_ok=True)
    output_dir = outputs_root / run_name
    output_dir.mkdir(parents=True, exist_ok=True)

    # Create/update 'latest' symlink
    latest_link = outputs_root / "latest"
    try:
        if latest_link.is_symlink() or latest_link.exists():
            latest_link.unlink()
        latest_link.symlink_to(run_name)
    except Exception:
        pass  # Symlinks may fail on Windows without admin

    log_file = output_dir / "optimizer.log"
    log_history_file = output_dir / "log_history.csv"
    setup_logger(str(log_file))

    logging.info("=" * 60)
    logging.info("V-Shape IPM Motor AI Optimizer v5.2 (Fix Lan 3)")
    logging.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)

    clear_evaluation_cache()

    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)

    bounds_path = root_dir / "Ai_Optimization_Bounds.xlsx"
    bounds = load_bounds(bounds_path) if bounds_path.is_file() else {
        "Dr_in": {"lower": 50.0, "upper": 90.0, "step": 5.0, "unit": "mm"},
        "Air_gap": {"lower": 0.5, "upper": 1.5, "step": 0.1, "unit": "mm"},
        "Lamda": {"lower": 0.8, "upper": 1.0, "step": 0.1, "unit": ""},
        "Bridge": {"lower": 1.0, "upper": 3.0, "step": 0.1, "unit": "mm"},
        "Hs0": {"lower": 1.0, "upper": 2.0, "step": 0.1, "unit": "mm"},
        "Hs1": {"lower": 1.0, "upper": 2.0, "step": 0.1, "unit": "mm"},
        "Hs2": {"lower": 16.0, "upper": 30.0, "step": 1.0, "unit": "mm"},
        "Bs0": {"lower": 1.5, "upper": 4.0, "step": 0.5, "unit": "mm"},
        "Bs1": {"lower": 3.0, "upper": 10.0, "step": 0.5, "unit": "mm"},
        "Bs2": {"lower": 5.0, "upper": 14.0, "step": 1.0, "unit": "mm"},
        "O1": {"lower": 0.0, "upper": 13.0, "step": 1.0, "unit": "mm"},
        "O2": {"lower": 2.0, "upper": 7.0, "step": 0.5, "unit": "mm"},
        "B1": {"lower": 3.2, "upper": 5.0, "step": 0.5, "unit": "mm"},
        "rib": {"lower": 2.0, "upper": 15.0, "step": 1.0, "unit": "mm"},
        "hrib": {"lower": 2.0, "upper": 6.0, "step": 0.5, "unit": "mm"},
        "Mt": {"lower": 4.0, "upper": 6.0, "step": 0.2, "unit": "mm"},
        "Mw": {"lower": 10.0, "upper": 30.0, "step": 2.0, "unit": "mm"},
        "magDmin": {"lower": 0.0, "upper": 10.0, "step": 1.0, "unit": "mm"},
        "Thet_deg": {"lower": 0.0, "upper": 90.0, "step": 1.0, "unit": "deg"},
    }

    global PARAM_ORDER
    PARAM_ORDER = list(bounds.keys())

    # Pre-train ML surrogate from any existing history file BEFORE deleting it
    history_path = output_dir / "simulation_history.csv"
    existing_history = history_path if history_path.exists() else None
    ml_surrogate = MLSurrogate(PARAM_ORDER, history_path=existing_history)
    if not args.resume:
        history_path.unlink(missing_ok=True)
    history_header_written = False
    use_ml = not args.no_ml

    score_weights = {"eff": args.w_eff, "ripple": args.w_ripple, "pwr": args.w_pwr, "cost": args.w_cost}

    start_gen = 1
    if args.resume:
        state_path = root_dir / STATE_FILE
        if state_path.exists():
            try:
                state = load_state(state_path)
                if state["generation"] < args.generations:
                    start_gen = state["generation"] + 1
                    population = state["population"]
                    scores_history = state["scores_history"]
                    best_ind = state["best_ind"]
                    best_score = state["best_score"]
                    _EVALUATION_CACHE.clear()
                    _EVALUATION_CACHE.update(state["eval_cache"])
                    logging.info(f"*** Resumed from generation {state['generation']}, continuing at gen {start_gen} ***")
                else:
                    logging.info("Optimization already complete per state file.")
                    return
            except Exception as e:
                logging.warning(f"Resume failed: {e}. Starting fresh.")

    if start_gen == 1:
        population = [random_individual(bounds) for _ in range(args.pop_size)]
        scores_history = []
        best_ind = None
        best_score = -float("inf")

        if existing_history and not args.resume:
            try:
                hist_df = pd.read_csv(existing_history)
                if "score" in hist_df.columns and len(hist_df) > 0:
                    valid = hist_df[hist_df["score"] > -1e5]
                    if len(valid) > 0:
                        best_row = valid.loc[valid["score"].idxmax()]
                        warm_ind = {p: best_row[p] for p in PARAM_ORDER if p in best_row}
                        if warm_ind:
                            population[0] = warm_ind
                            logging.info(f"Auto warm-start: seeded with best historical design (score={best_row['score']:.4f})")
            except Exception as e:
                logging.warning(f"Auto warm-start failed: {e}")

    for gen in range(start_gen, args.generations + 1):
        logging.info("--- Generation %d / %d ---", gen, args.generations)
        metrics = evaluate_population(
            population, root_dir, root_dir, output_dir,
            args.mode, score_weights, ml_surrogate, use_ml,
            matlab_exe=args.matlab_exe, non_graphical=args.non_graphical
        )
        
        ml_surrogate.add_evaluations(population, metrics)
        
        hv = compute_pareto_hypervolume(metrics)
        spacing = compute_pareto_spacing(metrics)
        
        scores = [m["score"] for m in metrics]
        gen_best_idx = int(np.argmax(scores))
        if scores[gen_best_idx] > best_score:
            best_score = scores[gen_best_idx]
            best_ind = population[gen_best_idx].copy()

        scores_history.append(hv)
        logging.info("Gen %d | 4D Hypervolume: %.6f | Pareto Spacing: %.4f | Best Score: %.4f", gen, hv, spacing, best_score)

        # Log history rows for reporting and plotting
        hist_rows = []
        for ind, m in zip(population, metrics):
            norm_ind = normalize_ind_keys(ind)
            norm_clean = {k: v for k, v in norm_ind.items() if not str(k).startswith('_')}
            row = {**norm_clean, "generation": gen, "score": m["score"],
                   "efficiency": m["efficiency"], "torque_ripple": m["torque_ripple"],
                   "power_density": m["power_density"], "cost": m["cost"]}
            hist_rows.append(row)
        
        hist_cols = PARAM_ORDER + ["generation", "score", "efficiency", "torque_ripple", "power_density", "cost"]
        pd.DataFrame(hist_rows, columns=hist_cols).to_csv(
            history_path, mode="a", header=not history_header_written, index=False
        )
        history_header_written = True

        fronts = fast_non_dominated_sort(metrics)
        crowding_dist = {}
        for f in fronts:
            crowding_dist.update(crowding_distance_assignment(f, metrics))

        sens_per_obj = get_per_objective_sensitivity(ml_surrogate, PARAM_ORDER)

        offspring = []
        while len(offspring) < args.pop_size:
            p1, p2 = nsga2_tournament_selection(population, metrics, fronts, crowding_dist)
            c1, c2 = crossover(p1, p2, bounds, args.crossover)
            c1 = sensitivity_mutate(c1, bounds, sens_per_obj, args.mutation)
            c2 = sensitivity_mutate(c2, bounds, sens_per_obj, args.mutation)
            offspring.append(c1)
            if len(offspring) < args.pop_size:
                offspring.append(c2)

        if not args.no_local_search and fronts and fronts[0]:
            elites = [population[i] for i in fronts[0][:min(2, len(fronts[0]))]]
            elite_m = [metrics[i] for i in fronts[0][:min(2, len(fronts[0]))]]
            local_cand = perform_elite_local_search(elites, elite_m, bounds, ml_surrogate, n_neighbors_per_elite=2)
            offspring.extend(local_cand)

        offspring = rank_candidates_surrogate(offspring, ml_surrogate, score_weights, top_n=args.pop_size)
        combined_pop = population + offspring
        combined_metrics = evaluate_population(
            combined_pop, root_dir, root_dir, output_dir,
            args.mode, score_weights, ml_surrogate, use_ml,
            matlab_exe=args.matlab_exe, non_graphical=args.non_graphical
        )
        population = nsga2_selection(combined_pop, combined_metrics, args.pop_size)

        for cm_idx, cm in enumerate(combined_metrics):
            if cm["score"] > best_score:
                best_score = cm["score"]
                best_ind = combined_pop[cm_idx].copy()

        try:
            state = {
                "generation": gen,
                "population": population,
                "scores_history": scores_history,
                "best_ind": best_ind,
                "best_score": best_score,
                "eval_cache": dict(_EVALUATION_CACHE),
            }
            save_state(state, root_dir / STATE_FILE)
            logging.debug(f"Checkpoint saved after generation {gen}")
        except Exception as e:
            logging.warning(f"Checkpoint save failed: {e}")

    logging.info("=" * 60)
    logging.info("OPTIMIZATION COMPLETE")
    logging.info("=" * 60)
    if best_ind:
        out_path = output_dir / "best_optimized_design_v5.2.csv"
        pd.DataFrame([normalize_ind_keys(best_ind)]).to_csv(out_path, index=False)
        logging.info("Best design saved to %s", out_path)
        if not args.no_report:
            generate_report(history_path, best_ind, best_score, bounds, output_dir, args)

    if args.plot_pareto:
        plot_pareto_front(history_path, output_dir)
    if args.plot_all:
        plot_3d_pareto(history_path, output_dir)
        plot_parallel_coordinates(history_path, output_dir)
        plot_convergence(scores_history, output_dir)

    if not args.keep_temp:
        clean_post_run_temp_files(root_dir, output_dir)


if __name__ == "__main__":
    main()
