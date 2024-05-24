from typing import List
import copy
import dataclasses
import functools

from reallm.api.core.dfg import ModelFamily, ModelInterface, ModelInterfaceType, ModelName, ModelRPC
from reallm.api.core.system_api import _LLM_ENVVARS, ExperimentSaveEvalControl, register_experiment
from reallm.api.quickstart.dataset import DatasetType, PromptOnlyDatasetConfig
from reallm.api.quickstart.device_mesh import ClusterDeviceMesh, RPCAllocation
from reallm.base.topology import PipeModelDataParallelTopology
from reallm.experiments.common.ppo_exp import PPOHyperparameters
import reallm.base.logging as logging

from .device_mapping import auto_device_mapping as auto

logger = logging.getLogger("Auto PPO exp", "colored")


def register_auto_ppo_experiment(actor_size: int,
                                 critic_size: int,
                                 gen_bs: int,
                                 train_bs: int,
                                 seqlen: int,
                                 mode: str,
                                 n_node_multiplier: int = 1):
    assert actor_size in [7, 13, 34, 70] and critic_size in [7, 13, 34, 70]
    if (actor_size == 7 and critic_size == 7)\
        or (critic_size == 7 and actor_size == 7):
        n_nodes = 1
    elif (actor_size == 13 and critic_size == 7)\
        or (critic_size == 13 and actor_size == 7):
        n_nodes = 2
    elif (actor_size == 34 and critic_size == 7)\
        or (critic_size == 34 and actor_size == 7)\
        or (actor_size == 13 and critic_size == 13):
        n_nodes = 4
    elif (actor_size == 70 and critic_size == 7)\
        or (critic_size == 70 and actor_size == 7)\
        or (actor_size == 34 and critic_size == 34):
        n_nodes = 8
    elif (actor_size == 70 and critic_size == 70):
        n_nodes = 16
    else:
        return

    n_nodes = n_nodes * n_node_multiplier
    assert n_nodes <= 16, f"n_node_multiplier {n_node_multiplier}, n_nodes {n_nodes}"

    if n_nodes == 1:
        nodelist = "QH-com28"
    elif n_nodes == 2:
        nodelist = "QH-com[27-28]"
    elif n_nodes == 4:
        nodelist = "QH-com[25-28]"
    elif n_nodes == 8:
        nodelist = "QH-com[20-22,24-28]"
    elif n_nodes == 16:
        nodelist = "QH-com[20-22,24-28,41-48]"

    actor_model_class = "llama" if actor_size != 34 else "codellama"
    critic_model_class = "llama" if critic_size != 34 else "codellama"

    @auto(n_nodes=n_nodes, nodelist=nodelist, mode=mode)
    @dataclasses.dataclass
    class AutoPPOExperiment:
        seed: int = 1
        exp_ctrl: ExperimentSaveEvalControl = dataclasses.field(default_factory=functools.partial(
            ExperimentSaveEvalControl,
            benchmark_steps=10,
        ),)
        ppo: PPOHyperparameters = dataclasses.field(default_factory=functools.partial(
            PPOHyperparameters,
            max_new_tokens=seqlen,
            min_new_tokens=seqlen,
        ))

        @property
        def dataset(self) -> DatasetType:
            return PromptOnlyDatasetConfig(
                max_prompt_len=128,
                n_tokens_per_batch=1048576,
                path="/lustre/fw/datasets/antropic-hh/ppo_prompt_only.jsonl",
                # path="/lustre/fw/datasets/imdb/rl/ppo_prompt.jsonl",
            )

        @property
        def rpcs(self) -> List[ModelRPC]:
            ppo_kwargs = dict(
                n_minibatches=self.ppo.ppo_n_minibatches,
                kl_ctl=self.ppo.kl_ctl,
                discount=self.ppo.discount,
                gae_lambda=self.ppo.gae_lambda,
                eps_clip=self.ppo.eps_clip,
                value_eps_clip=self.ppo.value_eps_clip,
                max_reward_clip=self.ppo.max_reward_clip,
                adaptive_kl_ctl=self.ppo.use_adaptive_kl_ctl,
                value_norm=self.ppo.value_norm,
                value_norm_type=self.ppo.value_norm_type,
                value_norm_beta=self.ppo.value_norm_beta,
                value_norm_eps=self.ppo.value_norm_eps,
            )
            generation_kwargs = dict(
                max_new_tokens=self.ppo.max_new_tokens,
                min_new_tokens=self.ppo.min_new_tokens,
                greedy=self.ppo.greedy,
                top_p=self.ppo.top_p,
                top_k=self.ppo.top_k,
                temperature=self.ppo.temperature,
            )
            actor_interface = ModelInterface(
                "ppo_actor",
                args={
                    **copy.deepcopy(ppo_kwargs),
                    "generation_config": generation_kwargs,
                    "early_stop_imp_ratio": self.ppo.early_stop_imp_ratio,
                    "force_no_logits_mask": True,
                    "adv_norm": self.ppo.adv_norm,
                },
            )
            ref_interface = copy.deepcopy(actor_interface)
            ref_interface.args["enable_save"] = False

            critic_interface = ModelInterface(
                "ppo_critic",
                args=copy.deepcopy(ppo_kwargs),
            )
            rw_interface = ModelInterface(
                "paired_rw",
                args=dict(
                    enable_save=False,
                    output_scaling=self.ppo.reward_output_scaling,
                    output_bias=self.ppo.reward_output_bias,
                ),
            )
            return [
                ModelRPC(
                    model_name=ModelName("actor", 0),
                    model_type=ModelFamily(actor_model_class, actor_size, is_critic=False),
                    interface_type=ModelInterfaceType.GENERATE,
                    interface_impl=actor_interface,
                    input_data=["packed_prompts", "prompt_cu_seqlens"],
                    output_data=[
                        "seq_no_eos_mask",
                        "packed_seq",
                        "cu_seqlens",
                        "packed_logprobs",
                        "prompt_mask",
                    ],
                    balanced_dp=True,
                    min_n_seqs=gen_bs,
                    max_n_seqs=gen_bs,
                ),
                ModelRPC(
                    model_name=ModelName("reward", 0),
                    model_type=ModelFamily(critic_model_class, critic_size, is_critic=True),
                    interface_type=ModelInterfaceType.INFERENCE,
                    interface_impl=rw_interface,
                    input_data=["packed_seq", "cu_seqlens"],
                    input_key_remap={"packed_seq": "packed_input_ids"},
                    output_data=["scores"],
                    output_key_remap={"scores": "rewards"},
                    min_n_seqs=gen_bs,
                    max_n_seqs=gen_bs,
                ),
                ModelRPC(
                    model_name=ModelName("ref", 0),
                    model_type=ModelFamily(actor_model_class, actor_size, is_critic=False),
                    interface_type=ModelInterfaceType.INFERENCE,
                    interface_impl=ref_interface,
                    input_data=[
                        "packed_seq",
                        "cu_seqlens",
                    ],
                    output_data=["logprobs"],
                    output_key_remap={"logprobs": "packed_ref_logprobs"},
                    min_n_seqs=gen_bs,
                    max_n_seqs=gen_bs,
                ),
                ModelRPC(
                    model_name=ModelName("critic", 0),
                    model_type=ModelFamily(critic_model_class, critic_size, is_critic=True),
                    interface_type=ModelInterfaceType.INFERENCE,
                    interface_impl=critic_interface,
                    input_data=["packed_seq", "cu_seqlens", "seq_no_eos_mask"],
                    output_data=["scores"],
                    output_key_remap={"scores": "values"},
                    min_n_seqs=gen_bs,
                    max_n_seqs=gen_bs,
                ),
                ModelRPC(
                    model_name=ModelName("actor", 0),
                    model_type=ModelFamily(actor_model_class, actor_size, is_critic=False),
                    interface_type=ModelInterfaceType.TRAIN_STEP,
                    interface_impl=actor_interface,
                    input_data=[
                        "packed_seq",
                        "cu_seqlens",
                        "packed_logprobs",
                        "packed_ref_logprobs",
                        "rewards",
                        "values",
                        "prompt_mask",
                        "seq_no_eos_mask",
                    ],
                    log_return_value=True,
                    min_n_seqs_per_dp=self.ppo.ppo_n_minibatches,
                    min_n_seqs=train_bs,
                    max_n_seqs=train_bs,
                    balanced_dp=True,
                ),
                ModelRPC(
                    model_name=ModelName("critic", 0),
                    interface_type=ModelInterfaceType.TRAIN_STEP,
                    model_type=ModelFamily(critic_model_class, critic_size, is_critic=True),
                    interface_impl=critic_interface,
                    input_data=[
                        "packed_seq",
                        "cu_seqlens",
                        "packed_logprobs",
                        "packed_ref_logprobs",
                        "rewards",
                        "values",
                        "prompt_mask",
                        "seq_no_eos_mask",
                    ],
                    log_return_value=True,
                    min_n_seqs_per_dp=self.ppo.ppo_n_minibatches,
                    min_n_seqs=train_bs,
                    max_n_seqs=train_bs,
                    balanced_dp=True,
                ),
            ]

    short_mode = mode[0]
    n_node_multiplier_str = f"nx{n_node_multiplier}" if n_node_multiplier > 1 else ""
    if critic_size == 7:
        register_experiment(
            f"sosp-a{actor_size}s{seqlen}g{gen_bs}t{train_bs}"
            f"{n_node_multiplier_str}-{short_mode}", AutoPPOExperiment)
    else:
        register_experiment(
            f"sosp-a{actor_size}c{critic_size}s{seqlen}g{gen_bs}t{train_bs}"
            f"{n_node_multiplier_str}-{short_mode}", AutoPPOExperiment)


import itertools

for actor_sz, critic_sz in itertools.product([7, 13, 34, 70], [7, 13, 34, 70]):
    for gen_bs in [128, 256, 512, 1024, 2048, 4096]:
        # for seqlen in [256, 512, 1024]:
        for seqlen in [128, 384, 896]:
            for mode in ["search", "model_pipe", "data_pipe", "test", "full_model"]:
                train_bs = gen_bs
                register_auto_ppo_experiment(actor_sz, critic_sz, gen_bs, train_bs, seqlen, mode)

actor_sz = critic_sz = 7
for gen_bs in [128, 256, 512, 1024, 2048, 4096]:
    # for seqlen in [256, 512, 1024]:
    for seqlen in [128, 384, 896]:
        for mode in ["search", "model_pipe", "data_pipe", "test", "full_model"]:
            train_bs = gen_bs
            register_auto_ppo_experiment(actor_sz,
                                         critic_sz,
                                         gen_bs,
                                         train_bs,
                                         seqlen,
                                         mode,
                                         n_node_multiplier=2)