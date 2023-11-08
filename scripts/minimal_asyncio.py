import asyncio
import multiprocessing as mp
import concurrent.futures
import viztracer
import time
import zmq
import torch
import numpy as np
from system.request_reply_stream import NameResolvingReplyServer, NameResolvingRequestClient, Request, Reply
import base.name_resolve
import base.names as names
import base.namedarray
import base.namedarray as namedarray
import system.request_reply_stream as request_reply_stream
import api.model
import api.dfg
from typing import Dict, List
import logging
import base.dataparallel as dataparallel
import os
from api.dfg import ModelInterfaceType, ModelRPC
import base.timeutil

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("asyncio minimal")

serialization_method = "pickle_compress"
exp_name = "asyncio-minimal"
trial_name = "test"

base.name_resolve.clear_subtree(names.trial_root(exp_name, trial_name))


def generate(data):
    time.sleep(4)
    return {
        "seq_no_eos_mask": torch.randint(0, 2, (10,)),
        "packed_seq": torch.randint(0, 60000, (10, 512), dtype=torch.long),
        "cu_seqlens": torch.randint(0, 2, (11,)),
        "packed_logprobs": torch.randn(10, 512),
        "packed_logits_mask": torch.randint(0, 2, (10, 512, 60000), dtype=torch.bool),
    }


def inference(data):
    time.sleep(0.2)
    return dict(logprobs=torch.randn(10, 512), scores=torch.randn(10, 512))


def train_step(data):
    time.sleep(2)
    return dict()


rollout = ModelRPC(
    "actor",
    ModelInterfaceType.GENERATE,
    input_data=["prompts", "prompt_att_mask"],
    output_data=[
        "seq_no_eos_mask",
        "packed_seq",
        "cu_seqlens",
        "packed_logprobs",
        "packed_logits_mask",
    ],
)
inf_reward = ModelRPC(
    "reward",
    ModelInterfaceType.INFERENCE,
    input_data=["packed_seq", "cu_seqlens"],
    input_key_remap={"packed_seq": "packed_input_ids"},
    output_data=["scores"],
    output_key_remap={"scores": "rewards"},
)

inf_ref_logits = ModelRPC(
    "ref",
    ModelInterfaceType.INFERENCE,
    input_data=[
        "packed_seq",
        "cu_seqlens",
        "packed_logits_mask",
    ],
    output_data=["logprobs"],
    output_key_remap={"logprobs": "packed_ref_logprobs"},
)

inf_values = ModelRPC(
    "critic",
    ModelInterfaceType.INFERENCE,
    input_data=["packed_seq", "cu_seqlens", "seq_no_eos_mask"],
    output_data=["scores"],
    output_key_remap={"scores": "values"},
)

train_actor = ModelRPC(
    "actor",
    ModelInterfaceType.TRAIN_STEP,
    input_data=[
        "packed_seq",
        "cu_seqlens",
        "packed_logprobs",
        "packed_ref_logprobs",
        "rewards",
        "values",
        "seq_no_eos_mask",
        "packed_logits_mask",
    ],
    log_return_value=True,
)

train_critic = ModelRPC(
    "critic",
    ModelInterfaceType.TRAIN_STEP,
    input_data=[
        "packed_seq",
        "cu_seqlens",
        "packed_logprobs",
        "packed_ref_logprobs",
        "rewards",
        "values",
        "seq_no_eos_mask",
    ],
    dp_broker_type="packed",
    log_return_value=True,
)


def model_worker(model_type):
    stream = NameResolvingReplyServer(exp_name, trial_name, model_type, serialization_method)

    tracer = viztracer.VizTracer(
        tracer_entries=int(2e6),
        max_stack_depth=10,
        ignore_frozen=True,
        min_duration=500,
        output_file=f"{model_type}.json",
    )
    tracer.start()
    ctl = base.timeutil.FrequencyControl(frequency_seconds=10)
    while True:
        try:
            req = stream.poll_request()
        except request_reply_stream.NoMessage:
            time.sleep(0.002)
            continue
        handle_name = req.handle_name
        if handle_name == "generate":
            res = generate(req.data)
        elif handle_name == "inference":
            res = inference(req.data)
        elif handle_name == "train_step":
            res = train_step(req.data)
        else:
            raise NotImplementedError()
        rep = Reply(base.namedarray.from_dict(res))
        stream.post_reply(rep)
        if ctl.check():
            tracer.save()


async def model_rpc_func(
    rpc_config: api.dfg.ModelRPC,
    rpc_futures: Dict[str, asyncio.Future],
    parent_rpc_names: List[str],
    data_registry: Dict[str, torch.Tensor],
    stream: request_reply_stream.RequestClient,
):
    logger.info(f"rpc {rpc_config.name} running")
    for parent in parent_rpc_names:
        await rpc_futures[parent]

    data = {}
    for k in rpc_config.input_data:
        if k not in rpc_config.input_key_remap:
            data[k] = data_registry[k]
        else:
            data[rpc_config.input_key_remap[k]] = data_registry[k]
    data = namedarray.from_dict(data)

    stream.post_request(Request(rpc_config.interface_type.value, data))
    while True:
        try:
            res = stream.poll_reply()
            break
        except request_reply_stream.NoMessage:
            await asyncio.sleep(0.01)

    res = res.data
    for k in rpc_config.output_data:
        if k in rpc_config.output_key_remap:
            data_registry[rpc_config.output_key_remap[k]] = res[k]
        else:
            data_registry[k] = res[k]

    rpc_futures[rpc_config.name].set_result(1)
    logger.info(f"rpc {rpc_config.name} finish")


def main():
    tracer = viztracer.VizTracer(
        tracer_entries=int(2e6),
        max_stack_depth=10,
        ignore_frozen=True,
        min_duration=500,
        output_file="master.json",
    )
    tracer.start()
    model_rpcs = [rollout, inf_ref_logits, inf_reward, inf_values, train_actor, train_critic]
    parents, _ = api.dfg.build_graph(model_rpcs)

    data_registry = {}

    streams = {
        model_type: NameResolvingRequestClient(exp_name, trial_name, model_type, serialization_method)
        for model_type in ["actor", "critic", "ref", "reward"]
    }

    event_loop = asyncio.get_event_loop()
    asyncio.set_event_loop(event_loop)

    for i in range(3):
        logger.info(f"################ Round {i+1} running ################")
        data_registry["prompts"] = torch.randint(0, 60000, (10, 256), dtype=torch.long)
        data_registry["prompt_att_mask"] = torch.randint(0, 2, (10, 256), dtype=torch.bool)

        futures = {rpc.name: asyncio.Future(loop=event_loop) for rpc in model_rpcs}
        tasks = []
        for i, rpc in enumerate(model_rpcs):
            stream = streams[rpc.model_name]

            task = event_loop.create_task(model_rpc_func(rpc, futures, parents[i], data_registry, stream))
            tasks.append(task)

        event_loop.run_until_complete(asyncio.gather(*tasks, *futures.values()))

        data_registry.clear()
    tracer.stop()
    tracer.save()


if __name__ == "__main__":
    procs = []
    for model_type in ["actor", "critic", "ref", "reward"]:
        p = mp.Process(target=model_worker, args=(model_type,))
        p.start()
        procs.append(p)
    main()
    for p in procs:
        os.system(f"kill -9 {p.pid}")
    os.system("viztracer --combine actor.json critic.json ref.json reward.json master.json -o result.json")
    os.system("rm actor.json critic.json ref.json reward.json master.json")
