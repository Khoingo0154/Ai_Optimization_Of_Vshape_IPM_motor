# Optimization Requirements for V-Shape IPM Motor

## Parameters

### Variable Parameters

| Name | Description | Initial Value | Lower Limit | Upper Limit | Step |
|------|-------------|---------------|-------------|-------------|------|
| Dr_in | Inner diameter of Rotor | 90mm | 50mm | 90mm | 5mm |
| Air_gap | Length of air gap between rotor and stator | 1mm | 0.5mm | 1.5mm | 0.1mm |
| Lamda | Lstack to D_ag ratio | 0.9 | 0.8 | 1 | 0.1 |
| Bridge | Distance between outer rotor diameter to magnet pole "holes" | 1.5mm | 1mm | 3mm | 0.1mm |
| Hs0 | Tooth slot height | 1.1899mm | 1mm | 2mm | 0.1mm |
| Hs1 | Slot "slope" | 1.5mm | 1mm | 2mm | 0.1mm |
| Hs2 | Slot height | 18.07656mm | 16mm | Hs0+Hs1+Hs2 < [(Ds_out-Ds_in)/2] - 12.25mm (38mm) | 1mm |
| Bs0 | Slot opening width | 2.1128mm | 1.5mm | 4mm | 0.5mm |
| Bs1 | Lower Slot width | 6.90142mm | 3mm | 10mm | 0.5mm |
| Bs2 | Upper Slot width | 10.88076 | 5mm | 14mm | 1mm |
| O1 | Distance between duct bottom | 5.4mm | 0mm | 13mm | 1mm |
| O2 | Duct distance from inner rotor diameter | 6mm | 2mm | 7mm | 0.5mm |
| B1 | Duct thickness | 3.5mm | 3.2mm | Mt - 0.3mm (5.7mm) | 0.5mm |
| rib | Rib width | 2mm | 2mm | 15mm | 1mm |
| hrib | Rib height | 2.4mm | 2mm | 6mm | 0.5mm |
| Mt | Magnet thickness | 5.282mm | 4mm | 6mm | 0.2mm |
| Mw | Magnet width | 25.44156mm | 10mm | 30mm | 2mm |
| magDmin | Minimum distance between magnets (in same pole) | 10mm | 0mm | 10mm | 1mm |
| thet_deg | Current excitation phase shift in degrees | 30° | 0° | 90° | 1° |

### Constant Parameters

| Name | Description | Value |
|------|-------------|-------|
| L_stk | Stack Length | 134 |
| Ds_out | Outer Diameter of Stator | 240mm |
| SlotNum | Number of stator Slots | 36 |
| PolesNum | Number of rotor poles | 6 |
| Imax | Input current maximum | 200A |
| J | Current density | 5.5A |
| f0 | Current frequency | 50Hz |
| Rotor initial angle | Rotor initial angle | −20° |

### Calculated Parameters

| Name | Description | Formula |
|------|-------------|---------|
| Dr_out | Outer diameter of Rotor | Ds_in − 2 × air_gap |
| Speed_rpm | Rotational speed of rotor (RPM) | (120 × f0) / PolesNum |
| D_ag | Diameter of middle of air gap | Lstk / Lamda |
| Ds_in | Inner Diameter of Stator | D_ag + air_gap |
| D1 | Minimum diameter of Permanent magnet ducts | Ds_in − 2 × air_gap − 2 × bridge |
| Acond | Area of single conductor | Imax / J |
| N | Number of conductors | ⌈0.7 × A_slot / Acond⌉ |
| t0 | Time when we achieve maximum torque | 0ms |
| Thet | Current excitation phase shift in radians | thet_deg × π / 180 |

### Measured Parameters

| Name | Description | Value |
|------|-------------|-------|
| A_slot | Slot Area | 213.1743016 mm² |

## Equations

### Efficiency

$$PowerEfficiency = \frac{P_{out}}{P_{in}}$$

- Unit: None
- **Goal: Maximize**

### Power Density

$$PowerDensity = \frac{P_{out}}{W_{total}}$$

- Unit: W/Kg
- **Goal: Maximize**

### Material Cost

$$TotalCost = \sum V_i \cdot costPerVolume_i, \quad i \in \{all\ materials\ used\}$$

- Unit: $
- **Goal: Minimize**

### Torque Ripples

$$TorqueRipples = \frac{pk2pk(Torque)}{mean(Torque)} \times 100$$

- Unit: %
- **Goal: Minimize**

### Flux Linkage

**Total airgap flux:**

$$\varphi_{total} = B_{av} \cdot \pi \cdot D_{sout} \cdot L_{stk}$$

where $B_{av}$ is the Magnetic Loading.

**Stator Tooth flux:**

$$\varphi_{st} = \frac{\varphi_{total}}{SlotNum}$$

**Pole flux:**

$$\varphi_p = \frac{\varphi_{total}}{PolesNum}$$

- Unit: Wb
- **Goal: Maximize**

## Objective Function

$$Objective\ function = Efficiency - Torque\ ripples$$

By subtracting the torque ripple from the Efficiency, designs with high efficiency and low torque ripples have a higher score than the ones with low efficiency and high torque ripples.

## Output Variables

For each iteration `N`, a file named `output_vars_iter_<N>.csv` is created containing that iteration's exported output variables.

### Output File Structure

| Time | TorqueRip | Eff | Pin | Pout | TotCost | PwrDens | TotWt | Torque | FluxA | FluxB | FluxC | Vind_A | Vind_B | Vind_C |
|------|-----------|-----|-----|------|---------|---------|-------|--------|-------|-------|-------|--------|--------|--------|
| [ms] | [%] | [%] | [kW] | [kW] | [-] | [kW] | [kg] | [N.m] | [Wb] | [Wb] | [Wb] | [V] | [V] | [V] |

### Variables Description

- **Time** — Time stamp for this row
- **TorqueRip** — Torque ripple [%]
- **Eff** — Motor efficiency [%]
- **Pin** — Input power [kW]
- **Pout** — Output power [kW]
- **TotCost** — Fixed design cost [-]
- **PwrDens** — Power density [kW]
- **TotWt** — Total mass [kg]
- **Torque** — Shaft torque [N.m]
- **FluxA/B/C** — 3-phase flux linkage (measured at the rotor position)
- **Vind_A/B/C** — 3-phase back-EMF (rate of change of flux linkage)

## Comments from PDF

- **Hs2 upper limit**: Decided based on min{Ds_in} so we don't lose valuable samples on optimization and when it's too big it's just not going to run that model.
- **A_slot**: Approximation Hs2 × Bs2 (undershoots).