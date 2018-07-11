# coding: utf-8

from __future__ import unicode_literals, division

# This module implements new error handlers for QChem runs.

import os
from monty.serialization import loadfn, dumpfn
from pymatgen.io.qchem.inputs import QCInput
from pymatgen.io.qchem.outputs import QCOutput
from custodian.custodian import ErrorHandler
from custodian.utils import backup
from pymatgen.analysis.graphs import MoleculeGraph
from pymatgen.io.babel import BabelMolAdaptor
import openbabel as ob
import networkx as nx

__author__ = "Samuel Blau, Brandon Wood, Shyam Dwaraknath"
__copyright__ = "Copyright 2018, The Materials Project"
__version__ = "0.1"
__maintainer__ = "Samuel Blau"
__email__ = "samblau1@gmail.com"
__status__ = "Alpha"
__date__ = "3/26/18"
__credits__ = "Xiaohui Qu"


class QChemErrorHandler(ErrorHandler):
    """
    Master QChemErrorHandler class that handles a number of common errors
    that occur during QChem runs.
    """

    is_monitor = False

    def __init__(self,
                 input_file="mol.qin",
                 output_file="mol.qout",
                 scf_max_cycles=200,
                 geom_max_cycles=200):
        """
        Initializes the error handler from a set of input and output files.

        Args:
            input_file (str): Name of the QChem input file.
            output_file (str): Name of the QChem output file.
            scf_max_cycles (int): The max iterations to set to fix SCF failure.
            geom_max_cycles (int): The max iterations to set to fix geometry
                optimization failure.
        """
        self.input_file = input_file
        self.output_file = output_file
        self.scf_max_cycles = scf_max_cycles
        self.geom_max_cycles = geom_max_cycles
        self.outdata = None
        self.errors = []

    def check(self):
        # Checks output file for errors.
        self.outdata = QCOutput(self.output_file).data
        self.errors = self.outdata.get("errors")
        if "out_of_opt_cycles" in self.errors:
            first_mol_graph = build_MoleculeGraph(self.outdata["initial_molecule"])
            last_mol_graph = build_MoleculeGraph(self.outdata["molecule_from_last_geometry"])
            if not is_isomorphic(first_mol_graph.graph, last_mol_graph.graph):
                return False
        else:
            return len(self.errors) > 0

    def correct(self):
        backup({self.input_file, self.output_file})
        actions = []
        self.qcinp = QCInput.from_file(self.input_file)

        if "SCF_failed_to_converge" in self.errors:
            # Check number of SCF cycles. If not set or less than scf_max_cycles,
            # increase to that value and rerun. If already set, check if
            # scf_algorithm is unset or set to DIIS, in which case set to GDM.
            # Otherwise, tell user to call SCF error handler and do nothing.
            if str(self.qcinp.rem.get("max_scf_cycles")) != str(
                    self.scf_max_cycles):
                self.qcinp.rem["max_scf_cycles"] = self.scf_max_cycles
                actions.append({"max_scf_cycles": self.scf_max_cycles})
            elif self.qcinp.rem.get("scf_algorithm", "diis").lower() == "diis":
                self.qcinp.rem["scf_algorithm"] = "gdm"
                actions.append({"scf_algorithm": "gdm"})
            elif self.qcinp.rem.get("scf_algorithm", "gdm").lower() == "gdm":
                self.qcinp.rem["scf_algorithm"] = "diis_gdm"
                actions.append({"scf_algorithm": "diis_gdm"})
            else:
                print(
                    "More advanced changes may impact the SCF result. Use the SCF error handler"
                )

        elif "out_of_opt_cycles" in self.errors:
            # Check number of opt cycles. If less than geom_max_cycles, increase
            # to that value, set last geom as new starting geom and rerun.
            if str(self.qcinp.rem.get(
                    "geom_opt_max_cycles")) != str(self.geom_max_cycles):
                self.qcinp.rem["geom_opt_max_cycles"] = self.geom_max_cycles
                actions.append({"geom_max_cycles:": self.scf_max_cycles})
                if len(self.outdata.get("energy_trajectory")) > 1:
                    self.qcinp.molecule = self.outdata.get(
                        "molecule_from_last_geometry")
                    actions.append({"molecule": "molecule_from_last_geometry"})
            else:
                if os.path.exists(os.path.join(os.path.split(os.path.abspath(self.input_file))[0], "/error_history.json")):
                    error_history = loadfn(os.path.join(os.path.split(os.path.abspath(self.input_file))[0], "/error_history.json"))
                    if "last_ten" in error_history:
                        return {"errors": self.errors, "actions": None}
                else:
                    self.qcinp.molecule = self.outdata.get("molecule_from_last_geometry")
                    actions.append({"molecule": "molecule_from_last_geometry"})
                    error_history = {}
                    error_history["last_ten"] = np.mean(self.outdata["energy_trajectory"][-10:])
                    dumpfn(error_history, "error_history.json")


        elif "unable_to_determine_lamda" in self.errors:
            # Set last geom as new starting geom and rerun. If no opt cycles,
            # use diff SCF strat? Diff initial guess? Change basis?
            if len(self.outdata.get("energy_trajectory")) > 1:
                self.qcinp.molecule = self.outdata.get(
                    "molecule_from_last_geometry")
                actions.append({"molecule": "molecule_from_last_geometry"})
            elif self.qcinp.rem.get("scf_algorithm", "diis").lower() == "diis":
                self.qcinp.rem["scf_algorithm"] = "rca_diis"
                actions.append({"scf_algorithm": "rca_diis"})
                if self.qcinp.rem.get("gen_scfman"):
                    self.qcinp.rem["gen_scfman"] = False
                    actions.append({"gen_scfman": False})
            else:
                print(
                    "Use a different initial guess? Perhaps a different basis?"
                )

        elif "linear_dependent_basis" in self.errors:
            # DIIS -> RCA_DIIS. If already RCA_DIIS, change basis?
            if self.qcinp.rem.get("scf_algorithm", "diis").lower() == "diis":
                self.qcinp.rem["scf_algorithm"] = "rca_diis"
                actions.append({"scf_algorithm": "rca_diis"})
                if self.qcinp.rem.get("gen_scfman"):
                    self.qcinp.rem["gen_scfman"] = False
                    actions.append({"gen_scfman": False})
            else:
                print("Perhaps use a better basis?")

        elif "failed_to_transform_coords" in self.errors:
            # Check for symmetry flag in rem. If not False, set to False and rerun.
            # If already False, increase threshold?
            if not self.qcinp.rem.get("sym_ignore") or self.qcinp.rem.get(
                    "symmetry"):
                self.qcinp.rem["sym_ignore"] = True
                self.qcinp.rem["symmetry"] = False
                actions.append({"sym_ignore": True})
                actions.append({"symmetry": False})
            else:
                print("Perhaps increase the threshold?")

        elif "input_file_error" in self.errors:
            print(
                "Something is wrong with the input file. Examine error message by hand."
            )
            return {"errors": self.errors, "actions": None}

        elif "failed_to_read_input" in self.errors:
            # Almost certainly just a temporary problem that will not be encountered again. Rerun job as-is.
            actions.append({"rerun job as-is"})

        elif "IO_error" in self.errors:
            # Almost certainly just a temporary problem that will not be encountered again. Rerun job as-is.
            actions.append({"rerun job as-is"})

        elif "read_molecule_error" in self.errors:
            # Almost certainly just a temporary problem that will not be encountered again. Rerun job as-is.
            actions.append({"rerun job as-is"})

        elif "never_called_qchem" in self.errors:
            # Almost certainly just a temporary problem that will not be encountered again. Rerun job as-is.
            actions.append({"rerun job as-is"})
            
        elif "unknown_error" in self.errors:
            print("Examine error message by hand.")
            return {"errors": self.errors, "actions": None}

        else:
            # You should never get here. If correct is being called then errors should have at least one entry,
            # in which case it should have been caught by the if/elifs above.
            print(
                "If you get this message, something has gone terribly wrong!")
            return {"errors": self.errors, "actions": None}

        os.rename(self.input_file, self.input_file + ".last")
        self.qcinp.write_file(self.input_file)
        return {"errors": self.errors, "actions": actions}


def edges_from_babel(molecule):
    babel_mol = BabelMolAdaptor(molecule).openbabel_mol
    edges = []
    for obbond in ob.OBMolBondIter(babel_mol):
        edges += [[obbond.GetBeginAtomIdx() - 1, obbond.GetEndAtomIdx() - 1]]
    return edges


def build_MoleculeGraph(molecule, edges=None):
    if edges == None:
        edges = edges_from_babel(molecule)
    mol_graph = MoleculeGraph.with_empty_graph(molecule)
    for edge in edges:
        mol_graph.add_edge(edge[0], edge[1])
    mol_graph.graph = mol_graph.graph.to_undirected()
    species = {}
    coords = {}
    for node in mol_graph.graph:
        species[node] = mol_graph.molecule[node].specie.symbol
        coords[node] = mol_graph.molecule[node].coords
    nx.set_node_attributes(mol_graph.graph, species, "specie")
    nx.set_node_attributes(mol_graph.graph, coords, "coords")
    return mol_graph


def _node_match(node, othernode):
    return node["specie"] == othernode["specie"]


def is_isomorphic(graph1, graph2):
    return nx.is_isomorphic(graph1, graph2, node_match=_node_match)



class QChemSCFErrorHandler(ErrorHandler):
    """
    QChem ErrorHandler class that addresses SCF non-convergence.
    """

    is_monitor = False

    def __init__(self,
                 input_file="mol.qin",
                 output_file="mol.qout",
                 rca_gdm_thresh=1.0E-3,
                 scf_max_cycles=200):
        """
        Initializes the error handler from a set of input and output files.

        Args:
            input_file (str): Name of the QChem input file.
            output_file (str): Name of the QChem output file.
            rca_gdm_thresh (float): The threshold for the prior scf algorithm.
                If last deltaE is larger than the threshold try RCA_DIIS
                first, else, try DIIS_GDM first.
            scf_max_cycles (int): The max iterations to set to fix SCF failure.
        """
        self.input_file = input_file
        self.output_file = output_file
        self.scf_max_cycles = scf_max_cycles
        self.geom_max_cycles = geom_max_cycles
        self.qcinp = QCInput.from_file(self.input_file)
        self.outdata = None
        self.errors = None
        self.qchem_job = qchem_job

    def check(self):
        # Checks output file for errors.
        self.outdata = QCOutput(self.output_file).data
        self.errors = self.outdata.get("errors")
        return len(self.errors) > 0

    def correct(self):
        print("This hasn't been implemented yet!")
        return {"errors": self.errors, "actions": None}
