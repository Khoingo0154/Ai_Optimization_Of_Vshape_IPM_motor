# Optimization Requirements for V-Shape IPM Motor

## Project Overview

This project automates batch simulation of a V-Shape Interior Permanent Magnet (IPM) motor design using Ansys Maxwell through MATLAB. The automation allows for sweeping many parameter combinations without manually editing the design in Maxwell each time.

## Input Files

### 1. Ai_Optimization_Bounds.xlsx
Defines the 19 design variables and their limits.

#### Columns:
- `Parameter` - Variable name as defined in the Maxwell design
- `Lower_Limit` - Minimum allowed value
- `Upper_Limit` - Maximum allowed value
- `Step` - Step size for the variable
- `Unit` - Unit string (e.g. `mm`, `deg`); leave blank for unitless values

### 2. Ai_Optimization_ParamValues.xlsx
Contains the actual parameter sets to simulate. Each row is one simulation iteration, and each column corresponds to one of the 19 parameters.

#### Columns (19 total):
1. `Dr_in` - Design variable 1
2. `Air_gap` - Design variable 2  
3. `Lamda` - Design variable 3
4. `Bridge` - Design variable 4
5. `Hs0` - Design variable 5
6. `Hs1` - Design variable 6
7. `Hs2` - Design variable 7
8. `Bs0` - Design variable 8
9. `Bs1` - Design variable 9
10. `Bs2` - Design variable 10
11. `O1` - Design variable 11
12. `O2` - Design variable 12
13. `B1` - Design variable 13
14. `rib` - Design variable 14
15. `hrib` - Design variable 15
16. `Mt` - Design variable 16
17. `Mw` - Design variable 17
18. `magDmin` - Design variable 18
19. `thet_deg` - Design variable 19

## Workflow

1. **Setup** - Loads the bounds file (variable names/units) and the parameter values file
2. **Connect to Maxwell** - Starts (or attaches to) the `Ansoft.ElectronicsDesktop` ActiveX server, opens/activates the project (`Matlab_Ai_Optimization.aedt`), and sets the active design to `Vshape_IPM`
3. **Iterate** - For each row in the parameter values table:
   - Assigns each of the 19 variables via `SetVariableValue` (appending the unit string when one is provided)
   - Resets the setup to time zero (`ResetSetupToTimeZero`)
   - Runs the analysis (`Analyze`)
   - Exports the output variables table to `output_vars_iter_<N>.csv` and reads it back into MATLAB as `outputData`
4. **Cleanup** - Releases the ActiveX COM objects. The project is *not* saved or closed by default

## Output Variables

For each iteration `N`, a file named `output_vars_iter_<N>.csv` is created containing that iteration's exported output variables.

### Output File Structure:
| Time | TorqueRip | Eff | Pin | Pout | TotCost | PwrDens | TotWt | Torque | FluxA | FluxB | FluxC | Vind_A | Vind_B | Vind_C |
|------|-----------|-----|-----|------|---------|---------|-------|--------|-------|-------|-------|--------|--------|--------|
| [ms] | [%]       | [%] | [kW]| [kW] | [-]     | [kW]    | [kg]  | [N.m]  | [Wb]  | [Wb]  | [Wb]  | [V]    | [V]    | [V]    |

### Variables Description:
- **Time** - Time stamp for this row
- **TorqueRip** - Torque ripple [%]
- **Eff** - Motor efficiency [%]
- **Pin** - Input power [kW]
- **Pout** - Output power [kW]
- **TotCost** - Fixed design cost [-]
- **PwrDens** - Power density [kW]
- **TotWt** - Total mass [kg]
- **Torque** - Shaft torque [N.m]
- **FluxA/B/C** - 3-phase flux linkage (measured at the rotor position)
- **Vind_A/B/C** - 3-phase back-EMF (rate of change of flux linkage)

## Customization Notes

- Saving/closing the project: commented out at the end of the script; uncomment `invoke(oProject, 'Save')` and/or `oDesktop.CloseProject(project_name)` if desired.
- Variable order: the order of columns in `Ai_Optimization_ParamValues.xlsx` must match the order of rows (parameters) in `Ai_Optimization_Bounds.xlsx`, since the script maps them positionally via the loop index `i`.

## Optimization Objectives

Based on the documentation, the optimization process focuses on:
1. Maximizing motor efficiency (Eff)
2. Maximizing power density (PwrDens)
3. Minimizing torque ripple (TorqueRip)
4. Optimizing overall motor performance metrics
5. Balancing design constraints within specified limits

## Implementation Details

The MATLAB script `Ai_optimization.m` automates the entire process:
- Reads parameter values from Excel files
- Sets up Ansys Maxwell design variables
- Runs simulations
- Collects and exports output data
- Generates CSV files for each simulation iteration

The output CSV files are used by a Python script to analyze the results and determine the next generation of design variable sets.