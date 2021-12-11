# spec_lookup.py
#
# This file is part of scqubits: a Python package for superconducting qubits,
# Quantum 5, 583 (2021). https://quantum-journal.org/papers/q-2021-11-17-583/
#
#    Copyright (c) 2019 and later, Jens Koch and Peter Groszkowski
#    All rights reserved.
#
#    This source code is licensed under the BSD-style license found in the
#    LICENSE file in the root directory of this source tree.
############################################################################

import itertools
import weakref

from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union, cast

import numpy as np
import qutip as qt

from numpy import ndarray
from qutip import Qobj

import scqubits
import scqubits.io_utils.fileio_serializers as serializers
import scqubits.utils.misc as utils
import scqubits.utils.spectrum_utils as spec_utils

from scqubits.core.namedslots_array import NamedSlotsNdarray
from scqubits.utils.typedefs import NpIndexTuple, NpIndices

if TYPE_CHECKING:
    from scqubits import HilbertSpace, SpectrumData
    from scqubits.core.param_sweep import Parameters
    from scqubits.core.qubit_base import QuantumSystem
    from scqubits.io_utils.fileio import IOData
    from scqubits.io_utils.fileio_qutip import QutipEigenstates
    from scqubits.legacy._param_sweep import _ParameterSweep
    from scqubits.utils.typedefs import QuantumSys
    from typing_extensions import Protocol

    class MixinCompatible("SpectrumLookupMixin", Protocol):
        def __getitem__(self, key: Any) -> Any:
            ...

        _parameters: Parameters
        _data: Dict[str, Any]
        _evals_count: int
        _current_param_indices: NpIndices
        _ignore_hybridization: bool


_OVERLAP_THRESHOLD = 0.5  # used, e.g., in relating bare states to dressed states


class SpectrumLookup(serializers.Serializable):
    """
    The `SpectrumLookup` is an integral building block of the `HilbertSpace` and
    `ParameterSweep` classes. In both cases it provides a convenient way to translate
    back and forth between labelling of eigenstates and eigenenergies via the indices
    of the dressed spectrum j = 0, 1, 2, ... on one hand, and the bare product-state
    labels of the form (0,0,0), (0,0,1), (0,1,0),... (here for the example of three
    subsys_list). The lookup table stored in a `SpectrumLookup` instance should be
    generated by calling `<HilbertSpace>.generate_lookup()` in the case of a
    `HilbertSpace` object. For `ParameterSweep` objects, the lookup table is
    generated automatically upon init, or manually via `<ParameterSweep>.run()`.

    Parameters
    ----------
    framework:
    dressed_specdata:
        dressed spectral data needed for generating the lookup mapping
    bare_specdata_list:
        bare spectral data needed for generating the lookup mapping
    auto_run:
        boolean variable that determines whether the lookup data is immediately
        generated upon initialization
    """

    def __init__(
        self,
        framework: "Union[_ParameterSweep, HilbertSpace, None]",
        dressed_specdata: "SpectrumData",
        bare_specdata_list: List["SpectrumData"],
        auto_run: bool = True,
    ) -> None:
        self._dressed_specdata = dressed_specdata
        self._bare_specdata_list = bare_specdata_list
        self._canonical_bare_labels: List[Tuple[int, ...]]
        self._dressed_indices: List[List[Union[int, None]]]
        self._out_of_sync = False
        self._init_params = [
            "_dressed_specdata",
            "_bare_specdata_list",
            "_canonical_bare_labels",
            "_dressed_indices",
        ]

        # Store ParameterSweep and/or HilbertSpace objects only as weakref.proxy
        # objects to avoid circular references that would prevent objects from
        # expiring appropriately and being garbage collected
        if hasattr(framework, "new_datastore"):
            # This recognizes the legacy version of `ParameterSweep`
            # Phase out and remove in future version.
            self._sweep = weakref.proxy(framework)
            self._hilbertspace = weakref.proxy(self._sweep._hilbertspace)
            cast("HilbertSpace", self._hilbertspace)
        elif isinstance(framework, scqubits.HilbertSpace):
            self._sweep = None
            self._hilbertspace = weakref.proxy(framework)
            cast("HilbertSpace", self._hilbertspace)
        else:
            self._sweep = None
            self._hilbertspace = None  # type:ignore

        if auto_run:
            self.run()

    def run(self):
        self._canonical_bare_labels = self._generate_bare_labels()
        self._dressed_indices = (
            self._generate_mappings()
        )  # lists of as many elements as there are parameter values.
        # For HilbertSpace objects the above is a single-element list.

    @classmethod
    def deserialize(cls, io_data: "IOData") -> "SpectrumLookup":
        """
        Take the given IOData and return an instance of the described class,
        initialized with the data stored in io_data.
        """
        alldata_dict = io_data.as_kwargs()
        new_spectrum_lookup = cls(
            framework=None,
            dressed_specdata=alldata_dict["_dressed_specdata"],
            bare_specdata_list=alldata_dict["_bare_specdata_list"],
            auto_run=False,
        )
        new_spectrum_lookup._canonical_bare_labels = alldata_dict[
            "_canonical_bare_labels"
        ]
        new_spectrum_lookup._dressed_indices = alldata_dict["_dressed_indices"]
        return new_spectrum_lookup

    def _generate_bare_labels(self) -> List[Tuple[int, ...]]:
        """
        Generates the list of bare-state labels in canonical order. For example,
        for a Hilbert space composed of two subsys_list sys1 and sys2, each label is
        of the type (3,0) meaning sys1 is in bare eigenstate 3, sys2 in bare
        eigenstate 0. The full list the reads [(0,0), (0,1), (0,2), ..., (0,max_2),
        (1,0), (1,1), (1,2), ..., (1,max_2), ... (max_1,0), (max_1,1), (max_1,2),
        ..., (max_1,max_2)]
        """
        dim_list = self._hilbertspace.subsystem_dims
        subsys_count = self._hilbertspace.subsystem_count

        basis_label_ranges = []
        for subsys_index in range(subsys_count):
            basis_label_ranges.append(range(dim_list[subsys_index]))

        basis_labels_list = list(
            itertools.product(*basis_label_ranges)
        )  # generate list of bare basis states (tuples)
        return basis_labels_list

    def _generate_mappings(self) -> List[List[Union[int, None]]]:
        """
        For each parameter value of the parameter sweep (may only be one if called
        from HilbertSpace, so no sweep), generate the map between bare states and
        dressed states.

        Returns ------- each list item is a list of dressed indices whose order
        corresponds to the ordering of bare indices (as stored in
        .canonical_bare_labels, thus establishing the mapping
        """
        param_indices = range(self._dressed_specdata.param_count)
        dressed_indices_list = []
        for index in param_indices:
            dressed_indices = self._generate_single_mapping(index)
            dressed_indices_list.append(dressed_indices)
        return dressed_indices_list

    def _generate_single_mapping(self, param_index: int) -> List[Union[int, None]]:
        """
        For a single parameter value with index `param_index`, create a list of the
        dressed-state indices in an order that corresponds one to one to the
        canonical bare-state product states with largest overlap (whenever possible).

        Parameters
        ----------
        param_index:
            index of the parameter value

        Returns
        -------
            dressed-state indices
        """
        overlap_matrix = spec_utils.convert_evecs_to_ndarray(
            self._dressed_specdata.state_table[param_index]  # type:ignore
        )

        dressed_indices: List[Union[int, None]] = []
        for bare_basis_index in range(
            self._hilbertspace.dimension
        ):  # for given bare basis index, find dressed index
            max_position = (np.abs(overlap_matrix[:, bare_basis_index])).argmax()
            max_overlap = np.abs(overlap_matrix[max_position, bare_basis_index])
            if max_overlap ** 2 > _OVERLAP_THRESHOLD:
                dressed_indices.append(max_position)
            else:
                dressed_indices.append(None)  # overlap too low, make no assignment
        return dressed_indices

    @utils.check_sync_status
    def dressed_index(
        self, bare_labels: Tuple[int, ...], param_index: int = 0
    ) -> Union[int, None]:
        """
        For given bare product state return the corresponding dressed-state index.

        Parameters
        ----------
        bare_labels:
            bare_labels = (index, index2, ...)
        param_index:
            index of parameter value of interest

        Returns
        -------
            dressed state index closest to the specified bare state
        """
        try:
            lookup_position = self._canonical_bare_labels.index(bare_labels)
        except ValueError:
            return None
        return self._dressed_indices[param_index][lookup_position]

    @utils.check_sync_status
    def bare_index(
        self, dressed_index: int, param_index: int = 0
    ) -> Union[Tuple[int, ...], None]:
        """
        For given dressed index, look up the corresponding bare index.

        Returns
        -------
            Bare state specification in tuple form. Example: (1,0,3) means subsystem 1
            is in bare state 1, subsystem 2 in bare state 0, and subsystem 3 in bare
            state 3.
        """
        try:
            lookup_position = self._dressed_indices[param_index].index(dressed_index)
        except ValueError:
            return None
        basis_labels = self._canonical_bare_labels[lookup_position]
        return basis_labels

    @utils.check_sync_status
    def dressed_eigenstates(self, param_index: int = 0) -> List["QutipEigenstates"]:
        """
        Return the list of dressed eigenvectors

        Parameters
        ----------
        param_index:
            position index of parameter value in question, if called from within
            `ParameterSweep`

        Returns
        -------
            dressed eigenvectors for the external parameter fixed to the value
            indicated by the provided index
        """
        return self._dressed_specdata.state_table[param_index]  # type:ignore

    @utils.check_sync_status
    def dressed_eigenenergies(self, param_index: int = 0) -> ndarray:
        """
        Return the array of dressed eigenenergies

        Parameters
        ----------
            position index of parameter value in question

        Returns
        -------
            dressed eigenenergies for the external parameter fixed to the value
            indicated by the provided index
        """
        return self._dressed_specdata.energy_table[param_index]

    @utils.check_sync_status
    def energy_bare_index(
        self, bare_tuple: Tuple[int, ...], param_index: int = 0
    ) -> Union[float, None]:
        """
        Look up dressed energy most closely corresponding to the given bare-state labels

        Parameters
        ----------
        bare_tuple:
            bare state indices
        param_index:
            index specifying the position in the self.param_vals array

        Returns
        -------
            dressed energy, if lookup successful
        """
        dressed_index = self.dressed_index(bare_tuple, param_index)
        if dressed_index is None:
            return None
        return self._dressed_specdata.energy_table[param_index][dressed_index]

    @utils.check_sync_status
    def energy_dressed_index(self, dressed_index: int, param_index: int = 0) -> float:
        """
        Look up the dressed eigenenergy belonging to the given dressed index.

        Parameters
        ----------
        dressed_index:
            index of dressed state of interest
        param_index:
            relevant if used in the context of a ParameterSweep

        Returns
        -------
            dressed energy
        """
        return self._dressed_specdata.energy_table[param_index][dressed_index]

    @utils.check_sync_status
    def bare_eigenstates(
        self, subsys: "QuantumSystem", param_index: int = 0
    ) -> ndarray:
        """
        Return ndarray of bare eigenstates for given subsystem and parameter index.
        Eigenstates are expressed in the basis internal to the subsystem.
        """
        framework = self._sweep or self._hilbertspace
        subsys_index = framework.get_subsys_index(subsys)
        return self._bare_specdata_list[subsys_index].state_table[param_index]

    @utils.check_sync_status
    def bare_eigenenergies(
        self, subsys: "QuantumSystem", param_index: int = 0
    ) -> ndarray:
        """
        Return list of bare eigenenergies for given subsystem.

        Parameters
        ----------
        subsys:
            Hilbert space subsystem for which bare eigendata is to be looked up
        param_index:
            position index of parameter value in question

        Returns
        -------
            bare eigenenergies for the specified subsystem and the external parameter
            fixed to the value indicated by its index
        """
        subsys_index = self._hilbertspace.get_subsys_index(subsys)
        return self._bare_specdata_list[subsys_index].energy_table[param_index]

    def bare_productstate(self, bare_index: Tuple[int, ...]) -> Qobj:
        """
        Return the bare product state specified by `bare_index`.

        Parameters
        ----------
        bare_index:

        Returns
        -------
            ket in full Hilbert space
        """
        subsys_dims = self._hilbertspace.subsystem_dims
        product_state_list = []
        for subsys_index, state_index in enumerate(bare_index):
            dim = subsys_dims[subsys_index]
            product_state_list.append(qt.basis(dim, state_index))
        return qt.tensor(*product_state_list)


class SpectrumLookupMixin:
    """
    SpectrumLookupMixin is used as a mix-in class by `ParameterSweep`. It makes various
    spectrum and spectrum lookup related methods directly available at the
    `ParameterSweep` level.
    """

    def __init__(self, hilbertspace: "HilbertSpace"):
        if not hasattr(self, "_hilbertspace"):
            self._hilbertspace = cast("HilbertSpace", weakref.ref(hilbertspace))

    @property
    def _bare_product_states_labels(self) -> List[Tuple[int, ...]]:
        """
        Generates the list of bare-state labels in canonical order. For example,
         for a Hilbert space composed of two subsystems sys1 and sys2, each label is
         of the type (3,0) meaning sys1 is in bare eigenstate 3, sys2 in bare
         eigenstate 0. The full list then reads
         [(0,0), (0,1), (0,2), ..., (0,max_2),
         (1,0), (1,1), (1,2), ..., (1,max_2),
         ...
         (max_1,0), (max_1,1), (max_1,2), ..., (max_1,max_2)]
        """
        return list(
            itertools.product(
                *map(range, self._hilbertspace.subsystem_dims)  # type:ignore
            )
        )

    def generate_lookup(self: "MixinCompatible") -> NamedSlotsNdarray:
        """
        For each parameter value of the parameter sweep, generate the map between
        bare states and
        dressed states.

        Returns
        -------
            each list item is a list of dressed indices whose order corresponds to the
            ordering of bare indices (as stored in .canonical_bare_labels,
            thus establishing the mapping)
        """
        dressed_indices = np.empty(shape=self._parameters.counts, dtype=object)

        param_indices = itertools.product(*map(range, self._parameters.counts))
        for index in param_indices:
            dressed_indices[index] = self._generate_single_mapping(index)
        dressed_indices = np.asarray(dressed_indices[:].tolist())

        parameter_dict = self._parameters.ordered_dict.copy()
        return NamedSlotsNdarray(dressed_indices, parameter_dict)

    def _generate_single_mapping(
        self: "MixinCompatible",
        param_indices: Tuple[int, ...],
    ) -> ndarray:
        """
        For a single set of parameter values, specified by a tuple of indices
        ``param_indices``, create an array of the dressed-state indices in an order
        that corresponds one to one to the bare product states with largest overlap
        (whenever possible).

        Parameters
        ----------
        param_indices:
            indices of the parameter values

        Returns
        -------
            dressed-state indices
        """
        overlap_matrix = spec_utils.convert_evecs_to_ndarray(
            self._data["evecs"][param_indices]
        )

        dim = self._hilbertspace.dimension
        dressed_indices: List[Union[int, None]] = [None] * dim
        for dressed_index in range(self._evals_count):
            max_position = (np.abs(overlap_matrix[dressed_index, :])).argmax()
            max_overlap = np.abs(overlap_matrix[dressed_index, max_position])
            if self._ignore_hybridization or (max_overlap ** 2 > _OVERLAP_THRESHOLD):
                overlap_matrix[:, max_position] = 0
                dressed_indices[int(max_position)] = dressed_index

        return np.asarray(dressed_indices)

    def set_npindextuple(
        self: "MixinCompatible", param_indices: Optional[NpIndices] = None
    ) -> NpIndexTuple:
        param_indices = param_indices or self._current_param_indices
        if not isinstance(param_indices, tuple):
            param_indices = (param_indices,)
        return param_indices

    @utils.check_sync_status
    def dressed_index(
        self: "MixinCompatible",
        bare_labels: Tuple[int, ...],
        param_indices: Optional[NpIndices] = None,
    ) -> Union[ndarray, int, None]:
        """
        For given bare product state return the corresponding dressed-state index.

        Parameters
        ----------
        bare_labels:
            bare_labels = (index, index2, ...)
        param_indices:
            indices of parameter values of interest

        Returns
        -------
            dressed state index closest to the specified bare state
        """
        param_indices = self.set_npindextuple(param_indices)
        try:
            lookup_position = self._bare_product_states_labels.index(bare_labels)
        except ValueError:
            return None
        return self._data["dressed_indices"][param_indices + (lookup_position,)]

    @utils.check_sync_status
    def bare_index(
        self: "MixinCompatible",
        dressed_index: int,
        param_indices: Optional[Tuple[int, ...]] = None,
    ) -> Union[Tuple[int, ...], None]:
        """
        For given dressed index, look up the corresponding bare index.

        Returns
        -------
            Bare state specification in tuple form. Example: (1,0,3) means subsystem 1
            is in bare state 1, subsystem 2 in bare state 0,
            and subsystem 3 in bare state 3.
        """
        param_index_tuple = self.set_npindextuple(param_indices)
        if not self.all_params_fixed(param_index_tuple):
            raise ValueError(
                "All parameters must be fixed to concrete values for "
                "the use of `.bare_index`."
            )
        try:
            lookup_position = np.where(
                self._data["dressed_indices"][param_index_tuple] == dressed_index
            )[0][0]
        except IndexError:
            raise ValueError(
                "Could not identify a bare index for the given dressed "
                "index {}.".format(dressed_index)
            )
        basis_labels = self._bare_product_states_labels[lookup_position]
        return basis_labels

    @utils.check_sync_status
    def eigensys(
        self: "MixinCompatible", param_indices: Optional[Tuple[int, ...]] = None
    ) -> ndarray:
        """
        Return the list of dressed eigenvectors

        Parameters
        ----------
        param_indices:
            position indices of parameter values in question

        Returns
        -------
            dressed eigensystem for the external parameter fixed to the value indicated
            by the provided index
        """
        param_index_tuple = self.set_npindextuple(param_indices)
        return self._data["evecs"][param_index_tuple]

    @utils.check_sync_status
    def eigenvals(
        self: "MixinCompatible", param_indices: Optional[Tuple[int, ...]] = None
    ) -> ndarray:
        """
        Return the array of dressed eigenenergies - primarily for running the sweep

        Parameters
        ----------
            position indices of parameter values in question

        Returns
        -------
            dressed eigenenergies for the external parameters fixed to the values
            indicated by the provided indices
        """
        param_indices_tuple = self.set_npindextuple(param_indices)
        return self._data["evals"][param_indices_tuple]

    @utils.check_sync_status
    def energy_by_bare_index(
        self: "MixinCompatible",
        bare_tuple: Tuple[int, ...],
        subtract_ground: bool = False,
        param_indices: Optional[NpIndices] = None,
    ) -> NamedSlotsNdarray:  # the return value may also be np.nan
        """
        Look up dressed energy most closely corresponding to the given bare-state labels

        Parameters
        ----------
        bare_tuple:
            bare state indices
        subtract_ground:
            whether to subtract the ground state energy
        param_indices:
            indices specifying the set of parameters

        Returns
        -------
            dressed energies, if lookup successful, otherwise nan;
        """
        param_indices = self.set_npindextuple(param_indices)
        dressed_index = self.dressed_index(bare_tuple, param_indices)

        if dressed_index is None:
            return np.nan  # type:ignore
        if isinstance(dressed_index, int):
            energy = self["evals"][param_indices + (dressed_index,)]
            if subtract_ground:
                energy -= self["evals"][param_indices + (0,)]
            return energy

        dressed_index = np.asarray(dressed_index)
        energies = np.empty_like(dressed_index)
        it = np.nditer(dressed_index, flags=["multi_index", "refs_ok"])
        sliced_energies = self["evals"][param_indices]

        for location in it:
            location = location.tolist()
            if location is None:
                energies[it.multi_index] = np.nan
            else:
                energies[it.multi_index] = sliced_energies[it.multi_index][location]
                if subtract_ground:
                    energies[it.multi_index] -= sliced_energies[it.multi_index][0]
        return NamedSlotsNdarray(
            energies, sliced_energies._parameters.paramvals_by_name
        )

    @utils.check_sync_status
    def energy_by_dressed_index(
        self: "MixinCompatible",
        dressed_index: int,
        subtract_ground: bool = False,
        param_indices: Optional[Tuple[int, ...]] = None,
    ) -> float:
        """
        Look up the dressed eigenenergy belonging to the given dressed index,
        usually to be used with pre-slicing

        Parameters
        ----------
        dressed_index:
            index of dressed state of interest
        subtract_ground:
            whether to subtract the ground state energy
        param_indices:
            specifies the desired choice of parameter values

        Returns
        -------
            dressed energy
        """
        param_indices_tuple = self.set_npindextuple(param_indices)
        self._current_param_indices: NpIndices = slice(None, None, None)
        energies = self["evals"][param_indices_tuple + (dressed_index,)]
        if subtract_ground:
            energies -= self["evals"][param_indices_tuple + (0,)]
        return energies

    @utils.check_sync_status
    def bare_eigenstates(
        self: "MixinCompatible",
        subsys: "QuantumSys",
        param_indices: Optional[Tuple[int, ...]] = None,
    ) -> NamedSlotsNdarray:
        """
        Return ndarray of bare eigenstates for given subsystems and parameter index.
        Eigenstates are expressed in the basis internal to the subsystems. Usually to be
        with pre-slicing.
        """
        param_indices_tuple = self.set_npindextuple(param_indices)
        subsys_index = self._hilbertspace.get_subsys_index(subsys)
        self._current_param_indices = slice(None, None, None)
        return self["bare_evecs"][subsys_index][param_indices_tuple]

    @utils.check_sync_status
    def bare_eigenvals(
        self: "MixinCompatible",
        subsys: "QuantumSys",
        param_indices: Optional[Tuple[int, ...]] = None,
    ) -> NamedSlotsNdarray:
        """
        Return `NamedSlotsNdarray` of bare eigenenergies for given subsystem, usually
        to be used with preslicing.

        Parameters
        ----------
        subsys:
            Hilbert space subsystem for which bare eigendata is to be looked up
        param_indices:
            position indices of parameter values in question

        Returns
        -------
            bare eigenenergies for the specified subsystem and the external parameter
            fixed to the value indicated by its index
        """
        param_indices_tuple = self.set_npindextuple(param_indices)
        subsys_index = self._hilbertspace.get_subsys_index(subsys)
        self._current_param_indices = slice(None, None, None)
        return self["bare_evals"][subsys_index][param_indices_tuple]

    def bare_productstate(self: "MixinCompatible", bare_index: Tuple[int, ...]) -> Qobj:
        """
        Return the bare product state specified by `bare_index`. Note: no parameter
        dependence here, since the Hamiltonian is always represented in the bare
        product eigenbasis.

        Parameters
        ----------
        bare_index:

        Returns
        -------
            ket in full Hilbert space
        """
        subsys_dims = self._hilbertspace.subsystem_dims
        product_state_list = []
        for subsys_index, state_index in enumerate(bare_index):
            dim = subsys_dims[subsys_index]
            product_state_list.append(qt.basis(dim, state_index))
        return qt.tensor(*product_state_list)

    def all_params_fixed(self: "MixinCompatible", param_indices) -> bool:
        if isinstance(param_indices, slice):
            param_indices = (param_indices,)
        return len(self._parameters) == len(param_indices)
