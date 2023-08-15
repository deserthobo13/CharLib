import shutil

from liberty.LibrarySettings import LibrarySettings
from liberty.LogicCell import LogicCell, CombinationalCell, SequentialCell

class Characterizer:
    """Main object of Charlib. Keeps track of settings and cells."""

    def __init__(self) -> None:
        self.settings = LibrarySettings()
        self.cells = []
        self.num_files_generated = 0

    def __str__(self) -> str:
        lines = []
        lines.append('Library settings:')
        for line in str(self.settings).split('\n'):
            lines.append(f'    {line}')
        lines.append('Cells:')
        for cell in self.cells:
            for line in str(cell).split('\n'):
                lines.append(f'    {line}')
        return '\n'.join(lines)

    def last_cell(self) -> LogicCell:
        """Get last cell"""
        return self.cells[-1]

    def add_cell(self, name, in_ports, out_ports, functions, **kwargs):
        # Create a new logic cell
        self.cells.append(CombinationalCell(name, in_ports, out_ports, functions, **kwargs))

    def add_flop(self, name, in_ports, out_ports, clock, flops, functions, **kwargs):
        # Create a new sequential cell
        self.cells.append(SequentialCell(name, in_ports, out_ports, clock, flops, functions, **kwargs))

    def initialize_work_dir(self):
        if self.settings.run_sim:
            # Clear out the old work_dir if it exists
            if self.settings.work_dir.exists() and self.settings.work_dir.is_dir():
                shutil.rmtree(self.settings.work_dir)
            self.settings.work_dir.mkdir()
        else:
            print("Reusing previous working directory and files")

    def characterize(self, *cells):
        """Characterize the passed cells, or all cells if none are passed"""

        # If no target cells were given, characterize all cells
        for cell in cells if cells else self.cells:
            cell.characterize(self.settings)

    def print_msg(self, message: str):
        if not self.settings.suppress_message:
            print(message)
    
    def print_sim(self, message: str):
        if not self.settings.suppress_sim_message:
            print(f'SIM: {message}')
    
    def print_debug(self, message: str):
        if not self.settings.suppress_debug_message:
            print(f'DEBUG: {message}')