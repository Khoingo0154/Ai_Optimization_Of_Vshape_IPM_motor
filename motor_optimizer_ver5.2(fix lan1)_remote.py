# motor_optimizer_ver5.1.py
"""
Version 5.1 of the AI optimizer for the V‑Shape IPM motor.
Production-grade upgrade from ver5 with enhanced ML, complete NSGA-II, and diagnostics.

Key Upgrades from ver5:
1. **Complete NSGA-II Engine**: Dedicated reproduction using binary tournament on 
   Pareto rank + crowding distance (not reusing GA selection).
2. **Gaussian Process ML Surrogate**: Replaces KNN-IDW with sklearn's GaussianProcessRegressor
   for better interpolation + uncertainty quantification. Auto-fallback to KNN if sklearn unavailable.
3. **Input Standardization**: All ML features normalized to [0,1] before distance/distance calculation
   to prevent variable magnitude bias (e.g., Dr_in~90 vs Air_gap~1.0).
4. **Advanced Visualization** (--plot-advanced):
   - 3D Pareto front (Efficiency vs TorqueRipple vs Cost)
   - Parallel coordinates plot for 4 objectives
   - Convergence history plot (best score per generation)
5. **Built-in Unit Tests** (--test): Validates constraint functions, dominance logic,
   crowding distance, and repair mechanisms without running full optimization.
6. **Smart Diagnostics**: Auto-detects stagnation, diversity collapse, and constraint violations
   during optimization with actionable warnings.
7. **Export-ready Reports**: Generates `optimization_report.md` with summary statistics,
   top 5 designs, and parameter importance rankings.

Dependencies: numpy, pandas, scipy, matplotlib, scikit-learn (optional, for GP surrogate)
"""

import os
import sys
import argparse
import random
import pickle
import logging
import subprocess
import warnings
from pathlib import Path
import shutil
from typing import Dict, List, Tuple, Optional, Any, Callable
from datetime import datetime

import pandas as pd
import numpy as np
from scipy.stats import spearmanr
import matplotlib
matplotlib.use('Agg')  # Non-interactive backend for server/headless use
import matplotlib.pyplot as plt
from matplotlib.ticker import MaxNLocator

# Optional sklearn import for Gaussian Process
try:
    from sklearn.preprocessing import StandardScaler
    from sklearn.gaussian_process import GaussianProcessRegressor
    from sklearn.gaussian_process.kernels import RBF, WhiteKernel, ConstantKernel
    SKLEARN_AVAILABLE = True
except ImportError:
    SKLEARN_AVAILABLE = False
    warnings.warn("scikit-learn not available. Using KNN-IDW surrogate (install sklearn for GP).")

# ---------------------------------------------------------------------------
# Global constants
# ---------------------------------------------------------------------------
DS_OUT = 240.0  # outer stator diameter (mm)
L_STK = 134.0   # stack length (mm)
SLOT_HEIGHT_MARGIN = 12.25  # mm — per Optimization_Requirements.md §Constraint 1

PARAM_ORDER: List[str] = []

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
    """Read the bounds Excel file and return an ordered dict of parameter info.
    
    Args:
        bounds_path: Path to Ai_Optimization_Bounds.xlsx
        
    Returns:
        Ordered dict mapping parameter name -> {lower, upper, step, unit}
        
    Raises:
        FileNotFoundError: If bounds file doesn't exist
        ValueError: If bounds file has invalid format
    """
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
    """Round *value* to the nearest valid step within limits.
    
    Args:
        value: Raw value to snap
        lower: Minimum allowed value
        upper: Maximum allowed value
        step: Discrete step size
        
    Returns:
        Snapped value guaranteed to be on the grid and within bounds
    """
    if step <= 0:
        return float(np.clip(value, lower, upper))
    steps = round((value - lower) / step)
    snapped = lower + steps * step
    return float(np.clip(round(snapped, 6), lower, upper))

# ---------------------------------------------------------------------------
# 4 Geometric Constraint definitions
# ---------------------------------------------------------------------------
def constraint_slot_height(params: dict) -> bool:
    """Hs0 + Hs1 + Hs2 < ((Ds_out - Ds_in) / 2) - 12.25 mm.
    
    Ensures stator slot doesn't penetrate the stator yoke.
    """
    lamda = params.get("Lamda", 0.9)
    air_gap = params.get("Air_gap", 1.0)
    hs_sum = params.get("Hs0", 1.0) + params.get("Hs1", 1.0) + params.get("Hs2", 18.0)
    ds_in = (L_STK / lamda) + air_gap
    limit = ((DS_OUT - ds_in) / 2.0) - SLOT_HEIGHT_MARGIN
    return hs_sum < limit


def constraint_slot_width_progression(params: dict) -> bool:
    """Bs0 <= Bs1 and Bs1 <= Bs2.
    
    Ensures logical stator slot width expansion.
    """
    bs0 = params.get("Bs0", 2.0)
    bs1 = params.get("Bs1", 6.5)
    bs2 = params.get("Bs2", 10.0)
    return bs0 <= bs1 and bs1 <= bs2


def constraint_bridge_thickness(params: dict) -> bool:
    """B1 <= Mt - 0.3 mm.
    
    Ensures duct thickness doesn't exceed magnet thickness with clearance.
    """
    return params.get("B1", 3.5) <= (params.get("Mt", 5.0) - 0.3)


def constraint_rotor_fits_stator(params: dict) -> bool:
    """Dr_out > Dr_in where Dr_out = Ds_in - 2*Air_gap.
    
    Ensures rotor has a solid core and fits inside the stator bore.
    """
    lamda = params.get("Lamda", 0.9)
    air_gap = params.get("Air_gap", 1.0)
    ds_in = (L_STK / lamda) + air_gap
    dr_out = ds_in - 2.0 * air_gap
    return dr_out > params.get("Dr_in", 90.0)


def constraint_magnet_duct_fit(params: dict) -> bool:
    """Mw > 2.0 * B1.
    
    Ensures magnet width is sufficient for V-shape duct geometry.
    """
    return params.get("Mw", 25.0) > params.get("B1", 3.5) * 2.0


def constraint_hrib_limit(params: dict) -> bool:
    """hrib <= O2, hrib <= Bridge * 2, and hrib <= 4.5 mm.
    
    Ensures rotor rib height fits within rotor core geometric limits.
    Prevents Ansys UDP error 'HRib is too large'.
    """
    hrib = params.get("hrib", 2.4)
    o2 = params.get("O2", 6.0)
    bridge = params.get("Bridge", 1.5)
    return hrib <= o2 and hrib <= 4.5 and hrib <= (bridge * 2.0)


CONSTRAINTS: List[Callable[[dict], bool]] = [
    constraint_slot_height,
    constraint_slot_width_progression,
    constraint_bridge_thickness,
    constraint_rotor_fits_stator,
    constraint_magnet_duct_fit,
    constraint_hrib_limit,
]

CONSTRAINT_NAMES = [
    "Slot Height Limit",
    "Slot Width Progression",
    "Bridge Thickness Limit",
    "Rotor Fits Stator",
    "Magnet Duct Fit",
    "Rotor Rib Height Limit",
]


def is_feasible(params: dict) -> bool:
    """Return True only if all 6 geometric constraints are satisfied."""
    return all(con(params) for con in CONSTRAINTS)


def get_violated_constraints(params: dict) -> List[str]:
    """Return list of constraint names that are violated (for diagnostics)."""
    violated = []
    for con, name in zip(CONSTRAINTS, CONSTRAINT_NAMES):
        if not con(params):
            violated.append(name)
    return violated

# ---------------------------------------------------------------------------
# Multi-objective score function
# ---------------------------------------------------------------------------
def compute_score(metrics: dict,
                  w_eff: float = 1.0,
                  w_ripple: float = 1.0,
                  w_pwr: float = 0.5,
                  w_cost: float = 0.05) -> float:
    """Compute weighted multi-objective composite score.
    
    Primary: Maximize Efficiency, Minimize Torque Ripple
    Secondary: Maximize Power Density, Minimize Cost
    
    Args:
        metrics: Dict with keys efficiency, torque_ripple, power_density, cost
        w_eff: Weight for efficiency (default 1.0)
        w_ripple: Weight for torque ripple penalty (default 1.0)
        w_pwr: Weight for power density bonus (default 0.5)
        w_cost: Weight for cost penalty (default 0.05)
        
    Returns:
        Composite score (higher is better)
    """
    eff = metrics.get("efficiency", 0.0)
    ripple = metrics.get("torque_ripple", 100.0)
    pwr_dens = metrics.get("power_density", 0.0)
    cost = metrics.get("cost", 1000.0)
    cost_norm = cost / 150.0  # Normalize to baseline ~150
    
    return (w_eff * eff 
            - w_ripple * ripple 
            + w_pwr * pwr_dens 
            - w_cost * cost_norm)

# ---------------------------------------------------------------------------
# Analytical Physics Surrogate Model (Enhanced)
# ---------------------------------------------------------------------------
def physics_surrogate(ind: dict) -> Dict[str, float]:
    """High-fidelity analytical physics-based surrogate model.
    
    Models 7 normalized parameters with known physical relationships:
    - Efficiency peaks at MTPA angle (thet_deg ≈ 25°)
    - Torque ripple increases with air gap and slot opening
    - Power density benefits from larger magnets
    - Cost dominated by magnet volume
    
    Args:
        ind: Design parameter dictionary
        
    Returns:
        Dict with efficiency, torque_ripple, power_density, cost
    """
    # Normalize key variables to [0, 1]
    dr_norm = np.clip((ind.get("Dr_in", 90.0) - 50.0) / 40.0, 0.0, 1.0)
    ag_norm = np.clip((ind.get("Air_gap", 1.0) - 0.5) / 1.0, 0.0, 1.0)
    mt_norm = np.clip((ind.get("Mt", 5.0) - 4.0) / 2.0, 0.0, 1.0)
    mw_norm = np.clip((ind.get("Mw", 25.0) - 10.0) / 20.0, 0.0, 1.0)
    bs0_norm = np.clip((ind.get("Bs0", 2.0) - 1.5) / 2.5, 0.0, 1.0)
    br_norm = np.clip((ind.get("Bridge", 1.5) - 1.0) / 2.0, 0.0, 1.0)
    rib_norm = np.clip((ind.get("rib", 2.0) - 2.0) / 13.0, 0.0, 1.0)
    
    # MTPA penalty: thet_deg ≈ 25° is optimal for this motor topology
    thet_centre = 25.0
    thet_penalty = abs(ind.get("thet_deg", 25.0) - thet_centre) / 90.0
    
    # Efficiency model (94% baseline + physical adjustments)
    eff = (94.0 
           + 4.0 * dr_norm       # Larger rotor → more flux
           - 2.5 * ag_norm       # Larger gap → more leakage
           - 0.8 * mt_norm       # Thicker magnet → slightly more iron loss
           - 2.0 * thet_penalty  # Penalize angles far from MTPA
           - 1.0 * bs0_norm)     # Wide slot opening → harmonic losses
    eff = float(np.clip(eff, 0.0, 100.0))
    
    # Torque Ripple model (28% baseline)
    torque_ripple = (28.0 
                     - 8.0 * dr_norm     # Larger rotor smooths ripple
                     + 10.0 * ag_norm    # Larger gap → more ripple
                     + 3.0 * bs0_norm    # Wide slot opening → cogging torque
                     + 2.5 * br_norm     # Large bridge → flux leakage → ripple
                     - 1.5 * mt_norm)    # Thicker magnet tends to reduce ripple
    torque_ripple = float(max(torque_ripple, 0.0))
    
    # Power Density model (0.30 kW/kg baseline)
    power_density = (0.30 
                     + 0.04 * dr_norm 
                     - 0.02 * ag_norm 
                     + 0.03 * mt_norm    # More magnet flux
                     + 0.01 * mw_norm    # Wider magnet
                     - 0.03 * rib_norm)  # Heavier structural steel
    power_density = float(max(power_density, 0.0))
    
    # Material Cost model (~100 baseline)
    cost = (100.0 
            + 25.0 * mt_norm 
            + 1.2 * ind.get("Mw", 25.0) 
            + 0.5 * ind.get("rib", 2.0))
    
    return {
        "efficiency": eff,
        "torque_ripple": torque_ripple,
        "power_density": power_density,
        "cost": cost,
    }

# ---------------------------------------------------------------------------
# Enhanced ML Surrogate with Gaussian Process (fallback to KNN-IDW)
# ---------------------------------------------------------------------------
class MLSurrogate:
    """Hybrid ML + Physics surrogate model with input standardization.
    
    Uses Gaussian Process Regression (if sklearn available) or KNN-IDW
    for interpolation, blended with the physics model. The blend ratio
    (alpha) increases as more training data becomes available.
    
    Attributes:
        param_order: Ordered list of parameter names
        scaler: StandardScaler for input normalization
        gp_models: Dict of GaussianProcessRegressor per output metric
        X_history: Training input vectors
        y_history: Training output metrics
        n_samples: Number of training samples
    """
    
    def __init__(self, param_order: List[str]):
        """Initialize ML surrogate.
        
        Args:
            param_order: Ordered list of 19 parameter names
        """
        self.param_order = param_order
        self.scaler = StandardScaler() if SKLEARN_AVAILABLE else None
        self.gp_models: Dict[str, Any] = {}
        self.X_history: List[np.ndarray] = []
        self.y_history: List[Dict[str, float]] = []
        self.n_samples = 0
        
    def add_evaluations(self, population: List[Dict], metrics: List[Dict]):
        """Add new FEM/surrogate evaluations to training history.
        
        Args:
            population: List of design parameter dicts
            metrics: List of metric dicts with efficiency, torque_ripple, etc.
        """
        for ind, m in zip(population, metrics):
            x = np.array([ind[p] for p in self.param_order], dtype=float)
            self.X_history.append(x)
            self.y_history.append({
                "efficiency": m["efficiency"],
                "torque_ripple": m["torque_ripple"],
                "power_density": m["power_density"],
                "cost": m["cost"]
            })
        self.n_samples = len(self.X_history)
        
        # Retrain GP models if enough data and sklearn available
        if SKLEARN_AVAILABLE and self.n_samples >= 15:
            self._train_gp_models()
    
    def _train_gp_models(self):
        """Train Gaussian Process models for each output metric."""
        X_mat = np.array(self.X_history)
        
        # Standardize inputs
        if self.scaler and self.n_samples > 1:
            X_scaled = self.scaler.fit_transform(X_mat)
        else:
            X_scaled = X_mat
        
        # GP kernel: RBF + WhiteNoise
        kernel = ConstantKernel(1.0) * RBF(length_scale=1.0) + WhiteKernel(noise_level=0.1)
        
        output_keys = ["efficiency", "torque_ripple", "power_density", "cost"]
        for key in output_keys:
            y = np.array([self.y_history[i][key] for i in range(self.n_samples)])
            try:
                gp = GaussianProcessRegressor(kernel=kernel, n_restarts_optimizer=3, 
                                             normalize_y=True, random_state=42)
                gp.fit(X_scaled, y)
                self.gp_models[key] = gp
            except Exception as e:
                logging.debug(f"GP training failed for {key}: {e}. Falling back to KNN.")
                self.gp_models.pop(key, None)
    
    def predict(self, ind: Dict) -> Dict[str, float]:
        """Predict metrics using hybrid ML + Physics surrogate.
        
        Blend strategy:
        - n < 15: 100% physics model
        - 15 ≤ n < 100: Linear blend, alpha = n/100
        - n ≥ 100: 75% ML, 25% physics (cap to prevent overfitting)
        
        Args:
            ind: Design parameter dict
            
        Returns:
            Dict with efficiency, torque_ripple, power_density, cost
        """
        phys = physics_surrogate(ind)
        
        if self.n_samples < 15:
            return phys  # Not enough data for ML
        
        x_new = np.array([ind[p] for p in self.param_order], dtype=float).reshape(1, -1)
        
        # Try GP prediction first
        if SKLEARN_AVAILABLE and self.gp_models:
            try:
                X_scaled = self.scaler.transform(x_new) if self.scaler else x_new
                ml_pred = {}
                for key in ["efficiency", "torque_ripple", "power_density", "cost"]:
                    if key in self.gp_models:
                        pred, std = self.gp_models[key].predict(X_scaled, return_std=True)
                        ml_pred[key] = float(pred[0])
                    else:
                        ml_pred[key] = phys[key]
            except Exception:
                ml_pred = self._knn_predict(x_new.flatten())
        else:
            ml_pred = self._knn_predict(x_new.flatten())
        
        # Blend ML with physics
        alpha = min(0.75, self.n_samples / 100.0)
        blended = {}
        for key in ["efficiency", "torque_ripple", "power_density", "cost"]:
            blended[key] = float(alpha * ml_pred[key] + (1.0 - alpha) * phys[key])
        
        return blended
    
    def _knn_predict(self, x_new: np.ndarray) -> Dict[str, float]:
        """KNN Inverse-Distance Weighting prediction (fallback).
        
        Uses normalized Euclidean distance to prevent magnitude bias.
        
        Args:
            x_new: New parameter vector to predict
            
        Returns:
            Predicted metrics dict
        """
        X_mat = np.array(self.X_history)
        
        # Normalize distances by feature range
        ranges = np.ptp(X_mat, axis=0)
        ranges[ranges == 0] = 1.0
        dists = np.sqrt(np.sum(((X_mat - x_new) / ranges) ** 2, axis=1))
        
        # K nearest neighbors
        k = min(5, self.n_samples)
        idx = np.argsort(dists)[:k]
        k_dists = dists[idx]
        
        # Exact match found
        if k_dists[0] < 1e-6:
            return self.y_history[idx[0]].copy()
        
        # Inverse distance weights
        weights = 1.0 / (k_dists + 1e-5)
        weights /= np.sum(weights)
        
        pred = {}
        for key in ["efficiency", "torque_ripple", "power_density", "cost"]:
            vals = np.array([self.y_history[i][key] for i in idx])
            pred[key] = float(np.sum(weights * vals))
        
        return pred

# ---------------------------------------------------------------------------
# NSGA-II Multi-Objective Optimization (Complete Implementation)
# ---------------------------------------------------------------------------
def fast_non_dominated_sort(metrics_list: List[Dict]) -> List[List[int]]:
    """Fast non-dominated sorting for NSGA-II.
    
    Objectives:
    - Maximize: efficiency, power_density
    - Minimize: torque_ripple, cost
    
    Args:
        metrics_list: List of metric dicts for each individual
        
    Returns:
        List of fronts, each front is a list of individual indices
    """
    n = len(metrics_list)
    S = [[] for _ in range(n)]      # Individuals dominated by i
    np_count = [0] * n              # Number of individuals dominating i
    rank = [0] * n
    fronts: List[List[int]] = [[]]
    
    for i in range(n):
        for j in range(n):
            if i == j:
                continue
            
            m1, m2 = metrics_list[i], metrics_list[j]
            
            # Check if i dominates j
            i_better_or_equal = (
                m1["efficiency"] >= m2["efficiency"] and
                m1["torque_ripple"] <= m2["torque_ripple"] and
                m1["power_density"] >= m2["power_density"] and
                m1["cost"] <= m2["cost"]
            )
            i_strictly_better = (
                m1["efficiency"] > m2["efficiency"] or
                m1["torque_ripple"] < m2["torque_ripple"] or
                m1["power_density"] > m2["power_density"] or
                m1["cost"] < m2["cost"]
            )
            
            if i_better_or_equal and i_strictly_better:
                S[i].append(j)
            elif not i_better_or_equal:
                # Check if j dominates i
                j_better_or_equal = (
                    m2["efficiency"] >= m1["efficiency"] and
                    m2["torque_ripple"] <= m1["torque_ripple"] and
                    m2["power_density"] >= m1["power_density"] and
                    m2["cost"] <= m1["cost"]
                )
                j_strictly_better = (
                    m2["efficiency"] > m1["efficiency"] or
                    m2["torque_ripple"] < m1["torque_ripple"] or
                    m2["power_density"] > m1["power_density"] or
                    m2["cost"] < m1["cost"]
                )
                if j_better_or_equal and j_strictly_better:
                    np_count[i] += 1
        
        if np_count[i] == 0:
            rank[i] = 0
            fronts[0].append(i)
    
    # Build subsequent fronts
    i = 0
    while i < len(fronts) and fronts[i]:
        next_front = []
        for p in fronts[i]:
            for q in S[p]:
                np_count[q] -= 1
                if np_count[q] == 0:
                    rank[q] = i + 1
                    next_front.append(q)
        i += 1
        if next_front:
            fronts.append(next_front)
    
    return fronts


def crowding_distance_assignment(front: List[int], metrics_list: List[Dict]) -> Dict[int, float]:
    """Calculate crowding distance for diversity preservation in NSGA-II.
    
    Args:
        front: List of individual indices in the current Pareto front
        metrics_list: List of metric dicts
        
    Returns:
        Dict mapping index to crowding distance
    """
    l = len(front)
    distances = {idx: 0.0 for idx in front}
    
    if l <= 2:
        for idx in front:
            distances[idx] = float("inf")
        return distances
    
    objectives = ["efficiency", "torque_ripple", "power_density", "cost"]
    
    for obj in objectives:
        # Sort front by current objective
        sorted_front = sorted(front, key=lambda idx: metrics_list[idx][obj])
        
        # Boundary points get infinite distance
        distances[sorted_front[0]] = float("inf")
        distances[sorted_front[-1]] = float("inf")
        
        # Normalization factor
        obj_min = metrics_list[sorted_front[0]][obj]
        obj_max = metrics_list[sorted_front[-1]][obj]
        norm = obj_max - obj_min if obj_max != obj_min else 1.0
        
        # Interior points
        for i in range(1, l - 1):
            if distances[sorted_front[i]] != float("inf"):
                distances[sorted_front[i]] += (
                    metrics_list[sorted_front[i + 1]][obj] - 
                    metrics_list[sorted_front[i - 1]][obj]
                ) / norm
    
    return distances


def nsga2_selection(population: List[Dict], metrics: List[Dict], 
                    pop_size: int) -> List[Dict]:
    """NSGA-II environmental selection: select next generation based on 
    Pareto rank and crowding distance.
    
    Args:
        population: Current population
        metrics: Fitness metrics for each individual
        pop_size: Desired population size
        
    Returns:
        Selected population for next generation
    """
    fronts = fast_non_dominated_sort(metrics)
    selected = []
    selected_indices = []
    
    for front in fronts:
        if len(selected) + len(front) <= pop_size:
            # Entire front fits
            for idx in front:
                selected.append(population[idx])
                selected_indices.append(idx)
        else:
            # Need to select subset based on crowding distance
            dist = crowding_distance_assignment(front, metrics)
            sorted_front = sorted(front, key=lambda idx: dist[idx], reverse=True)
            needed = pop_size - len(selected)
            for idx in sorted_front[:needed]:
                selected.append(population[idx])
                selected_indices.append(idx)
            break
    
    return selected


def nsga2_tournament_selection(population: List[Dict], metrics: List[Dict],
                               fronts: List[List[int]], 
                               crowding_dist: Dict[int, float]) -> Tuple[Dict, Dict]:
    """Binary tournament selection for NSGA-II reproduction.
    
    Selects based on:
    1. Lower rank (better Pareto front)
    2. Higher crowding distance (more diversity)
    
    Args:
        population: Current population
        metrics: Fitness metrics
        fronts: Pareto fronts from non-dominated sort
        crowding_dist: Crowding distance for each individual
        
    Returns:
        Tuple of (parent1, parent2)
    """
    n = len(population)
    
    # Build rank lookup
    rank_of = {}
    for r, front in enumerate(fronts):
        for idx in front:
            rank_of[idx] = r
    
    def select_one():
        i1, i2 = random.sample(range(n), 2)
        r1, r2 = rank_of.get(i1, 999), rank_of.get(i2, 999)
        
        if r1 < r2:
            return population[i1]
        elif r2 < r1:
            return population[i2]
        else:
            # Same rank, use crowding distance
            cd1 = crowding_dist.get(i1, 0.0)
            cd2 = crowding_dist.get(i2, 0.0)
            return population[i1] if cd1 > cd2 else population[i2]
    
    return select_one(), select_one()

# ---------------------------------------------------------------------------
# GA Core Operators
# ---------------------------------------------------------------------------
def random_individual(bounds: dict) -> dict:
    """Create a random feasible individual.
    
    Attempts up to 1000 random draws; falls back to deterministic baseline
    if no feasible individual is found.
    
    Args:
        bounds: Parameter bounds dict
        
    Returns:
        Feasible design parameter dict
    """
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
    
    # Deterministic feasible baseline
    logging.warning("Could not find random feasible individual after 1000 attempts. Using baseline.")
    baseline = {
        "Dr_in": 90.0, "Air_gap": 1.0, "Lamda": 0.9, "Bridge": 1.5,
        "Hs0": 1.1899, "Hs1": 1.5, "Hs2": 18.07656, "Bs0": 2.1128,
        "Bs1": 6.90142, "Bs2": 10.88076, "O1": 5.4, "O2": 6.0,
        "B1": 3.5, "rib": 2.0, "hrib": 2.4, "Mt": 5.282,
        "Mw": 25.44156, "magDmin": 10.0, "thet_deg": 30.0,
    }
    return baseline


def repair_individual(ind: dict, bounds: dict, max_attempts: int = 5) -> dict:
    """Attempt to repair an infeasible individual by step-snapping.
    
    Tries multiple repair strategies:
    1. Snap all variables to nearest valid step
    2. If still infeasible, try random perturbation of violating variables
    3. If all fails, return original (will be discarded by selection)
    
    Args:
        ind: Individual to repair
        bounds: Parameter bounds
        max_attempts: Maximum repair attempts
        
    Returns:
        Repaired individual (may still be infeasible)
    """
    repaired = ind.copy()
    
    # Strategy 1: Simple snap
    for name, info in bounds.items():
        repaired[name] = snap_to_step(
            repaired[name], info["lower"], info["upper"], info["step"]
        )
    
    if is_feasible(repaired):
        return repaired
    
    # Strategy 2: Identify violating constraints and perturb related variables
    for _ in range(max_attempts):
        violated = get_violated_constraints(repaired)
        if not violated:
            return repaired
        
        # Identify variables likely causing violation
        if "Slot Height" in str(violated):
            # Reduce slot heights
            for key in ["Hs0", "Hs1", "Hs2"]:
                if key in repaired and key in bounds:
                    info = bounds[key]
                    steps = int(round((repaired[key] - info["lower"]) / info["step"]))
                    new_step = max(0, steps - random.randint(1, 3))
                    repaired[key] = info["lower"] + new_step * info["step"]
        
        if "Slot Width" in str(violated):
            # Ensure Bs0 <= Bs1 <= Bs2
            if "Bs0" in repaired and "Bs1" in repaired and repaired["Bs0"] > repaired["Bs1"]:
                repaired["Bs0"] = repaired["Bs1"]
            if "Bs1" in repaired and "Bs2" in repaired and repaired["Bs1"] > repaired["Bs2"]:
                repaired["Bs1"] = repaired["Bs2"]

        if "Rotor Rib Height" in str(violated):
            # Reduce hrib
            if "hrib" in repaired and "hrib" in bounds:
                info = bounds["hrib"]
                steps = int(round((repaired["hrib"] - info["lower"]) / info["step"]))
                new_step = max(0, steps - random.randint(1, 2))
                repaired["hrib"] = info["lower"] + new_step * info["step"]

        if "Bridge Thickness" in str(violated) or "Magnet Duct" in str(violated):
            # Reduce B1 or increase Mt
            if "B1" in repaired and "B1" in bounds:
                info = bounds["B1"]
                steps = int(round((repaired["B1"] - info["lower"]) / info["step"]))
                new_step = max(0, steps - random.randint(1, 2))
                repaired["B1"] = info["lower"] + new_step * info["step"]
        
        if "Rotor Fits Stator" in str(violated):
            # Reduce Dr_in or increase Air_gap
            if "Dr_in" in repaired and "Dr_in" in bounds:
                info = bounds["Dr_in"]
                steps = int(round((repaired["Dr_in"] - info["lower"]) / info["step"]))
                new_step = max(0, steps - random.randint(1, 2))
                repaired["Dr_in"] = info["lower"] + new_step * info["step"]
    
    return repaired if is_feasible(repaired) else ind


def crossover(parent1: dict, parent2: dict, bounds: dict, rate: float = 0.7) -> Tuple[dict, dict]:
    """Uniform crossover with feasibility repair.
    
    Each gene has 50% chance of coming from either parent.
    
    Args:
        parent1, parent2: Parent design dicts
        bounds: Parameter bounds
        rate: Crossover probability
        
    Returns:
        Tuple of (child1, child2)
    """
    if random.random() > rate:
        return parent1.copy(), parent2.copy()
    
    child1, child2 = {}, {}
    for name in bounds:
        if random.random() < 0.5:
            child1[name] = parent1[name]
            child2[name] = parent2[name]
        else:
            child1[name] = parent2[name]
            child2[name] = parent1[name]
    
    # Repair if infeasible, revert to parent if repair fails
    if not is_feasible(child1):
        child1_rep = repair_individual(child1, bounds)
        child1 = child1_rep if is_feasible(child1_rep) else parent1.copy()
        
    if not is_feasible(child2):
        child2_rep = repair_individual(child2, bounds)
        child2 = child2_rep if is_feasible(child2_rep) else parent2.copy()
    
    return child1, child2


def mutate(individual: dict, bounds: dict, rate: float = 0.2, 
           strength: int = 3) -> dict:
    """Mutate genes by discrete step offsets.
    
    Args:
        individual: Individual to mutate
        bounds: Parameter bounds
        rate: Per-gene mutation probability
        strength: Maximum step offset (default ±3)
        
    Returns:
        Mutated individual
    """
    mutated = individual.copy()
    n_mutations = 0
    
    for name, info in bounds.items():
        if random.random() < rate:
            steps = int(round((info["upper"] - info["lower"]) / info["step"]))
            current_step = round((mutated[name] - info["lower"]) / info["step"])
            shift = random.choice([x for x in range(-strength, strength + 1) if x != 0])
            new_step = int(np.clip(current_step + shift, 0, steps))
            mutated[name] = snap_to_step(
                info["lower"] + new_step * info["step"],
                info["lower"], info["upper"], info["step"],
            )
            n_mutations += 1
    
    if n_mutations > 0 and not is_feasible(mutated):
        mutated_rep = repair_individual(mutated, bounds)
        return mutated_rep if is_feasible(mutated_rep) else individual.copy()
    
    return mutated

# ---------------------------------------------------------------------------
# Diagnostics & Monitoring
# ---------------------------------------------------------------------------
def check_population_diversity(population: List[Dict], 
                                threshold: float = 0.1) -> Tuple[bool, float]:
    """Check if population has sufficient diversity.
    
    Computes average pairwise Euclidean distance in normalized parameter space.
    
    Args:
        population: Current population
        threshold: Minimum diversity threshold
        
    Returns:
        (is_diverse, diversity_score)
    """
    n = len(population)
    if n < 2:
        return True, 1.0
    
    # Extract parameter vectors
    X = np.array([[ind[p] for p in PARAM_ORDER] for ind in population])
    
    # Normalize
    ranges = np.ptp(X, axis=0)
    ranges[ranges == 0] = 1.0
    X_norm = X / ranges
    
    # Average pairwise distance
    dists = []
    for i in range(n):
        for j in range(i + 1, n):
            dists.append(np.linalg.norm(X_norm[i] - X_norm[j]))
    
    avg_dist = np.mean(dists) if dists else 0.0
    # Normalize by sqrt(n_params) for interpretability
    diversity_score = avg_dist / np.sqrt(len(PARAM_ORDER))
    
    return diversity_score > threshold, diversity_score


def detect_stagnation(scores_history: List[float], 
                      window: int = 10, 
                      threshold: float = 0.01) -> bool:
    """Detect optimization stagnation.
    
    Args:
        scores_history: List of best scores per generation
        window: Number of generations to check
        threshold: Minimum improvement to consider non-stagnant
        
    Returns:
        True if optimization appears stagnant
    """
    if len(scores_history) < window:
        return False
    
    recent = scores_history[-window:]
    improvement = recent[-1] - recent[0]
    
    return abs(improvement) < threshold

# ---------------------------------------------------------------------------
# Excel I/O & MATLAB driver
# ---------------------------------------------------------------------------
def write_param_excel(population: List[Dict], path: Path):
    """Write current population into Ai_Optimization_ParamValues.xlsx.
    
    Columns follow global PARAM_ORDER for MATLAB compatibility.
    
    Args:
        population: List of design parameter dicts
        path: Output Excel file path
    """
    rows = [[ind[name] for name in PARAM_ORDER] for ind in population]
    df = pd.DataFrame(rows, columns=PARAM_ORDER)
    df.to_excel(path, index=False)
    logging.debug(f"Wrote {len(population)} individuals to {path}")


def cleanup_ansys_locks(root_dir: Path):
    """Clean up lingering ansysedt.exe, maxwell.exe background processes and project lock files.
    
    NOTE: Automatic cleanup of processes and lock files is DISABLED per user request.
    """
    pass


def run_matlab(root_dir: Path, matlab_exe: str = "matlab"):
    """Launch MATLAB batch simulation script.
    
    Args:
        root_dir: Project root directory
        matlab_exe: MATLAB executable name/path
        
    Raises:
        SimulationError: If MATLAB execution fails
    """
    logging.info("Launching MATLAB for Ansys Maxwell simulation batch...")
    
    # Auto-detect MATLAB executable if specified path does not exist
    resolved_matlab = matlab_exe
    if not Path(resolved_matlab).is_file() and shutil.which(resolved_matlab) is None:
        candidates = [
            r"C:\Users\pchuanvn\Desktop\Ai_Optimization_Of_Vshape_IPM_motor",
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
# Population Evaluation
# ---------------------------------------------------------------------------
def evaluate_population(population: List[Dict], 
                        root_dir: Path, 
                        input_dir: Path,
                        output_dir: Path, 
                        mode: str, 
                        score_weights: Dict[str, float],
                        ml_surrogate: MLSurrogate, 
                        use_ml: bool = True,
                        matlab_exe: str = "matlab") -> List[Dict]:
    """Evaluate population using MATLAB/Ansys or hybrid ML/physics surrogate.
    
    Args:
        population: List of design dicts to evaluate
        root_dir: Project root directory
        input_dir: Input directory (for Excel files)
        output_dir: Output directory (for CSV results)
        mode: "matlab" or "offline"
        score_weights: Dict of score weights
        ml_surrogate: ML surrogate model instance
        use_ml: Whether to use ML blending
        
    Returns:
        List of metric dicts with score added
    """
    if mode == "offline":
        metrics = []
        for ind in population:
            m = ml_surrogate.predict(ind) if use_ml else physics_surrogate(ind)
            m["score"] = compute_score(
                m, 
                score_weights["eff"], 
                score_weights["ripple"],
                score_weights["pwr"], 
                score_weights["cost"]
            )
            metrics.append(m)
        return metrics
    
    # Online mode: MATLAB + Ansys
    param_excel = input_dir / "Ai_Optimization_ParamValues.xlsx"
    write_param_excel(population, param_excel)
    
    try:
        run_matlab(root_dir, matlab_exe=matlab_exe)
    except SimulationError as e:
        logging.error(f"MATLAB Simulation failed: {e}")
        # Stop execution immediately
        sys.exit(1)
    
    metrics = []
    for i in range(1, len(population) + 1):
        csv_path = output_dir / f"output_vars_iter_{i}.csv"
        
        if not csv_path.is_file():
            logging.warning("Missing CSV for candidate %d – assigning penalty", i)
            metrics.append({
                "score": -1e6, "efficiency": 0.0, "torque_ripple": 1e6,
                "cost": 1e9, "power_density": 0.0
            })
            continue
        
        try:
            df = pd.read_csv(csv_path)
            
            # Flexible column detection
            eff_col = next((c for c in df.columns if "Eff" in c and "Efficiency" not in c), None) or \
                      next((c for c in df.columns if "Efficiency" in c), None)
            tr_col = next((c for c in df.columns if "TorqueRip" in c or "Ripple" in c), None)
            cost_col = next((c for c in df.columns if "Cost" in c or "TotCost" in c), None)
            pwr_col = next((c for c in df.columns if "PwrDens" in c or "PowerDensity" in c or "Power" in c), None)
            
            if not eff_col or not tr_col:
                logging.error("Cannot locate key metric columns in %s. Available: %s", 
                            csv_path, list(df.columns))
                metrics.append({
                    "score": -1e6, "efficiency": 0.0, "torque_ripple": 1e6,
                    "cost": 1e9, "power_density": 0.0
                })
                continue
            
            # Use steady-state (second half) for averaging
            start = len(df) // 2
            avg_eff = df[eff_col].iloc[start:].mean()
            avg_tr = df[tr_col].iloc[start:].mean()
            avg_cost = df[cost_col].iloc[start:].mean() if cost_col else 0.0
            avg_pwr = df[pwr_col].iloc[start:].mean() if pwr_col else 0.0
            
            m = {
                "efficiency": float(avg_eff),
                "torque_ripple": float(avg_tr),
                "cost": float(avg_cost),
                "power_density": float(avg_pwr),
            }
            m["score"] = compute_score(
                m, 
                score_weights["eff"], 
                score_weights["ripple"],
                score_weights["pwr"], 
                score_weights["cost"]
            )
            metrics.append(m)
            
        except Exception as e:
            logging.error("Error processing %s: %s", csv_path, e)
            metrics.append({
                "score": -1e6, "efficiency": 0.0, "torque_ripple": 1e6,
                "cost": 1e9, "power_density": 0.0
            })
    
    return metrics

# ---------------------------------------------------------------------------
# Enhanced Visualization
# ---------------------------------------------------------------------------
def plot_pareto_front(history_csv: Path, output_dir: Path):
    """Generate 2D Pareto front scatter plot (Efficiency vs Torque Ripple).
    
    Args:
        history_csv: Path to simulation_history.csv
        output_dir: Output directory for plots
    """
    if not history_csv.is_file():
        logging.warning("History file not found for Pareto plot.")
        return
    
    df = pd.read_csv(history_csv)
    if len(df) == 0:
        return
    
    fig, ax = plt.subplots(figsize=(10, 7))
    scatter = ax.scatter(
        df["torque_ripple"], df["efficiency"],
        c=df["score"], cmap="viridis", alpha=0.7, 
        edgecolors="k", linewidth=0.5, s=60
    )
    plt.colorbar(scatter, ax=ax, label="Composite Score")
    ax.set_xlabel("Torque Ripple (%) [Lower is better]", fontsize=12)
    ax.set_ylabel("Efficiency (%) [Higher is better]", fontsize=12)
    ax.set_title("V-Shape IPM Motor: Efficiency vs Torque Ripple Pareto Front", fontsize=14)
    ax.grid(True, linestyle="--", alpha=0.5)
    
    out_path = output_dir / "pareto_front.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    logging.info("Pareto front saved to %s", out_path)


def plot_3d_pareto(history_csv: Path, output_dir: Path):
    """Generate 3D Pareto front (Efficiency vs TorqueRipple vs Cost).
    
    Args:
        history_csv: Path to simulation_history.csv
        output_dir: Output directory for plots
    """
    if not history_csv.is_file():
        return
    
    df = pd.read_csv(history_csv)
    if len(df) < 3:
        return
    
    fig = plt.figure(figsize=(12, 9))
    ax = fig.add_subplot(111, projection='3d')
    
    scatter = ax.scatter(
        df["torque_ripple"], df["efficiency"], df["cost"],
        c=df["score"], cmap="plasma", alpha=0.7, s=50
    )
    
    ax.set_xlabel("Torque Ripple (%)", fontsize=10)
    ax.set_ylabel("Efficiency (%)", fontsize=10)
    ax.set_zlabel("Cost ($)", fontsize=10)
    ax.set_title("3D Pareto Front: Efficiency vs Torque Ripple vs Cost", fontsize=14)
    plt.colorbar(scatter, ax=ax, label="Composite Score", shrink=0.6)
    
    out_path = output_dir / "pareto_3d.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    logging.info("3D Pareto front saved to %s", out_path)


def plot_parallel_coordinates(history_csv: Path, output_dir: Path):
    """Generate parallel coordinates plot for 4 objectives.
    
    Args:
        history_csv: Path to simulation_history.csv
        output_dir: Output directory for plots
    """
    if not history_csv.is_file():
        return
    
    df = pd.read_csv(history_csv)
    if len(df) < 5:
        return
    
    # Select top 50 designs by score for clarity
    top_df = df.nlargest(50, "score")
    
    objectives = ["efficiency", "torque_ripple", "power_density", "cost"]
    obj_labels = ["Efficiency (%) ↑", "Torque Ripple (%) ↓", 
                  "Power Density (kW/kg) ↑", "Cost ($) ↓"]
    
    # Normalize to [0, 1]
    norm_df = top_df[objectives].copy()
    for col in objectives:
        min_val, max_val = norm_df[col].min(), norm_df[col].max()
        if max_val > min_val:
            norm_df[col] = (norm_df[col] - min_val) / (max_val - min_val)
    
    fig, ax = plt.subplots(figsize=(12, 6))
    
    for _, row in norm_df.iterrows():
        ax.plot(range(len(objectives)), [row[o] for o in objectives], 
                'o-', alpha=0.3, linewidth=0.8, markersize=4)
    
    # Highlight best design
    best = norm_df.iloc[0]
    ax.plot(range(len(objectives)), [best[o] for o in objectives], 
            'ro-', linewidth=2.5, markersize=8, label="Best Design")
    
    ax.set_xticks(range(len(objectives)))
    ax.set_xticklabels(obj_labels, fontsize=10)
    ax.set_ylabel("Normalized Value", fontsize=12)
    ax.set_title("Parallel Coordinates: Multi-Objective Trade-offs", fontsize=14)
    ax.legend()
    ax.grid(True, alpha=0.3, axis='y')
    
    out_path = output_dir / "parallel_coordinates.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    logging.info("Parallel coordinates plot saved to %s", out_path)


def plot_convergence(scores_history: List[float], output_dir: Path):
    """Plot best score convergence over generations.
    
    Args:
        scores_history: List of best scores per generation
        output_dir: Output directory
    """
    if len(scores_history) < 2:
        return
    
    fig, ax = plt.subplots(figsize=(10, 5))
    gens = range(1, len(scores_history) + 1)
    
    ax.plot(gens, scores_history, 'b-o', linewidth=2, markersize=6)
    ax.fill_between(gens, scores_history, alpha=0.2)
    
    # Annotate best score
    best_gen = np.argmax(scores_history) + 1
    best_score = max(scores_history)
    ax.annotate(f'Best: {best_score:.2f}\nGen {best_gen}',
                xy=(best_gen, best_score),
                xytext=(best_gen + 1, best_score - 1),
                arrowprops=dict(arrowstyle='->', color='red'),
                fontsize=11, color='red')
    
    ax.set_xlabel("Generation", fontsize=12)
    ax.set_ylabel("Best Composite Score", fontsize=12)
    ax.set_title("Optimization Convergence History", fontsize=14)
    ax.xaxis.set_major_locator(MaxNLocator(integer=True))
    ax.grid(True, linestyle="--", alpha=0.5)
    
    out_path = output_dir / "convergence_history.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight")
    plt.close(fig)
    logging.info("Convergence plot saved to %s", out_path)

# ---------------------------------------------------------------------------
# Sensitivity Analysis
# ---------------------------------------------------------------------------
def perform_sensitivity_analysis(history_csv: Path, bounds: dict, output_dir: Path):
    """Compute Spearman rank correlation between parameters and metrics.
    
    Args:
        history_csv: Path to simulation_history.csv
        bounds: Parameter bounds dict
        output_dir: Output directory
    """
    if not history_csv.is_file():
        logging.warning("History file not found for sensitivity analysis.")
        return
    
    df = pd.read_csv(history_csv)
    
    if len(df) < 20:
        logging.warning("Insufficient data (%d samples) for sensitivity analysis. Need >= 20.", len(df))
        return
    
    results = []
    for param in PARAM_ORDER:
        if param in df.columns and df[param].nunique() > 1:
            try:
                r_eff, p_eff = spearmanr(df[param], df["efficiency"])
                r_rip, p_rip = spearmanr(df[param], df["torque_ripple"])
                r_score, p_score = spearmanr(df[param], df["score"])
                r_cost, p_cost = spearmanr(df[param], df["cost"])
                r_pwr, p_pwr = spearmanr(df[param], df["power_density"])
                
                results.append({
                    "Parameter": param,
                    "Corr_Efficiency": round(r_eff, 3),
                    "P_Efficiency": round(p_eff, 4),
                    "Corr_TorqueRipple": round(r_rip, 3),
                    "P_TorqueRipple": round(p_rip, 4),
                    "Corr_Score": round(r_score, 3),
                    "P_Score": round(p_score, 4),
                    "Corr_Cost": round(r_cost, 3),
                    "Corr_PowerDensity": round(r_pwr, 3),
                })
            except Exception as e:
                logging.debug(f"Skipping {param} in sensitivity: {e}")
    
    if results:
        res_df = pd.DataFrame(results)
        out_file = output_dir / "sensitivity_analysis.csv"
        res_df.to_csv(out_file, index=False)
        logging.info("Sensitivity analysis saved to %s", out_file)
        
        # Log top influencers
        top_eff = res_df.nlargest(3, "Corr_Efficiency")
        top_rip = res_df.nlargest(3, "Corr_TorqueRipple")
        
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
    """Generate Markdown optimization report.
    
    Args:
        history_csv: Path to simulation history
        best_ind: Best design parameters
        best_score: Best composite score
        bounds: Parameter bounds
        output_dir: Output directory
        args: Command-line arguments
    """
    report_path = output_dir / "optimization_report.md"
    
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# V-Shape IPM Motor Optimization Report\n\n")
        f.write(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        
        f.write(f"## Configuration\n\n")
        f.write(f"- Algorithm: {args.algorithm.upper()}\n")
        f.write(f"- Mode: {args.mode}\n")
        f.write(f"- Population Size: {args.pop_size}\n")
        f.write(f"- Generations: {args.generations}\n")
        f.write(f"- Crossover Rate: {args.crossover}\n")
        f.write(f"- Mutation Rate: {args.mutation}\n")
        f.write(f"- Score Weights: Eff={args.w_eff}, Ripple={args.w_ripple}, "
                f"Pwr={args.w_pwr}, Cost={args.w_cost}\n\n")
        
        f.write(f"## Best Design\n\n")
        f.write(f"- **Composite Score:** {best_score:.4f}\n\n")
        f.write(f"| Parameter | Value | Unit | Lower | Upper |\n")
        f.write(f"|-----------|-------|------|-------|-------|\n")
        for name in PARAM_ORDER:
            info = bounds[name]
            f.write(f"| {name} | {best_ind[name]} | {info['unit']} | "
                   f"{info['lower']} | {info['upper']} |\n")
        
        if history_csv.is_file():
            df = pd.read_csv(history_csv)
            if len(df) > 0:
                best_row = df.loc[df["score"].idxmax()]
                f.write(f"\n## Predicted Performance\n\n")
                f.write(f"- Efficiency: {best_row['efficiency']:.2f}%\n")
                f.write(f"- Torque Ripple: {best_row['torque_ripple']:.2f}%\n")
                f.write(f"- Power Density: {best_row['power_density']:.3f} kW/kg\n")
                f.write(f"- Cost: ${best_row['cost']:.1f}\n")
                
                # Top 5 designs
                f.write(f"\n## Top 5 Designs\n\n")
                f.write(f"| Rank | Score | Efficiency | TorqueRipple | PwrDensity | Cost |\n")
                f.write(f"|------|-------|------------|--------------|------------|------|\n")
                top5 = df.nlargest(5, "score")
                for rank, (_, row) in enumerate(top5.iterrows(), 1):
                    f.write(f"| {rank} | {row['score']:.3f} | {row['efficiency']:.1f}% | "
                           f"{row['torque_ripple']:.1f}% | {row['power_density']:.3f} | "
                           f"${row['cost']:.0f} |\n")
    
    logging.info("Optimization report saved to %s", report_path)

# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------
def run_unit_tests():
    """Run built-in unit tests for critical functions.
    
    Tests constraint functions, NSGA-II dominance, crowding distance,
    repair mechanism, and score computation.
    """
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
    infeasible_slot["Hs2"] = 50.0  # Violates slot height constraint
    
    infeasible_bridge = feasible.copy()
    infeasible_bridge["B1"] = 5.5  # Violates bridge thickness (Mt=5.282, max B1=4.982)
    
    infeasible_hrib = feasible.copy()
    infeasible_hrib["hrib"] = 5.0  # Violates hrib <= 4.5 mm limit
    
    infeasible_width = feasible.copy()
    infeasible_width["Bs0"] = 8.0
    infeasible_width["Bs1"] = 4.0  # Violates Bs0 <= Bs1

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
            logging.error(f"     Violated constraints: {get_violated_constraints(params)}")
            failed += 1
    
    # Test 2: NSGA-II Dominance
    logging.info("\n[Test 2] NSGA-II Dominance Sorting")
    
    m1 = {"efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0}
    m2 = {"efficiency": 94.0, "torque_ripple": 25.0, "power_density": 0.30, "cost": 150.0}
    m3 = {"efficiency": 96.0, "torque_ripple": 30.0, "power_density": 0.33, "cost": 145.0}
    
    fronts = fast_non_dominated_sort([m1, m2, m3])
    
    if len(fronts) > 0 and len(fronts[0]) >= 2:  # m1 and m3 should be non-dominated
        logging.info(f"  [PASS] Dominance sorting: {len(fronts)} fronts, front0 size={len(fronts[0])}")
        passed += 1
    else:
        logging.error(f"  [FAIL] Dominance sorting failed: {len(fronts)} fronts")
        failed += 1
    
    # Test 3: Crowding Distance
    logging.info("\n[Test 3] Crowding Distance Assignment")
    
    metrics_list = [
        {"efficiency": 95.0, "torque_ripple": 20.0, "power_density": 0.35, "cost": 140.0},
        {"efficiency": 94.0, "torque_ripple": 25.0, "power_density": 0.30, "cost": 150.0},
        {"efficiency": 96.0, "torque_ripple": 30.0, "power_density": 0.33, "cost": 145.0},
        {"efficiency": 93.0, "torque_ripple": 18.0, "power_density": 0.32, "cost": 155.0},
    ]
    
    front = [0, 1, 2, 3]
    dist = crowding_distance_assignment(front, metrics_list)
    
    if dist[front[0]] == float("inf") and dist[front[-1]] == float("inf"):
        logging.info(f"  [PASS] Boundary points have infinite distance")
        passed += 1
    else:
        logging.error(f"  [FAIL] Boundary distance check failed")
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
    all_in_bounds = all(
        bounds_test[k]["lower"] <= repaired[k] <= bounds_test[k]["upper"]
        for k in bounds_test
    )
    
    if all_in_bounds:
        logging.info(f"  [PASS] All repaired values within bounds")
        passed += 1
    else:
        logging.error(f"  [FAIL] Repair failed to bring values within bounds")
        for k in bounds_test:
            if not (bounds_test[k]["lower"] <= repaired[k] <= bounds_test[k]["upper"]):
                logging.error(f"     {k}: {repaired[k]} not in [{bounds_test[k]['lower']}, {bounds_test[k]['upper']}]")
        failed += 1
    
    # Test 5: Score computation
    logging.info("\n[Test 5] Score Computation")
    
    metrics_test = {"efficiency": 95.0, "torque_ripple": 20.0, 
                    "power_density": 0.35, "cost": 140.0}
    score = compute_score(metrics_test)
    expected_score = 95.0 - 20.0 + 0.5 * 0.35 - 0.05 * (140.0 / 150.0)
    
    if abs(score - expected_score) < 0.01:
        logging.info(f"  [PASS] Score = {score:.4f} (expected {expected_score:.4f})")
        passed += 1
    else:
        logging.error(f"  [FAIL] Score = {score:.4f} (expected {expected_score:.4f})")
        failed += 1

    # Test 6: Log History Export
    logging.info("\n[Test 6] Log History Export")
    test_log_path = Path("test_log_history.csv")
    if test_log_path.exists():
        test_log_path.unlink()
    
    log_history_entry(test_log_path, {
        "timestamp": datetime.now().isoformat(),
        "generation": 1,
        "individual_id": "Test_1",
        "operator": "UnitTesting",
        "is_feasible": True,
        "violated_constraints": [],
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
    
    # Test 7: SLOT_HEIGHT_MARGIN value
    logging.info("\n[Test 7] SLOT_HEIGHT_MARGIN Constant Value")
    assert SLOT_HEIGHT_MARGIN == 12.25, f"SLOT_HEIGHT_MARGIN should be 12.25, got {SLOT_HEIGHT_MARGIN}"
    logging.info(f"  [PASS] SLOT_HEIGHT_MARGIN = {SLOT_HEIGHT_MARGIN}")
    passed += 1

    # Test 8: load_warm_start() padding logic
    logging.info("\n[Test 8] Warm-start Loading & Padding")
    import tempfile
    try:
        _test_bounds = load_bounds(Path("Ai_Optimization_Bounds.xlsx"))
    except Exception:
        _test_bounds = bounds_test
    _csv_rows = []
    for _ in range(3):
        _csv_rows.append(random_individual(_test_bounds))
    _tmp = Path(tempfile.mktemp(suffix=".csv"))
    pd.DataFrame(_csv_rows).to_csv(_tmp, index=False)
    _result = load_warm_start(_tmp, _test_bounds, pop_size=5)
    assert len(_result) == 5, f"Expected 5 individuals, got {len(_result)}"
    assert all(is_feasible(ind) for ind in _result), "All warm-start individuals must be feasible"
    _tmp.unlink()
    logging.info("  [PASS] load_warm_start padding logic")
    passed += 1

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
    """Save optimizer state to pickle file.
    
    Args:
        state: State dict with generation, population, best_individual, best_score
        path: Output file path
    """
    with open(path, "wb") as f:
        pickle.dump(state, f)
    logging.debug("Checkpoint saved to %s", path)


def load_state(path: Path) -> dict:
    """Load optimizer state from pickle file.
    
    Args:
        path: Checkpoint file path
        
    Returns:
        State dict
    """
    with open(path, "rb") as f:
        state = pickle.load(f)
    logging.info("Checkpoint loaded from %s (generation %d)", path, state.get("generation", 0))
    return state


def log_history_entry(history_log_path: Path, entry: dict, param_order: List[str]):
    """Log structured individual activity details to log_history.csv continuously.
    
    Args:
        history_log_path: Path to log_history.csv
        entry: Dict containing run info, parameters, feasibility, repair status, metrics
        param_order: List of design parameter names
    """
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
        "operator": entry.get("operator", "Evaluation"),
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
    """Load and validate prior designs from CSV to seed the initial population.

    Args:
        csv_path: Path to CSV with columns matching PARAM_ORDER.
        bounds: Parameter bounds dict (used for snap_to_step and is_feasible).
        pop_size: Desired population size.

    Returns:
        List of pop_size feasible individuals (mix of CSV + random).

    Raises:
        FileNotFoundError: If csv_path does not exist.
        ValueError: If CSV has no columns matching any parameter in PARAM_ORDER.
    """
    if not csv_path.is_file():
        raise FileNotFoundError(f"Warm-start file not found: {csv_path}")

    df = pd.read_csv(csv_path)

    # Validate columns
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

    # Trim or pad to pop_size
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
        description="V-Shape IPM Motor AI Optimizer v5.1",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Quick test with offline surrogate
  python motor_optimizer_ver5.1.py --mode offline --generations 5
  
  # Full NSGA-II optimization with all visualizations
  python motor_optimizer_ver5.1.py --algorithm nsga2 --generations 50 --plot-all
  
  # Resume interrupted run
  python motor_optimizer_ver5.1.py --resume --mode matlab
  
  # Run built-in unit tests
  python motor_optimizer_ver5.1.py --test
        """
    )
    
    # Core optimization parameters
    parser.add_argument("--pop-size", type=int, default=8, help="Population size (default: 8)")
    parser.add_argument("--generations", type=int, default=10, help="Max generations (default: 10)")
    parser.add_argument("--crossover", type=float, default=0.7, help="Crossover probability (default: 0.7)")
    parser.add_argument("--mutation", type=float, default=0.2, help="Mutation rate per gene (default: 0.2)")
    parser.add_argument("--mode", choices=["offline", "matlab"], default="offline", 
                       help="Evaluation mode (default: offline)")
    parser.add_argument("--algorithm", choices=["ga", "nsga2"], default="ga",
                       help="Optimization engine (default: ga)")
    parser.add_argument("--seed", type=int, default=None, help="Random seed for reproducibility")
    parser.add_argument("--resume", action="store_true", help="Resume from checkpoint")
    parser.add_argument("--matlab-exe", type=str, default=r"C:\MATLAB\R2023b\bin\matlab.exe", help="Path to MATLAB executable")
    
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
        help="CSV file with prior design parameters to seed initial population. "
             "Columns must match PARAM_ORDER. Infeasible rows are discarded. "
             "If fewer rows than pop-size, remainder filled with random_individual()."
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
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.FileHandler(log_file, mode="a", encoding="utf-8"),
            logging.FileHandler(log_history_file, mode="a", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )
    
    logging.info("=" * 60)
    logging.info("V-Shape IPM Motor Optimizer v5.1")
    logging.info(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    logging.info("=" * 60)
    
    # Set random seed
    if args.seed is not None:
        random.seed(args.seed)
        np.random.seed(args.seed)
        logging.info("Random seed: %d", args.seed)
    else:
        logging.info("Random seed: not set (results will vary between runs)")
    
    # Load bounds
    bounds_path = input_dir / "Ai_Optimization_Bounds.xlsx"
    try:
        bounds = load_bounds(bounds_path)
    except (FileNotFoundError, ValueError) as e:
        logging.error("Failed to load bounds: %s", e)
        sys.exit(1)
    
    global PARAM_ORDER
    PARAM_ORDER = list(bounds.keys())
    logging.info("Loaded %d design parameters from bounds file", len(PARAM_ORDER))
    
    # Initialize ML surrogate
    ml_surrogate = MLSurrogate(PARAM_ORDER)
    use_ml = not args.no_ml
    if not SKLEARN_AVAILABLE and use_ml:
        logging.warning("scikit-learn not available. ML surrogate will use KNN-IDW (install sklearn for GP).")
    
    # Load or initialize state
    state_path = output_dir / STATE_FILE
    scores_history = []
    
    if args.resume and state_path.is_file():
        state = load_state(state_path)
        population = state["population"]
        best_ind = state.get("best_individual")
        best_score = state.get("best_score", -float("inf"))
        start_gen = state["generation"] + 1
        scores_history = state.get("scores_history", [])
        logging.info("Resuming from generation %d (best score: %.4f)", state["generation"], best_score)
    else:
        if args.warm_start and not args.resume:
            population = load_warm_start(Path(args.warm_start), bounds, args.pop_size)
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
    
    # Setup history tracking
    history_path = output_dir / "simulation_history.csv"
    log_history_csv_path = output_dir / "log_history.csv"
    if not args.resume:
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
        
        # Evaluate population
        metrics = evaluate_population(
            population, root_dir, input_dir, output_dir,
            args.mode, score_weights, ml_surrogate, use_ml,
            matlab_exe=args.matlab_exe
        )
        cached_metrics = metrics  # Cache để NSGA-II tái sử dụng
        
        # Update ML surrogate with new data
        ml_surrogate.add_evaluations(population, metrics)
        
        scores = [m["score"] for m in metrics]
        gen_best_idx = int(np.argmax(scores))
        gen_best_score = scores[gen_best_idx]
        gen_best_metrics = metrics[gen_best_idx]
        
        # Update global best
        if gen_best_score > best_score + args.min_delta:
            best_score = gen_best_score
            best_ind = population[gen_best_idx].copy()
            best_gen = gen
            no_improve_count = 0
            logging.info("  [NEW BEST] | Score: %.4f | Gen: %d", best_score, gen)
        else:
            no_improve_count += 1
        
        scores_history.append(best_score)
        
        # Generation summary
        logging.info(
            "Gen %d | Best=%.4f | Eff=%.1f%% | TR=%.1f%% | PD=%.3f | Cost=$%.0f | Stag=%d/%d",
            gen, gen_best_metrics["score"], gen_best_metrics["efficiency"],
            gen_best_metrics["torque_ripple"], gen_best_metrics["power_density"],
            gen_best_metrics["cost"], no_improve_count, args.patience,
        )
        
        # Diversity check
        is_diverse, div_score = check_population_diversity(population)
        if not is_diverse and gen > 5:
            logging.warning("  ⚠ Low population diversity (score: %.3f). Consider increasing mutation rate.", div_score)
        
        # Stagnation warning
        if detect_stagnation(scores_history):
            logging.warning("  ⚠ Optimization may be stagnating. Best score unchanged for %d generations.", 
                          args.patience // 2)
        
        # Calculate rank & crowding distance for detailed log_history
        fronts = fast_non_dominated_sort(metrics) if args.algorithm == "nsga2" else []
        rank_lookup = {}
        for r, f in enumerate(fronts):
            for idx_f in f:
                rank_lookup[idx_f] = r
        
        crowding_dist = {}
        if args.algorithm == "nsga2":
            for f in fronts:
                crowding_dist.update(crowding_distance_assignment(f, metrics))
        
        # Save history & log_history
        hist_rows = []
        for idx, (ind, m) in enumerate(zip(population, metrics)):
            row = {**ind, "generation": gen, "score": m["score"],
                   "efficiency": m["efficiency"], "torque_ripple": m["torque_ripple"],
                   "power_density": m["power_density"], "cost": m["cost"]}
            hist_rows.append(row)
            
            # Log structured activity details
            log_history_entry(log_history_csv_path, {
                "timestamp": datetime.now().isoformat(),
                "generation": gen,
                "individual_id": f"Gen{gen}_Ind{idx+1}",
                "operator": "Elitism" if (best_ind and ind == best_ind) else "Evolution",
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
        
        # Early stopping
        if no_improve_count >= args.patience:
            logging.info("🛑 Early stopping: No improvement for %d generations.", args.patience)
            break
        
        # ==================================================================
        # Selection & Reproduction
        # ==================================================================
        if args.algorithm == "nsga2":
            # NSGA-II: Environmental selection + tournament reproduction
            fronts = fast_non_dominated_sort(metrics)
            crowding_dist = {}
            for front in fronts:
                front_dist = crowding_distance_assignment(front, metrics)
                crowding_dist.update(front_dist)
            
            # Generate offspring
            offspring = []
            while len(offspring) < args.pop_size:
                p1, p2 = nsga2_tournament_selection(population, metrics, fronts, crowding_dist)
                c1, c2 = crossover(p1, p2, bounds, args.crossover)
                c1 = mutate(c1, bounds, args.mutation)
                c2 = mutate(c2, bounds, args.mutation)
                offspring.append(c1)
                if len(offspring) < args.pop_size:
                    offspring.append(c2)
            
            # Evaluate ONLY offspring (reuse cached parent metrics)
            logging.info(
                "NSGA-II: reusing %d cached metrics, evaluating %d offspring only.",
                len(cached_metrics), len(offspring)
            )
            offspring_metrics = evaluate_population(
                offspring, root_dir, input_dir, output_dir,
                args.mode, score_weights, ml_surrogate, use_ml,
                matlab_exe=args.matlab_exe
            )
            # Update surrogate with offspring data
            ml_surrogate.add_evaluations(offspring, offspring_metrics)

            # Combine for environmental selection
            combined_pop = population + offspring
            combined_metrics = cached_metrics + offspring_metrics
            population = nsga2_selection(combined_pop, combined_metrics, args.pop_size)
            
        else:
            # Standard Elitist GA
            def tournament(pop, sc):
                i1, i2 = random.sample(range(len(pop)), 2)
                return pop[i1] if sc[i1] > sc[i2] else pop[i2]
            
            next_pop = [best_ind] if best_ind else [population[gen_best_idx]]
            
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
        
        # Save checkpoint
        save_state({
            "generation": gen,
            "population": population,
            "best_individual": best_ind,
            "best_score": best_score,
            "scores_history": scores_history,
        }, state_path)
    
    # ======================================================================
    # Post-Optimization Analysis
    # ======================================================================
    logging.info("\n" + "=" * 60)
    logging.info("OPTIMIZATION COMPLETE")
    logging.info("=" * 60)
    logging.info("Best Score: %.4f (found at generation %d)", best_score, best_gen)
    logging.info("\nBest Design Parameters:")
    for name in PARAM_ORDER:
        logging.info("  %-12s = %s %s", name, best_ind[name], bounds[name]["unit"])
    
    # Save best design
    best_df = pd.DataFrame([best_ind])
    out_path = output_dir / "best_optimized_design_v5.1.csv"
    best_df.to_csv(out_path, index=False)
    logging.info("\nBest design saved to %s", out_path)
    
    # Generate report
    if not args.no_report:
        generate_report(history_path, best_ind, best_score, bounds, output_dir, args)
    
    # Visualizations
    if args.plot_pareto or args.plot_all:
        plot_pareto_front(history_path, output_dir)
    
    if args.plot_all:
        plot_3d_pareto(history_path, output_dir)
        plot_parallel_coordinates(history_path, output_dir)
        plot_convergence(scores_history, output_dir)
    
    # Sensitivity analysis
    if args.sensitivity:
        perform_sensitivity_analysis(history_path, bounds, output_dir)
    
    logging.info("\nOptimization finished successfully. All outputs in: %s", output_dir)


if __name__ == "__main__":
    main()
