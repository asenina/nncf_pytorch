"""
 Copyright (c) 2019-2020 Intel Corporation
 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at
      http://www.apache.org/licenses/LICENSE-2.0
 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

# pylint:disable=too-many-lines
from collections import Counter
from collections import OrderedDict
from pathlib import Path
from string import Template
from typing import Dict
from typing import List
from typing import Optional
from typing import Set
from typing import Tuple

import functools
import networkx as nx
import numpy as np
import operator
import shutil
import torch
from copy import deepcopy

from nncf.utils import get_scale_shape

from nncf.common.quantization.structs import QuantizerSetupType
from nncf.dynamic_graph.graph import NNCFNodeExpression
from torch import nn

from nncf.algo_selector import COMPRESSION_ALGORITHMS
from nncf.common.os import safe_open
from nncf.common.quantization.structs import QuantizableModule
from nncf.common.quantization.structs import QuantizationConstraints
from nncf.common.quantization.structs import QuantizerGroup
from nncf.common.utils.logger import logger as nncf_logger
from nncf.api.compression import CompressionLevel
from nncf.compression_method_api import PTCompressionAlgorithmBuilder
from nncf.compression_method_api import PTCompressionAlgorithmController
from nncf.config import NNCFConfig
from nncf.debug import CallCountTracker
from nncf.debug import DebugInterface
from nncf.debug import is_debug
from nncf.dynamic_graph.context import Scope
from nncf.dynamic_graph.context import TracingContext
from nncf.dynamic_graph.graph import InputAgnosticOperationExecutionContext
from nncf.dynamic_graph.graph import NNCFGraph
from nncf.dynamic_graph.input_wrapping import MODEL_INPUT_OP_NAME
from nncf.dynamic_graph.transform_graph import is_nncf_module
from nncf.hw_config import HWConfig
from nncf.hw_config import HWConfigType
from nncf.initialization import SimpleDataLoaderRunner
from nncf.layer_utils import _NNCFModuleMixin
from nncf.module_operations import UpdateWeight
from nncf.nncf_network import ExtraCompressionModuleType
from nncf.nncf_network import InsertionCommand
from nncf.nncf_network import InsertionPoint
from nncf.nncf_network import InsertionPointGraph
from nncf.nncf_network import InsertionPointGraphNodeType
from nncf.nncf_network import InsertionType
from nncf.nncf_network import NNCFNetwork
from nncf.nncf_network import OperationPriority
from nncf.quantization.init_precision import PrecisionInitializerFactory
from nncf.quantization.init_range import DataLoaderRangeInitializeRunner
from nncf.quantization.init_range import PerLayerRangeInitConfig
from nncf.quantization.init_range import RangeInitConfig
from nncf.quantization.init_range import RangeInitParams
from nncf.quantization.init_range import StatCollectorGenerator
from nncf.quantization.layers import BaseQuantizer
from nncf.quantization.layers import PTQuantizerSpec
from nncf.quantization.layers import QUANTIZATION_MODULES
from nncf.quantization.layers import QuantizationMode
from nncf.quantization.layers import QuantizerConfig
from nncf.quantization.layers import QuantizerExportMode
from nncf.quantization.layers import QuantizersSwitcher
from nncf.quantization.metrics import MemoryCostMetric
from nncf.quantization.metrics import NetworkQuantizationShareMetric
from nncf.quantization.metrics import NetworkQuantizationShareMetricBuildTimeInfo
from nncf.quantization.metrics import ShareEdgesQuantizedDataPath
from nncf.quantization.precision_constraints import HardwareQuantizationConstraints
from nncf.quantization.precision_init.adjacent_quantizers import GroupsOfAdjacentQuantizers
from nncf.quantization.precision_init.autoq_init import AutoQPrecisionInitParams
from nncf.quantization.precision_init.base_init import BasePrecisionInitParams
from nncf.quantization.precision_init.hawq_init import HAWQPrecisionInitParams
from nncf.quantization.precision_init.manual_init import ManualPrecisionInitParams
from nncf.quantization.quantizer_id import InputQuantizerId
from nncf.quantization.quantizer_id import NonWeightQuantizerId
from nncf.quantization.quantizer_id import QuantizerId
from nncf.quantization.quantizer_id import WeightQuantizerId
from nncf.quantization.quantizer_propagation import QuantizerPropagationSolver
from nncf.quantization.quantizer_propagation import QuantizerPropagationStateGraph
from nncf.quantization.quantizer_setup import MultiConfigQuantizerSetup
from nncf.quantization.quantizer_setup import QuantizationPointId
from nncf.quantization.quantizer_setup import QuantizerSetupBase
from nncf.quantization.quantizer_setup import SingleConfigQuantizationPoint
from nncf.quantization.quantizer_setup import SingleConfigQuantizerSetup
from nncf.quantization.schedulers import QUANTIZATION_SCHEDULERS
from nncf.quantization.structs import NonWeightQuantizerInfo
from nncf.quantization.structs import WeightQuantizerInfo
from nncf.structures import AutoQPrecisionInitArgs
from nncf.structures import QuantizationPrecisionInitArgs
from nncf.structures import QuantizationRangeInitArgs
from nncf.tensor_statistics.algo import TensorStatisticsCollectionBuilder
from nncf.tensor_statistics.collectors import ReductionShape
from nncf.tensor_statistics.statistics import MinMaxTensorStatistic
from nncf.tensor_statistics.statistics import TensorStatistic
from nncf.utils import in_scope_list
from nncf.utils import is_main_process
from nncf.utils import should_consider_scope

class QuantizerSetupGeneratorBase:
    DEFAULT_QUANTIZER_CONFIG = QuantizerConfig(num_bits=8,
                                               mode=QuantizationMode.SYMMETRIC,
                                               signedness_to_force=None,
                                               per_channel=False)

    def __init__(self, quant_config: NNCFConfig,
                 target_model: NNCFNetwork,
                 precision_init_type: str = None,
                 precision_init_params: BasePrecisionInitParams = None,
                 range_init_params: RangeInitParams = None):
        self._target_model = target_model  # type: NNCFNetwork
        self._quantization_config = quant_config

        self._quantize_inputs = self._quantization_config.get('quantize_inputs', True)
        self._quantize_outputs = self._quantization_config.get('quantize_outputs', False)

        self.ignored_scopes = self._quantization_config.get('ignored_scopes')
        self.target_scopes = self._quantization_config.get('target_scopes')

        self.global_quantizer_constraints = {}  # type: Dict[QuantizerGroup, QuantizationConstraints]
        self._ignored_scopes_per_group = {}  # type: Dict[QuantizerGroup, List[str]]
        self._target_scopes_per_group = {}  # type: Dict[QuantizerGroup, List[str]]

        for quantizer_group in QuantizerGroup:
            self._parse_group_params(self._quantization_config, quantizer_group)

        self._precision_init_type = precision_init_type
        self._precision_init_params = precision_init_params
        self._range_init_params = range_init_params
        self._num_potential_quantized_weights = len(self._target_model.get_nncf_modules())

    def generate_setup(self) -> SingleConfigQuantizerSetup:
        raise NotImplementedError

    def get_build_time_metric_infos(self):
        raise NotImplementedError

    def _parse_group_params(self, quant_config: 'NNCFConfig', quantizer_group: QuantizerGroup):
        group_name = quantizer_group.value
        params_dict = quant_config.get(group_name, {})
        self.global_quantizer_constraints[quantizer_group] = QuantizationConstraints.from_config_dict(params_dict)
        self._ignored_scopes_per_group[quantizer_group] = params_dict.get('ignored_scopes')
        self._target_scopes_per_group[quantizer_group] = params_dict.get('target_scopes')

    @staticmethod
    def get_scoped_quantizer_config(base_config: QuantizerConfig,
                                    parent_module_scope_str: str,
                                    scope_overrides: Dict = None) -> QuantizerConfig:
        qconfig = deepcopy(base_config)
        if scope_overrides is None:
            scope_overrides = {}
        for overridden_scope in scope_overrides.keys():
            if in_scope_list(parent_module_scope_str, overridden_scope):
                config_overrides = scope_overrides[overridden_scope]
                if config_overrides.get("bits") is not None:
                    qconfig.num_bits = config_overrides["bits"]
                if config_overrides.get("mode") is not None:
                    qconfig.mode = config_overrides["mode"]
                if config_overrides.get("per_channel") is not None:
                    qconfig.per_channel = config_overrides["per_channel"]
                if config_overrides.get("signed") is not None:
                    qconfig.signedness_to_force = config_overrides["signed"]
        return qconfig

    def _get_default_qconfig(self, constraints: QuantizationConstraints = None):
        qconfig = deepcopy(self.DEFAULT_QUANTIZER_CONFIG)
        if constraints is not None:
            qconfig = constraints.apply_constraints_to(qconfig)
        return qconfig

    def _should_consider_scope_for_group(self, scope_str: str, group: QuantizerGroup) -> bool:
        if self.target_scopes is not None or self._target_scopes_per_group[group] is not None:
            if in_scope_list(scope_str, self.target_scopes):
                return True
            if in_scope_list(scope_str, self._target_scopes_per_group[group]):
                return True

            return False

        if in_scope_list(scope_str, self.ignored_scopes):
            return False
        if in_scope_list(scope_str, self._ignored_scopes_per_group[group]):
            return False

        return True

    def _filter_by_ignored_algo(self, modules: Dict[Scope, _NNCFModuleMixin]):
        retval = {}  # type: Dict[Scope, torch.nn.Module]
        for module_scope, module in modules.items():
            if 'quantization' in module.ignored_algorithms:
                continue
            retval[module_scope] = module
        return retval

    def _filter_by_weight_ignored_target_scopes(self, modules: Dict[Scope, torch.nn.Module]):
        retval = {}  # type: Dict[Scope, torch.nn.Module]
        for module_scope, module in modules.items():
            if not self._should_consider_scope_for_group(str(module_scope), QuantizerGroup.WEIGHTS):
                nncf_logger.info("Ignored adding Weight quantizer in scope: {}".format(module_scope))
                continue
            retval[module_scope] = module
        return retval

    def _assign_qconfig_lists_to_modules(self, modules: Dict[Scope, torch.nn.Module]) -> \
        Dict[Scope, List[QuantizerConfig]]:
        raise NotImplementedError

    def get_quantizable_modules(self) -> List[QuantizableModule]:
        modules = self._target_model.get_nncf_modules()
        quantized_modules_with_potential_qconfig = []

        modules = self._filter_by_ignored_algo(modules)
        modules = self._filter_by_weight_ignored_target_scopes(modules)
        module_scope_vs_qconfig_list = self._assign_qconfig_lists_to_modules(modules)

        for module_scope, qconfig_list in module_scope_vs_qconfig_list.items():
            module = modules[module_scope]
            if qconfig_list is not None:
                qconfig_list_copy = deepcopy(qconfig_list)
                for qconfig in qconfig_list_copy:
                    qconfig.input_shape = module.weight.shape
                quantized_modules_with_potential_qconfig.append(QuantizableModule(module,
                                                                                  module_scope,
                                                                                  qconfig_list_copy))
        return quantized_modules_with_potential_qconfig


class PatternBasedQuantizerSetupGenerator(QuantizerSetupGeneratorBase):
    def __init__(self, quant_config: NNCFConfig, target_model: NNCFNetwork,
                 precision_init_type: str = None,
                 precision_init_params: BasePrecisionInitParams = None,
                 range_init_params: RangeInitParams = None):
        super().__init__(quant_config, target_model, precision_init_type, precision_init_params, range_init_params)
        self.quantizable_subgraph_patterns = self._quantization_config.get('quantizable_subgraph_patterns', None)
        self._num_potential_quantized_activations = 0

    def _assign_qconfig_lists_to_modules(self,
                                         module_scope_vs_module_dict: Dict[Scope, torch.nn.Module]) -> \
            Dict[Scope, List[QuantizerConfig]]:
        global_constraints = self.global_quantizer_constraints[QuantizerGroup.WEIGHTS]
        default_qconfig = self._get_default_qconfig(constraints=global_constraints)
        scope_overrides_dict = self._quantization_config.get("scope_overrides", {})
        retval = {}  # type: Dict[Scope, List[QuantizerConfig]]
        for module_scope in module_scope_vs_module_dict:
            qconfig_for_current_scope = self.get_scoped_quantizer_config(default_qconfig,
                                                                         str(module_scope),
                                                                         scope_overrides_dict)
            retval[module_scope] = [qconfig_for_current_scope]
        return retval

    def _quantize_weights(self) -> List[SingleConfigQuantizationPoint]:
        retval = []
        quantizable_modules = self.get_quantizable_modules()
        for _, module_scope, qconfig_list in quantizable_modules:
            nncf_logger.info("Adding signed Weight quantizer in scope: {}".format(module_scope))

            assert len(qconfig_list) == 1, "Non-HW config scenarios should produce single quantizer configs for each " \
                                        "weight module!"
            qconfig = qconfig_list[0]
            ip = InsertionPoint(InsertionType.NNCF_MODULE_PRE_OP, module_scope=module_scope)
            retval.append(SingleConfigQuantizationPoint(ip, qconfig))
        return retval

    class InsertionInfo:
        def __init__(self, insertion_point: InsertionPoint,
                     is_input=False,
                     is_output=False,
                     shape_to_operate_on=None):
            self.insertion_point = insertion_point
            self.is_input = is_input
            self.is_output = is_output
            self.shape_to_operate_on = shape_to_operate_on

        def __eq__(self, other: 'InsertionInfo'):
            return self.insertion_point == other.insertion_point

        def __str__(self):
            return str(self.insertion_point)

        def __hash__(self):
            return hash(str(self))

    def _get_post_pattern_insertion_infos(self, pattern: NNCFNodeExpression,
                                          original_graph: NNCFGraph) -> List[InsertionInfo]:
        io_infos = original_graph.get_matching_nncf_graph_pattern_io_list(pattern)

        insertion_infos = []
        for io_info in io_infos:
            # The input/output is given in terms of edges, but the post-hooks are currently applied to
            # nodes. Multiple output edges in a pattern I/O info may originate from one and the same
            # node, and we have to ensure that these resolve into just one insertion point - thus the usage of "set".
            pattern_insertion_info_set = set()
            if len(io_info.output_edges) > 1:
                nncf_logger.debug("WARNING: pattern has more than one activation output")

            for nncf_node in io_info.output_nodes:
                ip = InsertionPoint(InsertionType.OPERATOR_POST_HOOK,
                                    ia_op_exec_context=nncf_node.op_exec_context.input_agnostic)
                ii = PatternBasedQuantizerSetupGenerator.InsertionInfo(ip,
                                                                       is_output=True,
                                                                       shape_to_operate_on=None)
                pattern_insertion_info_set.add(ii)
                # TODO: determine output shapes for output nodes to enable per-channel quantization

            # Ignore input nodes in the pattern for now, rely on the _quantize_inputs functions.
            # TODO: handle input quantization here as well

            # Since this function is currently only used for activation quantization purposes via operator
            # post-hook mechanism, we may take any edge and it will point from the same node where we will have to
            # insert a quantizer later. However, in the future the output edges may refer to activation tensors
            # with different sizes, in which case we have to insert different per-channel quantizers to
            # accomodate different trainable params if there is a difference in the channel dimension.
            # Furthermore, currently there is no distinction for single tensor output to multiple nodes and
            # multiple tensor output to multiple nodes ("chunk" operation is an example of the latter).
            # The pattern may also have unexpected outputs from a node in the middle of the pattern (see
            # "densenet121.dot" for an example of this) - need to decide what to do with that in terms
            # of quantization.
            # TODO: address the issues above.

            for nncf_edge in io_info.output_edges:
                ip = InsertionPoint(InsertionType.OPERATOR_POST_HOOK,
                                    ia_op_exec_context=nncf_edge.from_node.op_exec_context.input_agnostic, )
                ii = PatternBasedQuantizerSetupGenerator.InsertionInfo(ip,
                                                                       is_output=False,
                                                                       shape_to_operate_on=nncf_edge.tensor_shape)
                pattern_insertion_info_set.add(ii)
            insertion_infos += list(pattern_insertion_info_set)

        insertion_infos = list(
            set(insertion_infos))  # Filter the overlapping insertion points from different matches (happens for GNMT)

        return insertion_infos

    def _quantize_activations(self) -> SingleConfigQuantizerSetup:
        pattern = self._make_quantizable_subgraph_pattern()
        original_graph = self._target_model.get_original_graph()
        target_insertion_infos = self._get_post_pattern_insertion_infos(pattern, original_graph)
        self._num_potential_quantized_activations = len(target_insertion_infos)

        filtered_insertion_points = []
        for ii in target_insertion_infos:
            ia_op_exec_context = ii.insertion_point.ia_op_exec_context
            operator_scope_str = str(ia_op_exec_context)
            if not self._quantize_outputs and ii.is_output:
                nncf_logger.info("Ignored adding Activation Quantize "
                                 "in scope (output scope, quantize_outputs=False): {}".format(operator_scope_str))
                continue
            if not self._should_consider_scope_for_group(operator_scope_str, QuantizerGroup.ACTIVATIONS):
                nncf_logger.info("Ignored adding Activation quantizer in scope: {}".format(operator_scope_str))
                continue
            filtered_insertion_points.append(InsertionPoint(InsertionType.OPERATOR_POST_HOOK,
                                                            ia_op_exec_context=ia_op_exec_context))

        retval = SingleConfigQuantizerSetup()

        global_constraints = self.global_quantizer_constraints[QuantizerGroup.ACTIVATIONS]
        default_qconfig = self._get_default_qconfig(constraints=global_constraints)

        act_config = self._quantization_config.get('activations', {})
        if 'linked_quantizer_scopes' in act_config:
            linked_scopes_groups_list = act_config['linked_quantizer_scopes']
        else:
            linked_scopes_groups_list = None
        target_insertion_point_lists = self.coalesce_insertion_points(filtered_insertion_points,
                                                                      linked_scopes_groups_list)

        scope_overrides_dict = self._quantization_config.get("scope_overrides", {})

        for ip_list in target_insertion_point_lists:
            assert ip_list
            main_ip = ip_list[0]
            ia_op_exec_context = main_ip.ia_op_exec_context
            operator_scope_str = str(ia_op_exec_context)

            qconfig = self.get_scoped_quantizer_config(default_qconfig,
                                                       operator_scope_str,
                                                       scope_overrides_dict)

            main_qp = SingleConfigQuantizationPoint(main_ip, qconfig)
            if len(ip_list) == 1:
                retval.add_independent_quantization_point(main_qp)
            else:
                linked_ips = ip_list[1:]
                linked_qps = [SingleConfigQuantizationPoint(linked_ip, qconfig) for linked_ip in linked_ips]
                qp_group = [main_qp] + linked_qps
                retval.add_unified_scale_group(qp_group)
        return retval

    def _get_input_quantization_points(self) -> List[SingleConfigQuantizationPoint]:
        retval = []
        insertion_point_graph = self._target_model.get_insertion_point_graph()
        input_ips = insertion_point_graph.get_input_insertion_points()

        for ip in input_ips:
            assert ip.ia_op_exec_context.operator_name == MODEL_INPUT_OP_NAME
            input_id = ip.ia_op_exec_context.call_order
            if self._target_model.input_infos[input_id].is_integer_input():
                continue
            base_config = self._get_default_qconfig(self.global_quantizer_constraints[QuantizerGroup.ACTIVATIONS])
            qconfig = self.get_scoped_quantizer_config(base_config,
                                                       str(ip.ia_op_exec_context),
                                                       scope_overrides=self._quantization_config.get("scope_overides"))
            qp = SingleConfigQuantizationPoint(ip, qconfig)
            retval.append(qp)

        return retval

    @staticmethod
    def _make_default_quantizable_subgraph_pattern():
        import nncf.dynamic_graph.patterns as p
        pattern = p.LINEAR_OPS | p.ARITHMETIC | p.ANY_BN_ACT_COMBO | \
                  p.LINEAR_OPS + p.ANY_BN_ACT_COMBO | p.ARITHMETIC + p.ANY_BN_ACT_COMBO | p.SINGLE_OPS | p.MATMUL
        return pattern

    @staticmethod
    def coalesce_insertion_points(target_insertion_points: List[InsertionPoint],
                                  linked_scopes_groups_list: List[List[str]]) -> List[List[InsertionPoint]]:
        """Accepts a list of InsertionPoints and groups these according to linked_scope_groups_list.
        Each entry in linked_scope_groups_list must be a valid string representation of a single
        InputAgnosticOperationExecutionContext object."""
        #pylint:disable=too-many-branches
        if linked_scopes_groups_list is None:
            return [[ip, ] for ip in target_insertion_points]
        ia_op_exec_context_list = [x.ia_op_exec_context for x in target_insertion_points]
        retval = []
        insertion_point_indices_vs_group_id = OrderedDict()

        for group_idx, group_list in enumerate(linked_scopes_groups_list):
            for group_member_scope_str in group_list:
                ia_op_exec_context = InputAgnosticOperationExecutionContext.from_str(group_member_scope_str)
                matching_indices = list(
                    filter(lambda x: ia_op_exec_context_list[x] == ia_op_exec_context,
                           range(len(ia_op_exec_context_list))))
                if len(matching_indices) > 1:
                    raise RuntimeError(
                        "Linked activation quantizer entry {} specifies more than 1 activation quantizer:\n {}".format(
                            group_member_scope_str,
                            "\n".join([str(ia_op_exec_context_list[i]) for i in matching_indices])))
                if len(matching_indices) == 0:
                    raise RuntimeError("No match for linked quantizer entry {} among activation quantizers!".format(
                        group_member_scope_str))

                target_idx = matching_indices[0]
                if target_idx in insertion_point_indices_vs_group_id:
                    raise RuntimeError(
                        "Linked activation quantizer groups {} and {} "
                        "overlap!".format(group_idx,
                                          insertion_point_indices_vs_group_id[target_idx])
                    )
                insertion_point_indices_vs_group_id[target_idx] = group_idx

        for i in range(len(ia_op_exec_context_list)):
            if i not in insertion_point_indices_vs_group_id:
                insertion_point_indices_vs_group_id[i] = None

        group_indices_list = [[] for _ in linked_scopes_groups_list]  # type: List[List[int]]
        for insertion_point_idx, group_idx in insertion_point_indices_vs_group_id.items():
            if group_idx is not None:
                group_indices_list[group_idx].append(insertion_point_idx)

        for intra_group_indices in group_indices_list:
            main_ip_idx = intra_group_indices[0]
            main_ip = target_insertion_points[main_ip_idx]
            grouped_list = [main_ip, ]
            for linked_ip_idx in intra_group_indices[1:]:
                grouped_list.append(target_insertion_points[linked_ip_idx])
            retval.append(grouped_list)

        for insertion_point_idx, group_idx in insertion_point_indices_vs_group_id.items():
            if group_idx is None:
                retval.append([target_insertion_points[insertion_point_idx], ])

        return retval


    def _make_quantizable_subgraph_pattern(self):
        full_pattern = self._make_default_quantizable_subgraph_pattern()
        if self.quantizable_subgraph_patterns is not None:
            for pattern in self.quantizable_subgraph_patterns:
                if not isinstance(pattern, str):
                    custom_pattern = functools.reduce(operator.add,
                                                      [NNCFNodeExpression(node) for node in pattern])
                else:
                    custom_pattern = NNCFNodeExpression(pattern)
                full_pattern = full_pattern | custom_pattern
        return full_pattern

    def _apply_overriding_precision_init(self, quantizer_setup: SingleConfigQuantizerSetup,
                                         precision_init_type: str,
                                         precision_init_params: BasePrecisionInitParams) -> \
        SingleConfigQuantizerSetup:
        with self._target_model.temporary_clean_view() as intermediate_model:
            stats = QuantizationBuilder.get_statistics_for_quantizer_setup(intermediate_model,
                                                                           quantizer_setup,
                                                                           self._range_init_params)
            intermediate_builder = ExperimentalQuantizationBuilder(quantizer_setup, stats)
            intermediate_builder.apply_to(intermediate_model)
            #pylint:disable=line-too-long
            intermediate_ctrl = intermediate_model.commit_compression_changes()  # type: ExperimentalQuantizationController

            # intermediate_ctrl.init_range()
            precision_constraints = HardwareQuantizationConstraints()
            final_quantizer_setup = intermediate_ctrl.init_precision(precision_init_type,
                                                                     precision_init_params,
                                                                     precision_constraints)
        return final_quantizer_setup

    def generate_setup(self) -> SingleConfigQuantizerSetup:
        # Due to the lack of the QuantizerPropagationStateGraph information in pattern-based mode,
        # the resulting setup will have no information about which quantization points share inputs.
        setup = self._quantize_activations()
        weight_qps = self._quantize_weights()
        for weight_qp in weight_qps:
            setup.add_independent_quantization_point(weight_qp)

        if self._quantize_inputs:
            input_qps = self._get_input_quantization_points()
            for input_qp in input_qps:
                setup.add_independent_quantization_point(input_qp)
        if self._precision_init_type is not None:
            setup = self._apply_overriding_precision_init(setup,
                                                          self._precision_init_type,
                                                          self._precision_init_params)

        return setup

    def get_build_time_metric_infos(self):
        return NetworkQuantizationShareMetricBuildTimeInfo(self._num_potential_quantized_activations,
                                                           self._num_potential_quantized_weights)

class IQuantizerSetupDisambiguator:
    def select_final_quantizer_setup(self, multi_config_setup: MultiConfigQuantizerSetup) -> SingleConfigQuantizerSetup:
        raise NotImplementedError


class DefaultQuantizerSetupDisambiguator(IQuantizerSetupDisambiguator):
    def __init__(self, target_model: NNCFNetwork,
                 precision_init_type: str = None,
                 precision_init_params: BasePrecisionInitParams = None,
                 range_init_params: RangeInitParams = None,
                 override_bit_options_with_precision_init: bool = False):
        self._precision_init_type = precision_init_type
        self._precision_init_params = precision_init_params
        self._range_init_params = range_init_params
        self._target_model = target_model
        self._override_bit_options_with_precision_init = override_bit_options_with_precision_init

    @staticmethod
    def select_first_qconfig_with_bitwidth_variants_for_each_point(
            multi_config_setup: MultiConfigQuantizerSetup) -> MultiConfigQuantizerSetup:
        new_setup = deepcopy(multi_config_setup)
        for qp_id, qp in multi_config_setup.quantization_points.items():
            main_qconfig = qp.possible_qconfigs[0]
            constrained_qconfig_list = [main_qconfig]
            if len(qp.possible_qconfigs) > 1:
                constrained_qconfig_list += list(filter(main_qconfig.is_a_bitwidth_variant, qp.possible_qconfigs[1:]))
            new_setup.quantization_points[qp_id].possible_qconfigs = constrained_qconfig_list
        return new_setup

    def select_final_quantizer_setup(self, multi_config_setup: MultiConfigQuantizerSetup) -> SingleConfigQuantizerSetup:
        if self._precision_init_type is not None:
            with self._target_model.temporary_clean_view() as intermediate_model:
                stats = QuantizationBuilder.get_statistics_for_quantizer_setup(intermediate_model,
                                                                               multi_config_setup,
                                                                               self._range_init_params)
                bitwidth_varying_only_multi_setup = \
                    self.select_first_qconfig_with_bitwidth_variants_for_each_point(multi_config_setup)

                init_setup = bitwidth_varying_only_multi_setup.select_first_qconfig_for_each_point()
                intermediate_builder = ExperimentalQuantizationBuilder(init_setup, stats)
                intermediate_builder.apply_to(intermediate_model)
                #pylint:disable=line-too-long
                intermediate_ctrl = intermediate_model.commit_compression_changes()  # type: ExperimentalQuantizationController

                # intermediate_ctrl.init_range()
                hw_constraints = HardwareQuantizationConstraints()
                if not self._override_bit_options_with_precision_init:
                    for qp_id, qp in multi_config_setup.quantization_points.items():
                        quantizer_module_id = intermediate_ctrl.setup_to_module_id_translation_dict[qp_id]
                        hw_constraints.add(quantizer_module_id, qp.possible_qconfigs)
                final_quantizer_setup = intermediate_ctrl.init_precision(self._precision_init_type,
                                                                         self._precision_init_params,
                                                                         hw_constraints)
        else:
            final_quantizer_setup = multi_config_setup.select_first_qconfig_for_each_point()
        return final_quantizer_setup


class PropagationBasedQuantizerSetupGenerator(QuantizerSetupGeneratorBase):
    def __init__(self, quant_config: NNCFConfig, target_model: NNCFNetwork,
                 hw_config: HWConfig = None,
                 precision_init_type: str = None,
                 precision_init_params: BasePrecisionInitParams = None,
                 range_init_params: RangeInitParams = None,
                 debug_interface: 'QuantizationDebugInterface' = None):
        super().__init__(quant_config, target_model, precision_init_type, precision_init_params, range_init_params)

        self.hw_config = hw_config

        self._hw_precision_constraints = HardwareQuantizationConstraints()
        self._debug_interface = debug_interface
        self._num_potential_quantized_activations = 0

    def generate_setup(self) -> SingleConfigQuantizerSetup:
        quantizable_modules = self.get_quantizable_modules()

        insertion_point_graph = self._target_model.get_insertion_point_graph()
        if self._debug_interface:
            self._debug_interface.visualize_insertion_point_graph(insertion_point_graph)
        prop_graph_solver = QuantizerPropagationSolver(
            ignored_scopes=self.ignored_scopes,
            debug_interface=self._debug_interface,
            hw_config=self.hw_config,
            default_qconfig_list=[self._get_default_qconfig(
                constraints=self.global_quantizer_constraints[
                    QuantizerGroup.ACTIVATIONS])],
            input_infos=self._target_model.get_input_infos(),
            quantizable_modules=quantizable_modules,
            scope_overrides=self._quantization_config.get("scope_overrides", {}),
            global_constraints=self.global_quantizer_constraints)

        merged_ip_graph = insertion_point_graph.get_ip_graph_with_merged_hw_optimized_operations(self.hw_config)
        quantization_proposal = prop_graph_solver.run_on_ip_graph(merged_ip_graph)
        self._num_potential_quantized_activations = prop_graph_solver.get_num_potential_quantized_activations()

        quantizer_setup = deepcopy(quantization_proposal.quantizer_setup)
        quantization_proposal.quantizer_setup = quantizer_setup

        disambiguator = DefaultQuantizerSetupDisambiguator(
            self._target_model,
            self._precision_init_type,
            self._precision_init_params,
            self._range_init_params,
            override_bit_options_with_precision_init=self.hw_config is None)

        single_config_quantizer_setup = disambiguator.select_final_quantizer_setup(
            quantization_proposal.quantizer_setup)

        finalized_proposal = quantization_proposal.finalize(single_config_quantizer_setup,
                                                            strict=self.hw_config is not None)
        finalized_quantizer_setup = prop_graph_solver.get_final_quantizer_setup(finalized_proposal)
        finalized_quantizer_setup = self._handle_quantize_inputs_option(finalized_quantizer_setup)
        return finalized_quantizer_setup

    @staticmethod
    def _check_if_ip_graph_nodes_point_to_single_module(ip_graph_node_list: List[dict]):
        """Does not access actual modules - only uses the InputAgnosticOperationExecutionContext info."""
        ia_op_exec_contexts_list = []  # type: List[InputAgnosticOperationExecutionContext]
        for ip_graph_op_node in ip_graph_node_list:
            nncf_node = ip_graph_op_node[InsertionPointGraph.REGULAR_NODE_REF_NODE_ATTR]
            ia_op_exec_context = nncf_node[NNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR].input_agnostic
            ia_op_exec_contexts_list.append(ia_op_exec_context)

        contexts_correspond_to_single_module = True
        first_op_context = ia_op_exec_contexts_list[0]
        for other_op_context in ia_op_exec_contexts_list:
            if other_op_context.scope_in_model != first_op_context.scope_in_model:
                contexts_correspond_to_single_module = False
                break

        if not contexts_correspond_to_single_module:
            raise RuntimeError("NNCF module has more than 1 associated graph operation node corresponding"
                               "to different module hierarchy locations - cannot make sure that weight "
                               "quantization will be correct")

    def _assign_qconfig_lists_to_modules(self, module_scope_vs_module_dict: Dict[Scope, torch.nn.Module]) -> Dict[
            Scope, List[QuantizerConfig]]:
        retval = {}  # type: Dict[Scope, List[QuantizerConfig]]
        insertion_point_graph = self._target_model.get_insertion_point_graph()
        global_constraints = self.global_quantizer_constraints[QuantizerGroup.WEIGHTS]
        default_qconfig = self._get_default_qconfig(constraints=global_constraints)
        scope_overrides_dict = self._quantization_config.get("scope_overrides", {})
        if self.hw_config is not None:
            meta_vs_qconfig_map = self.hw_config.get_metatype_vs_quantizer_configs_map(for_weights=True)
        for module_scope in module_scope_vs_module_dict:
            qconfig_for_current_scope = self.get_scoped_quantizer_config(default_qconfig,
                                                                         str(module_scope),
                                                                         scope_overrides_dict)
            if self.hw_config is None:
                qconfig_list = [qconfig_for_current_scope]
            else:
                associated_ops = insertion_point_graph.get_op_nodes_in_scope(module_scope)
                if not associated_ops:
                    raise RuntimeError(
                        "Could not find a patched operation corresponding to NNCF module scope {}".format(
                            str(module_scope)))

                if len(associated_ops) > 1:
                    self._check_if_ip_graph_nodes_point_to_single_module(associated_ops)
                graph_operation = associated_ops[0]
                metatype = graph_operation[InsertionPointGraph.OPERATOR_METATYPE_NODE_ATTR]
                qconfig_list = meta_vs_qconfig_map[metatype]
                if HWConfig.is_wildcard_quantization(qconfig_list):  # Empty list = wildcard quantization
                    qconfig_list = [default_qconfig]
                elif HWConfig.is_qconf_list_corresponding_to_unspecified_op(qconfig_list):
                    continue  # The module will not have its weights quantized
                try:
                    local_constraints = global_constraints
                    for overridden_scope, scoped_override_dict in scope_overrides_dict.items():
                        if in_scope_list(str(module_scope), overridden_scope):
                            scope_constraints = QuantizationConstraints.from_config_dict(scoped_override_dict)
                            local_constraints = local_constraints.get_updated_constraints(scope_constraints)
                    qconfig_list = local_constraints.constrain_qconfig_list(qconfig_list)

                except RuntimeError as e:
                    err_msg = "Quantization parameter constraints specified in NNCF config are incompatible with HW "
                    err_msg += "capabilities as specified in HW config type '{}'. ".format(self.hw_config.target_device)
                    err_msg += "First conflicting quantizer location: {}".format(str(module_scope))
                    raise RuntimeError(err_msg) from e

            retval[module_scope] = qconfig_list
        return retval

    def _handle_quantize_inputs_option(self, quantizer_setup: SingleConfigQuantizerSetup) -> SingleConfigQuantizerSetup:
        qp_ids_to_discard = []
        for qp_id, qp in quantizer_setup.quantization_points.items():
            if qp.is_activation_quantization_point():
                insertion_point = qp.insertion_point
                ia_op_exec_context = insertion_point.ia_op_exec_context
                if not self._quantize_inputs and ia_op_exec_context.operator_name == MODEL_INPUT_OP_NAME:
                    qp_ids_to_discard.append(qp_id)
        for qp_id in qp_ids_to_discard:
            quantizer_setup.discard(qp_id, keep_shared_input_qps=True)
        return quantizer_setup

    def get_build_time_metric_infos(self):
        return NetworkQuantizationShareMetricBuildTimeInfo(self._num_potential_quantized_activations,
                                                           self._num_potential_quantized_weights)


@COMPRESSION_ALGORITHMS.register('quantization')
class QuantizationBuilder(PTCompressionAlgorithmBuilder):
    def __init__(self, config, should_init: bool = True):
        super().__init__(config, should_init)
        self._debug_interface = QuantizationDebugInterface() if is_debug() else None
        self._weight_quantizers = OrderedDict()  # Quantizers applied via UpdateWeights
        self._non_weight_quantizers = OrderedDict()  # All the other quantizers
        self._processed_activation_quantizer_insertion_points = set()  # type: Set[InsertionPoint]
        self._groups_of_adjacent_quantizers = GroupsOfAdjacentQuantizers()  # type: GroupsOfAdjacentQuantizers
        self._setup_to_module_id_translation_dict = {}  # type: Dict[QuantizationPointId, QuantizerId]
        self.quantizer_setup_type = self.config.get('quantizer_setup_type')
        self.eval_ops_exec_ctx = []
        self._build_time_metric_infos = None
        self.hw_config = None

        hw_config_type = self.config.get("hw_config_type")
        if hw_config_type is not None:
            hw_config_path = HWConfig.get_path_to_hw_config(hw_config_type)
            self.hw_config = HWConfig.from_json(hw_config_path)

        self._range_init_params = None
        self._precision_init_type = None
        self._precision_init_params = None
        if should_init:
            self._parse_init_params()

        self._use_logarithm_scale_per_group = {}  # type: Dict[QuantizerGroup, bool]

        for quantizer_group in QuantizerGroup:
            group_name = quantizer_group.value
            params_dict = self.config.get(group_name, {})
            self._use_logarithm_scale_per_group[quantizer_group] = params_dict.get('logarithm_scale', False)

    def _parse_init_params(self):
        init_config = self.config.get('initializer', {})
        self._range_init_params = self._parse_range_init_params(init_config)
        self._precision_init_type, self._precision_init_params = self._parse_precision_init_params(init_config)

    def _parse_range_init_params(self, initializer_config: Dict) -> RangeInitParams:
        init_range_config_dict_or_list = initializer_config.get('range', {})
        if not init_range_config_dict_or_list:
            try:
                self.config.get_extra_struct(QuantizationRangeInitArgs)
                has_range_init_args = True
            except KeyError:
                has_range_init_args = False

            if has_range_init_args:
                nncf_logger.warning("Enabling quantization range initialization with default parameters.")
                num_init_samples = 256
            else:
                nncf_logger.warning("Initializer section not specified for quantization algorithm in NNCF config and "
                                    "quantization init args not supplied - quantizer range initialization will not be "
                                    "done")
                return None

            init_range_config_dict_or_list = {'num_init_samples': num_init_samples}

        max_num_init_samples = 0
        global_range_init_config = None
        scope_overrides = []  # type: List[PerLayerRangeInitConfig]
        if isinstance(init_range_config_dict_or_list, dict):
            global_range_init_config = RangeInitConfig.from_dict(init_range_config_dict_or_list)
            max_num_init_samples = global_range_init_config.num_init_samples
        else:
            for sub_init_range_config_dict in init_range_config_dict_or_list:
                scope_overrides.append(PerLayerRangeInitConfig.from_dict(sub_init_range_config_dict))
                max_num_init_samples_config = max(scope_overrides, key=lambda x: x.num_init_samples)
                max_num_init_samples = max_num_init_samples_config.num_init_samples

        if max_num_init_samples == 0:
            return None

        try:
            range_init_args = self.config.get_extra_struct(QuantizationRangeInitArgs)
        except KeyError as e:
            raise ValueError(
                'Should run range initialization as specified via config,'
                'but the initializing data loader is not provided as an extra struct. '
                'Refer to `NNCFConfig.register_extra_structs` and the `QuantizationRangeInitArgs` class') from e

        return RangeInitParams(range_init_args.data_loader,
                               range_init_args.device,
                               global_range_init_config,
                               scope_overrides)

    def _parse_precision_init_params(self, initializer_config: Dict) -> Tuple[str, BasePrecisionInitParams]:
        init_precision_config = initializer_config.get('precision', None)
        if not init_precision_config:
            return None, None
        precision_init_type = init_precision_config.get('type', 'manual')
        if precision_init_type == 'hawq':
            try:
                precision_init_args = self.config.get_extra_struct(QuantizationPrecisionInitArgs)
            except KeyError as e:
                raise ValueError(
                    'Specified non-manual precision initialization in the NNCF config, '
                    'but the initializing data loader and loss criterion are not provided as an extra struct. '
                    'Refer to `NNCFConfig.register_extra_structs` and the `QuantizationPrecisionInitArgs` '
                    'class') from e
            precision_init_params = HAWQPrecisionInitParams.from_config(
                init_precision_config,
                precision_init_args
            )
        elif precision_init_type == "autoq":
            if self.hw_config is not None and self.hw_config.target_device != HWConfigType.VPU.value:
                raise ValueError("Unsupported device ({}). Automatic Precision Initialization only supports for "
                                 "target_device NONE or VPU".format(self.hw_config.target_device))
            try:
                precision_init_args = self.config.get_extra_struct(AutoQPrecisionInitArgs)
            except KeyError as e:
                raise ValueError('Specified Automated precision initialization in the NNCF config, '
                                 'but the initializing data loader and loss criterion are not provided as an extra '
                                 'struct. Refer to `NNCFConfig.register_extra_structs` and the '
                                 '`AutoQPrecisionInitArgs` class') from e

            hw_config_type = None
            if self.hw_config is not None:
                hw_config_type = HWConfigType.from_str(self.hw_config.target_device)
            precision_init_params = AutoQPrecisionInitParams.from_config(init_precision_config,
                                                                         precision_init_args,
                                                                         hw_config_type)
        else:
            precision_init_params = ManualPrecisionInitParams.from_config(init_precision_config)

        return precision_init_type, precision_init_params

    def _apply_to(self, target_model: NNCFNetwork) -> List[InsertionCommand]:
        target_model.register_compression_module_type(ExtraCompressionModuleType.ACTIVATION_QUANTIZER)
        single_config_quantizer_setup = self._get_quantizer_setup(target_model)
        minmax_values_for_range_init = {}
        if self.should_init:
            stats_for_range_init = self._get_statistics_for_final_range_init(target_model,
                                                                             single_config_quantizer_setup,
                                                                             self._range_init_params)
            minmax_values_for_range_init = single_config_quantizer_setup.get_minmax_values(stats_for_range_init,
                                                                                           target_model)
        insertion_commands, setup_to_module_id_translation_dict = \
            self._build_insertion_commands_list_for_quantizer_setup(single_config_quantizer_setup,
                                                                    target_model,
                                                                    minmax_values_for_range_init)

        self._setup_to_module_id_translation_dict = setup_to_module_id_translation_dict
        all_quantizations = {}
        all_quantizations.update({k: v.quantizer_module_ref for k, v in self._weight_quantizers.items()})
        all_quantizations.update({k: v.quantizer_module_ref for k, v in self._non_weight_quantizers.items()})
        self._groups_of_adjacent_quantizers.parse_from_quantizer_setup(all_quantizations, single_config_quantizer_setup,
                                                                       setup_to_module_id_translation_dict)

        # NOTE: Order of activations must be the same to correctly broadcast parameters (e.g. scales) in distributed
        # mode (see call of `_dist_broadcast_coalesced` in torch/nn/parallel/distributed.py for more details)
        # pylint: disable=protected-access
        target_model.sort_compression_modules(ExtraCompressionModuleType.ACTIVATION_QUANTIZER)

        if self._debug_interface is not None:
            target_model.debug_interface.add_interface(self._debug_interface)
        return insertion_commands

    @staticmethod
    def get_statistics_for_quantizer_setup(target_model: NNCFNetwork,
                                           quantizer_setup: QuantizerSetupBase,
                                           range_init_params: RangeInitParams) \
        -> Dict[InsertionPoint, Dict[ReductionShape, TensorStatistic]]:
        if range_init_params is None:
            return {}
        observation_points_vs_collectors_dict = StatCollectorGenerator. \
            generate_collectors_for_range_init_statistics_collection(target_model,
                                                                     quantizer_setup,
                                                                     range_init_params)

        with target_model.temporary_clean_view() as intermediate_model:
            stat_builder = TensorStatisticsCollectionBuilder(NNCFConfig(),
                                                             observation_points_vs_collectors_dict)
            stat_builder.apply_to(intermediate_model)
            stat_ctrl = intermediate_model.commit_compression_changes()
            runner = SimpleDataLoaderRunner(intermediate_model, range_init_params.device)
            runner.run(range_init_params.init_range_data_loader,
                       range_init_params.get_max_num_init_steps())

        retval = {}
        for ip, collector in stat_ctrl.ip_vs_collector_dict.items():
            retval[ip] = collector.get_statistics()
        return retval

    def _get_statistics_for_final_range_init(self, target_model: NNCFNetwork,
                                             quantizer_setup: QuantizerSetupBase,
                                             range_init_params: RangeInitParams) \
            -> Dict[InsertionPoint, Dict[ReductionShape, TensorStatistic]]:
        return self.get_statistics_for_quantizer_setup(target_model, quantizer_setup, range_init_params)

    def _get_quantizer_setup(self, target_model: NNCFNetwork) -> SingleConfigQuantizerSetup:
        if self.quantizer_setup_type == QuantizerSetupType.PROPAGATION_BASED:
            setup_generator = PropagationBasedQuantizerSetupGenerator(self.config,
                                                                      target_model,
                                                                      self.hw_config,
                                                                      self._precision_init_type,
                                                                      self._precision_init_params,
                                                                      self._range_init_params,
                                                                      self._debug_interface)
        else:
            setup_generator = PatternBasedQuantizerSetupGenerator(self.config,
                                                                  target_model,
                                                                  self._precision_init_type,
                                                                  self._precision_init_params,
                                                                  self._range_init_params)
        single_config_quantizer_setup = setup_generator.generate_setup()
        self._build_time_metric_infos = setup_generator.get_build_time_metric_infos()
        return single_config_quantizer_setup

    def build_controller(self, target_model: NNCFNetwork) -> PTCompressionAlgorithmController:
        return QuantizationController(target_model,
                                      self.config,
                                      self.should_init,
                                      self._debug_interface,
                                      self._weight_quantizers,
                                      self._non_weight_quantizers,
                                      self._groups_of_adjacent_quantizers,
                                      build_time_metric_info=self._build_time_metric_infos,
                                      build_time_range_init_params=self._range_init_params)

    def __create_quantize_module(self, quantizer_spec: PTQuantizerSpec):
        quantizer_cls = QUANTIZATION_MODULES.get(quantizer_spec.mode)
        return quantizer_cls(quantizer_spec)
    
    def __create_scale_module(self, next_bn, conv):
        class ScaledWeights(nn.Module):
            def __init__(self, bn, conv):
                super().__init__()
                self.bn = bn
                self.do_scaling = False
                self.scale_factor = [torch.ones([self.bn.num_features], device=self.bn.weight.device)]


            def forward(self, weight):
                # W * gamma / sigma            
                if self.do_scaling:
                    running_std = torch.sqrt(self.bn.running_var + self.bn.eps)
                    tmp = self.bn.weight / running_std
                    tmp.to(weight.device)
                    with torch.no_grad():
                        self.scale_factor[0] = torch.clamp(tmp, min=1e-5, max=torch.max(tmp))
                    weights_shape = [1] * len(weight.shape)
                    weights_shape[0] = -1
                    bias_shape = [1] * len(weight.shape)
                    bias_shape[1] = -1
                    scaled_weight = weight * self.scale_factor[0].reshape(weights_shape)
                    return scaled_weight
                else:
                    return weight

        return ScaledWeights(next_bn, conv)

    def _add_single_weight_quantizer(self, target_model: NNCFNetwork, insertion_point: InsertionPoint,
                                     qconfig: QuantizerConfig,
                                     range_init_minmax_values: Tuple[torch.Tensor, torch.Tensor] = None) -> Tuple[
        WeightQuantizerId, InsertionCommand]:
        device = next(target_model.parameters()).device
        quantizer_id = WeightQuantizerId(insertion_point.module_scope)
        module = target_model.get_module_by_scope(insertion_point.module_scope)
        scale_shape = get_scale_shape(module.weight.shape, is_weights=True, per_channel=qconfig.per_channel)
        use_logarithm_scale = self._use_logarithm_scale_per_group[QuantizerGroup.WEIGHTS]
        qspec = PTQuantizerSpec.from_config(qconfig, narrow_range=True,
                                            scale_shape=tuple(scale_shape),
                                            logarithm_scale=use_logarithm_scale)
        quantizer = self.__create_quantize_module(qspec).to(device)
        if range_init_minmax_values is not None:
            quantizer.apply_minmax_init(range_init_minmax_values[0], range_init_minmax_values[1],
                                        log_module_name=str(insertion_point))
        op = UpdateWeight(quantizer).to(device)
        self._weight_quantizers[quantizer_id] = WeightQuantizerInfo(quantizer,
                                                                    target_model.get_module_by_scope(
                                                                        insertion_point.module_scope
                                                                    ))
        command = InsertionCommand(insertion_point, op, OperationPriority.QUANTIZATION_PRIORITY)
        return quantizer_id, command


    def _add_single_scaled_weight_op(self, target_model: NNCFNetwork, insertion_point: InsertionPoint) -> InsertionCommand:
        device = next(target_model.parameters()).device
        module = target_model.get_module_by_scope(insertion_point.module_scope)
        command = None
        if module in target_model.pair_conv_bn:
            scaled_weight_op = self.__create_scale_module(target_model.pair_conv_bn[module], module)
            op = UpdateWeight(scaled_weight_op).to(device)
            command = InsertionCommand(insertion_point, op, OperationPriority.QUANTIZATION_PRIORITY)
        return command

    class ActivationQuantizationHook:
        """Cannot simply register the quantizer module as a callable hook, since we need to call
        a thread-local version of the quantizer module during base module execution."""

        def __init__(self, context: TracingContext, quantizer_storage_key: str,
                     debug_interface: 'QuantizationDebugInterface' = None):
            self.compressed_context = context
            self.quantizer_storage_key = quantizer_storage_key
            self.debug_interface = debug_interface

        def __call__(self, *args, **kwargs):
            if self.debug_interface is not None:
                self.debug_interface.register_activation_quantize_call(str(self.quantizer_storage_key))
            replica = self.compressed_context.base_module_thread_local_replica
            return replica.activation_quantizers[self.quantizer_storage_key](*args, **kwargs)

    def _build_insertion_commands_list_for_quantizer_setup(self,
                                                           quantizer_setup: SingleConfigQuantizerSetup,
                                                           target_model: NNCFNetwork,
                                                           minmax_values_for_range_init: Dict[
                                                               QuantizationPointId, MinMaxTensorStatistic]) -> \
            Tuple[List[InsertionCommand], Dict[QuantizationPointId, QuantizerId]]:
        insertion_commands = []
        qp_id_vs_quant_module_id_dict = {}  # type: Dict[QuantizationPointId, QuantizerId]

        non_unified_scales_quantization_point_ids = set(quantizer_setup.quantization_points.keys())

        for unified_scales_group in quantizer_setup.unified_scale_groups:
            for us_qp_id in unified_scales_group:
                non_unified_scales_quantization_point_ids.discard(us_qp_id)

            quant_module_id, commands = self._build_commands_for_single_unified_scale_group(
                target_model,
                quantizer_setup,
                unified_scales_group,
                minmax_values_for_range_init)
            for us_qp_id in unified_scales_group:
                qp_id_vs_quant_module_id_dict[us_qp_id] = quant_module_id
            insertion_commands += commands

        for qp_id in non_unified_scales_quantization_point_ids:
            qp = quantizer_setup.quantization_points[qp_id]
            ip = qp.insertion_point
            qconfig = quantizer_setup.quantization_points[qp_id].qconfig
            quantizer_module_id = None
            commands = []

            range_init_minmax_values = None
            if minmax_values_for_range_init:
                minmax_stat = minmax_values_for_range_init[qp_id] if qp_id in minmax_values_for_range_init else None
                if minmax_stat is None:
                    nncf_logger.warning("Tensor statistics for location {} were not collected! The corresponding "
                                        "quantizer range will not be initialized!".format(ip))
                else:
                    range_init_minmax_values = (minmax_stat.min_values, minmax_stat.max_values)

            if qp.is_activation_quantization_point():
                quantizer_module_id, commands = self._add_single_activation_quantizer(target_model,
                                                                                      [ip, ],
                                                                                      qconfig,
                                                                                      range_init_minmax_values)                                                     
            elif qp.is_weight_quantization_point():
                commands = []
                command_scaled_weight = self._add_single_scaled_weight_op(target_model, ip)

                quantizer_module_id, command = self._add_single_weight_quantizer(target_model, ip, qconfig,
                                                                                 range_init_minmax_values)

                if command_scaled_weight is not None:
                    command.fn.op.scale_factor = command_scaled_weight.fn.op.scale_factor
                    commands.append(command_scaled_weight)
                commands.append(command)

            qp_id_vs_quant_module_id_dict[qp_id] = quantizer_module_id
            insertion_commands += commands
        return insertion_commands, qp_id_vs_quant_module_id_dict

    def _build_commands_for_single_unified_scale_group(self,
                                                       target_model: NNCFNetwork,
                                                       quantizer_setup: SingleConfigQuantizerSetup,
                                                       unified_scales_group: Set[QuantizationPointId],
                                                       minmax_values_for_range_init: Dict[QuantizationPointId,
                                                                                          MinMaxTensorStatistic]) -> \
            Tuple[QuantizerId, List[InsertionCommand]]:
        qp_ids_list_for_current_group = list(unified_scales_group)

        # The primary insertion point (to be associated with the actual quantizer module, not just hooks to it)
        # will be determined based on the string representation of said insertion point, to avoid random selection
        sorted_qp_ids = sorted(qp_ids_list_for_current_group,
                               key=lambda x: str(quantizer_setup.quantization_points[x].insertion_point))

        # Currently only the unified scales for activation quantizers are supported.
        assert all([quantizer_setup.quantization_points[qp_id].is_activation_quantization_point() for qp_id in
                    sorted_qp_ids])

        primary_qp_id = sorted_qp_ids[0]
        linked_qp_ids = sorted_qp_ids[1:]
        insertion_points = [quantizer_setup.quantization_points[primary_qp_id].insertion_point, ] + \
                           [quantizer_setup.quantization_points[qp_id].insertion_point for qp_id in linked_qp_ids]
        qconfig = quantizer_setup.quantization_points[primary_qp_id].qconfig
        linked_qconfigs = [quantizer_setup.quantization_points[qp_id].qconfig for qp_id in linked_qp_ids]
        for linked_qconfig in linked_qconfigs:
            if not qconfig.compatible_with_a_unified_scale_linked_qconfig(linked_qconfig):
                raise RuntimeError("The quantizer configurations for unified scale quantization points should"
                                   "be identical!")

        range_init_minmax_values = None
        if minmax_values_for_range_init:
            # Hopefully this will suffice.
            # TODO: gather unified statistic by linking stat collectors_and_modules_to_init instead
            min_values = None
            max_values = None
            for qp_id in sorted_qp_ids:
                minmax_stat = minmax_values_for_range_init[qp_id] if qp_id in minmax_values_for_range_init else None
                if minmax_stat is None:
                    nncf_logger.warning("Tensor statistics for location {} were not collected! The corresponding "
                                        "quantizer range will not be initialized!".format(
                        quantizer_setup.quantization_points[qp_id].insertion_point))
                    continue

                if min_values is None:
                    min_values = minmax_stat.min_values
                else:
                    min_values = torch.min(min_values, minmax_stat.min_values)

                if max_values is None:
                    max_values = minmax_stat.max_values
                else:
                    max_values = torch.max(max_values, minmax_stat.max_values)
            if min_values is not None and max_values is not None:
                range_init_minmax_values = min_values, max_values

        quantizer_module_id, commands = self._add_single_activation_quantizer(target_model,
                                                                              insertion_points,
                                                                              qconfig,
                                                                              range_init_minmax_values)
        return quantizer_module_id, commands

    def _select_final_qconfig(self, quantizer_config_list: List[QuantizerConfig]) -> QuantizerConfig:
        # Quantizer config list entries should arrive in the same order as they are listed
        # in the HW config, where they are sorted by descending order of priority
        return quantizer_config_list[0]


    def _add_single_activation_quantizer(self, target_model: NNCFNetwork,
                                         insertion_points: List[InsertionPoint],
                                         qconfig: QuantizerConfig,
                                         range_init_minmax_values: Tuple[torch.Tensor, torch.Tensor] = None) -> \
            Tuple[NonWeightQuantizerId, List[InsertionCommand]]:
        """Will return one or more insertion commands - depending on whether insertion_points has one or
        more entries. The first insertion point in the list will be associated with the actual quantizer
        module, while the rest will still have quantization enabled, but the quantization at these points
        will share the quantizer module with the first."""
        if not insertion_points:
            raise RuntimeError("No insertion points to put an activation quantizer into!")
        primary_ip = insertion_points[0]

        ia_op_exec_context = primary_ip.ia_op_exec_context
        operator_scope_str = str(ia_op_exec_context)
        device = next(target_model.parameters()).device
        use_logarithm_scale = self._use_logarithm_scale_per_group[QuantizerGroup.WEIGHTS]
        input_shape = target_model.get_input_shape_for_insertion_point(primary_ip)
        scale_shape = get_scale_shape(list(input_shape), is_weights=False, per_channel=qconfig.per_channel)
        qspec = PTQuantizerSpec.from_config(qconfig,
                                            narrow_range=False,
                                            scale_shape=tuple(scale_shape),
                                            logarithm_scale=use_logarithm_scale)
        quantizer = self.__create_quantize_module(qspec).to(device)
        if range_init_minmax_values is not None:
            quantizer.apply_minmax_init(min_values=range_init_minmax_values[0],
                                        max_values=range_init_minmax_values[1],
                                        log_module_name=str(primary_ip))

        qids = [NonWeightQuantizerId(ip.ia_op_exec_context, ip.input_port_id) for ip in insertion_points]
        serialized_insertions_list = [str(x) for x in qids]
        quantizer_storage_key = ";".join(serialized_insertions_list)

        assert quantizer_storage_key not in target_model.get_compression_modules_by_type(
            ExtraCompressionModuleType.ACTIVATION_QUANTIZER)

        target_model.add_compression_module(quantizer_storage_key, quantizer,
                                            ExtraCompressionModuleType.ACTIVATION_QUANTIZER)

        quantizer_id = NonWeightQuantizerId(ia_op_exec_context, primary_ip.input_port_id)

        if len(insertion_points) > 1:
            nncf_logger.info(
                "Processing linked activation quantizer group:\n {}\n".format("\n".join(serialized_insertions_list)))

        self._non_weight_quantizers[quantizer_id] = \
            NonWeightQuantizerInfo(quantizer, insertion_points)

        insertion_commands = []
        for curr_insertion_point in insertion_points:
            if curr_insertion_point in self._processed_activation_quantizer_insertion_points:
                raise RuntimeError(
                    "Ambiguous call to {fn} with call order {co} in current scope. "
                    "Cannot insert quantization hooks "
                    "automatically!".format(fn=ia_op_exec_context.operator_name, co=ia_op_exec_context.call_order)
                )
            self._processed_activation_quantizer_insertion_points.add(curr_insertion_point)

            nncf_logger.info("Adding {}{} Activation Quantize in scope: {}".format(
                "signed" if quantizer.signed else "unsigned",
                " logarithm_scale" if quantizer.is_using_log_scale_storage else "",
                operator_scope_str
            ))

            # Hooks will be identical for each affected ia_op_exec_context in the linked scenario
            # - will call one and the same quantizer
            hook = self.ActivationQuantizationHook(target_model.get_tracing_context(),
                                                   quantizer_storage_key,
                                                   self._debug_interface)

            insertion_commands.append(
                InsertionCommand(curr_insertion_point, hook, OperationPriority.QUANTIZATION_PRIORITY))
        return quantizer_id, insertion_commands

    def _are_frozen_layers_allowed(self) -> Tuple[bool, str]:
        message_template = Template('Frozen layers are$denial allowed for $algo_prefix quantization')
        bits = set()
        bits.update({wq.quantizer_module_ref.num_bits for wq in self._weight_quantizers.values()})
        bits.update({nwq.quantizer_module_ref.num_bits for nwq in self._non_weight_quantizers.values()})

        if self._precision_init_params or len(bits) > 1:
            return False, message_template.substitute(denial=' not', algo_prefix='mixed precision')

        if len(bits) == 1:
            bitwidth = bits.pop()
            algo_prefix = f'INT{bitwidth}'
            if bitwidth == 8:
                return True, message_template.substitute(denial='', algo_prefix=algo_prefix)
            return False, message_template.substitute(denial=' not', algo_prefix=algo_prefix)
        return True, message_template.substitute(denial='', algo_name='empty')


class QuantizationControllerBase(PTCompressionAlgorithmController):
    def enable_activation_quantization(self):
        raise NotImplementedError

    def enable_weight_quantization(self):
        raise NotImplementedError

    def disable_activation_quantization(self):
        raise NotImplementedError

    def disable_weight_quantization(self):
        raise NotImplementedError

    def init_range(self):
        raise NotImplementedError


class QuantizationController(QuantizationControllerBase):
    def __init__(self, target_model: NNCFNetwork,
                 quantization_config: 'NNCFConfig',
                 should_init: bool,
                 debug_interface: 'QuantizationDebugInterface',
                 weight_quantizers: Dict[WeightQuantizerId, WeightQuantizerInfo],
                 non_weight_quantizers: Dict[NonWeightQuantizerId, NonWeightQuantizerInfo],
                 groups_of_adjacent_quantizers: GroupsOfAdjacentQuantizers,
                 collect_compression_metrics: bool = True,
                 build_time_metric_info: NetworkQuantizationShareMetricBuildTimeInfo = None,
                 build_time_range_init_params: RangeInitParams = None):
        super().__init__(target_model)
        self.debug_interface = debug_interface
        self.quantization_config = quantization_config
        self._collect_compression_metrics = collect_compression_metrics
        self._build_time_range_init_params = build_time_range_init_params

        self.weight_quantizers = weight_quantizers  # type: Dict[WeightQuantizerId, WeightQuantizerInfo]
        self.non_weight_quantizers = non_weight_quantizers  # type: Dict[NonWeightQuantizerId, NonWeightQuantizerInfo]
        self.all_quantizations = OrderedDict()  # type: Dict[QuantizerId, BaseQuantizer]
        self.all_quantizations.update({k: v.quantizer_module_ref for k, v in self.weight_quantizers.items()})
        self.all_quantizations.update({k: v.quantizer_module_ref for k, v in self.non_weight_quantizers.items()})
        self._distributed = False
        self._groups_of_adjacent_quantizers = groups_of_adjacent_quantizers

        should_export_to_onnx_qdq = quantization_config.get("export_to_onnx_standard_ops",
                                                            False)
        if should_export_to_onnx_qdq:
            export_mode = QuantizerExportMode.ONNX_QUANTIZE_DEQUANTIZE_PAIRS
        else:
            export_mode = QuantizerExportMode.FAKE_QUANTIZE

        for quantizer in self.all_quantizations.values():  # type: BaseQuantizer
            quantizer.set_export_mode(export_mode)

        if self._collect_compression_metrics:
            self.metric_store = {}
            quantizer_setup_type = self.quantization_config.get('quantizer_setup_type')
            # These metrics are collected here and are updated when the method .statistics() is called
            self.non_stable_metric_collectors = [NetworkQuantizationShareMetric(target_model, self.weight_quantizers, \
                                                                                self.non_weight_quantizers,
                                                                                quantizer_setup_type,
                                                                                build_time_metric_info),
                                                 MemoryCostMetric(target_model, self.weight_quantizers,
                                                                  self.non_weight_quantizers)]
            # These metrics are collected once here and are not updated when the method .statistics() is called
            self.stable_metric_collectors = [ShareEdgesQuantizedDataPath(target_model)]
            self.update_metric_store(True)

        params = quantization_config.get('params', {})

        self.is_staged_scheduler = bool(params)

        if is_main_process() and should_init:
            self.run_batchnorm_adaptation(self.quantization_config)
        
        # Staged scheduler must be created after initialized to prevent extra logic with disabled quantizations

        scheduler_params = quantization_config.get('scheduler_params')
        if self.is_staged_scheduler:
            if scheduler_params is not None:
                params.update(scheduler_params)
            scheduler_cls = QUANTIZATION_SCHEDULERS.get("staged")
            self._scheduler = scheduler_cls(self, params)
        elif scheduler_params is not None:
            scheduler_cls = QUANTIZATION_SCHEDULERS.get("base")
            self._scheduler = scheduler_cls(self, scheduler_params)

    @property
    def groups_of_adjacent_quantizers(self) -> GroupsOfAdjacentQuantizers:
        return self._groups_of_adjacent_quantizers

    def do_folding_conv_bn(self):
        for conv in self._model.pair_conv_bn.keys():
            conv.folding_conv_bn = True
            conv.pre_ops['0'].op.do_scaling = True

    def freeze_bn_stats(self):
        for bn in self._model.pair_conv_bn.values():
            bn.training = False

    def prepare_for_export(self):
        for quantizer_id, quantizer in self.all_quantizations.items():
            if not quantizer.is_enabled_quantization():
                nncf_logger.warning('Disabled quantization on export to ONNX: {}'.format(quantizer_id))

        # remove ScaledWeights module
        for conv, bn in self._model.pair_conv_bn.items():
            scaled_wights_op = conv.remove_pre_forward_operation('0')
            self._fusing_conv2d_and_bn2d(conv, bn)
        self._replace_bn_identity()

    def _replace_bn_identity(self):
        def recursively(model):
            for module_name in model._modules:
                if isinstance(model._modules[module_name], torch.nn.BatchNorm2d):
                    model._modules[module_name] = torch.nn.Identity()
                if len(model._modules[module_name]._modules) > 0:
                    recursively(model._modules[module_name])

        recursively(self._model)


    def _fusing_conv2d_and_bn2d(self, conv, bn):
        # update weight and bias convolution
          w = conv.weight
          b = conv.bias
          gamma = bn.weight
          sigma = torch.sqrt(bn.running_var + bn.eps)
          mu = bn.running_mean
          betta = bn.bias
          scale_factor = gamma / sigma
          scale_factor = torch.clamp(scale_factor, min=1e-5, max=torch.max(scale_factor).data)

          weights_shape = [1] * len(w.shape)
          weights_shape[0] = -1
          w_folded = w * scale_factor.reshape(weights_shape)
          b_folded = - mu * scale_factor + betta
          if b is not None:
              b_folded += b

          conv.weight = torch.nn.Parameter(w_folded)
          conv.bias = torch.nn.Parameter(b_folded)
          return conv



    def update_metric_store(self, do_all: bool = False):
        for collector in self.non_stable_metric_collectors:
            collector.collect()
            self.metric_store[collector.NAME_STR] = collector.get_metric_table()
        if do_all:
            for collector in self.stable_metric_collectors:
                collector.collect()
                self.metric_store[collector.NAME_STR] = collector.get_metric_table()

    def distributed(self):
        self._distributed = True
        self._broadcast_initialized_params_for_each_quantizer()

    def _broadcast_initialized_params_for_each_quantizer(self):
        # NOTE: Order of quantization modules must be the same on GPUs to correctly broadcast num_bits
        sorted_quantizers = OrderedDict(sorted(self.all_quantizations.items(), key=lambda x: str(x[0])))
        for quantizer in sorted_quantizers.values():  # type: BaseQuantizer
            quantizer.broadcast_initialized_params()

    def _do_runtime_range_init(self, range_init_params: RangeInitParams):
        modules_to_init = OrderedDict()
        for wq_id, wq_info in self.weight_quantizers.items():
            scope_str = str(wq_id)
            group = QuantizerGroup.WEIGHTS
            init_config = range_init_params.get_init_config_for_scope_and_group(scope_str, group)
            modules_to_init[scope_str] = (wq_info.quantizer_module_ref, init_config)

        for aq_id, aq_info in self.non_weight_quantizers.items():
            scope_str = str(aq_id)
            group = QuantizerGroup.ACTIVATIONS
            init_config = range_init_params.get_init_config_for_scope_and_group(scope_str, group)
            modules_to_init[scope_str] = (aq_info.quantizer_module_ref, init_config)

        # NOTE: Order of modules must be the same to correctly broadcast parameters (e.g. input_low
        # and input_range)
        modules_to_init = OrderedDict(sorted(modules_to_init.items()))
        self.modules_to_range_init = modules_to_init
        runner = DataLoaderRangeInitializeRunner(self._model, modules_to_init, range_init_params.device)

        quantizers = [module for module, config in modules_to_init.values()]
        quantizers_switcher = QuantizersSwitcher(quantizers)
        # bypass quantization to collect statistics from floating point model
        quantizers_switcher.disable_quantizers()
        runner.run(range_init_params.init_range_data_loader,
                   range_init_params.get_max_num_init_steps())
        quantizers_switcher.enable_quantizers()

        self._model.rebuild_graph()

    def compression_level(self) -> CompressionLevel:
        if self.is_staged_scheduler:
            return self.scheduler.compression_level()
        return CompressionLevel.FULL

    def init_precision(self,
                       precision_init_type: str,
                       precision_init_params: BasePrecisionInitParams,
                       precision_constraints: HardwareQuantizationConstraints) -> SingleConfigQuantizerSetup:
        """
        Precision initialization happens based on an measure of layer sensitivity to perturbations. The measure is
        calculated by average Hessian trace estimation for each layer using Hutchinson algorithm.
        """
        init_impl = PrecisionInitializerFactory.create(precision_init_type)
        initializer = init_impl(self, precision_init_params, precision_constraints)
        nncf_logger.info("Initialization of quantization precisions")
        return initializer.apply_init()

    def init_range(self, range_init_params: RangeInitParams = None):
        """
        Tracks input statistics for quantizers in the model and sets ranges of the quantizers to correspond to
        minimum and maximum input tensor levels observed.
        :param range_init_params: specifies parameters for this range initialization call; if None, the parameters
        that were used during compressed model creation will be used.
        """
        if range_init_params is None:
            if self._build_time_range_init_params is None:
                nncf_logger.warning("Requested a quantization controller to do range initialization without params, but"
                                    " the build time range initialization was not supplied with params as well - range "
                                    "initialization will not be done")
                return
            range_init_params = self._build_time_range_init_params

        self._do_runtime_range_init(range_init_params)

        if self._distributed:
            self._broadcast_initialized_params_for_each_quantizer()

    def update_range_config_by_default(self, init_range_config: Dict):
        global_init_range_config = dict()
        global_init_range_config.update(init_range_config)
        if global_init_range_config.get("type") is None:
            global_init_range_config["type"] = "mean_min_max"

        if global_init_range_config.get("num_init_samples") is None:
            global_init_range_config["num_init_samples"] = 256

        num_init_samples = global_init_range_config.get('num_init_samples', 256)
        if num_init_samples < 0:
            raise AttributeError('Number of initialization samples must be >= 0')
        return global_init_range_config

    def get_weights_activation_quantizers_pairs(self) -> List[Tuple[List[WeightQuantizerId], NonWeightQuantizerId]]:
        """
        finds all neighbour weight and input activation quantizers that share the same module (e.g. conv or linear).
        Single activation quantizer can be in pair with multiple neighbour weight quantizers, e.g. like in SqueezeNet,
        when two Convolutions share the same input activation.
        :return: list of pairs - (list of weight quantizers, activation quantizer)
        """
        pairs = []

        nncf_network = self._model
        nncf_graph = nncf_network.get_original_graph()
        non_weight_quantizers = {key: quantizer_info.quantizer_module_ref for key, quantizer_info \
                                 in self.non_weight_quantizers.items() if not isinstance(key, InputQuantizerId)}

        def traverse_graph(curr_nx_node_key: str, weight_quantizers: List[nn.Module]) -> Optional[List[nn.Module]]:
            nx_node = nncf_graph.get_nx_node_by_key(curr_nx_node_key)
            module_scope = nx_node[NNCFGraph.OP_EXEC_CONTEXT_NODE_ATTR].scope_in_model
            module = nncf_network.get_module_by_scope(module_scope)
            if is_nncf_module(module):
                if hasattr(module, 'pre_ops'):
                    for ops in module.pre_ops.values():
                        if isinstance(ops, UpdateWeight):
                            weight_quantizers.append(ops.op)
            else:
                for succ_nx_node_key in nncf_graph.get_successors(curr_nx_node_key):
                    return traverse_graph(succ_nx_node_key, weight_quantizers)
            return weight_quantizers

        for activation_quantizer_id in sorted(non_weight_quantizers, key=str):
            activation_ctx = activation_quantizer_id.ia_op_exec_context
            post_hooked_nx_node_key = nncf_graph.get_node_key_by_iap_context(activation_ctx)
            weight_quantizers = []
            weight_quantizer_ids = []
            for next_nx_node_key in nncf_graph.get_successors(post_hooked_nx_node_key):
                weight_quantizers = traverse_graph(next_nx_node_key, weight_quantizers)

            for wt_quant_module in weight_quantizers:
                for other_wt_quant_id, other_wt_quant_module_info in self.weight_quantizers.items():
                    if other_wt_quant_module_info.quantizer_module_ref is wt_quant_module:
                        weight_quantizer_ids.append(other_wt_quant_id)
                        break
                else:
                    raise RuntimeError("Weight quantizer module obtained during graph traversal not found among "
                                       "weight quantizer module references available in quantization controller!")

            if weight_quantizer_ids:
                pairs.append((weight_quantizer_ids, activation_quantizer_id))
        return pairs

    def enable_activation_quantization(self):
        for m in self.non_weight_quantizers.values():
            m.quantizer_module_ref.enable_quantization()

    def enable_weight_quantization(self):
        for m in self.weight_quantizers.values():
            m.quantizer_module_ref.enable_quantization()

    def disable_activation_quantization(self):
        for m in self.non_weight_quantizers.values():
            m.quantizer_module_ref.disable_quantization()

    def disable_weight_quantization(self):
        for m in self.weight_quantizers.values():
            m.quantizer_module_ref.disable_quantization()

    def _get_local_init_range_config(self, scope: Scope, scope_overrides: Dict[str, Dict],
                                     global_init_range_config: Dict, quantizer_group: str):
        if isinstance(global_init_range_config, dict):
            module_init_range_config = global_init_range_config
        else:
            module_init_range_config = None
            matched_init_range_config = []
            for range_init_subconfig in global_init_range_config:
                target_scopes = range_init_subconfig.get("target_scopes", None)
                ignored_scopes = range_init_subconfig.get("ignored_scopes", None)
                target_quantizer_group = range_init_subconfig.get("target_quantizer_group", quantizer_group)
                if quantizer_group == target_quantizer_group and\
                     should_consider_scope(str(scope), target_scopes, ignored_scopes):
                    matched_init_range_config.append(range_init_subconfig)

            if len(matched_init_range_config) > 1:
                raise AssertionError("The range initialization configs conflict with each other. "
                                     "Conflicting configs: {} for scope {}.".format(matched_init_range_config,
                                                                                    str(scope)))


            if len(matched_init_range_config) == 1:
                module_init_range_config = matched_init_range_config[0]
            else:
                raise AssertionError("The range initialization configs conflict with each other. "
                                     "Conflicting configs: {} for scope {}.".format(matched_init_range_config,
                                                                                    str(scope)))

        for overridden_scope in scope_overrides.keys():
            if in_scope_list(str(scope), overridden_scope):
                override_config = scope_overrides[overridden_scope].get('initializer', {}).get("range")
                if override_config is not None:
                    module_init_range_config = override_config

        if module_init_range_config is None:
            module_init_range_config = self.update_range_config_by_default({})

        return module_init_range_config

    def statistics(self, quickly_collected_only=False):
        stats = super().statistics()
        num_enabled_quantization = len([1 for q in self.all_quantizations.values() if q.is_enabled_quantization()])
        multiplier = 100 / len(self.all_quantizations)
        stats["ratio_of_enabled_quantizations"] = num_enabled_quantization * multiplier
        if self._collect_compression_metrics and not quickly_collected_only:
            self.update_metric_store()
            for metric in self.metric_store.values():
                for add_info, table in metric.items():
                    stats[add_info] = table
        return stats


class QuantizationDebugInterface(DebugInterface):
    QUANTIZERS_IN_NNCF_MODULES_TRACKER_NAME = 'quantized_modules'
    ACTIVATION_QUANTIZERS_TRACKER_NAME = 'activation_quantizers'

    def __init__(self):
        self.call_trackers = {
            self.QUANTIZERS_IN_NNCF_MODULES_TRACKER_NAME: CallCountTracker(
                QuantizationDebugInterface.QUANTIZERS_IN_NNCF_MODULES_TRACKER_NAME),
            self.ACTIVATION_QUANTIZERS_TRACKER_NAME: CallCountTracker(
                QuantizationDebugInterface.ACTIVATION_QUANTIZERS_TRACKER_NAME),
        }
        self.graph_size = 0

        from nncf.debug import DEBUG_LOG_DIR
        self.dump_dir = Path(DEBUG_LOG_DIR) / Path("debug_dumps")
        self.dump_dir.mkdir(parents=True, exist_ok=True)
        self.scale_dump_dir = self.dump_dir / Path("scale")
        self.prop_graph_dump_dir = self.dump_dir / Path("quant_prop")
        if self.prop_graph_dump_dir.exists():
            shutil.rmtree(str(self.prop_graph_dump_dir))
        self.forward_call_count = 0
        self._strict_forward = False

    def init_actual(self, owner_model: NNCFNetwork):
        quantization_types = [class_type.__name__ for class_type in QUANTIZATION_MODULES.registry_dict.values()]
        quantizers_in_nncf_modules = owner_model.get_modules_in_nncf_modules_by_type(quantization_types)
        nncf_module_quantizations_id_list = [str(scope) for scope in
                                             quantizers_in_nncf_modules.keys()]  # type: List[str]

        activation_quantizer_id_list = owner_model.get_compression_modules_by_type(
            ExtraCompressionModuleType.ACTIVATION_QUANTIZER).keys()  # type: List[str]
        self.call_trackers[self.QUANTIZERS_IN_NNCF_MODULES_TRACKER_NAME].init_with_key_list(
            nncf_module_quantizations_id_list)
        self.call_trackers[self.ACTIVATION_QUANTIZERS_TRACKER_NAME].init_with_key_list(
            activation_quantizer_id_list)
        if self.scale_dump_dir.exists():
            shutil.rmtree(str(self.scale_dump_dir))
        self.scale_dump_dir.mkdir(parents=True, exist_ok=True)
        self._strict_forward = True

    def pre_forward_actions(self, module: 'NNCFNetwork'):
        self.reset_counters()

    def post_forward_actions(self, module: 'NNCFNetwork'):
        self.register_forward_call()
        # pylint:disable=protected-access
        ctx = module.get_tracing_context()
        self.set_graph_size(ctx.graph.get_nodes_count())

        quantization_types = [class_type.__name__ for class_type in QUANTIZATION_MODULES.registry_dict.values()]
        nncf_module_quantizations = module.get_modules_in_nncf_modules_by_type(
            quantization_types)  # type: Dict['Scope', nn.Module]

        for qm_scope, qm_module in nncf_module_quantizations.items():
            # Important - this will not work for DataParallel since it copies the
            # entire parent module for each thread and the `call_count` attributes
            # are incremented for thread local copies of `qm_module`, which are not
            # the same as the primary copies of `qm_module` iterated over at this point
            self.register_quantizer_module_call(str(qm_scope), qm_module.call_count)
            self.dump_scale(qm_module.get_trainable_params(), str(qm_scope))
            qm_module.reset_call_counter()
        self.print_call_stats()

        call_dict = ctx.get_node_call_counter_dict()
        total_calls = sum(call_dict.values())
        nncf_logger.debug("{} nodes called out of total {}".format(total_calls,
                                                                   ctx.graph.get_nodes_count()))
        if self._strict_forward:
            for tracker in self.call_trackers.values():
                if tracker.get_never_called_keys():
                    # This will always trigger for DataParallel - disregard or disable debug mode
                    # for DataParallel runs
                    raise RuntimeError("{} has never called modules: {}!".format(
                        tracker.name, tracker.get_never_called_keys()))

    def dump_scale(self, quantizer_scale_params: Dict[str, torch.Tensor], quantizer_name: str):
        import re
        quantizer_normalized_name = re.sub(r'[^\w\-_\. ]', '_', quantizer_name)
        for scale_param_name, scale_param in quantizer_scale_params.items():
            fname = "{}_{}.txt".format(quantizer_normalized_name, scale_param_name)
            with safe_open(self.scale_dump_dir / fname, "ba") as file:
                np.savetxt(file, scale_param.cpu().numpy().flatten())

    def reset_counters(self):
        for tracker in self.call_trackers.values():
            tracker.reset()

    def register_quantizer_module_call(self, key, counts=None):
        self.call_trackers[self.QUANTIZERS_IN_NNCF_MODULES_TRACKER_NAME].register_call(key, counts)

    def register_activation_quantize_call(self, key: str):
        self.call_trackers[self.ACTIVATION_QUANTIZERS_TRACKER_NAME].register_call(key)

    def print_call_stats(self):
        nncf_logger.debug(" Graph size: {} nodes".format(self.graph_size))
        for tracker in self.call_trackers.values():
            msg = " {} tracker:".format(tracker.name)
            msg += " {} total calls;".format(tracker.get_total_call_count())

            never_called = tracker.get_never_called_keys()
            if never_called:
                msg += " {} entries never called;".format(len(never_called))

            overcalled = tracker.get_overcalled_keys_with_call_counts()
            if overcalled:
                msg += " {} entries called more than once;".format(len(overcalled))
            nncf_logger.debug(msg)

    def set_graph_size(self, new_size):
        if new_size != self.graph_size:
            nncf_logger.debug('\n')
            nncf_logger.debug(
                " warning - graph size has changed from {} to {} since last forward".format(self.graph_size,
                                                                                            new_size))
        self.graph_size = new_size

    def register_forward_call(self):
        self.forward_call_count += 1

    def visualize_quantizer_propagation(self,
                                        prop_solver: QuantizerPropagationSolver,
                                        prop_graph: QuantizerPropagationStateGraph,
                                        iteration: str):
        self.prop_graph_dump_dir.mkdir(parents=True, exist_ok=True)
        fname = "quant_prop_iter_{}.dot".format(iteration)
        prop_solver.debug_visualize(prop_graph,
                                    self.prop_graph_dump_dir / Path(fname))

    def visualize_insertion_point_graph(self, insertion_point_graph: InsertionPointGraph):
        out_graph = nx.MultiDiGraph()
        for node_key, node in insertion_point_graph.nodes.items():
            if node[InsertionPointGraph.NODE_TYPE_NODE_ATTR] == InsertionPointGraphNodeType.INSERTION_POINT:
                insertion_point_data = node[InsertionPointGraph.INSERTION_POINT_DATA_NODE_ATTR]  # type: InsertionPoint
                label = "IP: {}".format(insertion_point_data.insertion_type)
                if insertion_point_data.input_port_id is not None:
                    label += " port " + str(insertion_point_data.input_port_id)
                out_graph.add_node(node_key, label=label, color="red")
            elif node[InsertionPointGraph.NODE_TYPE_NODE_ATTR] == InsertionPointGraphNodeType.OPERATOR:
                out_graph.add_node(node_key)
            else:
                raise RuntimeError("Invalid InsertionPointGraph node!")
        for u, v in insertion_point_graph.edges:
            out_graph.add_edge(u, v)

        for node_key, node in insertion_point_graph.nodes.items():
            if node[InsertionPointGraph.NODE_TYPE_NODE_ATTR] == InsertionPointGraphNodeType.OPERATOR:
                for ip_node_key in node[InsertionPointGraph.ASSOCIATED_IP_NODE_KEYS_NODE_ATTR]:
                    out_graph.add_edge(node_key, ip_node_key, style="dashed", headport='e', tailport='e')

        nx.drawing.nx_pydot.write_dot(out_graph, self.dump_dir / Path("insertion_point_graph.dot"))


class ExperimentalQuantizationBuilder(QuantizationBuilder):
    def __init__(self, quantizer_setup: SingleConfigQuantizerSetup,
                 tensor_stats_for_all_setup_variations: Dict[InsertionPoint, Dict[ReductionShape, TensorStatistic]]):
        should_init = bool(tensor_stats_for_all_setup_variations)
        super().__init__(NNCFConfig(), should_init=should_init)
        self._quantizer_setup = quantizer_setup
        self._tensor_stats = tensor_stats_for_all_setup_variations

    def _handle_frozen_layers(self):
        pass

    def _get_quantizer_setup(self, target_model: NNCFNetwork) -> SingleConfigQuantizerSetup:
        return self._quantizer_setup

    def _get_statistics_for_final_range_init(self,
                                             target_model: NNCFNetwork,
                                             quantizer_setup: QuantizerSetupBase,
                                             range_init_params: RangeInitParams) -> Dict[
        InsertionPoint, Dict[ReductionShape, TensorStatistic]]:
        return self._tensor_stats

    def build_controller(self, target_model: NNCFNetwork) -> PTCompressionAlgorithmController:
        groups_of_adjacent_quantizers = GroupsOfAdjacentQuantizers()
        all_quantizations = {}  # type: Dict[QuantizerId, BaseQuantizer]
        all_quantizations.update({k: v.quantizer_module_ref for k, v in self._weight_quantizers.items()})
        all_quantizations.update({k: v.quantizer_module_ref for k, v in self._non_weight_quantizers.items()})

        groups_of_adjacent_quantizers.parse_from_quantizer_setup(all_quantizations,
                                                                 self._quantizer_setup,
                                                                 self._setup_to_module_id_translation_dict)

        build_time_metric_infos = NetworkQuantizationShareMetricBuildTimeInfo(len(self._non_weight_quantizers),
                                                                              len(self._weight_quantizers))

        return ExperimentalQuantizationController(target_model,
                                                  self._weight_quantizers,
                                                  self._non_weight_quantizers,
                                                  groups_of_adjacent_quantizers,
                                                  self._quantizer_setup,
                                                  self._setup_to_module_id_translation_dict,
                                                  self._tensor_stats,
                                                  build_time_metric_infos)


class ExperimentalQuantizationController(QuantizationController):
    def __init__(self, target_model: NNCFNetwork,
                 weight_quantizers: Dict[WeightQuantizerId, WeightQuantizerInfo],
                 non_weight_quantizers: Dict[NonWeightQuantizerId, NonWeightQuantizerInfo],
                 groups_of_adjacent_quantizers: GroupsOfAdjacentQuantizers,
                 initial_quantizer_setup: SingleConfigQuantizerSetup,
                 setup_to_module_id_translation_dict: Dict[QuantizationPointId, QuantizerId],
                 tensor_stats: Dict[InsertionPoint, Dict[ReductionShape, TensorStatistic]],
                 build_time_metric_info: NetworkQuantizationShareMetricBuildTimeInfo):
        super().__init__(target_model,
                         NNCFConfig(),
                         should_init=False,
                         debug_interface=None,
                         weight_quantizers=weight_quantizers,
                         non_weight_quantizers=non_weight_quantizers,
                         groups_of_adjacent_quantizers=groups_of_adjacent_quantizers,
                         collect_compression_metrics=True,
                         build_time_metric_info=build_time_metric_info)
        self._target_model_ref = target_model
        self._initial_quantizer_setup = initial_quantizer_setup
        self._tensor_stats = tensor_stats
        self.setup_to_module_id_translation_dict = setup_to_module_id_translation_dict
        self.module_id_to_qp_id_translation_dict = {}  # type: Dict[QuantizerId, Set[QuantizationPointId]]
        for qp_id, qid in self.setup_to_module_id_translation_dict.items():
            if qid in self.module_id_to_qp_id_translation_dict:
                self.module_id_to_qp_id_translation_dict[qid].add(qp_id)
            else:
                self.module_id_to_qp_id_translation_dict[qid] = {qp_id}

    def get_quantizer_setup_for_current_state(self) -> SingleConfigQuantizerSetup:
        retval = SingleConfigQuantizerSetup()
        retval.shared_input_operation_set_groups = self._initial_quantizer_setup.shared_input_operation_set_groups
        retval.unified_scale_groups = self._initial_quantizer_setup.unified_scale_groups
        for qp_id, qp in self._initial_quantizer_setup.quantization_points.items():
            quant_module_id = self.setup_to_module_id_translation_dict[qp_id]
            quant_module = self.all_quantizations[quant_module_id]
            qconfig = quant_module.get_quantizer_config()
            new_qp = SingleConfigQuantizationPoint(qp.insertion_point, qconfig)
            retval.quantization_points[qp_id] = new_qp
        return retval

    def is_new_setup_requires_regeneration(self, quantizer_setup: SingleConfigQuantizerSetup) -> bool:
        current_setup = self.get_quantizer_setup_for_current_state()
        if Counter(current_setup.quantization_points.keys()) != Counter(quantizer_setup.quantization_points.keys()):
            raise ValueError("The new setup is inconsistent with the original parameter space!")
        for qp_id in quantizer_setup.quantization_points:
            current_qconfig = current_setup.quantization_points[qp_id].qconfig
            new_qconfig = quantizer_setup.quantization_points[qp_id].qconfig
            if current_qconfig.per_channel != new_qconfig.per_channel or \
                    (new_qconfig.signedness_to_force is not None and
                     current_qconfig.signedness_to_force != new_qconfig.signedness_to_force) or \
                    current_qconfig.mode != new_qconfig.mode:
                return True
        return False

    def apply_new_quantizer_setup(self, quantizer_setup: SingleConfigQuantizerSetup) -> Tuple[
            'ExperimentalQuantizationController', NNCFNetwork]:
        if not self.is_new_setup_requires_regeneration(quantizer_setup):
            for qp_id, qp in quantizer_setup.quantization_points.items():
                quant_module_id = self.setup_to_module_id_translation_dict[qp_id]
                quant_module = self.all_quantizations[quant_module_id]
                quant_module.num_bits = qp.qconfig.num_bits
            return self, self._target_model_ref
        new_model = self._target_model_ref.get_clean_shallow_copy()
        new_builder = ExperimentalQuantizationBuilder(quantizer_setup, self._tensor_stats)
        new_builder.apply_to(new_model)
        new_ctrl = new_model.commit_compression_changes()  # type: ExperimentalQuantizationController
        return new_ctrl, new_model
