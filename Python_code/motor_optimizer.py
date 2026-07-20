import os
import sys
import random
import argparse
import subprocess
import pandas as pd
import numpy as np
import matplotlib.pyplot as plt

# Constraints constants
DS_OUT = 240.0
L_STK = 134.0
PENALTY_SCORE = -10000.0

def load_bounds(bounds_path):
    """
    Loads parameter bounds and steps from the Excel file.
    """
    df = pd.read_excel(bounds_path, sheet_name=0)
    bounds = {}
    for _, row in df.iterrows():
        name = row['Parameter'].strip()
        bounds[name] = {
            'lower': float(row['Lower_Limit']),
            'upper': float(row['Upper_Limit']),
            'step': float(row['Step']),
            'unit': str(row['Unit']) if pd.notna(row['Unit']) else ""
        }
    return bounds

def snap_to_step(value, lower, upper, step):
    """
    Snaps a value to the nearest step within lower and upper bounds.
    """
    if step <= 0:
        return np.clip(value, lower, upper)
    
    steps_count = round((value - lower) / step)
    snapped = lower + steps_count * step
    snapped = round(snapped, 6)
    return np.clip(snapped, lower, upper)

def validate_constraints(params):
    """
    Validates design constraints as defined in the requirements.
    """
    Dr_in = params.get('Dr_in')
    Air_gap = params.get('Air_gap')
    Lamda = params.get('Lamda')
    Hs0 = params.get('Hs0')
    Hs1 = params.get('Hs1')
    Hs2 = params.get('Hs2')
    B1 = params.get('B1')
    Mt = params.get('Mt')

    # Constraint 1: Hs0 + Hs1 + Hs2 < [(Ds_out - Ds_in)/2] - 12.25
    # Ds_in = D_ag + air_gap = Lstk / lamda + air_gap
    Ds_in = (L_STK / Lamda) + Air_gap
    max_hs_sum = ((DS_OUT - Ds_in) / 2.0) - 12.25
    if Hs0 + Hs1 + Hs2 >= max_hs_sum:
        return False

    # Constraint 2: B1 <= Mt - 0.3
    if B1 > (Mt - 0.3):
        return False

    return True

def generate_random_individual(bounds):
    """
    Generates a single valid individual within bounds and satisfying all constraints.
    """
    attempts = 0
    while attempts < 1000:
        ind = {}
        for name, info in bounds.items():
            l = info['lower']
            u = info['upper']
            s = info['step']
            n_steps = int(round((u - l) / s))
            k = random.randint(0, n_steps)
            val = l + k * s
            ind[name] = round(val, 6)
        
        if validate_constraints(ind):
            return ind
        attempts += 1
    
    # Fallback to a known valid seed if random generation fails
    return {
        'Dr_in': 70.0, 'Air_gap': 1.0, 'Lamda': 0.9, 'Bridge': 1.5,
        'Hs0': 1.1899, 'Hs1': 1.5, 'Hs2': 18.07656, 'Bs0': 2.1128,
        'Bs1': 6.90142, 'Bs2': 10.88076, 'O1': 5.4, 'O2': 6.0,
        'B1': 3.5, 'rib': 2.0, 'hrib': 2.4, 'Mt': 5.282,
        'Mw': 25.44156, 'magDmin': 10.0, 'thet_deg': 30.0
    }

def tournament_selection(population, scores, tournament_size=3):
    """
    Selects the best individual out of a random tournament sample.
    """
    idxs = random.sample(range(len(population)), k=tournament_size)
    best_idx = max(idxs, key=lambda i: scores[i])
    return population[best_idx]

def crossover_blx(parent1, parent2, bounds, alpha=0.5):
    """
    Performs Blend Crossover (BLX-alpha) and validates bounds.
    """
    child1 = {}
    child2 = {}
    for name, info in bounds.items():
        l = info['lower']
        u = info['upper']
        s = info['step']
        p1 = parent1[name]
        p2 = parent2[name]
        
        lower = min(p1, p2) - alpha * abs(p1 - p2)
        upper = max(p1, p2) + alpha * abs(p1 - p2)
        
        c1 = random.uniform(lower, upper)
        c2 = random.uniform(lower, upper)
        
        child1[name] = snap_to_step(c1, l, u, s)
        child2[name] = snap_to_step(c2, l, u, s)
    return child1, child2

def mutate_polynomial(individual, bounds, eta=20, pm=0.2):
    """
    Performs Polynomial Mutation on the individual.
    """
    mutated = individual.copy()
    for name, info in bounds.items():
        if random.random() < pm:
            l = info['lower']
            u = info['upper']
            s = info['step']
            value = mutated[name]
            delta1 = (value - l) / (u - l + 1e-9)
            delta2 = (u - value) / (u - l + 1e-9)
            rand = random.random()
            if rand <= 0.5:
                xy = 1.0 - delta1
                val = 1.0 - (2.0 * rand + (1.0 - 2.0 * rand) * (xy ** (eta + 1.0)))
            else:
                xy = 1.0 - delta2
                val = 1.0 - (2.0 * (1.0 - rand) + (1.0 - 2.0 * (1.0 - rand)) * (xy ** (eta + 1.0)))
            new_value = value + val * (u - l)
            mutated[name] = snap_to_step(new_value, l, u, s)
    return mutated

def write_param_values(population, param_values_path, param_order):
    """
    Writes the population parameter sets to the Excel file.
    """
    rows = []
    for ind in population:
        row = [ind[name] for name in param_order]
        rows.append(row)
    
    df = pd.DataFrame(rows, columns=param_order)
    df.to_excel(param_values_path, index=False)

def run_matlab(script_dir, matlab_path="matlab"):
    """
    Runs the MATLAB simulation script synchronously.
    """
    print("Launching MATLAB simulation batch...")
    result = subprocess.run([matlab_path, "-batch", "Ai_optimization"], cwd=script_dir, capture_output=True, text=True)
    if result.returncode != 0:
        print("MATLAB execution failed with error:")
        print(result.stderr)
        raise RuntimeError("MATLAB simulation failed.")
    print("MATLAB simulation completed successfully.")

def evaluate_individual_csv(csv_path):
    """
    Parses the simulation CSV output and calculates the objective score.
    """
    if not os.path.exists(csv_path):
        return None
    try:
        df = pd.read_csv(csv_path)
    except Exception:
        return None
    if len(df) == 0:
        return None

    eff_cols = [c for c in df.columns if 'Efficiency' in c or 'Eff' in c]
    tr_cols = [c for c in df.columns if 'TorqueRipple' in c or 'TorqueRip' in c]
    cost_cols = [c for c in df.columns if 'TotalCost' in c or 'TotCost' in c]
    pwr_cols = [c for c in df.columns if 'PowerDensity' in c or 'PwrDens' in c]

    if not eff_cols or not tr_cols:
        return None

    eff_col = eff_cols[0]
    tr_col = tr_cols[0]
    cost_col = cost_cols[0] if cost_cols else None
    pwr_col = pwr_cols[0] if pwr_cols else None

    start_idx = len(df) // 2
    avg_eff = df[eff_col].iloc[start_idx:].mean()
    avg_tr = df[tr_col].iloc[start_idx:].mean()
    avg_cost = df[cost_col].iloc[start_idx:].mean() if cost_col else 0.0
    avg_pwr = df[pwr_col].iloc[start_idx:].mean() if pwr_col else 0.0

    score = avg_eff - avg_tr
    return {
        'score': score,
        'efficiency': avg_eff,
        'torque_ripple': avg_tr,
        'cost': avg_cost,
        'power_density': avg_pwr
    }

def mock_evaluate_individual(ind):
    """
    Mocks the simulation by running an analytical evaluation function.
    """
    eff = 95.0 + (ind['Dr_in'] - 50.0)/40.0 * 2.5 - (ind['Air_gap'] - 0.5) * 1.0
    tr = 25.0 - (ind['Dr_in'] - 50.0)/40.0 * 5.0 + (ind['Air_gap'] - 0.5) * 10.0
    cost = 100.0 + ind['Mt'] * 10.0 + ind['Mw'] * 0.5
    pwr = 0.3 + (ind['Dr_in'] - 50.0)/40.0 * 0.1
    
    score = eff - tr
    return {
        'score': score,
        'efficiency': eff,
        'torque_ripple': tr,
        'cost': cost,
        'power_density': pwr
    }

class SimulationCache:
    def __init__(self, history_path, param_order):
        self.history_path = history_path
        self.param_order = param_order
        self.history_df = self.load_history()

    def load_history(self):
        if os.path.exists(self.history_path):
            try:
                df = pd.read_csv(self.history_path)
                required = self.param_order + ['score', 'efficiency', 'torque_ripple', 'cost', 'power_density']
                if all(c in df.columns for c in required):
                    return df
            except Exception:
                pass
        return pd.DataFrame(columns=self.param_order + ['score', 'efficiency', 'torque_ripple', 'cost', 'power_density'])

    def save_history(self):
        self.history_df.to_csv(self.history_path, index=False)

    def find(self, ind):
        if len(self.history_df) == 0:
            return None
        mask = True
        for name in self.param_order:
            mask = mask & (np.abs(self.history_df[name] - ind[name]) < 1e-5)
        matched = self.history_df[mask]
        if len(matched) > 0:
            row = matched.iloc[0]
            return {
                'score': float(row['score']),
                'efficiency': float(row['efficiency']),
                'torque_ripple': float(row['torque_ripple']),
                'cost': float(row['cost']),
                'power_density': float(row['power_density'])
            }
        return None

    def add(self, ind, metrics):
        row = {name: ind[name] for name in self.param_order}
        row.update(metrics)
        new_row_df = pd.DataFrame([row])
        if len(self.history_df) == 0:
            self.history_df = new_row_df
        else:
            self.history_df = pd.concat([self.history_df, new_row_df], ignore_index=True)
        self.save_history()

def main():
    parser = argparse.ArgumentParser(description="AI Genetic Algorithm Optimizer for V-Shape IPM Motor")
    parser.add_argument("--pop-size", type=int, default=5, help="Population size for GA")
    parser.add_argument("--generations", type=int, default=3, help="Number of generations to optimize")
    parser.add_argument("--mutation-rate", type=float, default=0.2, help="Probability of mutating a parameter")
    parser.add_argument("--offline", action="store_true", help="Run offline simulation (mock evaluations)")
    parser.add_argument("--matlab", type=str, default="matlab", help="Path to MATLAB executable")
    args = parser.parse_args()

    script_dir = os.path.dirname(os.path.abspath(__file__))
    bounds_path = os.path.join(script_dir, "Ai_Optimization_Bounds.xlsx")
    param_values_path = os.path.join(script_dir, "Ai_Optimization_ParamValues.xlsx")
    history_path = os.path.join(script_dir, "simulation_history.csv")

    print("==================================================")
    print("      V-Shape IPM Motor AI Optimization Agent     ")
    print("==================================================")
    print(f"Population Size: {args.pop_size}")
    print(f"Generations    : {args.generations}")
    print(f"Offline Mode   : {args.offline}")
    print("==================================================")

    if not os.path.exists(bounds_path):
        print(f"Error: Bounds file not found at {bounds_path}")
        sys.exit(1)
    
    bounds = load_bounds(bounds_path)
    param_order = list(bounds.keys())
    cache = SimulationCache(history_path, param_order)

    print("\nInitializing valid population...")
    population = [generate_random_individual(bounds) for _ in range(args.pop_size)]
    best_overall_individual = None
    best_overall_score = -float('inf')
    best_overall_metrics = None
    convergence_scores = []

    for gen in range(1, args.generations + 1):
        print(f"\n--- Generation {gen} / {args.generations} ---")
        
        metrics_list = [None] * len(population)
        unseen_indices = []
        unseen_candidates = []

        for idx, ind in enumerate(population):
            cached_metrics = cache.find(ind)
            if cached_metrics is not None:
                metrics_list[idx] = cached_metrics
            else:
                unseen_indices.append(idx)
                unseen_candidates.append(ind)

        print(f"Candidates cached: {len(population) - len(unseen_candidates)} | Candidates to simulate: {len(unseen_candidates)}")

        if len(unseen_candidates) > 0:
            write_param_values(unseen_candidates, param_values_path, param_order)
            
            if args.offline:
                for idx_in_unseen, ind in enumerate(unseen_candidates):
                    metrics = mock_evaluate_individual(ind)
                    pop_idx = unseen_indices[idx_in_unseen]
                    metrics_list[pop_idx] = metrics
                    cache.add(ind, metrics)
            else:
                try:
                    run_matlab(script_dir, args.matlab)
                    for idx_in_unseen, ind in enumerate(unseen_candidates):
                        csv_name = f"output_vars_iter_{idx_in_unseen + 1}.csv"
                        csv_path = os.path.join(script_dir, csv_name)
                        metrics = evaluate_individual_csv(csv_path)
                        pop_idx = unseen_indices[idx_in_unseen]
                        if metrics is None:
                            print(f"Warning: Simulation failed for candidate {pop_idx + 1}. Penalty applied.")
                            metrics = {
                                'score': PENALTY_SCORE, 'efficiency': 0.0, 'torque_ripple': 100.0, 'cost': 999.0, 'power_density': 0.0
                            }
                        metrics_list[pop_idx] = metrics
                        cache.add(ind, metrics)
                except Exception as e:
                    print(f"Execution Error during simulation: {e}")
                    sys.exit(1)

        gen_scores = [m['score'] for m in metrics_list]
        max_gen_score = max(gen_scores)
        convergence_scores.append(max_gen_score)

        for idx, (ind, metrics) in enumerate(zip(population, metrics_list)):
            is_cached_str = " (cached)" if idx not in unseen_indices else ""
            print(f"Candidate {idx+1}{is_cached_str}: Score = {metrics['score']:.3f} | Eff = {metrics['efficiency']:.2f}% | Ripple = {metrics['torque_ripple']:.2f}%")
            
            if metrics['score'] > best_overall_score:
                best_overall_score = metrics['score']
                best_overall_individual = ind.copy()
                best_overall_metrics = metrics.copy()

        if gen < args.generations:
            next_population = []
            
            best_idx = np.argmax(gen_scores)
            next_population.append(population[best_idx].copy())

            attempts = 0
            while len(next_population) < args.pop_size and attempts < 1000:
                p1 = tournament_selection(population, gen_scores)
                p2 = tournament_selection(population, gen_scores)
                c1, c2 = crossover_blx(p1, p2, bounds)
                c1 = mutate_polynomial(c1, bounds, pm=args.mutation_rate)
                c2 = mutate_polynomial(c2, bounds, pm=args.mutation_rate)
                
                if validate_constraints(c1) and len(next_population) < args.pop_size:
                    next_population.append(c1)
                if validate_constraints(c2) and len(next_population) < args.pop_size:
                    next_population.append(c2)
                attempts += 1
            
            while len(next_population) < args.pop_size:
                next_population.append(generate_random_individual(bounds))

            population = next_population

    print("\n==================================================")
    print("             OPTIMIZATION COMPLETE               ")
    print("==================================================")
    print(f"Best Objective Score : {best_overall_score:.4f}")
    print(f"Best Efficiency     : {best_overall_metrics['efficiency']:.2f}%")
    print(f"Best Torque Ripple  : {best_overall_metrics['torque_ripple']:.2f}%")
    print(f"Estimated Cost      : {best_overall_metrics['cost']:.2f}")
    print(f"Power Density       : {best_overall_metrics['power_density']:.3f} kW/kg")
    print("\nBest Parameters:")
    for name, val in best_overall_individual.items():
        print(f"  {name:<10}: {val} {bounds[name]['unit']}")
    print("==================================================")

    plt.figure(figsize=(8, 5))
    plt.plot(range(1, len(convergence_scores) + 1), convergence_scores, marker='o', color='b', label='Best Score')
    plt.title('Optimization Convergence')
    plt.xlabel('Generation')
    plt.ylabel('Score')
    plt.grid(True)
    plt.legend()
    plt.tight_layout()
    plt.savefig(os.path.join(script_dir, "convergence_plot.png"))
    print("Convergence plot saved to convergence_plot.png")

    best_df = pd.DataFrame([best_overall_individual])
    best_df.to_csv(os.path.join(script_dir, "best_optimized_design.csv"), index=False)
    print("Best design saved to best_optimized_design.csv")

if __name__ == "__main__":
    main()
