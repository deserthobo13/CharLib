from PySpice import Circuit, Simulator
from pathlib import Path

'''
def static_power_leakage(i_leakage, vdd):
    

    vdd = settings.vdd.voltage * settings.units.voltage
    vss = settings.vss.voltage * settings.units.voltage
    return (i_leakage * vdd)
    
def static_current(inputs_logic_table):
    """Put logic here for calculatign static current"""
    return i_leakage
'''

circuit = Circuit('buff_1')
circuit_xor = Circuit('xor_2')
# Add two paths needed for the circuit
circuit.include(Path("/home/holland/Downloads/CharLib/gf180_temp/models/design.ngspice")) 
circuit.include(Path("/home/holland/Downloads/CharLib/gf180_temp/cells/gf180mcu_osu_sc_gp12t3v3__buf_1.spice"))
circuit.lib(Path("/home/holland/Downloads/CharLib/gf180_temp/models/sm141064.ngspice"), "typical")

circuit_xor.include(Path("/home/holland/Downloads/CharLib/gf180_temp/models/design.ngspice")) 
circuit_xor.include(Path("/home/holland/Downloads/CharLib/gf180_temp/cells/gf180mcu_osu_sc_gp12t3v3__xor2_1.spice"))
circuit_xor.lib(Path("/home/holland/Downloads/CharLib/gf180_temp/models/sm141064.ngspice"), "typical")


circuit.V('dd', 'vdd', circuit.gnd, 3.3)
circuit.V('ss', 'vss', circuit.gnd, 0.0)
circuit.V('in', 'vin', circuit.gnd, 0.0)
circuit.R('in', circuit.gnd, 'vin', 1e9)
circuit.X("buf", "GF180MCU_OSU_SC_GP12T3V3__BUF_1", 'vout','vin', 'vdd', 'vss')
print(circuit)

circuit_xor.V('dd', 'vdd', circuit_xor.gnd, 3.3)
circuit_xor.V('ss', 'vss', circuit_xor.gnd, 0.0)
circuit_xor.V('a', 'vin1', circuit_xor.gnd, 3.3)
circuit_xor.V('b', 'vin2', circuit_xor.gnd, 3.3)
circuit_xor.R('in1', circuit_xor.gnd, 'vin1', 1e9)
circuit_xor.R('in2', circuit_xor.gnd, 'vin2', 1e9)
circuit_xor.R('out', 'vout', circuit_xor.gnd, 1e3)
circuit_xor.X("xor", "GF180MCU_OSU_SC_GP12T3V3__XOR2_1", 'vin1','vin2', 'vout', 'vdd', 'vss')
print(circuit_xor)

simulator = Simulator.factory()

simulation = simulator.simulation(
    circuit,
    temperature=25,
    nominal_temperature=25
)

analysis = simulation.operating_point()
#i_leakage = analysis.VDD['i']
print(analysis.VDD)
print(f'Node: {analysis.VDD}, {float(analysis.VDD)}')


simulation = simulator.simulation(
    circuit_xor,
    temperature=25,
    nominal_temperature=25
)

analysis = simulation.operating_point()
print(analysis.VDD)
print(f'Node: {analysis.VDD}, {float(analysis.VDD)}')

# inputs = ['00','01','10', '11']
