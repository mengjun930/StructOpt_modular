import os
import logging
import numpy as np
import shutil
import decimal as dec
from tempfile import mkdtemp, NamedTemporaryFile, mktemp as uns_mktemp
from re import compile as re_compile, IGNORECASE
from structopt.tools import subprocess
from ase.calculators.lammpsrun import Prism
from pymatgen import Element
import time

from structopt.io import write_data

# "End mark" used to indicate that the calculation is done
CALCULATION_END_MARK = '__end_of_ase_invoked_calculation__'


class LAMMPS(object):
    """Simplied calculator object for performing LAMMPS calculations
    through ase and StructOpt. Only returns the relaxed structure
    and the total energy. The primary difference between this and the
    ase version is that input files are written and then executed, while
    in the ase version the input is passed directly through the Popen
    constructor as stdin"""

    def __init__(self, parameters, calcdir=None):
        self.parameters = parameters.copy()
        self.cwd = os.getcwd()

        # read_log depends on that the first (three) thermo_style custom args
        # can be capitilized and matched against the log output. I.e.
        # don't use e.g. 'ke' or 'cpu' which are labeled KinEng and CPU.
        self._custom_thermo_args = ['step', 'temp', 'press', 'cpu',
                                    'pxx', 'pyy', 'pzz', 'pxy', 'pxz', 'pyz',
                                    'ke', 'pe', 'etotal',
                                    'vol', 'lx', 'ly', 'lz', 'atoms']
        self._custom_thermo_mark = ' '.join([x.capitalize() for x in
                                             self._custom_thermo_args[0:3]])

        # Match something which can be converted to a float
        f_re = r'([+-]?(?:(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?|nan|inf))'
        n = len(self._custom_thermo_args)
        # Create a re matching exactly N white space separated floatish things
        self._custom_thermo_re = re_compile(r'^\s*' + r'\s+'.join([f_re]*n) + r'\s*$',
                                            flags=IGNORECASE)
        # thermo_content contains data "written by" thermo_style.
        # It is a list of dictionaries, each dict (one for each line
        # printed by thermo_style) contains a mapping between each
        # custom_thermo_args-argument and the corresponding
        # value as printed by lammps. thermo_content will be
        # re-populated by the read_log method.
        self.thermo_content = []
        self.pea = []

        if calcdir:
            self.calcdir = calcdir
        elif 'calcdir' in self.parameters:
            self.calcdir = self.parameters['calcdir']
        else:
            self.calcdir = os.getcwd()
        return


    def calculate(self, atoms, tmp_dir=None, data_file=None, input_file=None, trj_file=None, overwrite_data=True):
        self.atoms = atoms

        self.update_parameters_from_atoms(self.parameters, atoms)

        # setup the calculation
        if tmp_dir is None:
            tmp_dir = mkdtemp(prefix='LAMMPS-')
        self.tmp_dir = tmp_dir

        if data_file is None:
            data_file = os.path.join(self.tmp_dir, 'data.lammps')
        self.data_file = data_file

        if input_file is None:
            input_file = os.path.join(self.tmp_dir, 'input.lammps')
        self.input_file = input_file

        if trj_file is None:
            trj_file = os.path.join(self.tmp_dir, 'trj.lammps')
        self.trj_file = trj_file

        self.setup_dir(self.tmp_dir, self.parameters)
        os.chdir(self.tmp_dir)

        if overwrite_data or not os.path.exists(self.data_file):
            if 'atom_style' in self.parameters and self.parameters['atom_style'] == 'charge':
                self.write_data(self.data_file, atoms, atom_style = 'charge')
            else:
                self.write_data(self.data_file, atoms, atom_style = 'atomic')

        self.write_input(self.input_file, atoms, self.parameters, self._custom_thermo_args, self.trj_file, self.data_file)

        t0 = time.time()
        errors = self.run(self.parameters, self.input_file)
        t1 = time.time()
        print('******* Individual {} optimization elapsed: {:.6f}s *******'.format(atoms.id, t1 - t0))
        if errors:
            print('error infos of lammps relaxation')
            self.process_error(errors)  # This will raise an exception, stopping runtime
        # Read the thermodynamic and atom data
        # we are still in the tmp directory
        self.read_log_file(filename=os.path.join(self.tmp_dir, 'log.lammps'))
        self.read_trj_file(filename=self.trj_file)

        os.chdir(self.cwd)
        #print('print self.cwd when the calculation finish')
        #print(self.cwd)
        if self.parameters['keep_files'] == True:
            self.copy_files(self.tmp_dir, self.calcdir)
        print('delete tmp_dir if relaxation succeed')
        shutil.rmtree(self.tmp_dir)
        print(f'removed {self.tmp_dir}')
        os.system(f"rm -rf {os.path.abspath(os.path.join(os.path.join(self.tmp_dir, os.path.pardir), 'openmpi*'))}")     
        
        return


    @staticmethod
    def setup_dir(dir, parameters):
        """This function sets up the temporary directory and copies
        the necessary files to that folder"""
        
        for param in parameters:
            if param.endswith('_file') and param is not 'potential_file':  # explicitly don't make a copy of the potential file
                f = os.path.expandvars(parameters[param])
                shutil.copy(f, os.path.join(dir, os.path.basename(f)))

        return


    write_data = staticmethod(write_data)  # scope the global structopt.io.write_data as a staticmethod here


    @staticmethod
    def write_input(filename, atoms, parameters, thermo_args, trj_file, data_file):
        """Method which writes the LAMMPS in file"""

        with open(filename, 'w') as f:
            f.write('# (written by ASE)\n')

            # Write variables
            f.write('clear\n')
            f.write('dimension       3 \n')
            # Write the atoms data
            parameters = parameters
            pbc = atoms.get_pbc()
            f.write('units real \n')
            if 'atom_style' in parameters:
                f.write('atom_style {}\n'.format(parameters['atom_style']))
            f.write('boundary {} {} {} \n'.format(*('sp'[x] for x in pbc)))
            f.write('read_data {}\n'.format(data_file))

            # Write interaction parameters
            f.write('\n### interactions \n')
            for param in ['pair_style', 'pair_coeff', 'mass']:
                if param in parameters:
                    f.write('{} {}\n'.format(param, parameters[param]))
            if 'mass_multi' in parameters:
                f.write(parameters['mass_multi'])

            # Write thermo parameters
            f.write('thermo_style custom {}\n'.format(' '.join(thermo_args)))
            f.write('thermo_modify flush yes\n')
            f.write('thermo {}\n'.format(parameters['thermosteps']))

            # run MD for 100 steps
            run_MD = 1 
            if run_MD == 0:
                if parameters['pair_style'] == 'reax/c NULL':
                    f.write('\n### NVT at 5K \n')                
                    #f.write('\nvelocity    all create 5 51343 mom yes rot yes dist gaussian units box\n') 
                    f.write('\nfix          1 all nvt temp 300 300 0.001 \n')
                    f.write('\nfix fixqeqreax all qeq/reax 1 0.0 10.0 1.0e-2 reax/c \n')
                    for param in ['min_style', 'min_modify']:
                        if param in parameters:
                            f.write('{} {}\n'.format(param, parameters[param]))
                    f.write('minimize 1e-4 0 1000 1000\n')
                    f.write('neigh_modify every 1 delay 0 check yes\n')
                    f.write('thermo {}\n'.format(parameters['thermosteps']))
                    f.write('run          1000\nunfix        1\n')
                           
 
            # Relax the system
            f.write('\n### Relaxation \n')
            #f.write('fix fix_nve all nve\n')
            f.write('neigh_modify every 1 delay 0 check yes\n')
            if parameters['relax_box']:
                f.write('fix relax_box all box/relax iso 0.0 vmax 0.001\n')
            if parameters['pair_style'] == 'reax/c NULL':
                f.write('fix fixqeqreax all qeq/reax 1 0.0 10.0 1.0e-2 reax/c\n')
            # Generate the thermodynamic and structural information
            if parameters['pair_style'] == 'comb' or parameters['pair_style'] == 'reax/c NULL' or parameters['pair_style'] == 'reax/c NULL checkqeq no':
            #if parameters['atom_style'] == 'charge':
                f.write('dump dump_all all custom 10000 {} id type q x y z \n'.format(trj_file))
            else:    
                f.write('dump dump_all all custom 10000 {} id type x y z \n'.format(trj_file))
            for param in ['min_style', 'min_modify', 'minimize']:
                if param in parameters:
                    f.write('{} {}\n'.format(param, parameters[param]))
            
            #f.write('compute pea all pe/atom\n')
            #f.write('run 1\n')
            f.write('unfix fixqeqreax\n')
            #minimize by buckingham potential
            f.write('pair_style  buck/coul/long 12.0\n')
            f.write('pair_coeff  1 1 717654.7494 0.154 120.9967875\n')
            f.write('pair_coeff  1 2 391053.1759 0.194 290.39229\n')
            f.write('pair_coeff  2 2 271719.1310 0.234 696.941496\n')
            f.write('kspace_style pppm 1e-5\n')
            f.write('pair_modify     tail yes\n')
            f.write('set             type 1      charge 2.196\n')
            f.write('set             type 2      charge -1.098\n')
            f.write('minimize 0 1.5 1000 1000\n')
            f.write('undump dump_all\n')
            f.write('print {}'.format(CALCULATION_END_MARK))
        
        return


    @staticmethod
    def update_parameters_from_atoms(parameters, atoms):
        """The purpose of this function is to initialize the variables for 
        them being written to the lammps. This set of initializations
        depends on the atoms object, and hence cannot be done in __init__.py."""

        parameters.setdefault('thermosteps', 0)
        parameters.setdefault('timeout', 1800)
        parameters.setdefault('relax_box', False)

        # Initialize the potential parameters
        if 'pair_style' not in parameters:
            parameters['pair_style'] = 'lj/cut 10.0'
            parameters['pair_coeff'] = '* * 1 1'
            parameters['mass'] = '* 1.0'

        elif parameters['pair_style'] == 'eam':
            pot_file = os.path.expandvars(parameters['potential_file'])
            parameters['pair_coeff'] = '* * {}'.format(pot_file)

        elif parameters['pair_style'] == 'eam/alloy':
            elements = sorted(set(atoms.get_chemical_symbols()))
            pot_file = os.path.expandvars(parameters['potential_file'])
            pair_coeff = '* * {}'.format(pot_file)
            for element in elements:
                pair_coeff += ' {}'.format(element)
            parameters['pair_coeff'] = pair_coeff
        elif parameters['pair_style'] == 'eam/fs':
            elements = sorted(set(atoms.get_chemical_symbols()))
            pot_file = os.path.expandvars(parameters['potential_file'])
            pair_coeff = '* * {}'.format(pot_file)
            for element in elements:
                pair_coeff += ' {}'.format(element)
            parameters['pair_coeff'] = pair_coeff  
        elif 'lj/cut' in parameters['pair_style']:
            parameters['pair_coeff'] = '* * 1 1'
            parameters['mass'] = '* 1.0'
        elif parameters['pair_style'] == 'meam/c':
            elements = sorted(set(atoms.get_chemical_symbols()))
            pot_file = os.path.expandvars(parameters['potential_file'])
            pair_coeff = '* * {}'.format(pot_file)
            for element in elements:
                pair_coeff += ' {}'.format(element)
            # pair_coeff += ' /home/user/OtherProjects/StructOpt/v2-experiments-and-energy/potentials/TiOpar.meam '
            # additional parameters for tio2
            pair_coeff += ' ' + os.path.join(os.path.dirname(pot_file), 'TiOpar.meam')
            for element in elements:
                pair_coeff += ' {}'.format(element)
            parameters['pair_coeff'] = pair_coeff
        elif parameters['pair_style'] == 'comb':
            elements = sorted(set(atoms.get_chemical_symbols()))
            pot_file = os.path.expandvars(parameters['potential_file'])
            pair_coeff = '* * {}'.format(pot_file)
            for element in elements:
                pair_coeff += ' {}'.format(element)

            # perform qeq
            if parameters['charge_equilibration']:
                pair_coeff += '\nfix fix_qeq all qeq/comb 10 0.01'
            parameters['pair_coeff'] = pair_coeff

            # comb potential require charge instead of default atomic
            parameters['atom_style'] = 'charge'  

            # While MEAM potential do not need to specify the atomic mass because
            # it can read them from its potential file. The COMB potential needs 
            # users to specify the atomic mass explicitly.
            parameters['mass_multi'] = ''
            for i, element in enumerate(elements):
                parameters['mass_multi'] += 'mass {} {}  # {}\n'.format(i + 1, Element(element).atomic_mass.real, element)
        elif 'reax/c' in parameters['pair_style']:
            elements = sorted(set(atoms.get_chemical_symbols()), reverse=True)
            pot_file = os.path.expandvars(parameters['potential_file'])
            pair_coeff = '* * {}'.format(pot_file)
            for element in elements:
                pair_coeff += ' {}'.format(element)
            parameters['pair_coeff'] = pair_coeff            
           
            parameters['atom_style'] = 'charge'
            parameters['mass_multi'] = ''
            for i, element in enumerate(elements):
                parameters['mass_multi'] += 'mass {} {}  # {}\n'.format(i + 1, Element(element).atomic_mass.real, element)
            
        else:
            s = parameters['pair_style']
            raise NotImplementedError('{} pair_style not yet implemented'.format(s))

        return


    def run(self, parameters, input_fn):
        if 'LAMMPS_COMMAND' in os.environ:
            lammps_cmd_line = os.environ['LAMMPS_COMMAND']
        else:
            os.chdir(self.cwd)
            shutil.rmtree(self.tmp_dir)
            raise RuntimeError('Set LAMMPS_COMMAND environment variable')

        # input_file = open(input_fn)
        ncores = 2
        try:
            #p = subprocess.run([lammps_cmd_line, '-in', input_fn], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=parameters['timeout'], shell=False, check=False)
            p = subprocess.run(['mpirun', '-np', str(ncores), lammps_cmd_line, '-in', input_fn], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=parameters['timeout'], shell=False, check=False)
            output, error = p.stdout, p.stderr
            #print('##########')
            #print(output)
            #print(error)
            #print('#########')
        except subprocess.TimeoutExpired:
            output, error = None, None
            print("Timed out!")    
            return "Timed out!"
        # input_file.close()

        self.output = output.decode('utf-8').split('\n')[:-1]
        #print('-------output--utf-8----')
        #print(self.output)
        #print(len(self.output), 'len self.output')
        #print('mark', self.output[-2])
        # Check if the calculation completed without errors. If it does,
        # we need to save the files self.calcdir.
        if len(self.output) == 0 or CALCULATION_END_MARK not in self.output[-5:-1]:
            return "No output or no CALCULATION_END_MARK"
        else:
            self.stopcriterion = output.decode('utf-8').split('\n')[37]
            self.forcefinal = output.decode('utf-8').split('\n')[41]
            print("stopcriterion")
            print(self.stopcriterion, self.forcefinal)

        return False


    def read_log_file(self, filename=None):
        """Method which reads a LAMMPS output log file. This reads exclusively
        for the thermodynamic data."""

        if hasattr(self, 'output'):
            lines = self.output
        elif self.parameters['keep_files'] == True:            
            if filename is None:
                filename = '{}/log.lammps'.format(self.calcdir)
            with open(filename) as f:
                lines = f.readlines()
        else:
            raise RuntimeError('No log file detected. ' 
                               'Calculation not run or output not saved')

        thermo_content = []
        reading_thermo = False
        for line in lines:
            # get thermo output
            if line.startswith(self._custom_thermo_mark):
                reading_thermo = True
                continue

            thermo_step = self._custom_thermo_re.match(line)
            if reading_thermo and not thermo_step:
                reading_thermo = False
                continue            
            elif reading_thermo:
                # create a dictionary between each of the thermo_style args
                # and it's corresponding value
                thermo_content.append(dict(zip(self._custom_thermo_args,
                                               map(float, thermo_step.groups()))))

        self.thermo_content = thermo_content
        self.energy = thermo_content[-1]['pe']
        # print('===========================')
        # print('energy: ', self.energy)
        # print('===========================')

        return


    def read_trj_file(self, filename=None):
        """Method which reads the LAMMPS trj file. This is read primarily
        to get the atoms final relaxed structure"""

        if filename is None:
            filename = self.trj_file

        try:
            with open(filename) as f:
                lines = f.readlines()
        except FileNotFoundError:
            # Try looking in the log file instead
            filename = os.path.join(self.calcdir, 'log.lammps')
            try:
                with open(filename) as f:
                    lines = f.readlines()
            except FileNotFoundError:
                raise RuntimeError('No trajectory file detected. '
                                   'Calculation not run or output not saved')

        # Get a list referencing atoms to lammps types
        atoms = self.atoms
        species = sorted(set(atoms.get_chemical_symbols()))
        lines = lines[-(len(atoms)+9):]
        for i, line in enumerate(lines):

            if 'ITEM: TIMESTEP' in line:
                lo = [] ; hi = [] ; tilt = []
                id = [] ; type = []
                positions = [] ; pea = [] #; velocities = [] ; forces = []

            if 'ITEM: NUMBER OF ATOMS' in line:                
                n_atoms = int(lines[i + 1].split()[0])

            if 'ITEM: BOX BOUNDS' in line:
                tilt_items = line.split()[3:]
                for j in range(3):
                    box_line = lines[i + j + 1]                    
                    fields = box_line.split()
                    lo.append(float(fields[0]))
                    hi.append(float(fields[1]))
                    if (len(fields) >= 3):
                        tilt.append(float(fields[2]))

            if 'ITEM: ATOMS' in line:
                atom_lines = [l.split() for l in lines[i+1:i+1+n_atoms]]
                if 'atom_style' in self.parameters and self.parameters['atom_style'] == 'charge':
                    #ids, types, qs, xs, ys, zs, peas = zip(*atom_lines)
                    ids, types, qs, xs, ys, zs = zip(*atom_lines)
                    syms = [species[int(i) - 1] for i in types]
                    pos = [None for i in range(len(ids))]
                    peas = [None for i in range(len(ids))] 
                    for id, x, y, z, E in zip(ids, xs, ys, zs, peas):
                        pos[int(id) - 1] = [float(x), float(y), float(z)]
                else:
                    #ids, types, xs, ys, zs, peas = zip(*atom_lines)
                    ids, types, xs, ys, zs = zip(*atom_lines)
                    syms = [species[int(i) - 1] for i in types]
                    pos = [None for i in range(len(ids))]
                    peas = [None for i in range(len(ids))]
                    for id, x, y, z, E in zip(ids, xs, ys, zs, peas):
                        pos[int(id) - 1] = [float(x), float(y), float(z)]
                        #peas[int(id) - 1] = float(E)

                # Update the positions of the atom
                self.atoms.set_positions(pos)

        # determine cell tilt (triclinic case!)
        if (len(tilt) >= 3):
            if (len(tilt_items) >= 3):
                xy = tilt[tilt_items.index('xy')]
                xz = tilt[tilt_items.index('xz')]
                yz = tilt[tilt_items.index('yz')]
            else:
                xy = tilt[0]
                xz = tilt[1]
                yz = tilt[2]
        else:
            xy = xz = yz = 0
        xhilo = (hi[0] - lo[0]) - xy - xz
        yhilo = (hi[1] - lo[1]) - yz
        zhilo = (hi[2] - lo[2])

        cell = [[xhilo,0,0],[xy,yhilo,0],[xz,yz,zhilo]]
        if all(atoms.get_pbc()):
            self.atoms.set_cell(cell)
                
        return

        
    def get_potential_energy(self, atoms):
        self.update(atoms)
        return self.energy


    def update(self, atoms):
        if not hasattr(self, 'atoms') or self.atoms != atoms:
            self.calculate(atoms)


    def process_error(self, error_string):
        """This function is run immediately after detecting an error.
        We're in the temporary directory, so we have to copy the files
        back to the calculation directory, write and empty error file
        and raise an exception"""

        self.copy_files(self.tmp_dir, self.calcdir)

        error_file = os.path.join(self.calcdir, 'error')
        with open(error_file, 'a') as f:
            f.write(error_string)
        print('delete file if error')
        os.chdir(self.cwd)
        shutil.rmtree(self.tmp_dir)
        os.system(f"rm -rf {os.path.abspath(os.path.join(os.path.join(self.tmp_dir, os.path.pardir), 'openmpi*'))}")
        print(f'{self.cwd} {self.tmp_dir}')        
        raise RuntimeError('Error in LAMMPS calculation in {}:\n{}'.format(self.calcdir, error_string))


    @staticmethod
    def copy_files(from_, to_):
        if not os.path.isdir(to_):
            os.makedirs(to_)
        for f in os.listdir(from_):
            f = os.path.join(from_, f)
            if os.path.isfile(f):
                shutil.copy(f, to_)
            elif os.path.isdir(f):
                shutil.move(f, to_)
            else:
                raise ValueError("The thing trying to be copied is not a file or directory")

