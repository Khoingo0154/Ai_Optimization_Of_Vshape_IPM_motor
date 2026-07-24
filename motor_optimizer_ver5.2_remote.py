#!/usr/bin/env python3
# motor_optimizer_ver5.2_remote.py
"""
Version 5.2 of the AI optimizer for the V-Shape IPM motor.
Production-grade upgrade with direct Ansys Maxwell integration (PyAEDT / win32com ActiveX),
MATLAB-free simulation option (`--mode ansys`), evaluation caching, fixed sensitivity ranking,
accurate stagnation logging, and warm-start support.

Key Upgrades in ver5.2:
1. **Direct Ansys Maxwell Integration (`--mode ansys`)**: Eliminates MATLAB completely!
   Communicates directly with Ansys Maxwell 3D using PyAEDT and direct win32com ActiveX.
2. **Headless Execution (`--non-graphical`)**: Option to run Ansys Maxwell in background.
3. **Deduplicated Evaluation Caching**: Prevents re-evaluating unchanged individuals across
   generations and within NSGA-II/GA loops using `_EVALUATION_CACHE`.
4. **Absolute Sensitivity Ranking**: Ranks parameters by abs(Spearman correlation).
5. **Accurate Stagnation Logging**: Stagnation logging window matches actual parameter passed.
6. **Robust Elitism Tracking**: Object-identity based elitism operator tagging.
7. **Warm-Start from Prior CSV**: Seed population from previous design CSV files via `--warm-start`.
"""

import sys
import os
import math
import random
import logging
import argparse
import pickle
import shutil
import subprocess
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Tuple, Any, Optional

import pandas as pd
import numpy as np

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
# Global constants & Evaluation Cache
# ---------------------------------------------------------------------------
DS_OUT = 240.0  # outer stator diameter (mm)
L_STK = 134.0   # stack length (mm)
SLOT_HEIGHT_MARGIN = 12.25  # mm — per Optimization_Requirements.md §Constraint 1

PARAM_ORDER: List[str] = []

# Global cache mapping _make_ind_key(ind) -> metrics dict
_EVALUATION_CACHE: Dict[tuple, Dict[str, float]] = {}


def clear_evaluation_cache():
    """Clear the global evaluation cache."""
    _EVALUATION_CACHE.clear()


def get_evaluation_cache_size() -> int:
    """Return the number of cached unique evaluations."""
    return len(_EVALUATION_CACHE)


def _make_ind_key(ind: dict) -> tuple:
    """Create an immutable, rounded hashable key for design parameters.
    
    Ignores internal metadata keys starting with '_'.
    """
    return tuple(sorted((str(k), round(float(v), 6)) for k, v in ind.items() if not str(k).startswith('_')))


def setup_logger(log_filename="run_log.txt"):
    """
    Cấu hình logger ghi đồng thời ra console và file.
    Không làm thay đổi logic chạy, chỉ bổ sung quan sát.
    """
    # Sử dụng root logger để tất cả các hàm logging.info, logging.warning...
    # của script đều được xử lý bởi các handlers này.
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG)
    
    # Xóa các handler cũ nếu có (tránh ghi trùng khi chạy lại trong cùng session)
    if logger.handlers:
        logger.handlers.clear()

    # Format: [Thời gian] [Mức độ] Nội dung
    formatter = logging.Formatter(
        '[%(asctime)s] [%(levelname)s] %(message)s', 
        datefmt='%Y-%m-%d %H:%M:%S'
    )

    # 1. Handler ghi ra file run_log.txt
    file_handler = logging.FileHandler(log_filename, encoding='utf-8')
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)

    # 2. Handler ghi ra màn hình (Console)
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO) # Chỉ hiện INFO trở lên trên màn hình cho gọn
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
# Helper functions for bounds & constraints
# ---------------------------------------------------------------------------
def load_bounds(bounds_path: Path) -> Dict[str, Dict[str, Any]]:
    """Read the bounds Excel file and return an ordered dict of parameter info."""
    if not bounds_path.is_file():
        raise FileNotFoundError(f"Bounds file not found: {bounds_path}")
    
    df = pd.read_excel(bounds_path)
    required_cols = ["Parameter", "Lower_Limit", "Upper_Limit", "Step"]
    missing = [c for c in required_cols if c not in df.columns]
    if missing:
        raise ValueError(f"Bounds file missing columns: {missing}")
    
    bounds = {}
    for _, row in df.iterrows():
        name = str(row["Parameter"]).strip()
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
    """Round *value* to the nearest valid step within limits."""
    if step <= 0:
        return float(np.clip(value, lower, upper))
    steps = round((value - lower) / step)
    snapped = lower + steps * step
    return float(np.clip(round(snapped, 6), lower, upper))

# ---------------------------------------------------------------------------
# 4 Geometric Constraint definitions
# ---------------------------------------------------------------------------
def constraint_slot_height(params: dict) -> bool:
    """Hs0 + Hs1 + Hs2 < ((Ds_out - Ds_in) / 2) - 12.25 mm."""
    lamda = params.get("Lamda", 0.9)
    air_gap = params.get("Air_gap", 1.0)
    hs_sum = params.get("Hs0", 1.0) + params.get("Hs1", 1.0) + params.get("Hs2", 18.0)
    ds_in = (L_STK / lamda) + air_gap
    limit = ((DS_OUT - ds_in) / 2.0) - SLOT_HEIGHT_MARGIN
    return hs_sum < limit


def constraint_slot_width_progression(params: dict) -> bool:
    """Bs0 <= Bs1 and Bs1 <= Bs2."""
    bs0 = params.get("Bs0", 2.0)
    bs1 = params.get("Bs1", 6.5)
    bs2 = params.get("Bs2", 10.0)
    return bs0 <= bs1 and bs1 <= bs2


def constraint_bridge_thickness(params: dict) -> bool:
    """B1 <= Mt - 0.3 mm."""
    b1 = params.get("B1", 3.5)
    mt = params.get("Mt", 5.282)
    return b1 <= (mt - 0.3)


def constraint_rotor_fits_stator(params: dict) -> bool:
    """Dr_out > Dr_in."""
    lamda = params.get("Lamda", 0.9)
    air_gap = params.get("Air_gap", 1.0)
    dr_in = params.get("Dr_in", 90.0)
    ds_in = (L_STK / lamda) + air_gap
    dr_out = ds_in - 2.0 * air_gap
    return dr_out > dr_in


def constraint_magnet_duct_fit(params: dict) -> bool:
    """Mw > 2 * B1."""
    mw = params.get("Mw", 25.44)
    b1 = params.get("B1", 3.5)
    return mw > (2.0 * b1)


def constraint_hrib_limit(params: dict) -> bool:
    """hrib <= min(O2, 4.5, Bridge * 2)."""
    hrib = params.get("hrib", 2.4)
    o2 = params.get("O2", 6.0)
    bridge = params.get("Bridge", 1.5)
    max_hrib = min(o2, 4.5, bridge * 2.0)
    return hrib <= max_hrib


def is_feasible(params: dict) -> bool:
    """Check if all geometric constraints are satisfied."""
    return (constraint_slot_height(params) and
            constraint_slot_width_progression(params) and
            constraint_bridge_thickness(params) and
            constraint_rotor_fits_stator(params) and
            constraint_magnet_duct_fit(params) and
            constraint_hrib_limit(params))


def get_violated_constraints(params: dict) -> List[str]:
    """Return names of all violated constraints."""
    violated = []
    if not constraint_slot_height(params):
        violated.append("SlotHeight")
    if not constraint_slot_width_progression(params):
        violated.append("SlotWidthProgression")
    if not constraint_bridge_thickness(params):
        violated.append("BridgeThickness")
    if not constraint_rotor_fits_stator(params):
        violated.append("RotorFitsStator")
    if not constraint_magnet_duct_fit(params):
        violated.append("MagnetDuctFit")
    if not constraint_hrib_limit(params):
        violated.append("RibHeightLimit")
    return violated

# ---------------------------------------------------------------------------
# Fitness Score & Physics Model
# ---------------------------------------------------------------------------
def compute_score(metrics: dict, 
                  w_eff: float = 1.0, 
                  w_ripple: float = 1.0,
                  w_pwr: float = 0.5, 
                  w_cost: float = 0.05) -> float:
    """Compute weighted multi-objective fitness score."""
    eff = metrics.get("efficiency", 90.0)
    ripple = metrics.get("torque_ripple", 15.0)
    pwr = metrics.get("power_density", 0.3)
    cost = metrics.get("cost", 100.0)
    
    score = (w_eff * eff) - (w_ripple * ripple) + (w_pwr * pwr) - (w_cost * (cost / 150.0))
    return score


def physics_surrogate(ind: dict) -> Dict[str, float]:
    """Analytical physics-based surrogate model for offline evaluation."""
    air_gap = ind.get("Air_gap", 1.0)
    lamda = ind.get("Lamda", 0.9)
    mt = ind.get("Mt", 5.282)
    mw = ind.get("Mw", 25.44)
    bridge = ind.get("Bridge", 1.5)
    thet = ind.get("thet_deg", 30.0)
    
    eff_base = 94.5
    eff_gap_penalty = (air_gap - 1.0) * 1.2
    eff_mt_bonus = (mt - 5.0) * 0.8
    eff_thet_bonus = math.cos(math.radians(thet - 30.0)) * 0.5
    efficiency = float(np.clip(eff_base - eff_gap_penalty + eff_mt_bonus + eff_thet_bonus, 80.0, 98.0))
    
    ripple_base = 14.0
    ripple_gap_benefit = (air_gap - 1.0) * 3.0
    ripple_bridge_pen = (3.0 - bridge) * 1.5
    ripple_thet_pen = abs(thet - 30.0) * 0.1
    torque_ripple = float(np.clip(ripple_base - ripple_gap_benefit + ripple_bridge_pen + ripple_thet_pen, 5.0, 45.0))
    
    pwr_base = 0.32
    pwr_lamda = (lamda - 0.9) * 0.1
    pwr_mw = (mw - 25.0) * 0.005
    power_density = float(np.clip(pwr_base + pwr_lamda + pwr_mw, 0.15, 0.55))
    
    # stator_vol = (DS_OUT**2 - (L_STK/lamda)**2) * L_STK * 1e-6
    stator_vol = (math.pi / 4.0) * (DS_OUT**2 - (L_STK/lamda)**2) * L_STK * 1e-6
    
    magnet_vol = 2.0 * mt * mw * L_STK * 6.0 * 1e-6
    cost = float(np.clip(stator_vol * 15.0 + magnet_vol * 120.0, 50.0, 300.0))
    
    return {
        "efficiency": round(efficiency, 4),
        "torque_ripple": round(torque_ripple, 4),
        "power_density": round(power_density, 4),
        "cost": round(cost, 4),
    }

# ---------------------------------------------------------------------------
# Machine Learning Surrogate (GP / KNN-IDW Hybrid)
# ---------------------------------------------------------------------------
class MLSurrogate:
    """Surrogate model blending analytical physics with machine learning (GP or KNN)."""
    
    def __init__(self, param_order: List[str]):
        self.param_order = param_order
        self.scaler = MinMaxScaler() if SKLEARN_AVAILABLE else None
        self.models = {}
        self.is_trained = False
        self.X_data = []
        self.Y_data = {m: [] for m in ["efficiency", "torque_ripple", "power_density", "cost"]}
        
    def add_evaluations(self, population: List[Dict], metrics: List[Dict]):
        """Accumulate evaluation data for ML training."""
        for ind, m in zip(population, metrics):
            if "efficiency" in m and m.get("score", 0) > -1e5:
                x = [float(ind[p]) for p in self.param_order]
                self.X_data.append(x)
                for metric_key in self.Y_data:
                    self.Y_data[metric_key].append(float(m[metric_key]))
        
        if len(self.X_data) >= 15:
            self._train_models()
            
    def _train_models(self):
        """Train Gaussian Process or KNN models on accumulated data."""
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
                logging.warning(f"GP training failed: {e}. Falling back to KNN.")
                self.is_trained = False
        else:
            self.is_trained = False
            
    def predict(self, ind: Dict) -> Dict[str, float]:
        """Predict motor metrics for *ind* using hybrid ML + Physics."""
        phys = physics_surrogate(ind)
        if not self.is_trained or not self.X_data:
            return phys
            
        x = np.array([[float(ind[p]) for p in self.param_order]])
        
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
# NSGA-II Multi-Objective Sorting Utilities
# ---------------------------------------------------------------------------
def fast_non_dominated_sort(metrics_list: List[Dict]) -> List[List[int]]:
    """Deb's Fast Non-Dominated Sorting for multi-objective Pareto ranking."""
    S = [[] for _ in range(len(metrics_list))]
    n = [0] * len(metrics_list)
    fronts = [[]]

    for p in range(len(metrics_list)):
        for q in range(len(metrics_list)):
            if p == q:
                continue
            
            p_eff, q_eff = metrics_list[p]["efficiency"], metrics_list[q]["efficiency"]
            p_pwr, q_pwr = metrics_list[p]["power_density"], metrics_list[q]["power_density"]
            p_rip, q_rip = metrics_list[p]["torque_ripple"], metrics_list[q]["torque_ripple"]
            p_cost, q_cost = metrics_list[p]["cost"], metrics_list[q]["cost"]
            
            p_dominates_q = (p_eff >= q_eff and p_pwr >= q_pwr and p_rip <= q_rip and p_cost <= q_cost) and \
                            (p_eff > q_eff or p_pwr > q_pwr or p_rip < q_rip or p_cost < q_cost)
                            
            q_dominates_p = (q_eff >= p_eff and q_pwr >= p_pwr and q_rip <= p_rip and q_cost <= p_cost) and \
                            (q_eff > p_eff or q_pwr > p_pwr or q_rip < p_rip or q_cost < p_cost)

            if p_dominates_q:
                S[p].append(q)
            elif q_dominates_p:
                n[p] += 1

        if n[p] == 0:
            fronts[0].append(p)

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

    if not fronts[-1]:
        fronts.pop()

    return fronts


def crowding_distance_assignment(front: List[int], metrics_list: List[Dict]) -> Dict[int, float]:
    """Calculate crowding distance for individuals in a Pareto front."""
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
        sorted_front = sorted(front, key=lambda idx: metrics_list[idx][obj], reverse=maximize)
        distance[sorted_front[0]] = float("inf")
        distance[sorted_front[-1]] = float("inf")

        obj_min = metrics_list[sorted_front[-1]][obj]
        obj_max = metrics_list[sorted_front[0]][obj]
        
        if obj_max == obj_min:
            continue

        for i in range(1, len(sorted_front) - 1):
            if distance[sorted_front[i]] != float("inf"):
                prev_val = metrics_list[sorted_front[i - 1]][obj]
                next_val = metrics_list[sorted_front[i + 1]][obj]
                distance[sorted_front[i]] += abs(prev_val - next_val) / (obj_max - obj_min)

    return distance


def nsga2_selection(population: List[Dict], metrics: List[Dict], pop_size: int) -> List[Dict]:
    """Environmental selection for NSGA-II based on Pareto rank & crowding distance."""
    fronts = fast_non_dominated_sort(metrics)
    new_population = []
    
    for front in fronts:
        if len(new_population) + len(front) <= pop_size:
            new_population.extend([population[i] for i in front])
        else:
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
    """Binary tournament selection using Pareto rank and crowding distance."""
    rank_map = {}
    for r, f in enumerate(fronts):
        for idx in f:
            rank_map[idx] = r
            
    def select_one():
        i1, i2 = random.sample(range(len(population)), 2)
        r1, r2 = rank_map[i1], rank_map[i2]
        if r1 < r2:
            return population[i1]
        elif r2 < r1:
            return population[i2]
        else:
            d1, d2 = crowding_dist.get(i1, 0.0), crowding_dist.get(i2, 0.0)
            return population[i1] if d1 >= d2 else population[i2]

    return select_one(), select_one()

# ---------------------------------------------------------------------------
# Evolutionary Operators (Random, Repair, Crossover, Mutation)
# ---------------------------------------------------------------------------
def random_individual(bounds: dict) -> dict:
    """Create a random feasible individual."""
    for attempt in range(1000):
        individual = {}
        for name, info in bounds.items():
            steps = int(round((info["upper"] - info["lower"]) / info["step"]))
            k = random.randint(0, steps)
            val = info["lower"] + k * info["step"]
            individual[name] = snap_to_step(val, info["lower"], info["upper"], info["step"])
        
        if is_feasible(individual):
            if attempt > 100:
                logging.debug(f"Found feasible individual after {attempt + 1} attempts")
            return individual
    
    logging.warning("Could not find random feasible individual after 1000 attempts. Using baseline.")
    baseline = {
        "Dr_in": 90.0, "Air_gap": 1.0, "Lamda": 0.9, "Bridge": 1.5,
        "Hs0": 1.1899, "Hs1": 1.5, "Hs2": 18.07656, "Bs0": 2.1128,
        "Bs1": 6.90142, "Bs2": 10.88076, "O1": 5.4, "O2": 6.0,
        "B1": 3.5, "rib": 2.0, "hrib": 2.4, "Mt": 5.282,
        "Mw": 25.44156, "magDmin": 10.0, "thet_deg": 30.0,
    }
    assert is_feasible(baseline), "Hardcoded baseline is infeasible under current constraints!"
    return baseline


def repair_individual(ind: dict, bounds: dict, max_attempts: int = 5) -> dict:
    """Smart repair strategy for individuals violating geometric constraints."""
    repaired = ind.copy()
    
    for name, info in bounds.items():
        if name in repaired:
            repaired[name] = snap_to_step(repaired[name], info["lower"], info["upper"], info["step"])
            
    if is_feasible(repaired):
        return repaired
        
    for attempt in range(max_attempts):
        violated = get_violated_constraints(repaired)
        if not violated:
            return repaired
            
        if "SlotHeight" in violated:
            if "Hs2" in bounds:
                info = bounds["Hs2"]
                repaired["Hs2"] = snap_to_step(repaired["Hs2"] - info["step"], info["lower"], info["upper"], info["step"])
            elif "Hs1" in bounds:
                info = bounds["Hs1"]
                repaired["Hs1"] = snap_to_step(repaired["Hs1"] - info["step"], info["lower"], info["upper"], info["step"])

        if "SlotWidthProgression" in violated:
            if repaired.get("Bs0", 0) > repaired.get("Bs1", 0):
                repaired["Bs1"] = repaired["Bs0"]
            if repaired.get("Bs1", 0) > repaired.get("Bs2", 0):
                repaired["Bs2"] = repaired["Bs1"]

        if "BridgeThickness" in violated:
            if "B1" in bounds and "Mt" in bounds:
                max_b1 = repaired["Mt"] - 0.3
                info = bounds["B1"]
                repaired["B1"] = snap_to_step(min(repaired["B1"], max_b1), info["lower"], info["upper"], info["step"])

        if "MagnetDuctFit" in violated:
            if "Mw" in bounds and "B1" in bounds:
                min_mw = 2.0 * repaired["B1"] + 0.1
                info = bounds["Mw"]
                repaired["Mw"] = snap_to_step(max(repaired["Mw"], min_mw), info["lower"], info["upper"], info["step"])

        if "RibHeightLimit" in violated:
            if "hrib" in bounds:
                o2 = repaired.get("O2", 6.0)
                bridge = repaired.get("Bridge", 1.5)
                max_hrib = min(o2, 4.5, bridge * 2.0)
                info = bounds["hrib"]
                repaired["hrib"] = snap_to_step(min(repaired["hrib"], max_hrib), info["lower"], info["upper"], info["step"])

        if is_feasible(repaired):
            return repaired

    return repaired


def crossover(parent1: dict, parent2: dict, bounds: dict, rate: float = 0.7) -> Tuple[dict, dict]:
    """Uniform crossover operator producing two offspring."""
    if random.random() > rate:
        return parent1.copy(), parent2.copy()
        
    c1, c2 = {}, {}
    for name in bounds:
        if random.random() < 0.5:
            c1[name] = parent1[name]
            c2[name] = parent2[name]
        else:
            c1[name] = parent2[name]
            c2[name] = parent1[name]
            
    return repair_individual(c1, bounds), repair_individual(c2, bounds)


def mutate(individual: dict, bounds: dict, rate: float = 0.2, 
           sigma_scale: float = 0.15) -> dict:
    """Step offset Gaussian mutation per gene."""
    mutated = individual.copy()
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
# Diagnostics & Population Health
# ---------------------------------------------------------------------------
def check_population_diversity(population: List[Dict], 
                                threshold: float = 0.05) -> Tuple[bool, float]:
    """Check standard deviation diversity across population parameters."""
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


def detect_stagnation(scores_history: List[float], 
                      window: int = 10, 
                      threshold: float = 0.01) -> bool:
    """Detect optimization stagnation."""
    if len(scores_history) < window:
        return False
    recent = scores_history[-window:]
    improvement = recent[-1] - recent[0]
    return abs(improvement) < threshold

# ---------------------------------------------------------------------------
# Direct Ansys Maxwell & MATLAB Interop Data Exchange
# ---------------------------------------------------------------------------
def run_ansys_direct(population: List[Dict], 
                      root_dir: Path, 
                      output_dir: Path, 
                      ansys_version: str = "2023.2",
                      non_graphical: bool = False) -> bool:
    """Run direct Ansys Maxwell 3D simulations via PyAEDT or win32com ActiveX (No MATLAB required).
    
    Args:
        population: List of candidate design parameter dicts
        root_dir: Project root directory containing Matlab_Ai_Optimization.aedt
        output_dir: Output directory for CSV result files
        ansys_version: Ansys Desktop version string
        non_graphical: Whether to run Ansys Maxwell headlessly
        
    Returns:
        True if all candidate simulations executed successfully
    """
    project_path = root_dir / "Matlab_Ai_Optimization.aedt"
    if not project_path.is_file():
        raise SimulationError(f"Ansys AEDT project file not found: {project_path}")

    # Method A: PyAEDT high-level API
    if PYAEDT_AVAILABLE:
        logging.info("Connecting to Ansys Maxwell 3D via PyAEDT (non_graphical=%s)...", non_graphical)
        try:
            m3d = Maxwell3d(
                projectname=str(project_path),
                designname="Vshape_IPM",
                specified_version=ansys_version,
                non_graphical=non_graphical,
                new_desktop_session=False,
                close_on_exit=False
            )
            
            for i, ind in enumerate(population, start=1):
                logging.info("  [PyAEDT] Simulating candidate %d/%d...", i, len(population))
                for var_name in PARAM_ORDER:
                    if var_name in ind:
                        val = ind[var_name]
                        unit = "deg" if var_name == "thet_deg" else ("mm" if var_name != "Lamda" else "")
                        m3d[var_name] = f"{val}{unit}" if unit else str(val)

                m3d.delete_sampling_solutions()
                m3d.analyze_setup("Setup1")

                csv_path = output_dir / f"output_vars_iter_{i}.csv"
                m3d.post.export_report_to_csv("Setup1", "OutputVariablesTable", str(csv_path))
                
            return True
        except Exception as e:
            logging.warning(f"PyAEDT execution failed: {e}. Trying win32com ActiveX fallback...")

    # Method B: Direct Windows COM / ActiveX (1:1 MATLAB actxserver replacement)
    if PYWIN32_AVAILABLE:
        logging.info("Connecting to Ansys Electronics Desktop via win32com ActiveX...")
        try:
            oAnsoftApp = None
            prog_ids = [
                "Ansoft.ElectronicsDesktop",
                "Ansoft.ElectronicsDesktopStudent",
                "Ansoft.ElectronicsDesktopStudent.2025.2",
                "Ansoft.ElectronicsDesktop.2025.2",
            ]
            last_err = None
            for pid in prog_ids:
                try:
                    oAnsoftApp = win32com.client.Dispatch(pid)
                    logging.info(f"Connected to Ansys Electronics Desktop via COM ProgID: '{pid}'")
                    break
                except Exception as ex:
                    last_err = ex
                    
            if oAnsoftApp is None:
                raise SimulationError(f"Could not connect to any Ansys Electronics Desktop COM ProgID: {last_err}")

            oDesktop = oAnsoftApp.GetAppDesktop()
            
            try:
                oProject = oDesktop.SetActiveProject("Matlab_Ai_Optimization")
            except Exception:
                oProject = oDesktop.OpenProject(str(project_path))
                
            oDesign = oProject.SetActiveDesign("Vshape_IPM")
            oAnalysisModule = oDesign.GetModule("AnalysisSetup")
            oReportModule = oDesign.GetModule("ReportSetup")

            for i, ind in enumerate(population, start=1):
                logging.info("  [ActiveX] Simulating candidate %d/%d...", i, len(population))
                try:
                    oDesign.DeleteFullAllSolutions()
                except Exception:
                    pass
                
                for var_name in PARAM_ORDER:
                    if var_name in ind:
                        val = ind[var_name]
                        unit = "deg" if var_name == "thet_deg" else ("mm" if var_name != "Lamda" else "")
                        val_str = f"{val}{unit}" if unit else str(val)
                        oDesign.SetVariableValue(var_name, val_str)

                oAnalysisModule.ResetSetupToTimeZero("Setup1")
                oDesign.Analyze("Setup1")
                
                csv_path = output_dir / f"output_vars_iter_{i}.csv"
                oReportModule.ExportToFile("OutputVariablesTable", str(csv_path))
                
            return True
        except Exception as e:
            raise SimulationError(f"Direct Ansys COM ActiveX execution failed: {e}")

    raise SimulationError("Neither 'pyaedt' nor 'pywin32' is available in Python environment.")


def write_param_excel(population: List[Dict], path: Path):
    """Write population parameters to Excel for MATLAB ingestion."""
    rows = []
    for ind in population:
        row = {p: ind[p] for p in PARAM_ORDER}
        rows.append(row)
    df = pd.DataFrame(rows)
    df.to_excel(path, index=False)
    logging.debug("Wrote %d candidates to %s", len(population), path)


def run_matlab(root_dir: Path, matlab_exe: str = "matlab"):
    """Launch MATLAB batch simulation script."""
    logging.info("Launching MATLAB for Ansys Maxwell simulation batch...")
    
    resolved_matlab = matlab_exe
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
                logging.info(f"Auto-detected MATLAB executable: {resolved_matlab}")
                break
        if not found:
            raise SimulationError(f"MATLAB executable not found at '{matlab_exe}' and auto-detection failed.")
    
    try:
        result = subprocess.run(
            [resolved_matlab, "-wait", "-nosplash", "-nodesktop", "-r", "try, Ai_optimization; catch e, disp(e.message); pause(300); exit(1); end; exit(0);"],
            cwd=root_dir,
            capture_output=True,
            text=True,
            timeout=7200,  # 2 hour timeout
        )
        
        if result.returncode != 0:
            logging.error("MATLAB stderr: %s", result.stderr[-500:])
            raise SimulationError(f"MATLAB execution failed with code {result.returncode}")
        
        logging.debug("MATLAB stdout (last 200 chars): %s", result.stdout[-200:])
        
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
                        non_graphical: bool = False) -> List[Dict]:
    """Evaluate population with full deduplication caching.
    
    Supports modes:
        - "offline": Fast ML/Physics surrogate
        - "matlab": Invokes MATLAB + Ansys Maxwell via ActiveX
        - "ansys": Direct Python to Ansys Maxwell (PyAEDT / win32com - No MATLAB)
    """
    if eval_cache is None:
        eval_cache = _EVALUATION_CACHE

    uncached_indices = []
    uncached_population = []
    results = [None] * len(population)

    for idx, ind in enumerate(population):
        key = _make_ind_key(ind)
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
            uncached_population.append(ind)

    if not uncached_population:
        return results

    if mode == "offline":
        uncached_metrics = []
        for ind in uncached_population:
            m = ml_surrogate.predict(ind) if use_ml else physics_surrogate(ind)
            m["score"] = compute_score(
                m, 
                score_weights["eff"], 
                score_weights["ripple"],
                score_weights["pwr"], 
                score_weights["cost"]
            )
            uncached_metrics.append(m)
    elif mode == "ansys":
        # Direct Python -> Ansys Maxwell (No MATLAB)
        try:
            run_ansys_direct(uncached_population, root_dir, output_dir, non_graphical=non_graphical)
        except SimulationError as e:
            logging.error(f"Direct Ansys Simulation failed: {e}")
            sys.exit(1)
            
        uncached_metrics = _parse_csv_outputs(uncached_population, output_dir, score_weights)
    else:
        # Online mode: MATLAB + Ansys
        param_excel = input_dir / "Ai_Optimization_ParamValues.xlsx"
        write_param_excel(uncached_population, param_excel)
        
        try:
            run_matlab(root_dir, matlab_exe=matlab_exe)
        except SimulationError as e:
            logging.error(f"MATLAB Simulation failed: {e}")
            sys.exit(1)
            
        uncached_metrics = _parse_csv_outputs(uncached_population, output_dir, score_weights)

    for idx, ind, m in zip(uncached_indices, uncached_population, uncached_metrics):
        key = _make_ind_key(ind)
        eval_cache[key] = m.copy()
        results[idx] = m

    return results


def _parse_csv_outputs(uncached_population: List[Dict], output_dir: Path, score_weights: Dict[str, float]) -> List[Dict]:
    """Parse CSV simulation outputs exported by Ansys/MATLAB with robust NaN/empty handling."""
    uncached_metrics = []
    
    # Helper nội bộ: trích xuất số an toàn, tự động bỏ qua NaN/chuỗi lỗi
    def safe_extract(df: pd.DataFrame, col_name: str, fallback: float, 
                     use_window_mean: bool = True, window: int = 10) -> float:
        if col_name is None or col_name not in df.columns:
            return fallback
        # Chuyển sang numeric, ép lỗi thành NaN, rồi drop hết NaN
        series = pd.to_numeric(df[col_name], errors="coerce").dropna()
        if series.empty:
            return fallback
        if use_window_mean:
            # Lấy tối đa `window` dòng cuối cùng (pandas tự xử lý nếu len < window)
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

                # Tìm cột tự động linh hoạt hơn
                eff_col = next((c for c in df.columns if "Eff" in c), None)
                tr_col = next((c for c in df.columns if "Ripple" in c or "TorqueRip" in c), None)
                cost_col = next((c for c in df.columns if "Cost" in c or "TotCost" in c), None)
                pwr_col = next((c for c in df.columns if "Power" in c or "PwrDens" in c), None)

                # Trích xuất an toàn
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
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        return
        
    df = pd.read_csv(history_csv)
    if "efficiency" not in df.columns or "torque_ripple" not in df.columns:
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
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        return
        
    df = pd.read_csv(history_csv)
    req = ["efficiency", "torque_ripple", "cost"]
    if not all(c in df.columns for c in req):
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
    """Plot parallel coordinates for 4 optimization objectives."""
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not history_csv.is_file():
        return
        
    df = pd.read_csv(history_csv)
    cols = ["efficiency", "torque_ripple", "power_density", "cost"]
    if not all(c in df.columns for c in cols):
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
        import matplotlib.pyplot as plt
    except ImportError:
        return
        
    if not scores_history:
        return
        
    fig, ax = plt.subplots(figsize=(8, 5))
    gens = list(range(1, len(scores_history) + 1))
    ax.plot(gens, scores_history, marker="o", color="navy", linewidth=2)
    ax.set_xlabel("Generation")
    ax.set_ylabel("Best Score")
    ax.set_title("Optimization Convergence History")
    ax.grid(True, linestyle="--", alpha=0.5)
    
    out_path = output_dir / "convergence_history.png"
    fig.tight_layout()
    fig.savefig(out_path, dpi=300)
    plt.close(fig)
    logging.info("Saved Convergence History plot to %s", out_path)


def perform_sensitivity_analysis(history_csv: Path, bounds: dict, output_dir: Path):
    """Compute Spearman rank correlation between parameters and objectives."""
    if not history_csv.is_file():
        return
        
    df = pd.read_csv(history_csv)
    param_cols = [p for p in bounds if p in df.columns]
    
    results = []
    for param in param_cols:
        if df[param].nunique() > 1:
            try:
                corr_eff = df[param].corr(df["efficiency"], method="spearman")
                corr_rip = df[param].corr(df["torque_ripple"], method="spearman")
                results.append({
                    "Parameter": param,
                    "Corr_Efficiency": round(corr_eff, 4) if pd.notna(corr_eff) else 0.0,
                    "Corr_TorqueRipple": round(corr_rip, 4) if pd.notna(corr_rip) else 0.0,
                })
            except Exception as e:
                logging.debug(f"Skipping {param} in sensitivity: {e}")
    
    if results:
        res_df = pd.DataFrame(results)
        out_file = output_dir / "sensitivity_analysis.csv"
        res_df.to_csv(out_file, index=False)
        logging.info("Sensitivity analysis saved to %s", out_file)
        
        top_eff = res_df.reindex(res_df["Corr_Efficiency"].abs().sort_values(ascending=False).index).head(3)
        top_rip = res_df.reindex(res_df["Corr_TorqueRipple"].abs().sort_values(ascending=False).index).head(3)
        
        logging.info("Top 3 Efficiency Drivers: %s", 
                     ", ".join([f"{r.Parameter} ({r.Corr_Efficiency:+.3f})" 
                               for _, r in top_eff.iterrows()]))
        logging.info("Top 3 TorqueRipple Drivers: %s", 
                     ", ".join([f"{r.Parameter} ({r.Corr_TorqueRipple:+.3f})" 
                               for _, r in top_rip.iterrows()]))

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
            df = pd.read_csv(history_csv)
            n_sims = len(df)
        except Exception:
            pass
            
    content = f"""# V-Shape IPM Motor Optimization Report

**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}  
**Optimizer Version:** v5.2 (Production Remote)  
**Algorithm:** {args.algorithm.upper()}  
**Mode:** {args.mode}  
**Evaluated Candidates:** {n_sims}  
**Best Score Achieved:** `{best_score:.4f}`

---

## 🏆 Best Optimized Design Parameters

| Parameter | Optimized Value | Unit | Bounds [Min, Max] |
|---|---|---|---|
"""
    for p in bounds:
        val = best_ind.get(p, "N/A")
        unit = bounds[p]["unit"]
        lower = bounds[p]["lower"]
        upper = bounds[p]["upper"]
        content += f"| `{p}` | **{val}** | {unit} | [{lower}, {upper}] |\n"

    content += """
---

## 📊 Objective Weights & Settings
- **Efficiency Weight (`w_eff`):** `{}`
- **Torque Ripple Weight (`w_ripple`):** `{}`
- **Power Density Weight (`w_pwr`):** `{}`
- **Cost Penalty Weight (`w_cost`):** `{}`

""".format(args.w_eff, args.w_ripple, args.w_pwr, args.w_cost)

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(content)
        
    logging.info("Optimization report saved to %s", report_path)

# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------
def run_unit_tests():
    """Run built-in unit tests for critical functions."""
    logging.info("=" * 60)
    logging.info("Running Built-in Unit Tests")
    logging.info("=" * 60)
    
    global PARAM_ORDER
    if not PARAM_ORDER:
        try:
            PARAM_ORDER = list(load_bounds(Path("Ai_Optimization_Bounds.xlsx")).keys())
        except Exception:
            PARAM_ORDER = ["Dr_in", "Air_gap", "Lamda", "Bridge", "Hs0", "Hs1", "Hs2", "Bs0", "Bs1", "Bs2", "O1", "O2", "B1", "rib", "hrib", "Mt", "Mw", "magDmin", "thet_deg"]
    
    passed = 0
    failed = 0
    
    # Test 1: Constraint functions
    logging.info("\n[Test 1] Constraint Functions")
    feasible = {
        "Dr_in": 90.0, "Air_gap": 1.0, "Lamda": 0.9, "Bridge": 1.5,
        "Hs0": 1.1899, "Hs1": 1.5, "Hs2": 18.07656, "Bs0": 2.1128,
        "Bs1": 6.90142, "Bs2": 10.88076, "O1": 5.4, "O2": 6.0,
        "B1": 3.5, "rib": 2.0, "hrib": 2.4, "Mt": 5.282,
        "Mw": 25.44156, "magDmin": 10.0, "thet_deg": 30.0,
    }
    
    infeasible_slot = feasible.copy()
    infeasible_slot["Hs2"] = 50.0
    
    infeasible_bridge = feasible.copy()
    infeasible_bridge["B1"] = 5.5
    
    infeasible_hrib = feasible.copy()
    infeasible_hrib["hrib"] = 5.0
    
    infeasible_width = feasible.copy()
    infeasible_width["Bs0"] = 8.0
    infeasible_width["Bs1"] = 4.0

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
            logging.info(f"  [PASS] {name}: {'FEASIBLE' if result else 'INFEASIBLE'} (correct)")
            passed += 1
        else:
            logging.error(f"  [FAIL] {name}: expected {expected}, got {result}")
            failed += 1
    
    # Test 2: NSGA-II Dominance
    logging.info("\n[Test 2] NSGA-II Dominance Sorting")
    m1 = {"efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0}
    m2 = {"efficiency": 94.0, "torque_ripple": 25.0, "power_density": 0.30, "cost": 150.0}
    m3 = {"efficiency": 96.0, "torque_ripple": 30.0, "power_density": 0.33, "cost": 145.0}
    fronts = fast_non_dominated_sort([m1, m2, m3])
    
    if len(fronts) > 0 and len(fronts[0]) >= 2:
        logging.info(f"  [PASS] Dominance sorting: {len(fronts)} fronts, front0 size={len(fronts[0])}")
        passed += 1
    else:
        logging.error("  [FAIL] Dominance sorting failed")
        failed += 1
    
    # Test 3: Crowding Distance
    logging.info("\n[Test 3] Crowding Distance Assignment")
    metrics_list = [m1, m2, m3, {"efficiency": 93.0, "torque_ripple": 18.0, "power_density": 0.32, "cost": 155.0}]
    dist = crowding_distance_assignment([0, 1, 2, 3], metrics_list)
    if dist[0] == float("inf") and dist[3] == float("inf"):
        logging.info("  [PASS] Boundary points have infinite distance")
        passed += 1
    else:
        logging.error("  [FAIL] Boundary distance check failed")
        failed += 1
    
    # Test 4: Repair Mechanism
    logging.info("\n[Test 4] Repair Mechanism")
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
    if all(bounds_test[k]["lower"] <= repaired[k] <= bounds_test[k]["upper"] for k in bounds_test):
        logging.info("  [PASS] All repaired values within bounds")
        passed += 1
    else:
        logging.error("  [FAIL] Repair failed to bring values within bounds")
        failed += 1
    
    # Test 5: Score Computation
    logging.info("\n[Test 5] Score Computation")
    m_test = {"efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0}
    score = compute_score(m_test)
    exp_score = 95.0 - 20.0 + 0.5 * 0.35 - 0.05 * (140.0 / 150.0)
    if abs(score - exp_score) < 0.01:
        logging.info(f"  [PASS] Score = {score:.4f} (expected {exp_score:.4f})")
        passed += 1
    else:
        logging.error(f"  [FAIL] Score = {score:.4f} (expected {exp_score:.4f})")
        failed += 1

    # Test 6: Log History Export
    logging.info("\n[Test 6] Log History Export")
    test_log_path = Path("test_log_history.csv")
    if test_log_path.exists():
        test_log_path.unlink()
    log_history_entry(test_log_path, {
        "timestamp": datetime.now().isoformat(), "generation": 1, "individual_id": "Test_1",
        "operator": "UnitTesting", "is_feasible": True, "violated_constraints": [],
        "efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0,
        "score": 75.0, "pareto_rank": 1, "crowding_distance": 0.5, "mode": "offline",
        "Dr_in": 90.0, "Air_gap": 1.0, "Lamda": 0.9
    }, ["Dr_in", "Air_gap", "Lamda"])
    if test_log_path.exists() and test_log_path.stat().st_size > 0:
        logging.info("  [PASS] log_history.csv created and written successfully")
        passed += 1
        test_log_path.unlink()
    else:
        logging.error("  [FAIL] log_history.csv export failed")
        failed += 1

    # Test 7: SLOT_HEIGHT_MARGIN Constant Value
    logging.info("\n[Test 7] SLOT_HEIGHT_MARGIN Constant Value")
    assert SLOT_HEIGHT_MARGIN == 12.25, f"SLOT_HEIGHT_MARGIN should be 12.25, got {SLOT_HEIGHT_MARGIN}"
    logging.info(f"  [PASS] SLOT_HEIGHT_MARGIN = {SLOT_HEIGHT_MARGIN}")
    passed += 1

    # Test 8: load_warm_start() padding logic
    logging.info("\n[Test 8] Warm-start Loading & Padding")
    import tempfile
    _test_bounds = bounds_test
    _csv_rows = [random_individual(_test_bounds) for _ in range(3)]
    _tmp = Path(tempfile.mktemp(suffix=".csv"))
    pd.DataFrame(_csv_rows).to_csv(_tmp, index=False)
    _result = load_warm_start(_tmp, _test_bounds, pop_size=5)
    assert len(_result) == 5, f"Expected 5 individuals, got {len(_result)}"
    assert all(is_feasible(ind) for ind in _result), "All warm-start individuals must be feasible"
    _tmp.unlink()
    logging.info("  [PASS] load_warm_start padding logic")
    passed += 1

    # Test 9: NSGA-II Evaluation Caching Efficiency
    logging.info("\n[Test 9] NSGA-II Evaluation Caching Efficiency")
    clear_evaluation_cache()
    
    test_eval_cache = {}
    test_bounds_full = load_bounds(Path("Ai_Optimization_Bounds.xlsx")) if Path("Ai_Optimization_Bounds.xlsx").is_file() else bounds_test
    test_ml_mod = MLSurrogate(list(test_bounds_full.keys()))
    test_sc_w = {"eff": 1.0, "ripple": 1.0, "pwr": 0.5, "cost": 0.05}
    
    t_pop = [random_individual(test_bounds_full) for _ in range(6)]
    
    for g in range(1, 4):
        m_pop = evaluate_population(t_pop, Path("."), Path("."), Path("."), "offline", test_sc_w, test_ml_mod, use_ml=False, eval_cache=test_eval_cache)
        t_fronts = fast_non_dominated_sort(m_pop)
        t_cdist = {}
        for f in t_fronts:
            t_cdist.update(crowding_distance_assignment(f, m_pop))
            
        t_offspring = []
        while len(t_offspring) < 6:
            p1, p2 = nsga2_tournament_selection(t_pop, m_pop, t_fronts, t_cdist)
            c1, c2 = crossover(p1, p2, test_bounds_full, 0.7)
            c1 = mutate(c1, test_bounds_full, 0.2)
            c2 = mutate(c2, test_bounds_full, 0.2)
            t_offspring.append(c1)
            if len(t_offspring) < 6:
                t_offspring.append(c2)
                
        t_comb_pop = t_pop + t_offspring
        t_comb_m = evaluate_population(t_comb_pop, Path("."), Path("."), Path("."), "offline", test_sc_w, test_ml_mod, use_ml=False, eval_cache=test_eval_cache)
        t_pop = nsga2_selection(t_comb_pop, t_comb_m, 6)

    cache_size = len(test_eval_cache)
    if cache_size <= 6 * 4:
        logging.info(f"  [PASS] Caching efficiency: {cache_size} total unique evaluations across 3 generations (uncached would be 54)")
        passed += 1
    else:
        logging.error(f"  [FAIL] Evaluation caching failed: {cache_size} evaluations exceeds threshold")
        failed += 1

    # Test 10: Direct Ansys Integration Capability (PyAEDT / win32com)
    logging.info("\n[Test 10] Direct Ansys Integration Capability (PyAEDT / win32com)")
    if PYAEDT_AVAILABLE:
        logging.info("  [PASS] PyAEDT library is available")
        passed += 1
    elif PYWIN32_AVAILABLE:
        logging.info("  [PASS] win32com ActiveX fallback is available (pywin32 installed)")
        passed += 1
    else:
        logging.info("  [SKIP] Neither PyAEDT nor win32com is installed for direct Ansys execution")

    # Summary
    total = passed + failed
    logging.info(f"\n{'=' * 60}")
    logging.info(f"Unit Test Results: {passed}/{total} passed, {failed}/{total} failed")
    logging.info(f"{'=' * 60}")
    
    return failed == 0

# ---------------------------------------------------------------------------
# Checkpoint utilities
# ---------------------------------------------------------------------------
STATE_FILE = "optimizer_state.pkl"

def save_state(state: dict, path: Path):
    """Save optimizer state to pickle file."""
    with open(path, "wb") as f:
        pickle.dump(state, f)
    logging.debug("Checkpoint saved to %s", path)


def load_state(path: Path) -> dict:
    """Load optimizer state from pickle file."""
    with open(path, "rb") as f:
        state = pickle.load(f)
    logging.info("Checkpoint loaded from %s (generation %d)", path, state.get("generation", 0))
    return state


def log_history_entry(history_log_path: Path, entry: dict, param_order: List[str]):
    """Log structured individual activity details to log_history.csv continuously."""
    header = [
        "timestamp", "generation", "individual_id", "operator", "is_feasible",
        "violated_constraints", "repair_status", "efficiency", "torque_ripple",
        "power_density", "cost", "score", "pareto_rank", "crowding_distance", "mode"
    ] + param_order

    violated = entry.get("violated_constraints", [])
    if isinstance(violated, list):
        violated_str = "|".join(violated) if violated else "None"
    else:
        violated_str = str(violated)

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


def load_warm_start(csv_path: Path, bounds: dict, pop_size: int) -> List[dict]:
    """Load and validate prior designs from CSV to seed the initial population."""
    if not csv_path.is_file():
        raise FileNotFoundError(f"Warm-start file not found: {csv_path}")

    df = pd.read_csv(csv_path)

    matched = [p for p in PARAM_ORDER if p in df.columns]
    if not matched:
        raise ValueError(
            f"Warm-start CSV has no recognisable parameter columns. "
            f"Expected one of: {PARAM_ORDER}. Found: {list(df.columns)}"
        )
    missing_cols = [p for p in PARAM_ORDER if p not in df.columns]
    if missing_cols:
        logging.warning("Warm-start CSV missing columns (will use bounds midpoint): %s", missing_cols)

    valid = []
    n_discarded = 0
    for _, row in df.iterrows():
        ind = {}
        for name, info in bounds.items():
            if name in row:
                ind[name] = snap_to_step(float(row[name]), info["lower"], info["upper"], info["step"])
            else:
                mid = (info["lower"] + info["upper"]) / 2.0
                ind[name] = snap_to_step(mid, info["lower"], info["upper"], info["step"])

        if is_feasible(ind):
            valid.append(ind)
        else:
            n_discarded += 1
            logging.warning(
                "Warm-start: discarded infeasible row %d. Violated: %s",
                len(valid) + n_discarded,
                get_violated_constraints(ind)
            )

    from_csv = valid[:pop_size]
    n_random = max(0, pop_size - len(from_csv))
    padded = [random_individual(bounds) for _ in range(n_random)]

    logging.info(
        "Warm-start: loaded %d individuals from '%s' (%d discarded), padded with %d random.",
        len(from_csv), csv_path, n_discarded, n_random
    )
    return from_csv + padded

# ---------------------------------------------------------------------------
# Main Driver
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="V-Shape IPM Motor AI Optimizer v5.2",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Direct Python -> Ansys Maxwell (No MATLAB required!):
  python motor_optimizer_ver5.2_remote.py --mode ansys --generations 10
  
  # Quick test with offline surrogate:
  python motor_optimizer_ver5.2_remote.py --mode offline --generations 5
  
  # Run built-in unit tests:
  python motor_optimizer_ver5.2_remote.py --test
        """
    )
    
    # Core optimization parameters
    parser.add_argument("--pop-size", type=int, default=8, help="Population size (default: 8)")
    parser.add_argument("--generations", type=int, default=10, help="Max generations (default: 10)")
    parser.add_argument("--crossover", type=float, default=0.7, help="Crossover probability (default: 0.7)")
    parser.add_argument("--mutation", type=float, default=0.2, help="Mutation rate per gene (default: 0.2)")
    parser.add_argument("--mode", choices=["offline", "matlab", "ansys"], default="offline", 
                       help="Evaluation mode: 'offline' (surrogate), 'ansys' (direct PyAEDT/COM, no MATLAB), 'matlab' (MATLAB bridge)")
    parser.add_argument("--algorithm", choices=["ga", "nsga2"], default="ga",
                       help="Optimization engine (default: ga)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--matlab-exe", type=str, default=r"C:\MATLAB\R2023b\bin\matlab.exe", help="Path to MATLAB executable")
    parser.add_argument("--ansys-version", type=str, default="2023.2", help="Ansys Desktop version string for PyAEDT")
    parser.add_argument("--non-graphical", action="store_true", help="Run Ansys Maxwell in background headless mode")
    
    # Score weights
    parser.add_argument("--w-eff", type=float, default=1.0, help="Efficiency weight (default: 1.0)")
    parser.add_argument("--w-ripple", type=float, default=1.0, help="Torque Ripple weight (default: 1.0)")
    parser.add_argument("--w-pwr", type=float, default=0.5, help="Power Density weight (default: 0.5)")
    parser.add_argument("--w-cost", type=float, default=0.05, help="Cost penalty weight (default: 0.05)")
    
    # Early stopping
    parser.add_argument("--patience", type=int, default=20, help="Early stopping patience (default: 20)")
    parser.add_argument("--min-delta", type=float, default=0.01, help="Min improvement to reset patience (default: 0.01)")
    
    # Feature flags
    parser.add_argument("--no-ml", action="store_true", help="Disable ML surrogate blending")
    parser.add_argument("--plot-pareto", action="store_true", help="Generate 2D Pareto front plot")
    parser.add_argument("--plot-all", action="store_true", help="Generate all visualizations (Pareto, 3D, parallel coords, convergence)")
    parser.add_argument("--sensitivity", action="store_true", help="Perform sensitivity analysis")
    parser.add_argument(
        "--warm-start", default=None, metavar="PATH",
        help="CSV file with prior design parameters to seed initial population."
    )
    parser.add_argument("--test", action="store_true", help="Run built-in unit tests and exit")
    parser.add_argument("--no-report", action="store_true", help="Skip report generation")
    
    args = parser.parse_args()
    
    # Handle --test flag
    if args.test:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s",
                          handlers=[logging.StreamHandler(sys.stdout)])
        success = run_unit_tests()
        sys.exit(0 if success else 1)
    
    # Handle --plot-all (enables all plots)
    if args.plot_all:
        args.plot_pareto = True
        args.sensitivity = True
    
    # Setup paths
    script_dir = Path(__file__).resolve().parent
    root_dir = script_dir.parent if script_dir.name == "Python_code" else script_dir
    input_dir = root_dir
    output_dir = root_dir
    
    input_dir.mkdir(exist_ok=True)
    output_dir.mkdir(exist_ok=True)
    
    # Setup logging
    log_file = output_dir / "optimizer.log"
    log_history_file = output_dir / "log_history.log"
    setup_logger(str(log_file))
    
    logging.info("=" * 60)
    logging.info("V-Shape IPM Motor Optimizer v5.2")
    logging.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)
    
    clear_evaluation_cache()
    
    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        logging.info("Random seed: %d", args.seed)
    else:
        logging.info("Random seed: not set (results will vary between runs)")
    
    bounds_path = input_dir / "Ai_Optimization_Bounds.xlsx"
    try:
        bounds = load_bounds(bounds_path)
    except (FileNotFoundError, ValueError) as e:
        logging.error("Failed to load bounds: %s", e)
        sys.exit(1)
    
    global PARAM_ORDER
    PARAM_ORDER = list(bounds.keys())
    logging.info("Loaded %d design parameters from bounds file", len(PARAM_ORDER))
    
    ml_surrogate = MLSurrogate(PARAM_ORDER)
    use_ml = not args.no_ml
    if not SKLEARN_AVAILABLE and use_ml:
        logging.warning("scikit-learn not available. ML surrogate will use KNN-IDW (install sklearn for GP).")
    
    state_path = output_dir / STATE_FILE
    scores_history = []
    
    resuming = args.resume and state_path.is_file()
    if resuming:
        state = load_state(state_path)
        population = state["population"]
        best_ind = state.get("best_individual")
        best_score = state.get("best_score", -float("inf"))
        start_gen = state["generation"] + 1
        scores_history = state.get("scores_history", [])
        logging.info("Resuming from generation %d (best score: %.4f)", state["generation"], best_score)
    else:
        if args.resume:
            logging.warning("--resume flag passed, but checkpoint file '%s' not found. Initializing fresh run.", state_path)
        if args.warm_start:
            try:
                population = load_warm_start(Path(args.warm_start), bounds, args.pop_size)
            except (FileNotFoundError, ValueError) as e:
                logging.error("Failed to load warm-start data: %s", e)
                sys.exit(1)
        else:
            population = [random_individual(bounds) for _ in range(args.pop_size)]
        best_ind = None
        best_score = -float("inf")
        start_gen = 1
        logging.info("Initialized fresh population of %d individuals", args.pop_size)
    
    score_weights = {
        "eff": args.w_eff, "ripple": args.w_ripple,
        "pwr": args.w_pwr, "cost": args.w_cost,
    }
    
    history_path = output_dir / "simulation_history.csv"
    log_history_csv_path = output_dir / "log_history.csv"
    if not resuming:
        try:
            history_path.unlink(missing_ok=True)
            log_history_csv_path.unlink(missing_ok=True)
            log_history_file.unlink(missing_ok=True)
        except Exception:
            pass
    history_header_written = history_path.is_file()
    
    no_improve_count = 0
    best_gen = start_gen - 1
    
    logging.info("Configuration: Algorithm=%s | Mode=%s | ML=%s | PopSize=%d | Gens=%d",
                args.algorithm.upper(), args.mode, use_ml, args.pop_size, args.generations)
    
    # ======================================================================
    # Main Evolutionary Loop
    # ======================================================================
    for gen in range(start_gen, args.generations + 1):
        logging.info("--- Generation %d / %d ---", gen, args.generations)
        
        metrics = evaluate_population(
            population, root_dir, input_dir, output_dir,
            args.mode, score_weights, ml_surrogate, use_ml,
            matlab_exe=args.matlab_exe, non_graphical=args.non_graphical
        )
        
        ml_surrogate.add_evaluations(population, metrics)
        
        scores = [m["score"] for m in metrics]
        gen_best_idx = int(np.argmax(scores))
        gen_best_score = scores[gen_best_idx]
        gen_best_metrics = metrics[gen_best_idx]
        
        if gen_best_score > best_score + args.min_delta:
            best_score = gen_best_score
            best_ind = population[gen_best_idx].copy()
            best_gen = gen
            no_improve_count = 0
            logging.info("  [NEW BEST] | Score: %.4f | Gen: %d", best_score, gen)
        else:
            no_improve_count += 1
        
        scores_history.append(best_score)
        
        logging.info(
            "Gen %d | Best=%.4f | Eff=%.1f%% | TR=%.1f%% | PD=%.3f | Cost=$%.0f | Stag=%d/%d",
            gen, gen_best_metrics["score"], gen_best_metrics["efficiency"],
            gen_best_metrics["torque_ripple"], gen_best_metrics["power_density"],
            gen_best_metrics["cost"], no_improve_count, args.patience,
        )
        
        is_diverse, div_score = check_population_diversity(population)
        if not is_diverse and gen > 5:
            logging.warning("  ⚠ Low population diversity (score: %.3f). Consider increasing mutation rate.", div_score)
        
        stagnation_window = max(5, args.patience // 2)
        if detect_stagnation(scores_history, window=stagnation_window, threshold=args.min_delta):
            logging.warning("  ⚠ Optimization may be stagnating. Best score unchanged for %d generations.", 
                          stagnation_window)
        
        fronts = fast_non_dominated_sort(metrics) if args.algorithm == "nsga2" else []
        rank_lookup = {}
        for r, f in enumerate(fronts):
            for idx_f in f:
                rank_lookup[idx_f] = r
        
        crowding_dist = {}
        if args.algorithm == "nsga2":
            for f in fronts:
                crowding_dist.update(crowding_distance_assignment(f, metrics))
        
        hist_rows = []
        for idx, (ind, m) in enumerate(zip(population, metrics)):
            row = {**ind, "generation": gen, "score": m["score"],
                   "efficiency": m["efficiency"], "torque_ripple": m["torque_ripple"],
                   "power_density": m["power_density"], "cost": m["cost"]}
            hist_rows.append(row)
            
            is_elitist = ind.get("_is_elitist", False) or (best_ind is not None and id(ind) == id(best_ind))
            log_history_entry(log_history_csv_path, {
                "timestamp": datetime.now().isoformat(),
                "generation": gen,
                "individual_id": f"Gen{gen}_Ind{idx+1}",
                "operator": "Elitism" if is_elitist else "Evolution",
                "is_feasible": is_feasible(ind),
                "violated_constraints": get_violated_constraints(ind),
                "repair_status": "NotNeeded" if is_feasible(ind) else "Repaired",
                "efficiency": m["efficiency"],
                "torque_ripple": m["torque_ripple"],
                "power_density": m["power_density"],
                "cost": m["cost"],
                "score": m["score"],
                "pareto_rank": rank_lookup.get(idx, 0),
                "crowding_distance": crowding_dist.get(idx, 0.0),
                "mode": args.mode,
                **ind
            }, PARAM_ORDER)
        
        pd.DataFrame(hist_rows).to_csv(
            history_path, mode="a", header=not history_header_written, index=False
        )
        history_header_written = True
        
        if no_improve_count >= args.patience:
            logging.info("🛑 Early stopping: No improvement for %d generations.", args.patience)
            break
        
        if args.algorithm == "nsga2":
            fronts = fast_non_dominated_sort(metrics)
            crowding_dist = {}
            for front in fronts:
                front_dist = crowding_distance_assignment(front, metrics)
                crowding_dist.update(front_dist)
            
            offspring = []
            while len(offspring) < args.pop_size:
                p1, p2 = nsga2_tournament_selection(population, metrics, fronts, crowding_dist)
                c1, c2 = crossover(p1, p2, bounds, args.crossover)
                c1 = mutate(c1, bounds, args.mutation)
                c2 = mutate(c2, bounds, args.mutation)
                offspring.append(c1)
                if len(offspring) < args.pop_size:
                    offspring.append(c2)
            
            combined_pop = population + offspring
            combined_metrics = evaluate_population(
                combined_pop, root_dir, input_dir, output_dir,
                args.mode, score_weights, ml_surrogate, use_ml,
                matlab_exe=args.matlab_exe, non_graphical=args.non_graphical
            )
            population = nsga2_selection(combined_pop, combined_metrics, args.pop_size)
            
        else:
            def tournament(pop, sc):
                i1, i2 = random.sample(range(len(pop)), 2)
                return pop[i1] if sc[i1] > sc[i2] else pop[i2]
            
            next_pop = []
            if best_ind:
                elitist_child = best_ind.copy()
                elitist_child["_is_elitist"] = True
                next_pop.append(elitist_child)
            elif population:
                elitist_child = population[gen_best_idx].copy()
                elitist_child["_is_elitist"] = True
                next_pop.append(elitist_child)
            
            while len(next_pop) < args.pop_size:
                p1 = tournament(population, scores)
                p2 = tournament(population, scores)
                c1, c2 = crossover(p1, p2, bounds, args.crossover)
                c1 = mutate(c1, bounds, args.mutation)
                c2 = mutate(c2, bounds, args.mutation)
                next_pop.append(c1)
                if len(next_pop) < args.pop_size:
                    next_pop.append(c2)
            
            population = next_pop
        
        save_state({
            "generation": gen,
            "population": population,
            "best_individual": best_ind,
            "best_score": best_score,
            "scores_history": scores_history,
        }, state_path)
    
    logging.info("\n" + "=" * 60)
    logging.info("OPTIMIZATION COMPLETE")
    logging.info("=" * 60)
    logging.info("Best Score: %.4f (found at generation %d)", best_score, best_gen)
    logging.info("\nBest Design Parameters:")
    if best_ind:
        for name in PARAM_ORDER:
            logging.info("  %-12s = %s %s", name, best_ind.get(name, "N/A"), bounds[name]["unit"])
        
        best_df = pd.DataFrame([{k: v for k, v in best_ind.items() if not str(k).startswith('_')}])
        out_path = output_dir / "best_optimized_design_v5.2.csv"
        best_df.to_csv(out_path, index=False)
        logging.info("\nBest design saved to %s", out_path)
        
        if not args.no_report:
            generate_report(history_path, best_ind, best_score, bounds, output_dir, args)
    
    if args.plot_pareto or args.plot_all:
        plot_pareto_front(history_path, output_dir)
    
    if args.plot_all:
        plot_3d_pareto(history_path, output_dir)
        plot_parallel_coordinates(history_path, output_dir)
        plot_convergence(scores_history, output_dir)
    
    if args.sensitivity:
        perform_sensitivity_analysis(history_path, bounds, output_dir)
    
    logging.info("\nOptimization finished successfully. All outputs in: %s", output_dir)


if __name__ == "__main__":
    main()
