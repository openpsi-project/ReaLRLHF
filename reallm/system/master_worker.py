from collections import defaultdict
from typing import Dict, List, Optional, Set, Tuple, Union
import asyncio
import copy
import dataclasses
import gc
import getpass
import itertools
import os
import re
import sys
import threading
import time
import uuid

import colorama
import deepspeed
import numpy as np
import torch
import torch.distributed

from reallm.api.core.config import ModelName
from reallm.api.core.model_api import ReaLModelConfig
from reallm.base import datapack, dataparallel, logging, namedarray, numpy_utils, timeutil, topology
from reallm.base.asyncio_utils import (raise_asyncio_exception, setup_run_until_complete,
                                       teardown_run_util_complete)
from reallm.base.cluster import spec as cluster_spec
from reallm.base.constants import MODEL_SAVE_ROOT
from reallm.system.buffer import AsyncIOSequenceBuffer
import reallm.api.core.config as config_api
import reallm.api.core.data_api as data_api
import reallm.api.core.dfg as dfg
import reallm.api.core.model_api as model_api
import reallm.api.core.system_api as config_pkg
import reallm.system.request_reply_stream as request_reply_stream
import reallm.system.worker_base as worker_base

logger = logging.getLogger("master worker", "system")
blogger = logging.getLogger("benchmark")


def _get_n_seqs_from_batch_sample(sample: namedarray.NamedArray) -> int:
    assert ("input_lens" in sample.keys() or "cu_seqlens" in sample.keys()
            or "prompt_cu_seqlens" in sample.keys() or "prompt_lens" in sample.keys()), (
                list(sample.keys()),
                sample,
            )
    if "input_lens" in sample.keys():
        return len(sample["input_lens"])
    elif "cu_seqlens" in sample.keys():
        return len(sample["cu_seqlens"]) - 1
    # NOTE: The order matters. We should first try to get the length of the generated text rather than prompts.
    elif "prompt_lens" in sample.keys():
        return len(sample["prompt_lens"])
    elif "prompt_cu_seqlens" in sample.keys():
        return len(sample["prompt_cu_seqlens"]) - 1
    else:
        raise NotImplementedError(f"Unknown seqlens keys: {list(sample.keys())}.")


class ExperimentComplete(Exception):

    def __init__(self, message):
        disclaimer = (colorama.Fore.GREEN + "\033[1m" +
                      "<This is not an error. It is just a way to stop the experiment.> ")
        super().__init__(disclaimer + colorama.Style.RESET_ALL + colorama.Fore.YELLOW +
                         colorama.Style.BRIGHT + "\033[1m" + message + colorama.Style.RESET_ALL)


def request_all(
    stream: request_reply_stream.NameResolvingRequstClient,
    handlers: List[str],
    handle_type: str,
    datas: List[namedarray.NamedArray],
    verbose: bool = True,
):
    requests = [
        request_reply_stream.Payload(
            handler=handler,
            handle_name=handle_type,
            data=data,
        ) for handler, data in zip(handlers, datas)
    ]
    if verbose:
        blogger.debug(f"master worker #request_all# *end* time ${time.time_ns()}$")
    tik = time.perf_counter()

    # A protocol to ensure that any model worker execute jobs in the same order.
    [stream.post(r) for r in requests]
    [stream.poll(block=True, pattern=create_exact_match_pattern([p.syn_reply_id])) for p in requests]
    [
        stream.post(
            request_reply_stream.Payload(handler=r.handler, handle_name="ack", request_id=r.ack_reply_id))
        for r in requests
    ]
    t = time.perf_counter() - tik

    if verbose:
        blogger.debug(f'Request "{handle_type}" time in total: '
                      f"{t:.4f}s, {t / len(requests):.4f}s per request")
    return [r.request_id for r in requests]


def create_exact_match_pattern(string_list: List[Union[uuid.UUID, str]]) -> re.Pattern:
    escaped_strings = [re.escape(str(s)) for s in string_list]
    pattern = f"({'|'.join(escaped_strings)})$"
    return re.compile(pattern)


async def _awaitable_response(
    stream: request_reply_stream.NameResolvingRequstClient,
    pattern: re.Pattern | None,
) -> request_reply_stream.Payload:
    while True:
        try:
            return stream.poll(pattern=pattern, block=False)
        except request_reply_stream.NoMessage:
            await asyncio.sleep(0.01)
            continue


async def gather_all_replies(
    stream: request_reply_stream.NameResolvingRequstClient,
    request_ids: List[str],
    verbose: bool = True,
) -> List:
    """Collect responses from multiple streams. Blocking method."""
    responses = await asyncio.gather(
        *
        [_awaitable_response(stream, pattern=create_exact_match_pattern([req_id])) for req_id in request_ids])
    if verbose:
        blogger.debug(f"master worker #gather_all_replies# *end* time ${time.time_ns()}$")
    return responses


async def group_rpc_blocked(
    stream: request_reply_stream.NameResolvingRequstClient,
    handlers: List[Union[config_pkg.ModelShardID, str]],
    handle_type: str,
    datas: List[namedarray.NamedArray],
    verbose: bool = True,
) -> List[namedarray.NamedArray]:
    payloads = await gather_all_replies(stream,
                                        request_all(stream, handlers, handle_type, datas, verbose=verbose))
    return [p.data for p in payloads]


def split_packed_batch_into_seqs(
    sample: namedarray.NamedArray,
    input_lens: Optional[torch.Tensor] = None,
    return_seqlens: bool = False,
) -> List[namedarray.NamedArray]:
    if input_lens is None:
        if "input_lens" in sample:
            input_lens = sample["input_lens"]
        elif "prompt_lens" in sample:
            input_lens = sample["prompt_lens"]
        elif "cu_seqlens" in sample:
            input_lens = sample["cu_seqlens"][1:] - sample["cu_seqlens"][:-1]
        elif "prompt_cu_seqlens" in sample:
            input_lens = sample["prompt_cu_seqlens"][1:] - sample["prompt_cu_seqlens"][:-1]

    partitions = [(i, i + 1) for i in range(input_lens.shape[0])]
    sample["input_lens"] = input_lens
    sample.register_metadata(seqlens=input_lens.cpu().numpy().tolist())
    res = dataparallel.PackedParallelDataBroker.scatter_to(sample,
                                                           n_dp=len(input_lens),
                                                           partitions=partitions)
    if not return_seqlens:
        return res
    else:
        return res, input_lens


def _request_parameter_sync(
    stream: request_reply_stream.NameResolvingRequstClient,
    msid2mwid: Dict[config_pkg.ModelShardID, int],
    from_model_name: ModelName,
    to_model_name: ModelName,
    from_topo: topology.PipeModelDataParallelTopology,
    to_topo: topology.PipeModelDataParallelTopology,
    to_model_config: ReaLModelConfig,
):

    model_name = from_model_name
    target = to_model_name
    # Prioritize handlers of `from_model`, then handlers of `to_model`.
    # As a result, if both `from_model` and `to_model` reside in a model worker,
    # the handler in the received request will be `from_model`. Layers will also built in `from_model`.
    # After that, we assign layers of the `from_model` to `to_model`.
    handlers = [
        config_pkg.ModelShardID.from_parallelism_rank(model_name, from_topo, j)
        for j in range(from_topo.world_size())
    ]
    all_handler_mwids = set([msid2mwid[h] for h in handlers])
    dst_handlers = [
        config_pkg.ModelShardID.from_parallelism_rank(target, to_topo, j) for j in range(to_topo.world_size())
    ]
    for h in dst_handlers:
        if msid2mwid[h] not in all_handler_mwids:
            handlers.append(h)
            all_handler_mwids.add(msid2mwid[h])

    ps_data = {
        "from_model_name": model_name,
        "to_model_name": target,
        "from_topo": from_topo,
        "to_topo": to_topo,
        "to_model_config": to_model_config,
    }
    payloads = [
        request_reply_stream.Payload(handler=h,
                                     handle_name="empty",
                                     pre_hooks=["param_realloc"],
                                     pre_hook_data=[ps_data]) for h in handlers
    ]
    request_ids = [stream.post(p) for p in payloads]
    [stream.poll(pattern=create_exact_match_pattern([p.syn_reply_id]), block=True) for p in payloads]
    [
        stream.post(
            request_reply_stream.Payload(handler=p.handler, handle_name="ack", request_id=p.ack_reply_id))
        for p in payloads
    ]
    [stream.poll(pattern=create_exact_match_pattern([req_id]), block=True) for req_id in request_ids]


@dataclasses.dataclass
class InterfaceDataAmount:
    train_configs: List[ReaLModelConfig] = dataclasses.field(default_factory=list)
    train_bs: List[int] = dataclasses.field(default_factory=list)
    train_seqlens: List[List[int]] = dataclasses.field(default_factory=list)

    inf_configs: List[ReaLModelConfig] = dataclasses.field(default_factory=list)
    inf_bs: List[int] = dataclasses.field(default_factory=list)
    inf_seqlens: List[List[int]] = dataclasses.field(default_factory=list)

    gen_configs: List[ReaLModelConfig] = dataclasses.field(default_factory=list)
    gen_bs: List[int] = dataclasses.field(default_factory=list)
    prompt_lens: List[List[int]] = dataclasses.field(default_factory=list)
    gen_len: List[int] = dataclasses.field(default_factory=list)

    def clear(self):
        self.train_bs.clear()
        self.train_seqlens.clear()

        self.inf_bs.clear()
        self.inf_seqlens.clear()

        self.gen_bs.clear()
        self.prompt_lens.clear()
        self.gen_len.clear()

        self.train_configs.clear()
        self.inf_configs.clear()
        self.gen_configs.clear()


@dataclasses.dataclass
class RPCCorountineControl:
    ## Shared resources ##
    stop: asyncio.Event
    # for counting the number of finished training steps
    # one training step corresponds to traversal of the whole DFG
    train_count: asyncio.Queue
    # for loading data, save and eval model
    fetch_data_queue: asyncio.Queue
    eval_queue: asyncio.Queue
    save_queue: asyncio.Queue

    ## Per-coroutine resources ##
    # Used for counting the number of concurrent calls.
    can_do_rpc: Dict[str, asyncio.Semaphore]
    rpc_traversal: Dict[str, int]
    # for synchronizing req ids between req and reply coroutines
    request_queues: Dict[str, List[asyncio.Queue]]

    training_buffer_indices: Set[int] = dataclasses.field(default_factory=set)
    data_amount: InterfaceDataAmount = dataclasses.field(default_factory=InterfaceDataAmount)


def _attach_payloads_with_hooks(
    rpc: dfg.ModelRPC,
    payloads: Dict[config_api.ModelShardID, request_reply_stream.Payload],
    mwids: List[int],
    msid2mwid: Dict[config_pkg.ModelShardID, int],
    model_configs: Dict[str, None | ReaLModelConfig],
    model_topos: Dict[str, topology.PipeModelDataParallelTopology],
    main_handlers: List[config_pkg.ModelShardID],
    hook_type: str,
) -> Tuple[Dict[config_api.ModelShardID, request_reply_stream.Payload], List[int]]:
    assert hook_type in ["pre", "post"], hook_type

    main_mwids = set([msid2mwid[h] for h in main_handlers])
    for hook in getattr(rpc, f"{hook_type}_hooks"):
        if isinstance(hook, dfg.SyncParamHook):
            assert (hook.source is None) != (hook.target is None), hook
            if hook.source is None:
                src_topo = model_topos[rpc.model_name]
                dst_topo = model_topos[hook.target]
                dst_config = model_configs[hook.target]
                src_model_name, dst_model_name = rpc.model_name, hook.target
                other_model_name = hook.target
                other_topo = dst_topo
            else:
                src_topo = model_topos[hook.source]
                dst_topo = model_topos[rpc.model_name]
                dst_config = model_configs[rpc.model_name]
                src_model_name, dst_model_name = hook.source, rpc.model_name
                other_model_name = hook.source
                other_topo = src_topo

            ps_data = {
                "from_model_name": src_model_name,
                "to_model_name": dst_model_name,
                "from_topo": src_topo,
                "to_topo": dst_topo,
                "to_model_config": dst_config,
            }
            for h in main_handlers:
                getattr(payloads[h], f"{hook_type}_hooks").append("param_realloc")
                getattr(payloads[h], f"{hook_type}_hook_data").append(ps_data)
            other_handlers = [
                config_api.ModelShardID.from_parallelism_rank(other_model_name, other_topo, j)
                for j in range(other_topo.world_size())
            ]
            for h in other_handlers:
                if msid2mwid[h] not in mwids:
                    payloads[h] = request_reply_stream.Payload(
                        handler=h,
                        handle_name="empty",
                    )
                    setattr(payloads[h], f"{hook_type}_hooks", ["param_realloc"])
                    setattr(payloads[h], f"{hook_type}_hook_data", [ps_data])
                    mwids.append(msid2mwid[h])
                elif msid2mwid[h] not in main_mwids:
                    hh = next(hh for hh in payloads if msid2mwid[hh] == msid2mwid[h])
                    getattr(payloads[hh], f"{hook_type}_hooks").append("param_realloc")
                    getattr(payloads[hh], f"{hook_type}_hook_data").append(ps_data)

        elif isinstance(hook, dfg.OffloadHook):
            for h in main_handlers:
                getattr(payloads[h], f"{hook_type}_hooks").append("offload")
                getattr(payloads[h], f"{hook_type}_hook_data").append(dict(model_name=h.model_name))
        else:
            raise NotImplementedError(f"Unknown hook type: {hook}")
    return payloads, mwids


async def scatter_tensor_to_mws(
    rpc: dfg.ModelRPC,
    stream: request_reply_stream.NameResolvingRequstClient,
    msid2mwid: Dict[config_pkg.ModelShardID, int],
    model_topos: Dict[str, topology.PipeModelDataParallelTopology],
    model_configs: Dict[str, None | ReaLModelConfig],
    producer_names: Dict[str, str],
    producer_name2producer_handlers: Dict[str, List[config_pkg.ModelShardID]],
    producer_mappings: Dict[str, Dict[str, List[int]]],
    target_mapping: Dict[str, List[int]],
    buffer_indices: List[int],
    seqlens: List[int],
    handlers: List[config_pkg.ModelShardID],
) -> List[uuid.UUID]:

    dt_data = {
        "keys": rpc.input_data,
        "target": rpc.model_name,
        "producer_names": producer_names,
        "producer_mappings": producer_mappings,
        "target_mapping": target_mapping,
        "handle_name": rpc.interface_type.value,
        "input_key_remap": rpc.input_key_remap,
        "output_key_remap": rpc.output_key_remap,
        "rpc_name": rpc.name,
        "buffer_indices": buffer_indices,
        "seqlens": seqlens,
    }

    payloads = {
        handler: request_reply_stream.Payload(
            handler=handler,
            handle_name=rpc.interface_type.value,
            pre_hooks=["data_transfer"],
            pre_hook_data=[dt_data],
        )
        for handler in handlers
    }
    mwids = [msid2mwid[h] for h in handlers]
    assert len(mwids) == len(set(mwids))

    for producer_name in producer_names.values():
        for h in producer_name2producer_handlers[producer_name]:
            if msid2mwid[h] not in mwids:
                payloads[h] = request_reply_stream.Payload(
                    handler=h,
                    handle_name="empty",
                    pre_hooks=["data_transfer"],
                    pre_hook_data=[dt_data],
                )
                mwids.append(msid2mwid[h])

    payloads, mwids = _attach_payloads_with_hooks(
        rpc,
        payloads,
        mwids,
        msid2mwid=msid2mwid,
        model_configs=model_configs,
        model_topos=model_topos,
        main_handlers=handlers,
        hook_type="pre",
    )
    payloads, mwids = _attach_payloads_with_hooks(
        rpc,
        payloads,
        mwids,
        msid2mwid=msid2mwid,
        model_configs=model_configs,
        model_topos=model_topos,
        main_handlers=handlers,
        hook_type="post",
    )
    req_ids = [stream.post(p) for h, p in payloads.items() if h in handlers]
    other_req_ids = [stream.post(p) for h, p in payloads.items() if h not in handlers]
    await asyncio.gather(*[
        _awaitable_response(stream, pattern=create_exact_match_pattern([p.syn_reply_id]))
        for p in payloads.values()
    ])
    [
        stream.post(
            request_reply_stream.Payload(handler=p.handler, handle_name="ack", request_id=p.ack_reply_id))
        for p in payloads.values()
    ]
    return req_ids, other_req_ids


async def model_rpc_request_func(
    rpc: dfg.ModelRPC,
    msid2mwid: Dict[config_pkg.ModelShardID, int],
    src_rpc_model_name: ModelName,
    stream: request_reply_stream.NameResolvingRequstClient,
    buffer: AsyncIOSequenceBuffer,
    data_owner: Dict[Tuple[int, str], Tuple[str, int]],
    model_topos: Dict[str, topology.PipeModelDataParallelTopology],
    model_configs: Dict[str, None | ReaLModelConfig],
    ctrl: RPCCorountineControl,
):
    topo = model_topos[rpc.model_name]
    handlers = [
        config_pkg.ModelShardID.from_parallelism_rank(rpc.model_name, topo, j)
        for j in range(topo.world_size())
    ]

    producer_names = {}  # data key -> model name
    for k in rpc.input_data:
        if k in rpc.data_producers:
            producer_names[k] = rpc.data_producers[k]
        else:
            producer_names[k] = src_rpc_model_name
    keys_to_send = defaultdict(list)  # model name -> List[keys] to send
    for k in producer_names:
        keys_to_send[producer_names[k]].append(k)

    # convert producer model name to ModelShardID
    producer_name2producer_handlers = {}
    for producer_name in keys_to_send:
        producer_name2producer_handlers[producer_name] = [
            config_pkg.ModelShardID.from_parallelism_rank(producer_name, model_topos[producer_name], j)
            for j in range(model_topos[producer_name].world_size())
        ]

    can_do_rpc = ctrl.can_do_rpc[rpc.name]
    request_queues = ctrl.request_queues[rpc.name]

    response_coroutine_idx = 0

    this_rpc_consumed_seqs = 0
    while not ctrl.stop.is_set():
        await can_do_rpc.acquire()

        # Ensure that parent RPCs will not be over-consumed.
        while any(this_rpc_consumed_seqs >= (ctrl.rpc_traversal[c.name] + 1) * c.max_n_seqs
                  for c in rpc.children_rpcs):
            await asyncio.sleep(0.1)

        sample = await buffer.get_batch_for_rpc(rpc)

        if rpc.is_src:
            ctrl.training_buffer_indices = ctrl.training_buffer_indices.union(sample.indices)

        if rpc.interface_type == dfg.ModelInterfaceType.GENERATE:
            ctrl.data_amount.gen_configs.append(model_configs[rpc.model_name])
            ctrl.data_amount.gen_bs.append(len(sample.seqlens))
            ctrl.data_amount.gen_len.append(rpc.interface_impl.args["generation_config"]["min_new_tokens"])
            ctrl.data_amount.prompt_lens.append(sample.seqlens)
        elif rpc.interface_type == dfg.ModelInterfaceType.TRAIN_STEP:
            ctrl.data_amount.train_configs.append(model_configs[rpc.model_name])
            ctrl.data_amount.train_bs.append(len(sample.seqlens))
            ctrl.data_amount.train_seqlens.append(sample.seqlens)
        elif rpc.interface_type == dfg.ModelInterfaceType.INFERENCE:
            ctrl.data_amount.inf_configs.append(model_configs[rpc.model_name])
            ctrl.data_amount.inf_bs.append(len(sample.seqlens))
            ctrl.data_amount.inf_seqlens.append(sample.seqlens)

        this_rpc_consumed_seqs += len(sample.seqlens)

        # logger.info(f"Model rpc {rpc.name} requesting.")
        dp_size = topo.get_dim("data")
        if rpc.balanced_dp:
            assert len(sample.seqlens) % dp_size == 0
            min_n_seqs_per_dp = len(sample.seqlens) // dp_size
        else:
            min_n_seqs_per_dp = 1
        partitions = datapack.min_abs_diff_partition(np.array(sample.seqlens, dtype=np.int32),
                                                     dp_size,
                                                     min_size=min_n_seqs_per_dp)
        target_mapping = {i: list(range(v[0], v[1])) for i, v in enumerate(partitions)}

        # Set data owner of produced data by this RPC, such that downstream RPCs can know
        # whether to fetch these data.
        for dp_idx, (st, ed) in enumerate(partitions):
            for i in range(st, ed):
                for k in rpc.output_data:
                    if k in rpc.output_key_remap:
                        k = rpc.output_key_remap[k]
                    data_owner[sample.indices[i], k] = (rpc.model_name, dp_idx)

        # Get the data owner of this RPC's input data.
        producer_mappings: Dict[Tuple[str, str], List[Tuple[int, int]]] = {}
        for k in rpc.input_data:
            names, dp_indices = [], []
            for buf_idx in sample.indices:
                owner_name, dp_idx = data_owner[(buf_idx, k)]
                names.append(owner_name)
                dp_indices.append(dp_idx)
            assert len(set(names)) == 1
            producer_mapping = defaultdict(list)
            for i, dp_idx in enumerate(dp_indices):
                producer_mapping[dp_idx].append(i)
            producer_mapping = {k: sorted(v) for k, v in producer_mapping.items()}
            producer_mappings[names[0], k] = producer_mapping

        # send partitioned data to model workers
        req_ids, other_req_ids = await scatter_tensor_to_mws(
            rpc=rpc,
            stream=stream,
            msid2mwid=msid2mwid,
            model_topos=model_topos,
            model_configs=model_configs,
            producer_names=producer_names,
            producer_name2producer_handlers=producer_name2producer_handlers,
            producer_mappings=producer_mappings,
            target_mapping=target_mapping,
            buffer_indices=sample.indices,
            seqlens=sample.seqlens,
            handlers=handlers,
        )
        await request_queues[response_coroutine_idx].put((req_ids, other_req_ids, time.perf_counter()))
        response_coroutine_idx = (response_coroutine_idx + 1) % len(request_queues)
        logger.info(f"Model rpc {rpc.name} requested.")


async def model_rpc_reply_func(
    corountine_idx: int,
    rpc: dfg.ModelRPC,
    stream: request_reply_stream.NameResolvingRequstClient,
    buffer: AsyncIOSequenceBuffer,
    model_topos: Dict[str, topology.PipeModelDataParallelTopology],
    ctrl: RPCCorountineControl,
):
    topo = model_topos[rpc.model_name]
    dp_size = topo.get_dim("data")
    dp_head_indices = [topo.get_rank(data=i, pipe=topo.get_dim("pipe") - 1, model=0) for i in range(dp_size)]
    dp_head_handlers = [
        config_pkg.ModelShardID.from_parallelism_rank(rpc.model_name, topo, i) for i in dp_head_indices
    ]

    request_queue = ctrl.request_queues[rpc.name][corountine_idx]
    can_do_rpc = ctrl.can_do_rpc[rpc.name]

    while not ctrl.stop.is_set():
        req_ids, other_req_ids, tik = await request_queue.get()

        # empty requests with parameter synchronization hooks may be issued
        await asyncio.gather(*[
            _awaitable_response(stream, pattern=create_exact_match_pattern([req_id]))
            for req_id in other_req_ids
        ])

        responses = await asyncio.gather(
            *
            [_awaitable_response(stream, pattern=create_exact_match_pattern([req_id])) for req_id in req_ids])
        # logger.info(f"rpc {rpc.name} received responses {req_ids}")

        responses: List[request_reply_stream.Payload] = [responses[i] for i in dp_head_indices]
        recv_tik = time.perf_counter()

        if isinstance(responses[-1].data, dict) and responses[-1].data.get("seqlens") is not None:
            res = []
            for k in responses[0].data["keys"]:
                if k in rpc.output_key_remap:
                    res.append(rpc.output_key_remap[k])
                else:
                    res.append(k)
        else:
            res = dataparallel.PackedParallelDataBroker.gather_from([response.data for response in responses])

        if rpc.log_return_value:
            logger.info(f"RPC name {rpc.name} returns {res}")

        can_do_rpc.release()

        ctrl.rpc_traversal[rpc.name] += 1

        if rpc.is_dst:
            await ctrl.train_count.put(1)
        else:
            buffer_indices = sum([response.data["buffer_indices"] for response in responses], [])
            keys = res
            seqlens = sum([response.data["seqlens"] for response in responses], [])
            await buffer.amend_batch(buffer_indices, [(keys, seqlen) for seqlen in seqlens])

        logger.info(f"Model rpc {rpc.name} finished. Run time {time.perf_counter() - tik:.4f}s.")


async def load_data_func(
    src_rpc_dp_size: int,
    src_rpc_model_name: str,
    buffer: AsyncIOSequenceBuffer,
    data_owner: Dict[Tuple[int, str], Tuple[str, int]],
    stream: request_reply_stream.NameResolvingRequstClient,
    fetch_ctl: asyncio.Queue,
    stop_ctl: asyncio.Event,
):
    # FIXME: estimate average tokens per batch
    # FIXME: change dataset to plain dataset and do dynamic batching here
    while not stop_ctl.is_set():
        await fetch_ctl.get()
        # fetch data from dataloader to fill the sequence buffer
        blogger.info(f"Filling data into the buffer in a new epoch.")
        fetch_data_start = time.perf_counter()
        cur_epoch = latest_epoch = None
        while cur_epoch is None or cur_epoch == latest_epoch:
            data_batches: List[data_api.DataBatch] = await group_rpc_blocked(
                stream,
                handlers=[f"__data{i}__" for i in range(src_rpc_dp_size)],
                handle_type="fetch",
                datas=[None for _ in range(src_rpc_dp_size)],
                verbose=False,
            )

            # Update counters. All starting from 0.
            if cur_epoch is None:
                cur_epoch = latest_epoch = data_batches[0].epoch
            else:
                latest_epoch = data_batches[0].epoch

            # Merge fetched data. We assume fetched data is a flattened dict.
            datas = [x.data for x in data_batches]
            n_seqs = [_get_n_seqs_from_batch_sample(d) for d in datas]
            sample = dataparallel.ParallelDataBroker.gather_from([namedarray.from_dict(x) for x in datas])
            xs, seqlens = split_packed_batch_into_seqs(sample, return_seqlens=True)
            buffer_indices = await buffer.put_batch([(list(x.keys()), seqlen)
                                                     for x, seqlen in zip(xs, seqlens)])
            assert len(buffer_indices) == sum(n_seqs)

            for dp_i, (st, ed) in enumerate(
                    zip([0] + list(itertools.accumulate(n_seqs)), itertools.accumulate(n_seqs))):
                for buf_idx in buffer_indices[st:ed]:
                    for k in sample.keys():
                        data_owner[(buf_idx, k)] = (src_rpc_model_name, dp_i)

            await group_rpc_blocked(
                stream,
                handlers=[f"__data{i}__" for i in range(src_rpc_dp_size)],
                handle_type="store",
                datas=[
                    buffer_indices[st:ed]
                    for st, ed in zip([0] + list(itertools.accumulate(n_seqs)), itertools.accumulate(n_seqs))
                ],
                verbose=False,
            )

        async with buffer.lock:
            buffer.lock.notify(buffer.n_rpcs)

        blogger.info(
            f"Filling data finished. Time consumption: {time.perf_counter() - fetch_data_start:.3f}s.")


async def model_eval_thread_func(
    stream: request_reply_stream.NameResolvingRequstClient,
    handlers: List[config_pkg.ModelShardID],
    eval_queue: asyncio.Queue,
    stop_ctl: asyncio.Event,
):
    while not stop_ctl.is_set():
        epoch, epoch_step = await eval_queue.get()
        eval_stats = dataparallel.ParallelDataBroker.gather_from(await group_rpc_blocked(
            stream, handlers, "evaluate", [None for _ in handlers]))
        logger.info(f"Evaluation results at epoch {epoch + 1} step {epoch_step + 1}: {eval_stats}")


async def model_save_thread_func(
    stream: request_reply_stream.NameResolvingRequstClient,
    handlers: List[config_pkg.ModelShardID],
    model_save_root: str,
    save_queue: asyncio.Queue,
    stop_ctl: asyncio.Event,
):
    while not stop_ctl.is_set():
        epoch, epoch_step, global_step = await save_queue.get()

        # Only save replica ID 0.
        handlers = list(filter(lambda s: s.model_name.replica_id == 0, handlers))

        model_save_dirs = [
            os.path.join(model_save_root, s.model_name.role,
                         f"epoch{epoch}epochstep{epoch_step}globalstep{global_step}") for s in handlers
        ]
        await group_rpc_blocked(stream, handlers, "save", model_save_dirs)
        logger.info(f"Save models at epoch {epoch} step {epoch_step}.")


class MasterWorker(worker_base.Worker):
    os.makedirs(MODEL_SAVE_ROOT, exist_ok=True)

    def _configure(self, config: config_pkg.MasterWorker):
        self.config = config

        self.__model_topos: Dict[ModelName, topology.PipeModelDataParallelTopology] = config.model_topos

        # Build execution graph and initialize concurrency utilities.
        self.__model_rpcs, _ = dfg.build_graph(config.model_rpcs)
        for rpc in self.__model_rpcs:
            _dp_size = self.__model_topos[rpc.model_name].get_dim("data")
            _pp_size = self.__model_topos[rpc.model_name].get_dim("pipe")
            if rpc.min_n_seqs < _dp_size * _pp_size:
                logger.warning(f"The batch size of RPC `{rpc.name}` in terms of #seqs is smaller than "
                               f"dp_size * pp_size ({_dp_size}*{_pp_size}). Forcely enlarge the batch size "
                               f"to {_dp_size * _pp_size} (dp_size * pp_size). (original: {rpc.min_n_seqs})")
                rpc.min_n_seqs_per_dp = 1
                rpc.min_n_seqs = _dp_size * _pp_size

        self.__mwid2msids = defaultdict(list)
        for msid, mwid in self.config.msid2mwid.items():
            self.__mwid2msids[mwid].append(msid)

        self.__rpc_srcs = list(filter(lambda rpc: rpc.is_src, self.__model_rpcs))
        self.__rpc_dsts = list(filter(lambda rpc: rpc.is_dst, self.__model_rpcs))
        self.__n_rpc_srcs = len(self.__rpc_srcs)
        self.__n_rpc_dsts = len(self.__rpc_dsts)

        # Save and eval control.
        self.__total_train_epochs = config.exp_ctrl.total_train_epochs
        self.__save_ctl = timeutil.EpochStepTimeFreqCtl(
            freq_epoch=config.exp_ctrl.save_frequency_epochs,
            freq_step=config.exp_ctrl.save_frequency_steps,
            freq_sec=config.exp_ctrl.save_frequency_seconds,
        )
        self.__eval_ctl = timeutil.EpochStepTimeFreqCtl(
            freq_epoch=config.exp_ctrl.eval_frequency_epochs,
            freq_step=config.exp_ctrl.eval_frequency_steps,
            freq_sec=config.exp_ctrl.eval_frequency_seconds,
        )

        self.MODEL_SAVE_ROOT = os.path.join(
            MODEL_SAVE_ROOT,
            config.worker_info.experiment_name,
            config.worker_info.trial_name,
        )
        os.makedirs(self.MODEL_SAVE_ROOT, exist_ok=True)

        self.__initialized = False
        self._epoch = 0
        self._epoch_step = self._global_step = 0

        # for benchmark
        self.e2e_time_history = []
        self.level_time_history = defaultdict(list)
        self.__benchmark_steps = config.exp_ctrl.benchmark_steps

        return config.worker_info

    def __lazy_init(self):
        # Set up streams.
        handler_routing = copy.deepcopy(self.config.msid2mwid)
        src_rpc = self.__rpc_srcs[0]
        src_rpc_topo = self.config.model_topos[src_rpc.model_name]
        src_rpc_dp_size = src_rpc_topo.get_dim("data")
        src_rpc_pp_size = src_rpc_topo.get_dim("pipe")
        for i in range(src_rpc_dp_size):
            rank = src_rpc_topo.get_rank(data=i, pipe=src_rpc_pp_size - 1, model=0)
            handler_routing[f"__data{i}__"] = self.config.msid2mwid[
                config_pkg.ModelShardID.from_parallelism_rank(model_name=src_rpc.model_name,
                                                              topo=src_rpc_topo,
                                                              parallelism_rank=rank)]
        self.__stream = request_reply_stream.make_master_stream(
            self.config.worker_info,
            n_subscribers=self.config.n_model_workers,
            handler_routing=handler_routing,
        )
        self.__stream: request_reply_stream.NameResolvingRequstClient

        # Request training specification from data workers, e.g. batch size and total train steps.
        p = request_reply_stream.Payload(
            handler="__data0__",
            handle_name="spec",
        )
        self.__stream.post(p)
        self.__stream.poll(block=True, pattern=create_exact_match_pattern([p.syn_reply_id]))
        self.__stream.post(
            request_reply_stream.Payload(handler="__data0__", handle_name="ack", request_id=p.ack_reply_id))
        ft_spec: model_api.FinetuneSpec = self.__stream.poll(block=True,
                                                             pattern=create_exact_match_pattern(
                                                                 [p.request_id])).data
        ft_spec.total_train_epochs = self.config.exp_ctrl.total_train_epochs
        ft_spec.total_train_steps = ft_spec.total_train_epochs * ft_spec.steps_per_epoch

        batch_size = ft_spec.batch_size_per_device
        # logger.info(
        #     "\n\n"
        #     + "=" * 40
        #     + f"\nTotal train epochs: {ft_spec.total_train_epochs}"
        #     + f"\nTotal train steps: {ft_spec.total_train_steps}"
        #     + f"\nSteps per epoch: {ft_spec.steps_per_epoch}"
        #     + f"\nEffective batch size: {batch_size}\n"
        #     + "=" * 40
        #     + "\n"
        # )
        # logger.info(f"ft_spec = {ft_spec}")

        event_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(event_loop)

        self.__all_model_handlers: List[config_pkg.ModelShardID] = []
        self.__dp0_model_handlers: List[config_pkg.ModelShardID] = []
        for model_name, topo in self.config.model_topos.items():
            num_dp = topo.get_dim("data")
            self.__all_model_handlers += [
                config_pkg.ModelShardID.from_parallelism_rank(model_name, topo, j)
                for j in range(topo.world_size())
            ]
            self.__dp0_model_handlers += [
                config_pkg.ModelShardID.from_parallelism_rank(model_name, topo, j)
                for j in topo.filter_match(data=0)
            ]

        # logger.info("before create task initialize")
        self.__model_configs: Dict[ModelName, None | ReaLModelConfig] = {}
        for model_name in self.config.model_topos:
            p = request_reply_stream.Payload(
                handler=config_pkg.ModelShardID.from_parallelism_rank(model_name, topo, 0),
                handle_name="model_config",
            )
            self.__stream.post(p)
            self.__stream.poll(block=True, pattern=create_exact_match_pattern([p.syn_reply_id]))
            self.__stream.post(
                request_reply_stream.Payload(handler=p.handler, handle_name="ack", request_id=p.ack_reply_id))
            self.__model_configs[model_name] = self.__stream.poll(pattern=create_exact_match_pattern(
                [p.request_id]),
                                                                  block=True).data

        _param_senders = [v[0] for v in self.config.sync_param_pairs]
        _param_recevers = [v[1] for v in self.config.sync_param_pairs]
        for model_name, topo in self.config.model_topos.items():
            _handlers = [
                config_pkg.ModelShardID.from_parallelism_rank(model_name, topo, j)
                for j in range(topo.world_size())
            ]
            model_ft_specs = [ft_spec] * topo.world_size()

            if model_name.replica_id > 0:
                assert model_name in _param_recevers
                _param_realloc_src = _param_senders[_param_recevers.index(model_name)]
                _request_parameter_sync(
                    stream=self.__stream,
                    msid2mwid=self.config.msid2mwid,
                    from_model_name=_param_realloc_src,
                    to_model_name=model_name,
                    from_topo=self.config.model_topos[_param_realloc_src],
                    to_topo=self.config.model_topos[model_name],
                    to_model_config=self.__model_configs[model_name],
                )

            _task = event_loop.create_task(
                group_rpc_blocked(
                    self.__stream,
                    handlers=_handlers,
                    handle_type="initialize",
                    datas=model_ft_specs,
                ))
            event_loop.run_until_complete(asyncio.gather(_task))[0]

            if model_name.replica_id > 0:
                # Reversely sync parameters
                _request_parameter_sync(
                    stream=self.__stream,
                    msid2mwid=self.config.msid2mwid,
                    from_model_name=model_name,
                    to_model_name=_param_realloc_src,
                    to_topo=self.config.model_topos[_param_realloc_src],
                    from_topo=self.config.model_topos[model_name],
                    to_model_config=self.__model_configs[_param_realloc_src],
                )

        logger.info("initialize complete")

        self.__rpc_ctrl = RPCCorountineControl(
            stop=asyncio.Event(),
            train_count=asyncio.Queue(maxsize=len(self.__rpc_dsts)),
            fetch_data_queue=asyncio.Queue(1),
            eval_queue=asyncio.Queue(1),
            save_queue=asyncio.Queue(1),
            rpc_traversal={rpc.name: 0
                           for rpc in self.__model_rpcs},
            can_do_rpc={rpc.name: asyncio.Semaphore(rpc.max_concurrent_calls)
                        for rpc in self.__model_rpcs},
            request_queues={
                rpc.name: [asyncio.Queue(1) for _ in range(rpc.max_concurrent_calls)]
                for rpc in self.__model_rpcs
            },
        )

        self.__fetch_master_ctl = asyncio.Queue(1)

        # NOTE: we don't set a maximum buffer size here because we want to keep all data in the buffer
        self.__seqbuffer = AsyncIOSequenceBuffer(
            self.__model_rpcs,
            max_size=int(1e6),
            fetch_ctl=self.__rpc_ctrl.fetch_data_queue,
            fetch_master_ctl=self.__fetch_master_ctl,
        )

        self.__data_owner: Dict[Tuple[int, str], Tuple[str, int]] = {}

        logger.info(f"Creating asyncio coroutines...")

        src_rpc = [rpc for rpc in self.config.model_rpcs if rpc.is_src][0]
        src_rpc_model_name = src_rpc.model_name
        src_rpc_dp_size = self.config.model_topos[src_rpc.model_name].get_dim("data")

        coroutine_tasks = []
        for rpc in self.__model_rpcs:
            # should be a dict: pp_rank to streams

            request_task = event_loop.create_task(
                model_rpc_request_func(
                    rpc=rpc,
                    msid2mwid=self.config.msid2mwid,
                    src_rpc_model_name=src_rpc_model_name,
                    data_owner=self.__data_owner,
                    stream=self.__stream,
                    buffer=self.__seqbuffer,
                    model_topos=self.__model_topos,
                    model_configs=self.__model_configs,
                    ctrl=self.__rpc_ctrl,
                ))
            reply_tasks = []
            for j in range(rpc.max_concurrent_calls):
                _reply_task = event_loop.create_task(
                    model_rpc_reply_func(
                        corountine_idx=j,
                        rpc=rpc,
                        stream=self.__stream,
                        buffer=self.__seqbuffer,
                        model_topos=self.__model_topos,
                        ctrl=self.__rpc_ctrl,
                    ))
                reply_tasks.append(_reply_task)
            coroutine_tasks += [request_task] + reply_tasks

        load_data_task = event_loop.create_task(
            load_data_func(
                src_rpc_dp_size=src_rpc_dp_size,
                src_rpc_model_name=src_rpc_model_name,
                data_owner=self.__data_owner,
                buffer=self.__seqbuffer,
                stream=self.__stream,
                fetch_ctl=self.__rpc_ctrl.fetch_data_queue,
                stop_ctl=self.__rpc_ctrl.stop,
            ))
        eval_task = event_loop.create_task(
            model_eval_thread_func(
                stream=self.__stream,
                handlers=self.__all_model_handlers,
                eval_queue=self.__rpc_ctrl.eval_queue,
                stop_ctl=self.__rpc_ctrl.stop,
            ))
        save_task = event_loop.create_task(
            model_save_thread_func(
                stream=self.__stream,
                handlers=self.__all_model_handlers,
                model_save_root=self.MODEL_SAVE_ROOT,
                save_queue=self.__rpc_ctrl.save_queue,
                stop_ctl=self.__rpc_ctrl.stop,
            ))
        coroutine_tasks += [load_data_task, eval_task, save_task]

        # self.__event_loop = event_loop
        # self.__coroutine_tasks = coroutine_tasks

        # Set up a run context of EventLoop.run_util_complete, baiscally copy-paste from cpython.
        # With this context, we can call the non-block EventLoop._run_once (similar to worker._poll).
        self.__asyncio_ctx = setup_run_until_complete(event_loop, asyncio.gather(*coroutine_tasks))

        logger.info(f"asyncio coroutines created, master worker ready to run.")

        self.__initialized = True
        self._train_start_time = time.perf_counter()

        self.__clear_data_cache_reqids = None

    def _poll(self):
        if not self.__initialized:
            self.__lazy_init()

        # Main execution steps. The graph runs under-the-hood in RPC & stream threads.
        # Wait for the finish of the tranverse of the execution graph.
        execution_start = time.perf_counter()
        logger.info("Master worker is waiting for the finish of the execution graph.")
        _rpc_dst_cnt = 0
        while _rpc_dst_cnt < self.__n_rpc_dsts:
            try:
                self.__rpc_ctrl.train_count.get_nowait()
                _rpc_dst_cnt += 1
                continue
            except asyncio.QueueEmpty:
                pass
            try:
                # Similar to worker._poll. Run multiple times until a train step is finished.
                self.__asyncio_ctx.loop._run_once()
                # NOTE: The following line will propagate errors in corountines back to the main thread.
                # It raises asyncio.exceptions.InvalidStateError if the result is not ready.
                # (In our use cases, the result will never be ready because corountines run while-loops.)
                # We just ignore this error and continue running.
                self.__asyncio_ctx.future.result()
            except asyncio.exceptions.InvalidStateError:
                # Catch the exception when future.result() is not ready.
                pass
            except:
                raise_asyncio_exception(self.__asyncio_ctx)
        logger.info("Execution finished!")

        try:
            self.__fetch_master_ctl.get_nowait()
            is_new_epoch = True
        except asyncio.QueueEmpty:
            is_new_epoch = False

        should_eval = self.__eval_ctl.check(epochs=int(is_new_epoch), steps=1)
        should_save = self.__save_ctl.check(epochs=int(is_new_epoch), steps=1)

        if is_new_epoch:
            self._epoch += 1
            self._epoch_step = 0

        self._epoch_step += 1
        self._global_step += 1

        if should_eval:
            self.__rpc_ctrl.eval_queue.put_nowait((self._epoch, self._epoch_step))
        if should_save:
            self.__rpc_ctrl.save_queue.put_nowait((self._epoch, self._epoch_step, self._global_step))

        if is_new_epoch:
            if self._epoch > self.__total_train_epochs:
                self.experiment_complete_exit(f"Training completes! Yeah!!!")

        total_time_consumption = time.perf_counter() - self._train_start_time
        time_per_step = total_time_consumption / (self._global_step + 1)
        e2e_time = time.perf_counter() - execution_start
        self.e2e_time_history.append(e2e_time)

        # calculate flops
        #########################################
        from reallm.base.monitor import (caculuate_llama_forward_flops, calculate_llama_gen_flops,
                                         calculate_llama_train_flops)

        flops = 0
        for train_bs, train_seqlens, real_config in zip(
                self.__rpc_ctrl.data_amount.train_bs,
                self.__rpc_ctrl.data_amount.train_seqlens,
                self.__rpc_ctrl.data_amount.train_configs,
        ):
            flops += calculate_llama_train_flops(
                checkpoint_activations_factor=4,
                batch_size=train_bs,
                seqlens=train_seqlens,
                num_layers=real_config.n_layers,
                hidden_size=real_config.hidden_dim,
                intermediate_size=real_config.intermediate_dim,
                vocab_size=real_config.vocab_size,
            )
        for inf_bs, inf_seqlens, real_config in zip(
                self.__rpc_ctrl.data_amount.inf_bs,
                self.__rpc_ctrl.data_amount.inf_seqlens,
                self.__rpc_ctrl.data_amount.inf_configs,
        ):
            flops += caculuate_llama_forward_flops(
                batch_size=inf_bs,
                seqlens=inf_seqlens,
                num_layers=real_config.n_layers,
                hidden_size=real_config.hidden_dim,
                intermediate_size=real_config.intermediate_dim,
                vocab_size=real_config.vocab_size,
            )
        for gen_bs, prompt_lens, gen_len, real_config in zip(
                self.__rpc_ctrl.data_amount.gen_bs,
                self.__rpc_ctrl.data_amount.prompt_lens,
                self.__rpc_ctrl.data_amount.gen_len,
                self.__rpc_ctrl.data_amount.gen_configs,
        ):
            flops += calculate_llama_gen_flops(
                batch_size=gen_bs,
                prompt_lens=prompt_lens,
                gen_len=gen_len,
                num_layers=real_config.n_layers,
                hidden_size=real_config.hidden_dim,
                intermediate_size=real_config.intermediate_dim,
                vocab_size=real_config.vocab_size,
            )
        self.__rpc_ctrl.data_amount.clear()
        tflops = flops / (e2e_time * (10**12))
        tflops_per_gpu = flops / (e2e_time * self.config.n_model_workers * (10**12))
        #########################################

        # TODO: add time estimation
        logger.info(
            f"Epoch {self._epoch}/{self.config.exp_ctrl.total_train_epochs} "
            f"step {self._epoch_step} "
            f"(global step {self._global_step}) finishes. "
            f"#End to end# execution time: *{e2e_time:.3f}*s. "
            f"Total time consumption: {total_time_consumption:.3f}s. TFLOP/s per GPU: {tflops_per_gpu:.2f}, total TFLOP/s: {tflops:.2f}."
            # f"Estimated remaining time of this epoch: {self._buffer_size_tokens / buffer_size_decre_per_step * time_per_step:.3f}s."
        )

        if self.__benchmark_steps is not None and self._global_step >= self.__benchmark_steps:
            logger.info(
                f"Finished benchmark {self.__benchmark_steps}. Total time consumption {total_time_consumption:.3f}"
            )
            logger.info(f"avg #e2e# time *{np.mean(self.e2e_time_history):.3f}*")
            for i, level_time_history in self.level_time_history.items():
                logger.info(f"avg #level{i+1}# time *{np.mean(level_time_history):.3f}*")
            self.experiment_complete_exit(f"Benchmark completes! Yeah!!!")

        if self.__clear_data_cache_reqids is not None:
            [
                self.__stream.poll(block=True, pattern=create_exact_match_pattern([reqid]))
                for reqid in self.__clear_data_cache_reqids
            ]
        self.__clear_data_cache_reqids = request_all(
            self.__stream,
            [vs[0] for vs in self.__mwid2msids.values()],
            "clear_data_cache",
            [self.__rpc_ctrl.training_buffer_indices for _ in self.__all_model_handlers],
        )
        self.__rpc_ctrl.training_buffer_indices.clear()

        return worker_base.PollResult(sample_count=1, batch_count=1)

    def experiment_complete_exit(self, msg: str):
        self.__rpc_ctrl.stop.set()
        self.__asyncio_ctx.loop.stop()
        try:
            teardown_run_util_complete(self.__asyncio_ctx)
        except RuntimeError as e:
            raise ExperimentComplete(msg) from e