import subprocess

import structopt
from structopt.tools import root, single_core, parallel


@parallel
def relax(population):
    """Relax the entire population using LAMMPS.

    Args:
        population (Population): the population to relax
    """

    to_relax = [individual for individual in population if individual._modified]
    ncores = structopt.parameters.globals.ncores
    rank = structopt.parameters.globals.rank

    individuals_per_core = {r: [] for r in range(ncores)}
    for i, individual in enumerate(to_relax):
        individuals_per_core[i % ncores].append(individual.index)

    for index in individuals_per_core[rank]:
        individual = population[index]
        assert individual.index == index
        individual.relaxations.LAMMPS.relax(individual)

    if structopt.parameters.globals.ncores > 1:
        population.allgather(individuals_per_core)

