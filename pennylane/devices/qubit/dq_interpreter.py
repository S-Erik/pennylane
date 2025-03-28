# Copyright 2024 Xanadu Quantum Technologies Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

#     http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""
This module contains a class for executing plxpr using default qubit tools.
"""
from copy import copy

import jax
import numpy as np

from pennylane.capture import disable, enable
from pennylane.capture.base_interpreter import PlxprInterpreter
from pennylane.capture.primitives import (
    adjoint_transform_prim,
    cond_prim,
    ctrl_transform_prim,
    for_loop_prim,
    measure_prim,
    while_loop_prim,
)
from pennylane.measurements import MidMeasureMP, Shots

from .apply_operation import apply_operation
from .initialize_state import create_initial_state
from .measure import measure
from .sampling import measure_with_samples


class DefaultQubitInterpreter(PlxprInterpreter):
    """Implements a class for interpreting plxpr using python simulation tools.

    Args:
        num_wires (int): the number of wires to initialize the state with
        shots (int | None): the number of shots to use for the execution. Shot vectors are not supported yet.
        key (None, jax.numpy.ndarray): the ``PRNGKey`` to use for random number generation.


    >>> from pennylane.devices.qubit.dq_interpreter import DefaultQubitInterpreter
    >>> qml.capture.enable()
    >>> import jax
    >>> key = jax.random.PRNGKey(1234)
    >>> dq = DefaultQubitInterpreter(num_wires=2, shots=None, key=key)
    >>> @qml.for_loop(2)
    ... def g(i,y):
    ...     qml.RX(y,0)
    ...     return y
    >>> def f(x):
    ...     g(x)
    ...     return qml.expval(qml.Z(0))
    >>> dq(f)(0.5)
    Array(0.54030231, dtype=float64)
    >>> jaxpr = jax.make_jaxpr(f)(0.5)
    >>> dq.eval(jaxpr.jaxpr, jaxpr.consts, 0.5)
    Array(0.54030231, dtype=float64)

    This execution can be differentiated via backprop and jitted as normal. Note that finite shot executions
    still cannot be differentiated with backprop.

    >>> jax.grad(dq(f))(jax.numpy.array(0.5))
    Array(-1.68294197, dtype=float64, weak_type=True)
    >>> jax.jit(dq(f))(jax.numpy.array(0.5))
    Array(0.54030231, dtype=float64)
    """

    def __init__(
        self, num_wires: int, shots: int | None = None, key: None | jax.numpy.ndarray = None
    ):
        self.num_wires = num_wires
        self.shots = Shots(shots)
        if self.shots.has_partitioned_shots:
            raise NotImplementedError(
                "DefaultQubitInterpreter does not yet support partitioned shots."
            )
        if key is None:
            key = jax.random.PRNGKey(np.random.randint(100000))

        self.initial_key = key
        self.stateref = None
        super().__init__()

    @property
    def state(self) -> None | jax.numpy.ndarray:
        """The current state of the system. None if not initialized."""
        return self.stateref["state"] if self.stateref else None

    @state.setter
    def state(self, value: jax.numpy.ndarray | None):
        if self.stateref is None:
            raise AttributeError("execution not yet initialized.")
        self.stateref["state"] = value

    @property
    def key(self) -> jax.numpy.ndarray:
        """A jax PRNGKey. ``initial_key`` if not yet initialized."""
        return self.stateref["key"] if self.stateref else self.initial_key

    @key.setter
    def key(self, value):
        if self.stateref is None:
            raise AttributeError("execution not yet initialized.")
        self.stateref["key"] = value

    def setup(self) -> None:
        if self.stateref is None:
            self.stateref = {
                "state": create_initial_state(range(self.num_wires), like="jax"),
                "key": self.initial_key,
            }
        # else set by copying a parent interpreter and we need to modify same stateref

    def cleanup(self) -> None:
        self.initial_key = self.key  # be cautious of leaked tracers, but we should be fine.
        self.stateref = None

    def interpret_operation(self, op):
        self.state = apply_operation(op, self.state)

    def interpret_measurement_eqn(self, eqn: "jax.core.JaxprEqn"):
        if "mcm" in eqn.primitive.name:
            raise NotImplementedError(
                "DefaultQubitInterpreter does not yet support postprocessing mcms"
            )
        return super().interpret_measurement_eqn(eqn)

    def interpret_measurement(self, measurement):
        # measurements can sometimes create intermediary mps, but those intermediaries will not work with capture enabled
        disable()
        try:
            if self.shots:
                self.key, new_key = jax.random.split(self.key, 2)
                # note that this does *not* group commuting measurements
                # further work could figure out how to perform multiple measurements at the same time
                output = measure_with_samples(
                    [measurement], self.state, shots=self.shots, prng_key=new_key
                )[0]
            else:
                output = measure(measurement, self.state)
        finally:
            enable()
        return output


@DefaultQubitInterpreter.register_primitive(measure_prim)
def _(self, *invals, reset, postselect):
    mp = MidMeasureMP(invals, reset=reset, postselect=postselect)
    self.key, new_key = jax.random.split(self.key, 2)
    mcms = {}
    self.state = apply_operation(mp, self.state, mid_measurements=mcms, prng_key=new_key)
    return mcms[mp]


# pylint: disable=unused-argument
@DefaultQubitInterpreter.register_primitive(adjoint_transform_prim)
def _(self, *invals, jaxpr, n_consts, lazy=True):
    # TODO: requires jaxpr -> list of ops first
    raise NotImplementedError


# pylint: disable=too-many-arguments
@DefaultQubitInterpreter.register_primitive(ctrl_transform_prim)
def _(self, *invals, n_control, jaxpr, control_values, work_wires, n_consts):
    # TODO: requires jaxpr -> list of ops first
    raise NotImplementedError


# pylint: disable=too-many-arguments
@DefaultQubitInterpreter.register_primitive(for_loop_prim)
def _(self, start, stop, step, *invals, jaxpr_body_fn, consts_slice, args_slice):
    consts = invals[consts_slice]
    init_state = invals[args_slice]

    res = init_state
    for i in range(start, stop, step):
        res = copy(self).eval(jaxpr_body_fn, consts, i, *res)

    return res


# pylint: disable=too-many-arguments
@DefaultQubitInterpreter.register_primitive(while_loop_prim)
def _(self, *invals, jaxpr_body_fn, jaxpr_cond_fn, body_slice, cond_slice, args_slice):
    consts_body = invals[body_slice]
    consts_cond = invals[cond_slice]
    init_state = invals[args_slice]

    fn_res = init_state
    while copy(self).eval(jaxpr_cond_fn, consts_cond, *fn_res)[0]:
        fn_res = copy(self).eval(jaxpr_body_fn, consts_body, *fn_res)

    return fn_res


@DefaultQubitInterpreter.register_primitive(cond_prim)
def _(self, *invals, jaxpr_branches, consts_slices, args_slice):
    n_branches = len(jaxpr_branches)
    conditions = invals[:n_branches]
    args = invals[args_slice]

    for pred, jaxpr, const_slice in zip(conditions, jaxpr_branches, consts_slices):
        consts = invals[const_slice]
        if pred and jaxpr is not None:
            return copy(self).eval(jaxpr, consts, *args)
    return ()
