Configurations
=============================

We illustrate configurations for quickstart experiments in this page.
Each type of experiment (e.g., SFT, PPO) corresponds to a specific 
configuration class (e.g., :class:`realrlhf.SFTConfig` for SFT).

Since ReaL uses `Hydra <https://hydra.cc/>`_ for configuration management,
users can override these options provided by the class recursively
with command line arguments.
Please check :doc:`quickstart` for concrete examples.

.. currentmodule:: realrlhf

Experiment Configurations
--------------------------

.. autoclass:: CommonExperimentConfig

.. autoclass:: SFTConfig

.. autoclass:: RWConfig

.. autoclass:: DPOConfig

.. autoclass:: PPOHyperparameters

.. autoclass:: PPOConfig

Model Configurations
---------------------

.. autoclass:: ModelTrainEvalConfig

.. autoclass:: OptimizerConfig

.. autoclass:: ParallelismConfig

.. autoclass:: AllocationConfig

.. autoclass:: realrlhf.ReaLModelConfig

.. autoclass:: realrlhf.impl.model.nn.real_llm_api.ReaLModel
    :members:
    :undoc-members:
    :exclude-members: forward, state_dict, load_state_dict, build_reparallelization_plan, build_reparallelized_layers_async, patch_reparallelization, pre_process, post_process, share_embeddings_and_output_weights

Dataset Configurations
-----------------------

.. autoclass:: PromptAnswerDatasetConfig

.. autoclass:: PairedComparisonDatasetConfig

.. autoclass:: PromptOnlyDatasetConfig

``NamedArray``
-----------------------

``NamedArray``` is an object we use in model function calls.
It is inherited from the previous SRL project.

Named array extends plain arrays/tensors in the following ways.

1. NamedArray aggregates multiple arrays, possibly of different shapes.
2. Each array is given a name, providing a user-friendly way of indexing to the corresponding data.
3. NamedArrays can be nested. (Although it should *not* be nested in this system.)
4. NamedArray can store metadata such as sequence length, which is useful for padding and masking without causing CUDA synchronization.

Users can regard it as a nested dictionary of arrays, except that indexing a ``NamedArray`` results in *slicing every hosted arrays* (again, we don't use this feature in this project).

.. autoclass:: realrlhf.base.namedarray.NamedArray
    :members:

.. autofunction::realrlhf.base.namedarray.from_dict

.. autofunction::realrlhf.base.namedarray.recursive_aggregate

.. autofunction::realrlhf.base.namedarray.recursive_apply

Dataflow Graph
-----------------

.. autoclass:: realrlhf.MFCDef