"""This module contains test managers for various types of standard cells"""

import threading
from pathlib import Path

import numpy as np
import matplotlib.pyplot as plt

from PySpice.Spice.Library import SpiceLibrary
from PySpice.Spice.Netlist import Circuit
from PySpice.Unit import *

from characterizer.functions import *
from characterizer.Harness import CombinationalHarness, SequentialHarness, filter_harnesses_by_ports, find_harness_by_arc
from characterizer.LogicParser import parse_logic
from liberty.export import Cell, Pin

class TestManager:
    """A test manager for a standard cell"""
    def __init__ (self, name: str, in_ports: str|list, out_ports: list|None, functions: str|list, **kwargs):
        """Create a new TestManager for a standard cell.

        A TestManager manages cell data, runs simulations on cells, and stores results
        on the cell.
        
        :param name: cell name
        :param in_ports: a list of input pin names
        :param out_ports: a list of output pin names
        :param functions: a list of functions implemented by each of the cell's outputs (in verilog syntax)
        :param **kwargs: a dict of configuration and test parameters for the cell, including
            - netlist: path to the cell's spice netlist
            - model: path to transistor spice models
            - slews: input slew rates to test
            - loads: output capacitave loads to test
            - simulation_timestep: the time increment to use during simulation
            - test_vectors: a list of test vectors to run against this cell"""
        # Initialize the cell under test
        self._cell = Cell(name, kwargs.get('area', 0))
        for pin_name in in_ports:
            self.cell.add_pin(pin_name, 'input')
        for pin_name in out_ports:
            self.cell.add_pin(pin_name, 'output')

        # Parse functions and add to pins
        if isinstance(functions, str):
            functions = functions.upper().split() # Capitalize and split on space, then proceed
        if isinstance(functions, list):
            # Should be in the format ['Y=expr1', 'Z=expr2']
            for func in functions:
                if '=' in func:
                    func_pin, expr = func.split('=')
                    # Check for nonblocking assign in LHS of equation
                    if '<' in func_pin:
                        func_pin = func_pin.replace('<','').strip()
                    for pin_name in out_ports:
                        if pin_name == func_pin:
                            if parse_logic(expr):
                                # TODO: Check if we already recognize this function (instead of creating a new one)
                                self.cell[pin_name].function = Function(expr)
                            else:
                                raise ValueError(f'Invalid function "{expr}"')
                else:
                    raise ValueError(f'Expected an expression of the form "Y=A Z=B" for cell function, got "{func}"')

        # Characterization settings
        self.netlist = kwargs.get('netlist')
        self.models = kwargs.get('models', [])
        self._in_slews = kwargs.get('slews', [])
        self._out_loads = kwargs.get('loads', [])
        self._sim_timestep = 0
        if 'simulation_timestep' in kwargs:
            self.sim_timestep = kwargs['simulation_timestep']
        self.stored_test_vectors = kwargs.get('test_vectors')

        # Behavioral/internal-use settings
        self.plots = kwargs.get('plots', [])
        self._is_exported = False

    def __str__(self) -> str:
        lines = []
        lines.append(f'Test Manager for cell {self.cell.name}')
        lines.append(f'    Inputs:              {", ".join([p.name for p in self.in_ports])}')
        lines.append(f'    Outputs:             {", ".join([p.name for p in self.out_ports])}')
        lines.append('    Functions:')
        for p,f in zip(self.out_ports, self.functions):
            lines.append(f'        {p}={f}')
        if self.netlist:
            lines.append(f'    Netlist:             {str(self.netlist)}')
            lines.append(f'    Definition:          {self.definition().rstrip()}')
            lines.append(f'    Instance:            {self.instance()}')
        if self.in_slews:
            lines.append('    Simulation slew rates:')
            for slope in self.in_slews:
                lines.append(f'        {str(slope)}')
        if self.out_loads:
            lines.append('    Simulation load capacitances:')
            for load in self.out_loads:
                lines.append(f'        {str(load)}')
        lines.append(f'    Simulation timestep: {str(self.sim_timestep)}')
        return '\n'.join(lines)

    @property
    def cell(self) -> Cell:
        """Return the cell under test"""
        return self._cell

    @property
    def in_ports(self) -> list:
        """Return cell input io pins."""
        return [pin for pin in self.cell.pins.values() if pin.direction == 'input' and pin.is_io()]

    @property
    def out_ports(self) -> list:
        """Return cell output io pins."""
        return [pin for pin in self.cell.pins.values() if pin.direction == 'output' and pin.is_io()]

    @property
    def functions(self) -> list:
        """Return a list of functions on this cell's output pins."""
        return [pin.function for pin in self.out_ports]

    @property
    def models(self) -> list:
        """Return cell models"""
        return self._models

    @models.setter
    def models(self, value):
        models = []
        """Set paths to cell transistor models"""
        for model in value:
            modelargs = model.split()
            path = Path(modelargs.pop(0))
            if modelargs: # If the list is not empty (e.g. there is a section parameter)
                section = modelargs.pop(0)
                # Use a tuple so that this is included with .lib path section syntax
                if not path.is_file():
                    raise ValueError(f'Invalid model {path} {section}: {path} is not a file')
                models.append((path, section))
            else:
                if path.is_dir():
                    models.append(SpiceLibrary(path))
                elif path.is_file():
                    models.append(path)
                else:
                    raise FileNotFoundError(f'File {value} not found')
        self._models = models

    def _include_models(self, circuit):
        """Include models in the circuit netlist."""
        for model in self.models:
            if isinstance(model, SpiceLibrary):
                for device in self.used_models():
                    # TODO: Handle the case where we have multiple spice libraries
                    circuit.include(model[device])
            elif isinstance(model, Path):
                circuit.include(model)
            elif isinstance(model, tuple):
                circuit.lib(*model)

    @property
    def netlist(self) -> str:
        """Return path to cell netlist."""
        return self._netlist

    @netlist.setter
    def netlist(self, value):
        """Set path to cell netlist."""
        if isinstance(value, (str, Path)):
            if not Path(value).is_file():
                raise ValueError(f'Invalid value for netlist: {value} is not a file')
            self._netlist = Path(value)
        else:
            raise TypeError(f'Invalid type for netlist: {type(value)}')

    def definition(self) -> str:
        """Return the cell's spice definition"""
        # Search the netlist file for the circuit definition
        with open(self.netlist, 'r') as file:
            for line in file:
                if self.cell.name in line.upper() and 'SUBCKT' in line.upper():
                    file.close()
                    return line
            # If we reach this line before returning, the netlist file doesn't contain a circuit definition
            file.close()
            raise ValueError(f'No cell definition found in netlist {self.netlist}')

    def instance(self) -> str:
        """Return a subcircuit instantiation for this cell."""
        # Reorganize the definition into an instantiation with instance name XDUT
        # TODO: Instance name should probably be configurable from library settings
        instance = self.definition().split()[1:]  # Delete .subckt
        instance.append(instance.pop(0))        # Move circuit name to last element
        instance.insert(0, 'XDUT')                # Insert instance name
        return ' '.join(instance)

    def used_models(self) -> list:
        """Return a list of subcircuits used by this cell."""
        subckts = []
        with open(self.netlist, 'r') as file:
            for line in file:
                if line.lower().startswith('x'):
                    # Get the subckt name
                    # This should be the last item that doesn't contain =
                    for term in reversed(line.split()):
                        if '=' not in term:
                            subckts.append(term)
                            break
            file.close()
        print(subckts)
        return subckts

    @property
    def in_slews(self) -> list:
        """Return slew rates to use during testing."""
        return self._in_slews

    def add_in_slew(self, value: float):
        """Add a slew rate to the list of slew rates."""
        self._in_slews.append(float(value))

    @property
    def out_loads(self) -> list:
        """Return output capacitive loads to use during testing."""
        return self._out_loads

    def add_out_load(self, value: float):
        """Add a load to the list of output loads."""
        self._out_loads.append(float(value))

    @property
    def plots(self) -> list:
        """Return plotting configuration."""
        return self._plots
    
    @plots.setter
    def plots(self, value):
        """Set plot configuration

        :param value: a str or list specifying which plot types to generate."""
        if value == 'all':
            self._plots = ['io', 'delay', 'power']
        elif value == 'none':
            self._plots = []
        elif isinstance(value, list):
            self._plots = value
        else:
            raise ValueError(f'Invalid value for plots: "{value}"')

    @property
    def is_exported(self) -> bool:
        """Return whether the results have been exported"""
        return self._is_exported

    def set_exported(self):
        """Set a flag that this test manager's results have been exported"""
        self._is_exported = True

    @property
    def sim_timestep(self):
        """Return simulation timestep."""
        return self._sim_timestep

    @sim_timestep.setter
    def sim_timestep(self, value):
        """Set simulation timestep.
        
        :param value: The timestep to use during simulation. If `'auto'`, use 1/10 of smalleset slew rate."""
        if value == 'auto':
            if self.in_slews:
                # Use 1/10th of minimum slew rate
                self._sim_timestep = min(self.in_slews)/10.0
            else:
                raise ValueError('Cannot use auto for sim_timestep unless in_slews is set first!')
        elif isinstance(value, (float, int, str)):
            self._sim_timestep = float(value)
        else:
            raise TypeError(f'Invalid type for sim_timestep: {type(value)}')

    @property
    def test_vectors(self) -> list:
        """Generate a list of test vectors from this cell's functions"""
        # If given test vectors during configuration, use those
        if self.stored_test_vectors:
            return self.stored_test_vectors
        # Otherwise use functions to generate test vectors
        test_vectors = []
        for pin in self.out_ports:
            test_vectors += pin.function.test_vectors
        return test_vectors

    def _run_input_capacitance(self, settings, target_pin):
        """Measure the input capacitance of target_pin.

        Assuming a black-box model, treat the cell as a grounded capacitor with fixed capacitance.
        Perform an AC sweep on the circuit and evaluate the capacitance as d/ds(i(s)/v(s))."""
        print(f'Running input_capacitance for pin {target_pin} of cell {self.cell.name}')
        # TODO: buffer the input pin with an inverter to improve results
        vdd = settings.vdd.voltage * settings.units.voltage
        vss = settings.vss.voltage * settings.units.voltage
        # TODO: Make these values configurable from settings
        f_start = 10 @ u_Hz
        f_stop = 10 @ u_GHz
        r_in = 10 @ u_GOhm
        i_in = 1 @ u_uA
        r_out = 10 @ u_GOhm
        c_out = 1 @ u_pF

        # Initialize circuit
        circuit = Circuit(f'{self.cell.name}_pin_{target_pin}_cap')
        self._include_models(circuit)
        circuit.include(self.netlist)
        circuit.V('dd', 'vdd', circuit.gnd, vdd)
        circuit.V('ss', 'vss', circuit.gnd, vss)
        circuit.I('in', circuit.gnd, 'vin', f'DC 0 AC 1uA')
        circuit.R('in', circuit.gnd, 'vin', r_in)

        # Initialize device under test and wire up ports
        ports = self.definition().upper().split()[1:]
        subcircuit_name = ports.pop(0)
        connections = []
        for port in ports:
            if port == target_pin:
                connections.append('vin')
            elif port == settings.vdd.name.upper():
                connections.append('vdd')
            elif port == settings.vss.name.upper():
                connections.append('vss')
            else:
                # Add a resistor and capacitor to each output
                circuit.C(port, f'v{port}', circuit.gnd, c_out)
                circuit.R(port, f'v{port}', circuit.gnd, r_out)
                connections.append(f'v{port}')
        circuit.X('dut', subcircuit_name, *connections)

        # Initialize simulator
        simulator = circuit.simulator(temperature=settings.temperature,
                                      nominal_temperature=settings.temperature,
                                      simulator=settings.simulator)

        # Measure capacitance as the slope of the conductance
        analysis = simulator.ac('dec', 100, f_start, f_stop)
        impedance = np.abs(analysis.vin)/i_in
        [capacitance, _] = np.polyfit(analysis.frequency, np.reciprocal(impedance)/(2*np.pi), 1)

        return capacitance


class CombinationalTestManager(TestManager):
    """A combinational cell test manager"""

    def characterize(self, settings):
        """Characterize a combinational cell"""
        # Measure input capacitance for all input pins
        for pin in self.in_ports:
            input_capacitance = self._run_input_capacitance(settings, pin.name) @ u_F
            self.cell[pin.name].capacitance = input_capacitance.convert(settings.units.capacitance.prefixed_unit).value

        # Run delay simulation for all test vectors
        unsorted_harnesses = []
        for test_vector in self.test_vectors:
            # Generate harness
            harness = CombinationalHarness(self, test_vector)
            # Determine spice filename prefix
            trial_name = f'delay {self.cell.name} {harness.short_str()}'
            # Run delay characterization
            # Note: ngspice-shared doesn't work with multithreaded as the shared interface must be a singleton
            if settings.use_multithreaded and not settings.simulator == 'ngspice-shared':
                # Split simulation jobs into threads and run multiple simultaneously
                thread_id = 0
                threadlist = []
                for slew in self.in_slews:
                    for load in self.out_loads:
                        thread = threading.Thread(target=self._run_delay,
                                args=([settings, harness, slew, load, trial_name]),
                                name="%d" % thread_id)
                        threadlist.append(thread)
                        thread_id += 1
                [thread.start() for thread in threadlist]
                [thread.join() for thread in threadlist]
            else:
                # Run simulation jobs sequentially
                for slew in self.in_slews:
                    for load in self.out_loads:
                        self._run_delay(settings, harness, slew, load, trial_name)
            # Save harness to the cell
            unsorted_harnesses.append(harness)

        # Filter out harnesses that aren't worst-case conditions
        # We should be left with the critical path rise and fall harnesses for each i/o path
        harnesses = []
        for out_port in self.out_ports:
            for in_port in self.in_ports:
                for direction in ['rise', 'fall']:
                    # Iterate over harnesses that match output, input, and direction
                    matching_harnesses = [harness for harness in filter_harnesses_by_ports(unsorted_harnesses, in_port, out_port) if harness.out_direction == direction]
                    worst_case_harness = matching_harnesses[0]
                    for harness in matching_harnesses:
                        # FIXME: Currently we compare by average prop delay. Consider alternative strategies
                        if worst_case_harness.average_propagation_delay() < harness.average_propagation_delay():
                            worst_case_harness = harness # This harness is worse
                    harnesses.append(worst_case_harness)

        # Store propagation and transport delay in pin timing tables
        for out_port in self.out_ports:
            for in_port in self.in_ports:
                self.cell[out_port.name].add_timing(in_port.name)
                for direction in ['rise', 'fall']:
                    # Identify the correct harness
                    harness = find_harness_by_arc(harnesses, in_port, out_port, direction)

                    # Construct the table
                    index_1 = [str(slew) for slew in self.in_slews]
                    index_2 = [str(load) for load in self.out_loads]
                    prop_values = []
                    tran_values = []
                    for slew in index_1:
                        for load in index_2:
                            result = harness.results[slew][load]
                            prop_value = (result['prop_in_out'] @ u_s).convert(settings.units.time.prefixed_unit).value
                            prop_values.append(f'{prop_value:7f}')
                            tran_value = (result['trans_out'] @ u_s).convert(settings.units.time.prefixed_unit).value
                            tran_values.append(f'{tran_value:7f}')
                    template = f'delay_template_{len(index_1)}x{len(index_2)}' # TODO: Template names should probably be in LibrarySettings
                    self.cell[out_port.name].timing[in_port.name].add_table(f'cell_{direction}', template, prop_values, index_1, index_2)
                    self.cell[out_port.name].timing[in_port.name].add_table(f'{direction}_transition', template, tran_values, index_1, index_2)

        # Display plots
        if 'io' in self.plots:
            [self.plot_io(settings, harness) for harness in harnesses]
        if 'delay' in self.plots:
            [self.cell[out_pin.name].plot_delay(settings, self.cell.name) for out_pin in self.out_ports]
        if 'energy' in self.plots:
            print("Energy plotting not yet supported") # TODO: Add correct energy measurement procedure

    def _run_delay(self, settings, harness: CombinationalHarness, slew, load, trial_name):
        print(f'Running {trial_name} with slew={slew*settings.units.time}, load={load*settings.units.capacitance}')
        harness.results[str(slew)][str(load)] = self._run_delay_trial(settings, harness, slew, load)

    def _run_delay_trial(self, settings, harness: CombinationalHarness, slew, load):
        """Run delay measurement for a single trial"""
        # Set up parameters
        data_slew = slew * settings.units.time
        t_start = data_slew
        t_end = t_start + data_slew
        t_simend = 1000 * data_slew
        vdd = settings.vdd.voltage * settings.units.voltage
        vss = settings.vss.voltage * settings.units.voltage

        # Initialize circuit
        # TODO: Consider adding a driving cell (such as an inverter) to improve accuracy
        circuit = Circuit(f'{self.cell.name}_delay')
        self._include_models(circuit)
        circuit.include(self.netlist)
        (v_start, v_end) = (vss, vdd) if harness.in_direction == 'rise' else (vdd, vss)
        pwl_values = [(0, v_start), (t_start, v_start), (t_end, v_end), (t_simend, v_end)]
        circuit.PieceWiseLinearVoltageSource('in', 'vin', circuit.gnd, values=pwl_values)
        circuit.V('high', 'vhigh', circuit.gnd, vdd)
        circuit.V('low', 'vlow', circuit.gnd, vss)
        circuit.V('dd_dyn', 'vdd_dyn', circuit.gnd, vdd)
        circuit.V('ss_dyn', 'vss_dyn', circuit.gnd, vss)
        circuit.V('o_cap', 'vout', 'wout', circuit.gnd)
        circuit.C('0', 'wout', 'vss_dyn', load * settings.units.capacitance)

        # Initialize device under test subcircuit and wire up ports
        ports = self.definition().upper().split()[1:]
        subcircuit_name = ports.pop(0)
        connections = []
        for port in ports:
            if port == harness.target_in_port.pin.name:
                connections.append('vin')
            elif port == harness.target_out_port.pin.name:
                connections.append('vout')
            elif port == settings.vdd.name.upper():
                connections.append('vdd_dyn')
            elif port == settings.vss.name.upper():
                connections.append('vss_dyn')
            elif port in [pin.pin.name for pin in harness.stable_in_ports]:
                for stable_port in harness.stable_in_ports:
                    if port == stable_port.pin.name:
                        if stable_port.state == '1':
                            connections.append('vhigh')
                        elif stable_port.state == '0':
                            connections.append('vlow')
                        else:
                            raise ValueError(f'Invalid state identified during simulation setup for port {port}: {state}')
            elif port in [pin.pin.name for pin in harness.nontarget_out_ports]:
                for nontarget_port in harness.nontarget_out_ports:
                    if port == nontarget_port.pin.name:
                        connections.append(f'wfloat{str(nontarget_port.state)}')
        if len(connections) is not len(ports):
            raise ValueError(f'Failed to match all ports identified in definition "{self.definition().strip()}"')
        circuit.X('dut', subcircuit_name, *connections)

        # Initialize simulator
        simulator = circuit.simulator(temperature=settings.temperature,
                                      nominal_temperature=settings.temperature,
                                      simulator=settings.simulator)
        simulator.options('autostop', 'nopage', 'nomod', post=1, ingold=2, trtol=1)

        # Measure delay
        if harness.in_direction == 'rise':
            v_prop_start = settings.logic_low_to_high_threshold_voltage()
        else:
            v_prop_start = settings.logic_high_to_low_threshold_voltage()
        if harness.out_direction == 'rise':
            v_prop_end = settings.logic_low_to_high_threshold_voltage()
            v_trans_start = settings.logic_threshold_low_voltage()
            v_trans_end = settings.logic_threshold_high_voltage()
        else:
            v_prop_end = settings.logic_low_to_high_threshold_voltage()
            v_trans_start = settings.logic_threshold_high_voltage()
            v_trans_end = settings.logic_threshold_low_voltage()
        simulator.measure('tran', 'prop_in_out',
                        f'trig v(vin) val={v_prop_start} {harness.in_direction}=1',
                        f'targ v(vout) val={v_prop_end} {harness.out_direction}=1')
        simulator.measure('tran', 'trans_out',
                        f'trig v(vout) val={v_trans_start} {harness.out_direction}=1',
                        f'targ v(vout) val={v_trans_end} {harness.out_direction}=1')

        # Run transient analysis
        return simulator.transient(step_time=(self.sim_timestep * settings.units.time), end_time=t_simend)

    def plot_io(self, settings, harness):
        """Plot I/O voltages vs time"""
        # TODO: Look for ways to generate fewer plots here - maybe a creative 3D plot
        figures = []
        # Group data by slew rate so that inputs are the same
        for slew in self.in_slews:
            # Generate plots for Vin and Vout
            figure, (ax_i, ax_o) = plt.subplots(2,
                sharex=True,
                height_ratios=[3, 7],
                label=f'{self.cell.name} | {harness.arc_str()} | {str(slew*settings.units.time)}'
            )
            volt_units = str(settings.units.voltage.prefixed_unit)
            time_units = str(settings.units.time.prefixed_unit)
            ax_i.set(
                ylabel=f'Vin (pin {harness.target_in_port.pin.name}) [{volt_units}]',
                title='I/O Voltages vs. Time'
            )
            ax_o.set(
                ylabel=f'Vout (pin {harness.target_out_port.pin.name}) [{volt_units}]',
                xlabel=f'Time [{time_units}]'
            )
            for load in self.out_loads:
                analysis = harness.results[str(slew)][str(load)]
                ax_o.plot(analysis.time / settings.units.time, analysis.vout, label=f'Fanout={load*settings.units.capacitance}')
            ax_o.legend()
            ax_i.plot(analysis.time / settings.units.time, analysis.vin)

            # Add lines indicating logic levels and timing
            for ax in [ax_i, ax_o]:
                ax.grid()
                for level in [settings.logic_threshold_low_voltage(), settings.logic_threshold_high_voltage()]:
                    ax.axhline(level, color='0.5', linestyle='--')
                for t in [slew, 2*slew]:
                    ax.axvline(float(t), color='r', linestyle=':')

            figures.append(figure)
        return figures
        

class SequentialTestManager(TestManager):
    """A sequential cell test manager"""

    def __init__(self, name: str, in_ports: list, out_ports: list, clock: str, flops: str, function: str, **kwargs):
        super().__init__(name, in_ports, out_ports, function, **kwargs)
        # TODO: Use flops in place of functions for sequential cells
        self.set = kwargs.get('set')        # set pin name
        self.reset = kwargs.get('reset')    # reset pin name
        self.clock = clock                  # clock pin name
        self.flops = flops                  # registers
        
        self._clock_slew = 0
        if 'clock_slew' in kwargs.keys():
            self.clock_slew = kwargs['clock_slew'] # FIXME: Should this if statement just be `kwargs.get('clock_slew', 'auto')` instead?

        self._sim_setup_highest = 0
        self._sim_setup_lowest = 0
        self._sim_setup_timestep = 0
        self._sim_hold_highest = 0
        self._sim_hold_lowest = 0
        self._sim_hold_timestep = 0
        if 'simulation' in kwargs.keys():
            sim = kwargs['simulation']
            if 'setup' in sim.keys():
                setup = sim['setup']
                self.sim_setup_highest = setup.get('highest')
                self.sim_setup_lowest = setup.get('lowest')
                self.sim_setup_timestep = setup.get('timestep')
            if 'hold' in sim.keys():
                hold = sim['hold']
                self.sim_hold_highest = hold.get('highest')
                self.sim_hold_lowest = hold.get('lowest')
                self.sim_hold_timestep = hold.get('timestep')

    def __str__(self) -> str:
        lines = super().__str__().split('\n')
        function_line_index = lambda : lines.index([line for line in lines if 'Functions:' in line][0])
        # Insert pin names before functions line
        if self.clock:
            lines.insert(function_line_index(), f'    Clock pin:           {self.clock}')
        if self.set:
            lines.insert(function_line_index(), f'    Set pin:             {self.set}')
        if self.reset:
            lines.insert(function_line_index(), f'    Reset pin:           {self.reset}')
        if self.flops:
            lines.insert(function_line_index(), f'    Registers:           {", ".join(self.flops)}')
        return '\n'.join(lines)

    @property
    def clock(self) -> Pin:
        """Return clock pin"""
        return self.cell[self.clock_name]

    @property
    def clock_name(self) -> str:
        """Return clock pin name."""
        return self._clock_name

    @property
    def clock_trigger(self) -> str:
        """Return clock trigger type."""
        return self._clock_trigger

    @clock.setter
    def clock(self, value: str):
        """Assign clock trigger and pin"""
        (self._clock_trigger, pin) = _parse_triggered_pin(value, 'clock')
        self._clock_name = pin.name
        self.cell.add_pin(pin.name, pin.direction, pin.role)

    @property
    def clock_slew(self) -> float:
        """Return clock slew rate"""
        if self.in_slews and not self._clock_slew:
            return min(self.in_slews)
        return self._clock_slew

    @clock_slew.setter
    def clock_slew(self, value):
        """Assign clock slew rate"""
        if isinstance(value, (int, float)):
            if value > 0:
                self._clock_slew = float(value)
            else:
                raise ValueError('Clock slew rate must be greater than zero')
        elif value == 'auto':
            if not self.in_slews:
                raise ValueError('Cannot use auto clock slew rate unless in_slews is set first!')
            else:
                # Use minimum slew rate
                self._clock_slew = min(self.in_slews)
        else:
            raise TypeError(f'Invalid type for clock slew rate: {type(value)}')

    @property
    def set(self):
        """Return set pin"""
        return self.cell.pins.get(self.set_name)

    @property
    def set_name(self) -> str:
        """Return set pin name"""
        return self._set_name

    @property
    def set_trigger(self) -> str:
        "Return set pin trigger type"
        return self._set_trigger

    @set.setter
    def set(self, value):
        """Assign set pin and trigger"""
        (self._set_trigger, pin) = _parse_triggered_pin(value, 'set')
        self._set_name = pin.name
        self.cell.add_pin(pin.name, pin.direction, pin.role)

    @property
    def reset(self):
        """Return reset pin"""
        return self.cell.pins.get(self.reset_name)

    @property
    def reset_name(self) -> str:
        """Return reset pin name"""
        return self._reset_name
    
    @property
    def reset_trigger(self) -> str:
        """Return reset trigger type"""
        return self._reset_trigger

    @reset.setter
    def reset(self, value):
        """Assign reset pin and trigger"""
        (self._reset_trigger, pin) = _parse_triggered_pin(value, 'reset')
        self._reset_name = pin.name
        self.cell.add_pin(pin.name, pin.direction, pin.role)

    @property
    def flops(self) -> list:
        # TODO: Use flops in place of functions for sequential cells
        return self._flops

    @flops.setter
    def flops(self, value):
        # TODO: Use flops in place of functions for sequential cells
        if isinstance(value, str):
            self._flops = value.split()
        elif isinstance(value, list):
            self._flops = value
        else:
            raise TypeError(f'Invalid type for sequential cell flop names: {type(value)}')

    @property
    def sim_setup_lowest(self) -> float:
        return self._sim_setup_lowest

    @sim_setup_lowest.setter
    def sim_setup_lowest(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_setup_lowest = float(value)
            else:
                raise ValueError('sim_setup_lowest must be greater than zero')
        elif value == 'auto':
            if not self.in_slews:
                raise ValueError('Cannot use auto for sim_setup_lowest unless in_slews is set first!')
            else:
                # Use -10 * max input slew rate
                self._sim_setup_lowest = max(self.in_slews) * -10.0
        else:
            raise TypeError(f'Invalid type for sim_setup_lowest: {type(value)}')

    @property
    def sim_setup_highest(self) -> float:
        return self._sim_setup_highest

    @sim_setup_highest.setter
    def sim_setup_highest(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_setup_highest = float(value)
            else:
                raise ValueError('sim_setup_highest must be greater than zero')
        elif value == 'auto':
            if not self.in_slews:
                raise ValueError('Cannot use auto for sim_setup_highest unless in_slews is set first!')
            else:
                # Use 10 * max input slew rate
                self._sim_setup_highest = max(self.in_slews) * 10.0
        else:
            raise TypeError(f'Invalid type for sim_setup_highest: {type(value)}')

    @property
    def sim_setup_timestep(self) -> float:
        return self._sim_setup_timestep

    @sim_setup_timestep.setter
    def sim_setup_timestep(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_setup_timestep = float(value)
            else:
                raise ValueError('sim_hold_timestep must be greater than zero')
        elif value == 'auto':
            if self.in_slews:
                # 1st preference: 1/10th of minimum slew rate
                self._sim_setup_timestep = min(self.in_slews)/10.0
            else:
                # Otherwise, use sim timestep
                self._sim_setup_timestep = self.sim_timestep
        else:
            raise TypeError(f'Invalid type for sim_setup_timestamp: {type(value)}')

    @property
    def sim_hold_lowest(self) -> float:
        return self._sim_hold_lowest

    @sim_hold_lowest.setter
    def sim_hold_lowest(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_hold_lowest = float(value)
            else:
                raise ValueError('sim_hold_lowest must be greater than zero')
        elif value == 'auto':
            if self.in_slews:
                # Use -10 * min slew rate
                self._sim_hold_lowest = min(self.in_slews) * -10.0
            else:
                raise ValueError('Cannot use auto for sim_hold_lowest unless in_slews is set first!')
        else:
            raise TypeError(f'Invalid type for sim_hold_lowest: {type(value)}')

    @property
    def sim_hold_highest(self) -> float:
        return self._sim_hold_highest

    @sim_hold_highest.setter
    def sim_hold_highest(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_hold_highest = float(value)
            else:
                raise ValueError('sim_hold_highest must be greater than zero')
        elif value == 'auto':
            if self.in_slews:
                # Use 10 * max slew rate
                self._sim_hold_lowest = max(self.in_slews) * 10.0
            else:
                raise ValueError('Cannot use auto for sim_hold_highest unless in_slews is set first!')
        else:
            raise TypeError(f'Invalid type for sim_hold_highest: {type(value)}')

    @property
    def sim_hold_timestep(self) -> float:
        return self._sim_hold_timestep

    @sim_hold_timestep.setter
    def sim_hold_timestep(self, value):
        if isinstance(value, (int, float)):
            if value > 0:
                self._sim_hold_timestep = float(value)
            else:
                raise ValueError('sim_hold_timestep must be greater than zero')
        elif value == 'auto':
            if self.in_slews:
                # 1st preference: 1/10th of minimum slew rate
                self._sim_hold_timestep = min(self.in_slews)/10.0
            else:
                # Otherwise, use sim timestep
                self._sim_hold_timestep = self.sim_timestep
        else:
            raise TypeError(f'Invalid type for sim_setup_timestamp: {type(value)}')

    @property
    def test_vectors(self) -> list:
        """Generate a rise and fall test vector for each D->Q path"""
        if self.stored_test_vectors:
            return self.stored_test_vectors
        test_vectors = []
        for q_target in self.out_ports:
            for d_target in self.in_ports:
                for direction in ['01', '10']:
                    test_vector = []
                    test_vector.append('0101'if self.clock_trigger == 'posedge' else '1010')
                    if self.set:
                        test_vector.append('0' if self.set_trigger == 'posedge' else '1')
                    if self.reset:
                        test_vector.append('0' if self.reset_trigger == 'posedge' else '1')
                    for _ in self.flops:
                        test_vector.append('0') # FIXME Pretty sure flops are nonfunctional at the moment
                    for d in self.in_ports:
                        test_vector.append(direction if d is d_target else '0')
                    for q in self.out_ports:
                        test_vector.append(direction if q is q_target else '0')
                    test_vectors.append(test_vector)
        return test_vectors


    def characterize(self, settings):
        """Run Delay, Recovery & Removal characterization for a sequential cell"""
        # Measure input capacitance for all input pins
        in_cap_pins = [*self.in_ports, self.clock]
        if self.set:
            in_cap_pins += [self.set]
        if self.reset:
            in_cap_pins += [self.reset]
        for pin in in_cap_pins:
            input_capacitance = self._run_input_capacitance(settings, pin.name) @ u_F
            self.cell[pin.name].capacitance = input_capacitance.convert(settings.units.capacitance.prefixed_unit).value

        # Generate harnesses
        harnesses = []
        for test_vector in self.test_vectors:
            # Generate harness
            harness = SequentialHarness(self, test_vector)
            trial_name = f'delay {self.cell.name} {harness.short_str()}'
            # Run characterization
            # TODO: Figure out how to thread this
            for slew in self.in_slews:
                for load in self.out_loads:
                    self._run_delay(settings, harness, slew, load, trial_name)
            harnesses.append(harness)

        # Save test results to cell
        normalize_t_units = lambda value: (value @ u_s).convert(settings.units.time.prefixed_unit).value
        # TODO: Add setup and hold constraints on input pins
        for in_port in self.in_ports:
            for direction in ['rise', 'fall']:
                self.cell[in_port.name].add_timing(self.clock.name)
                # TODO: Build setup and hold constraint tables
                
        # Output ports
        for out_port in self.out_ports:
            for in_port in self.in_ports: # TODO: Add set and reset
                # Add propagation and transport delay table to output pin
                self.cell[out_port.name].add_timing(in_port.name)
                for direction in ['rise', 'fall']:
                    # Identify the correct harness
                    harness = find_harness_by_arc(harnesses, in_port, out_port, direction)

                    # Construct the table
                    index_1 = [str(slew) for slew in self.in_slews]
                    index_2 = [str(load) for load in self.out_loads]
                    prop_values = []
                    tran_values = []
                    for slew in index_1:
                        for load in index_2:
                            result = harness.results[slew][load]
                            prop_values.append(f'{normalize_t_units(result["prop_in_out"]):7f}')
                            tran_values.append(f'{normalize_t_units(result["trans_out"]):7f}')
                    template = f'delay_template_{len(index_1)}x{len(index_2)}' # TODO: Template names should be in LibrarySettings
                    self.cell[out_port.name].timing[in_port.name].add_table(f'cell_{direction}', template, prop_values, index_1, index_2)
                    self.cell[out_port.name].timing[in_port.name].add_table(f'{direction}_transition', template, tran_values, index_1, index_2)
        # TODO: Add internal power

        # Display plots
        if 'io' in self.plots:
            [self.plot_io(settings, harness) for harness in harnesses]
        if 'delay' in self.plots:
            [self.cell[out_pin.name].plot_delay(settings, self.cell.name) for out_pin in self.out_ports]
        if 'energy' in self.plots:
            pass # TODO

    def _run_delay(self, settings, harness: SequentialHarness, slew, load, trial_name):
        print(f'Running sequential {trial_name} with slew={str(slew * settings.units.time)}, load={str(load*settings.units.capacitance)}')
        t_setup = self._find_setup_time(settings, harness, slew, load, self.sim_hold_highest*settings.units.time)
        t_hold = self._find_hold_time(settings, harness, slew, load, t_setup)

    def _find_setup_time(self, settings, harness: SequentialHarness, slew, load, t_hold):
        """Perform a binary search to identify setup time"""
        # Get max and min allowable time, correcting for timestep
        t_max = (self.sim_setup_highest + self.sim_setup_timestep) * settings.units.time
        t_min = (self.sim_setup_lowest - self.sim_setup_timestep) * settings.units.time
        prev_t_prop = 1.0 # Set a very large value

        while t_min <= t_max:
            t_setup = (t_max + t_min) / 2
            try:
                harness.results[str(slew)][str(load)] = self._run_delay_trial(settings, harness, slew, load, t_setup, t_hold)
                failed = False
            except NameError:
                failed = True

            # Identify next setup time
            if failed or harness.results[str(slew)][str(load)]['prop_in_out'] > prev_t_prop:
                t_min = t_setup
            else:
                t_max = t_setup

            # Check that the next t_setup is greater than 1 timestep difference from the previous t_setup
            if not abs(t_setup - (t_max + t_min)/2) > (self.sim_setup_timestep * settings.units.time):
                break # We've achieved the desired accuracy

            # Save previous results for comparison with next iteration
            try:
                prev_t_prop = harness.results[str(slew)][str(load)]['prop_in_out']
            except KeyError:
                pass # If we fail the first test, keep running

        return t_setup

    def _find_hold_time(self, settings, harness: SequentialHarness, slew, load, t_setup):
        """Perform a binary search to identify hold time"""
        # Get max and min allowable time, correcting for timestep
        t_max = (self.sim_hold_highest + self.sim_hold_timestep) * settings.units.time
        t_min = (self.sim_hold_lowest - self.sim_hold_timestep) * settings.units.time
        prev_t_prop = 1.0 # Set a very large value

        while t_min <= t_max:
            t_hold = (t_max + t_min) / 2
            try:
                harness.results[str(slew)][str(load)] = self._run_delay_trial(settings, harness, slew, load, t_setup, t_hold)
                failed = False
            except NameError:
                failed = True

            # Identify the next hold time
            if failed or harness.results[str(slew)][str(load)]['prop_in_out'] > prev_t_prop:
                t_min = t_hold
            else:
                t_max = t_hold

            # Check that the next t_hold is greater than 1 timestep difference from the previous t_hold
            if not abs(t_hold - (t_max + t_min)/2) > (self.sim_hold_timestep * settings.units.time):
                break # We've achieved the desired accuracy

            # Save previous results for comparison with next iteration
            try:
                prev_t_prop = harness.results[str(slew)][str(load)]['prop_in_out']
            except KeyError:
                pass # If we fail the first test, keep running

        return t_hold

    def _wire_subcircuit(self, settings, harness: SequentialHarness):
        ports = self.definition().upper().split()[1:]
        connections = [ports.pop(0)]
        for port in ports:
            if port == harness.target_in_port.pin.name:
                connections.append('vin')
            elif port == harness.target_out_port.pin.name:
                connections.append('vout')
            elif port == settings.vdd.name.upper():
                connections.append('vdd_dyn')
            elif port == settings.vss.name.upper():
                connections.append('vss_dyn')
            elif port == harness.clock.pin.name:
                connections.append('vcin')
            elif self.reset and port == harness.reset.pin.name:
                connections.append('vrin')
            elif self.set and port == harness.set.pin.name:
                connections.append('vsin')
            elif port in [pin.pin.name for pin in harness.stable_in_ports]:
                for stable_port in harness.stable_in_ports:
                    if port == stable_port.pin.name:
                        if stable_port.state == '1':
                            connections.append('vhigh')
                        elif stable_port.state == '0':
                            connections.append('vlow')
                        else:
                            raise ValueError(f'Invalid state identified during simulation setup for port {port}: {state}')
            elif port in [pin.pin.name for pin in harness.nontarget_out_ports]:
                for nontarget_port in harness.nontarget_out_ports:
                    if port == nontarget_port.pin.name:
                        connections.append(f'wfloat{str(nontarget_port.state)}')
        if len(connections) is not len(ports)+1:
            raise ValueError(f'Failed to match all ports identified in definition "{self.definition().strip()}"')
        return connections

    def _run_delay_trial(self, settings, harness: SequentialHarness, slew, load, t_setup, t_hold):
        """Run delay measurement for a single trial
        
        This test first zeroes out a stored value in the target
        sequential cell, then measures the setup and hold delay. This
        test also takes some power-related measurements."""

        # Set up parameters
        clk_slew = self.clock_slew * settings.units.time
        data_slew = slew * settings.units.time
        vdd = settings.vdd.voltage * settings.units.voltage
        vss = settings.vss.voltage * settings.units.voltage

        # Set up timing parameters for clock and data events
        t_stabilizing = 100*data_slew # TODO: Figure out how to tune this so that the test works
        t_clk_edge_1_start = data_slew + t_setup
        t_clk_edge_1_end = t_clk_edge_1_start + clk_slew
        t_clk_edge_2_start = t_clk_edge_1_end + t_hold
        t_clk_edge_2_end = t_clk_edge_2_start + clk_slew
        t_removal = t_clk_edge_2_end + t_hold
        t_data_edge_1_start = t_removal + t_stabilizing
        t_data_edge_1_end = t_data_edge_1_start + data_slew
        t_clk_edge_3_start = t_data_edge_1_end + t_setup
        t_clk_edge_3_end = t_clk_edge_3_start + clk_slew
        t_data_edge_2_start = t_clk_edge_3_end + t_hold
        t_data_edge_2_end = t_data_edge_2_start + data_slew
        t_sim_end = t_data_edge_2_end + t_stabilizing

        # Initialize circuit
        # TODO: Consider adding a driving cell to improve accuracy
        circuit = Circuit(self.cell.name)
        self._include_models(circuit)
        circuit.include(self.netlist)
        circuit.V('high', 'vhigh', circuit.gnd, vdd)
        circuit.V('low', 'vlow', circuit.gnd, vss)
        circuit.V('dd_dyn', 'vdd_dyn', circuit.gnd, vdd)
        circuit.V('ss_dyn', 'vss_dyn', circuit.gnd, vss)
        circuit.V('o_cap', 'vout', 'wout', 0)
        circuit.C('0', 'wout', 'vss_dyn', load * settings.units.capacitance)

        # Set up clock input
        (v0, v1) = (vss, vdd) if harness.timing_type_clock == 'falling_edge' else (vdd, vss)
        circuit.PieceWiseLinearVoltageSource('cin', 'vcin', circuit.gnd, values=[
            (0, v0), (t_clk_edge_1_start, v0), (t_clk_edge_1_end, v1), (t_clk_edge_2_start, v1), (t_clk_edge_2_end, v0), (t_clk_edge_3_start, v0), (t_clk_edge_3_end, v1), (t_sim_end, v1)
        ])

        # Set up data input node
        # TODO: Fix this to handle multiple data inputs
        (v0, v1) = (vss, vdd) if harness.in_direction == 'rise' else (vdd, vss)
        circuit.PieceWiseLinearVoltageSource('in', 'vin', circuit.gnd, values=[
            (0, v0), (t_data_edge_1_start, v0), (t_data_edge_1_end, v1), (t_data_edge_2_start, v1), (t_data_edge_2_end, v0), (t_sim_end, v0)
        ])

        # Set up reset node
        # Note: active low reset
        if harness.reset:
            circuit.V('rin', 'vrin', circuit.gnd, vdd if harness.reset.state == '1' else vss)

        # Set up set node
        # Note: active low set
        if harness.set:
            circuit.V('sin', 'vsin', circuit.gnd, vdd if harness.set.state == '1' else vss)

        # Initialize device under test subcircuit and wire up ports
        connections = self._wire_subcircuit(settings, harness)
        circuit.X('dut', *connections)

        # Initialize simulator
        simulator = circuit.simulator(temperature=settings.temperature,
                                      nominal_temperature=settings.temperature,
                                      simulator=settings.simulator)
        simulator.options('autostop', 'nopage', 'nomod', post=1, ingold=2, trtol=1)

        # Set up voltage bounds for measurements
        if harness.in_direction == 'rise':
            v_prop_start = settings.logic_low_to_high_threshold_voltage()
            v_trans_start = settings.logic_threshold_low_voltage()
        elif harness.in_direction == 'fall':
            v_prop_start = settings.logic_high_to_low_threshold_voltage()
            v_trans_start = settings.logic_threshold_high_voltage()
        else:
            raise ValueError('Unable to configure simulation: no target input pin')
        if harness.out_direction == 'rise':
            v_trans_end = settings.logic_threshold_low_voltage()
            v_prop_end = settings.logic_low_to_high_threshold_voltage()
        elif harness.out_direction == 'fall':
            v_trans_end = settings.logic_threshold_high_voltage()
            v_prop_end = settings.logic_high_to_low_threshold_voltage()
        else:
            raise ValueError('Unable to configure simulation: no target output pin')
        if harness.timing_type_clock == 'rising_edge':
            clk_direction = 'rise'
            v_clk_transition = settings.logic_low_to_high_threshold_voltage()
        else:
            clk_direction = 'fall'
            v_clk_transition = settings.logic_high_to_low_threshold_voltage()

        # Measure propagation delay from first data edge to last output edge
        simulator.measure('tran', 'prop_in_out',
                          f'trig v(vin) val={v_prop_start} td={float(t_removal)} {harness.in_direction}=1',
                          f'targ v(vout) val={v_prop_end} {harness.out_direction}=LAST')
        
        # Measure transport delay from first data edge to first output edge
        simulator.measure('tran', 'trans_out',
                          f'trig v(vin) val={v_trans_start} td={float(t_removal)} {harness.in_direction}=1',
                          f'targ v(vout) val={v_trans_end} {harness.out_direction}=1')

        # Measure setup delay from first data edge to last clock edge
        simulator.measure('tran', 't_setup',
                          f'trig v(vin) val={v_prop_start} td={float(t_removal)} {harness.in_direction}=1',
                          f'targ v(vcin) val={v_clk_transition} {clk_direction}=1')
        
        # Measure hold delay from last clock edge to last data edge
        simulator.measure('tran', 't_hold',
                          f'trig v(vcin) val={v_clk_transition} td={float(t_removal)} {_flip_direction(clk_direction)}=last',
                          f'targ v(vin) val={v_prop_end} {_flip_direction(harness.in_direction)}=1')

        return simulator.transient(step_time=(self.sim_timestep * settings.units.time), end_time=t_sim_end)

    def plot_io(self, settings, harness):
        """Plot I/O voltages vs time"""
        # TODO: Look for ways to generate fewer plots here - maybe a creative 3D plot
        figures = []
        # Group data by slew rate so that inputs are the same
        for slew in self.in_slews:
            for load in self.out_loads:
                # Add axes for clk, s, r, d, q (in that order)
                # Use an additive approach in case some of those aren't present
                num_axes = 1
                CLK = 0
                if self.set:
                    S = num_axes
                    num_axes += 1
                if self.reset:
                    R = num_axes
                    num_axes += 1
                D = num_axes
                num_axes += 1
                Q = num_axes
                num_axes += 1
                ratios = np.ones(num_axes).tolist()
                ratios[-1] = num_axes
                figure, axes = plt.subplots(num_axes,
                    sharex=True,
                    height_ratios=ratios,
                    label=f'{self.cell.name} | {harness.short_str()}'
                )

                # Set up plots
                for ax in axes:
                    for level in [settings.logic_threshold_low_voltage(), settings.logic_threshold_high_voltage()]:
                        ax.axhline(level, color='0.5', linestyle='--')
                    # TODO: Set up vlines for important timing events
                    ax.set_yticks([settings.vss.voltage, settings.vdd.voltage])
                volt_units = str(settings.units.voltage.prefixed_unit)
                time_units = str(settings.units.time.prefixed_unit)
                axes[CLK].set(
                    title=f'Slew Rate: {str(slew*settings.units.time)} | Fanout: {str(load*settings.units.capacitance)}',
                    ylabel=f'CLK [{volt_units}]'
                )
                if self.set:
                    axes[S].set_ylabel(f'S [{volt_units}]')
                if self.reset:
                    axes[R].set_ylabel(f'R [{volt_units}]')
                axes[D].set_ylabel(f'D [{volt_units}]')
                axes[Q].set_ylabel(f'Q [{volt_units}]')
                axes[-1].set_xlabel(f'Time [{str(settings.units.time.prefixed_unit)}]')
                analysis = harness.results[str(slew)][str(load)]
                t = analysis.time / settings.units.time
                axes[CLK].plot(t, analysis.vcin)
                if self.set:
                    axes[S].plot(t, analysis.vsin)
                if self.reset:
                    axes[R].plot(t, analysis.vrin)
                axes[D].plot(t, analysis.vin)
                axes[Q].plot(t, analysis.vout)

                figures.append(figure)
        return figures

def _flip_direction(direction: str) -> str:
    return 'fall' if direction == 'rise' else 'rise'

def _gen_graycode(length: int):
    """Generate the list of Gray Codes of specified length"""
    if length <= 1:
        return [[0],[1]]
    inputs = []
    for j in _gen_graycode(length-1):
        j.insert(0, 0)
        inputs.append(j)
    for j in reversed(_gen_graycode(length-1)):
        j.insert(0, 1)
        inputs.append(j)
    return inputs

def _parse_triggered_pin(value: str, role: str) -> (str, Pin):
    """Parses input pin names with trigger types, e.g. 'posedge CLK'"""
    if not isinstance(value, str):
        raise TypeError(f'Invalid type for edge-triggered pin: {type(value)}')
    try:
        edge, name = value.split()
    except ValueError:
        raise ValueError(f'Invalid value for edge-triggered pin: {value}. Make sure you include both the trigger type and pin name (e.g. "posedge CLK")')
    if not edge in ['posedge', 'negedge']:
        raise ValueError(f'Invalid trigger type: {edge}. Trigger type must be one of "posedge" or "negedge"')
    return (edge, Pin(name, 'input', role))
