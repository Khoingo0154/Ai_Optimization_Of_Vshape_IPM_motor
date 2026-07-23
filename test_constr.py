
import sys
from importlib.machinery import SourceFileLoader
opt = SourceFileLoader('opt', 'motor_optimizer_ver5.1_remote.py').load_module()
params = {'Dr_in': 90, 'Air_gap': 0.5, 'Lamda': 0.9, 'Bridge': 2.1, 'Hs0': 1.2, 'Hs1': 2.0, 'Hs2': 29, 'Bs0': 4, 'Bs1': 5, 'Bs2': 9, 'O1': 8, 'O2': 6, 'B1': 3.7, 'rib': 14, 'hrib': 5.5, 'Mt': 6.0, 'Mw': 10, 'magDmin': 10, 'thet_deg': 63}
for c in opt.CONSTRAINTS:
    print(c.__name__, c(params))
print('Total feasible:', opt.is_feasible(params))
