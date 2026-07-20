# Optimization Requirements

## Parameters

| Name | Description | Type | Initial Value | Lower Limit | Upper Limit | Step |
|------|-------------|------|----------------|-------------|-------------|------|
| Dr_out | Outer diameter of Rotor | Calculated | Ds_in - 2*air_gap | - | - | - |
| Dr_in | Inner diameter of Rotor | Variable | 90mm | 50mm | 90mm | 5mm |
| L_stk | Stack Length | Constant | 134 | - | - | - |
| Speed_rpm | Rotational speed of rotor in Rotations per minute | Calculated | 120 · f₀ / PolesNum | - | - | - |
| Air_gap | Length of air gap between rotor and stator | Variable | 1mm | 0.5mm | 1.5mm | 0.1mm |
| Lamda | Lstack to D_ag ratio | Variable | 0.9 | 0.8 | 1 | 0.1 |
| D_ag | Diameter of middle of air gap | Calculated | Lstk/lamda | - | - | - |
| Ds_out | Outer Diameter of Stator | Constant | 240mm | - | - | - |
| Ds_in | Inner Diameter of Stator | Calculated | D_ag + air_gap | - | - | - |
| Bridge | Distance between outer rotor diameter to magnet pole "holes" | Variable | 1.5mm | 1mm | 3mm | 0.1mm |
| SlotNum | Number of stator Slots | Constant | 36 | - | - | - |
| A_slot | Slot Area | Measured | 213.1743016 mm² | - | - | - |
| Hs0 | Tooth slot height | Variable | 1.1899mm | 1mm | 2mm | 0.1mm |
| Hs1 | Slot "slope" | Variable | 1.5mm | 1mm | 2mm | 0.1mm |
| Hs2 | Slot height | Variable | 18.07656mm | 16mm | Hs0+Hs1+Hs2 < [(Ds_out-Ds_in)/2] - 12.25mm (38mm) | 1mm |
| Bs0 | Slot opening width | Variable | 2.1128mm | 1.5mm | 4mm | 0.5mm |
| Bs1 | Lower Slot width | Variable | 6.90142mm | 3mm | 10mm | 0.5mm |
| Bs2 | Upper Slot width | Variable | 10.88076 | 5mm | 14mm | 1mm |
| PolesNum | Number of rotor poles | Constant | 6 | - | - | - |
| O1 | Distance between duct bottom | Variable | 5.4mm | 0mm | 13mm | 1mm |
| O2 | Duct distance from inner rotor diameter | Variable | 6mm | 2mm | 7mm | 0.5mm |
| B1 | Duct thickness | Variable | 3.5mm | 3.2mm | Mt - 0.3mm (5.7mm) | 0.5mm |
| rib | Rib width | Variable | 2mm | 2mm | 15mm | 1mm |
| Hrib | Rib height | Variable | 2.4mm | 2mm | 6mm | 0.5mm |
| Mt | Magnet thickness | Variable | 5.282mm | 4mm | 6mm | 0.2mm |
| Mw | Magnet width | Variable | 25.44156mm | 10mm | 30mm | 2mm |
| magDmin | Minimum distance between magnets | Variable | 10mm | 0mm | 10mm | 1mm |
| D1 | Minimum diameter of Permanent magnet ducts | Calculated | Ds_in - 2*air_gap - 2*bridge | - | - | - |
| Acond | Area of single conductor | Calculated | I_max / J | - | - | - |
| N | Number of conductors | Calculated | ⌈0.7 · A_slot / A_cond⌉ | - | - | - |
| Imax | Input current maximum | Constant | 200A | - | - | - |
| J | Current density constant | Constant | 5.5A | - | - | - |
| f0 | Current frequency | Constant | 50Hz | - | - | - |
| t0 | Time when we achieve maximum torque | Calculated | 0ms | - | - | - |
| Thet | Current excitation phase shift in radians | Calculated | thet_deg · π / 180 | - | - | - |
| Thet_deg | Current excitation phase shift in degrees | Variable | 30° | 0° | 90° | 1° |
| Rotor initial angle | - | Constant | -20° | - | - | - |

**Note:** A_slot approximation: hs2 * bs2 (undershoot)

**Decision note:** Determined based on min{Ds_in} so we don't lose valuable samples on optimization and when it's too big it just won't run that model (in same pole).

---

## Equations

### 1. Efficiency

$$\text{Power Efficiency} = \frac{P_{out}}{P_{in}}$$

**Unit:** None  
**Goal:** Maximize

### 2. Power Density

$$\text{Power Density} = \frac{P_{out}}{W_{total}}$$

**Unit:** W/Kg  
**Goal:** Maximize

### 3. Material Cost

$$\text{Total Cost} = \sum V_i \cdot \text{costPerVolume}_i, \quad i \in \{\text{all materials used}\}$$

**Unit:** $  
**Goal:** Minimize

### 4. Torque Ripples

$$\text{Torque Ripples} = \frac{\text{pk2pk(Torque)}}{\text{mean(Torque)}} \times 100$$

**Unit:** %  
**Goal:** Minimize

### 5. Flux Linkage

#### Total Airgap Flux:

$$\varphi_{total} = B_{av} \pi D_{sout} L_{stk}$$

Where $B_{av}$ is the Magnetic Loading

#### Stator Tooth Flux:

$$\varphi_{st} = \frac{\varphi_{total}}{SlotNum}$$

#### Pole Flux:

$$\varphi_p = \frac{\varphi_{total}}{PolesNum}$$

**Unit:** Wb  
**Goal:** Maximize

---

## Objective Function

### Primary Objective

**Maximize Efficiency, Minimize Torque ripple**

### Optimization Approach

By subtracting the torque ripple from the Efficiency, we get a 3D mesh that allows us to visualize the design space. The objective function is defined as:

$$\text{Objective Function} = \text{Efficiency} - \text{Torque ripples}$$

### Design Philosophy

Designs with high efficiency and low torque ripples have a higher score than those with low efficiency and high torque ripples. This combined metric effectively balances the two competing objectives in the optimization process.

---

## Notes

- The 3D surface plot shows the relationship between Efficiency and Torque Ripples, illustrating the trade-off space for the optimization problem.
- All variable parameters have defined ranges and step sizes for the optimization algorithm to explore.
- Calculated parameters are derived from variable and constant parameters using the specified formulas.
- Constraints must be satisfied during optimization (e.g., Hs0 + Hs1 + Hs2 constraint for slot dimensions).
