"""
 Copyright (c) 2020 Intel Corporation
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

from nncf.tensorflow.algorithm_selector import get_compression_algorithm_builder
from nncf.tensorflow.api.composite_compression import TFCompositeCompressionAlgorithmBuilder
from nncf.tensorflow.helpers.utils import get_built_model
from nncf.config.config import NNCFConfig


def create_compression_algorithm_builder(config):
    compression_config = config.get('compression', {})

    if isinstance(compression_config, dict):
        compression_config = NNCFConfig(compression_config)
        compression_config.register_extra_structs(config.get_all_extra_structs_for_copy())
        compression_config['target_device'] = config.get('target_device', 'ANY')
        return get_compression_algorithm_builder(compression_config)(compression_config)
    if isinstance(compression_config, list):
        composite_builder = TFCompositeCompressionAlgorithmBuilder()
        for algo_config in compression_config:
            algo_config = NNCFConfig(algo_config)
            algo_config.register_extra_structs(config.get_all_extra_structs_for_copy())
            algo_config['target_device'] = config.get('target_device', 'ANY')
            composite_builder.add(get_compression_algorithm_builder(algo_config)(algo_config))
        return composite_builder
    return None


def create_compressed_model(model, config):
    model = get_built_model(model, config)

    builder = create_compression_algorithm_builder(config)
    if builder is None:
        return None, model
    compressed_model = builder.apply_to(model)
    compression_ctrl = builder.build_controller(compressed_model)
    return compression_ctrl, compressed_model