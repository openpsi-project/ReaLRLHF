from typing import List
import dataclasses
import functools

import numpy as np

from reallm.api.core.dfg import ModelFamily, ModelInterface, ModelInterfaceType, ModelRPC
from reallm.api.core.system_api import *
from reallm.api.quickstart.device_mesh import make_train_backend_config, RPCAllocation
from reallm.api.quickstart.model import ModelTrainEvalConfig, OptimizerConfig, ParallelismConfig
from reallm.base.topology import PipeModelDataParallelTopology
from reallm.profiler.utils import find_factors


def ppo_rpcs_example(actor_size, critic_size, bs, seqlen):
    prompt_seqlen = 128
    train_seqlen = prompt_seqlen + seqlen
    actor_interface = ModelInterface(
        "ppo_actor",
        args={},
    )
    ref_interface = copy.deepcopy(actor_interface)
    ref_interface.args["enable_save"] = False

    critic_interface = ModelInterface(
        "ppo_critic",
        args={},
    )
    rw_interface = ModelInterface(
        "paired_rw",
        args=dict(enable_save=False,),
    )
    actor_model_class = "llama" if actor_size != 34 else "codellama"
    critic_model_class = "llama" if critic_size != 34 else "codellama"

    return [
        ModelRPC(
            model_name="actor",
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
            min_n_seqs=bs,
            max_n_seqs=bs,
        ),
        ModelRPC(
            model_name="reward",
            model_type=ModelFamily(critic_model_class, critic_size, is_critic=True),
            interface_type=ModelInterfaceType.INFERENCE,
            interface_impl=rw_interface,
            input_data=["packed_seq", "cu_seqlens"],
            input_key_remap={"packed_seq": "packed_input_ids"},
            output_data=["scores"],
            output_key_remap={"scores": "rewards"},
            min_n_seqs=bs,
            max_n_seqs=bs,
        ),
        ModelRPC(
            model_name="ref",
            model_type=ModelFamily(actor_model_class, actor_size, is_critic=False),
            interface_type=ModelInterfaceType.INFERENCE,
            interface_impl=ref_interface,
            input_data=[
                "packed_seq",
                "cu_seqlens",
            ],
            output_data=["logprobs"],
            output_key_remap={"logprobs": "packed_ref_logprobs"},
            min_n_seqs=bs,
            max_n_seqs=bs,
        ),
        ModelRPC(
            model_name="critic",
            model_type=ModelFamily(critic_model_class, critic_size, is_critic=True),
            interface_type=ModelInterfaceType.INFERENCE,
            interface_impl=critic_interface,
            input_data=["packed_seq", "cu_seqlens", "seq_no_eos_mask"],
            output_data=["scores"],
            output_key_remap={"scores": "values"},
            min_n_seqs=bs,
            max_n_seqs=bs,
        ),
        ModelRPC(
            model_name="actor",
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
            min_n_seqs_per_dp=4,
            min_n_seqs=bs,
            max_n_seqs=bs,
        ),
        ModelRPC(
            model_name="critic",
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
            min_n_seqs_per_dp=4,
            min_n_seqs=bs,
            max_n_seqs=bs,
            balanced_dp=True,
        ),
    ]


# experiment config to run profiler (single instruction and model rpc)
@dataclasses.dataclass
class ProfileExperiment(Experiment):
    model_type: ModelFamily
    interface: ModelInterface

    n_nodes: int
    nodelist: str
    parallelism_config: ParallelismConfig

    num_gpus_per_node: int = 8
    seed: int = 1
    device_mesh_name: Optional[str] = None

    # list for profiler to enumerate
    bs_list: Optional[List[int]] = None
    seq_len_list: Optional[List[int]] = None
    gen_tokens_list: Optional[List[int]] = None

    # use_sequence_parallel: bool = False
    use_gradient_checkpointing: bool = True

    # profile_communication: bool = False
    # profile_rpc: bool = True

    single_rpc_profile: Optional[str] = None
    instruction_sync: bool = False

    def __post_init__(self):
        self.n_workers = self.n_nodes * self.num_gpus_per_node
        self.device_mesh_name = self.nodelist

    @property
    def all_rpcs(self):
        rollout = ModelRPC(
            model_name="actor",
            model_type=self.model_type,
            interface_type=ModelInterfaceType.GENERATE,
            interface_impl=self.interface,
            min_n_seqs=256,
            max_n_seqs=256,
        )

        inf = ModelRPC(
            model_name="actor",
            model_type=self.model_type,
            interface_type=ModelInterfaceType.INFERENCE,
            interface_impl=self.interface,
            min_n_seqs=256,
            max_n_seqs=256,
        )

        train = ModelRPC(
            model_name="actor",
            model_type=self.model_type,
            interface_type=ModelInterfaceType.TRAIN_STEP,
            interface_impl=self.interface,
            min_n_seqs=256,
            max_n_seqs=256,
        )
        return [rollout, inf, train]

    @property
    def rpcs(self) -> List[ModelRPC]:
        if self.single_rpc_profile == "gen":
            return [self.all_rpcs[0]]
        elif self.single_rpc_profile == "inf":
            return [self.all_rpcs[1]]
        elif self.single_rpc_profile == "train":
            return [self.all_rpcs[2]]
        else:
            return self.all_rpcs

    @property
    def rpc_allocations(self):
        rollout, inf, train = self.all_rpcs
        rollout_alloc = RPCAllocation(
            rpc=rollout,
            mapping=np.ones((self.n_nodes, self.num_gpus_per_node), dtype=np.int32),
            train_eval_config=ModelTrainEvalConfig(
                type=rollout.model_type._class,
                path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                base_model_path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                parallel=self.parallelism_config,
            ),
        )
        inf_alloc = RPCAllocation(
            rpc=inf,
            mapping=np.ones((self.n_nodes, self.num_gpus_per_node), dtype=np.int32),
            train_eval_config=ModelTrainEvalConfig(
                type=inf.model_type._class,
                path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                base_model_path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                parallel=self.parallelism_config,
            ),
        )
        train_alloc = RPCAllocation(
            rpc=train,
            mapping=np.ones((self.n_nodes, self.num_gpus_per_node), dtype=np.int32),
            train_eval_config=ModelTrainEvalConfig(
                type=train.model_type._class,
                path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                base_model_path=MODEL_FAMILY_TO_PATH[rollout.model_type],
                gradient_checkpointing=self.use_gradient_checkpointing,
                parallel=self.parallelism_config,
                optimizer=OptimizerConfig(type="adam"),
            ),
        )
        if self.single_rpc_profile == "gen":
            return [rollout_alloc]
        elif self.single_rpc_profile == "inf":
            return [inf_alloc]
        elif self.single_rpc_profile == "train":
            return [train_alloc]
        else:
            return [rollout_alloc, inf_alloc, train_alloc]

    def scheduling_setup(self) -> ExperimentScheduling:
        return ExperimentScheduling(profile_worker=TasksGroup(
            count=self.n_workers,
            scheduling=Scheduling.profile_worker_default(
                cpu=4,
                gpu=1,
                gpu_type="tesla",
                mem=100000,
                nodelist=self.nodelist,
            ),
        ))

    def initial_setup(self) -> ExperimentConfig:
        exp_ctrl: ExperimentSaveEvalControl = dataclasses.field(default_factory=functools.partial(
            ExperimentSaveEvalControl,
            benchmark_steps=10,
        ),)

        # rpc allocation for each rpc
        rpc = self.rpcs[0]
        m = self.rpc_allocations[0]
        model = Model(
            "real_model",
            args=dict(
                model_path=MODEL_FAMILY_TO_PATH[rpc.model_type],
                is_critic=False,
                init_critic_from_actor=False,  # FIXME:
                dtype="fp16",
                hf_model_family=rpc.model_type._class,
            ),
        )
        interface = rpc.interface_impl
        backend = make_train_backend_config(m.train_eval_config, instruction_sync=self.instruction_sync)

        profile_workers = [
            ProfileWorker(
                seed=self.seed,
                model=model,
                backend=backend,
                interface=interface,
                rpcs=self.rpcs,
                topo=PipeModelDataParallelTopology(
                    num_dp=self.parallelism_config.data_parallel_size,
                    num_mp=self.parallelism_config.model_parallel_size,
                    num_pp=self.parallelism_config.pipeline_parallel_size,
                ),
                bs_list=self.bs_list,
                seq_len_list=self.seq_len_list,
                gen_tokens_list=self.gen_tokens_list,
                profile_communication=False,
                profile_rpc=True,
                warmup_rounds=2,
                profile_rounds=1,
            ) for _ in range(self.n_workers)
        ]

        return ExperimentConfig(exp_ctrl=exp_ctrl,
                                model_rpcs=[],
                                model_worker=[],
                                profile_worker=profile_workers)


def register_profile_experiment(
    size: int,
    num_pp: int,
    num_mp: int,
    num_dp: int,
):
    assert size in [7, 13, 34, 70]
    model_class = "llama" if size != 34 else "codellama"
    actor_model_type = ModelFamily(model_class, size, False)
    n_nodes = max(1, (num_pp * num_mp * num_dp) // 8)

    # node_start = 42
    # node_end = node_start + n_nodes - 1
    # nodelist = f"QH-com[{node_start:02d}-{node_end:02d}]"
    if n_nodes <= 1:
        nodelist = "QH-com47"
    elif n_nodes == 2:
        nodelist = "QH-com[47-48]"
    elif n_nodes == 3:
        nodelist = "QH-com[25,47-48]"
    elif n_nodes == 4:
        nodelist = "QH-com[21-22,47-48]"
    elif n_nodes == 6:
        nodelist = "QH-com[42-47]"
    elif n_nodes == 7:
        nodelist = "QH-com[23-28,45]"
    elif n_nodes == 8:
        nodelist = "QH-com[23-28,45-46]"
    elif n_nodes == 16:
        nodelist = "QH-com[20-22,25-26,41-48]"
    else:
        raise NotImplementedError(n_nodes)

    exp_func = functools.partial(
        ProfileExperiment,
        model_type=actor_model_type,
        interface=ModelInterface(type_="profile"),
        n_nodes=n_nodes,
        num_gpus_per_node=min(8, num_pp * num_mp * num_dp),
        nodelist=nodelist,
        parallelism_config=ParallelismConfig(
            data_parallel_size=num_dp,
            model_parallel_size=num_mp,
            pipeline_parallel_size=num_pp,
            use_sequence_parallel=(num_mp > 1),
        ),
        instruction_sync=False,
    )
    # print(f"registering profile-s{size}p{num_pp}m{num_mp}d{num_dp}")
    register_experiment(f"profile-s{size}p{num_pp}m{num_mp}d{num_dp}", exp_func)

    for func_name in ["gen", "train", "inf"]:
        exp_func = functools.partial(
            ProfileExperiment,
            model_type=actor_model_type,
            interface=ModelInterface(type_="profile"),
            n_nodes=n_nodes,
            num_gpus_per_node=min(8, num_pp * num_mp * num_dp),
            nodelist=nodelist,
            parallelism_config=ParallelismConfig(
                data_parallel_size=num_dp,
                model_parallel_size=num_mp,
                pipeline_parallel_size=num_pp,
                use_sequence_parallel=(num_mp > 1),
            ),
            single_rpc_profile=func_name,
            instruction_sync=True,
        )
        register_experiment(f"profile-s{size}p{num_pp}m{num_mp}d{num_dp}-{func_name}", exp_func)


# register_profile_experiment(7, 2, 1, 4)

for num_gpus in [1, 2, 4, 8, 16, 24, 32, 48, 56, 64, 128]:
    for num_mp in [1, 2, 4, 8]:
        remain = num_gpus // num_mp
        for num_dp in find_factors(remain):
            num_pp = remain // num_dp
            for size in [7, 13, 34, 70]:
                register_profile_experiment(size, num_pp, num_mp, num_dp)