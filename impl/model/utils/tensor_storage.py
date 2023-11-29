from collections import defaultdict
from typing import Any, List, Optional, Tuple

import torch

from impl.model.utils.data import PipeCacheData, PipeTransferData
import base.logging as logging
import impl.model.utils.p2p as p2p

logger = logging.getLogger("tensor_utils")


def get_shape(tensor):
    return tensor.shape if torch.is_tensor(tensor) else None


def print_data_shapes(name, rank, mbid, x, ys):
    if rank == 0:
        logger.debug(f"{name}: rank {rank} mbid {mbid}")
        logger.debug(f"shapes: x.pp_input {get_shape(x.pp_input)}, x.pp_output {get_shape(x.pp_output)},"
                     f" x.cu_seqlens {get_shape(x.cu_seqlens)}")
        for i, y in enumerate(ys):
            logger.debug(f"shapes: ys[{i}].input_ids {get_shape(y.input_ids)}, "
                         f"ys[{i}].k_cache {get_shape(y.k_cache)}, ys[{i}].v_cache {get_shape(y.v_cache)}, "
                         f"ys[{i}].cache_seqlens {get_shape(y.cache_seqlens)}")


class TensorBuffer:
    # could store both tensors and other data

    def __init__(self):
        self.tensors = defaultdict(dict)

    def put(self, name: str, mbid: int, x: torch.Tensor):
        self.tensors[name][mbid] = x

    def alloc(self,
              name: str,
              mbid: int,
              shape: Tuple[int],
              dtype: torch.dtype,
              device: torch.device,
              require_grads: bool = False):
        self.tensors[name][mbid] = torch.zeros(shape, dtype=dtype, device=device, requires_grad=require_grads)

    def get(self, name: str, mbid: int, remove: bool = False):
        if remove:
            return self.tensors[name].pop(mbid)
        else:
            return self.tensors[name][mbid]

    def remove(self, name: str, mbid: Optional[int] = None, check_exists: bool = False):
        try:
            if mbid is None:
                del self.tensors[name]
            else:
                self.tensors[name].pop(mbid)
        except KeyError:
            if not check_exists:
                return
            raise KeyError(f"TensorBuffer.remove: key {name} mbid {mbid} not found")

    def check_name(self, name: str):
        return name in self.tensors

    def check_mbid(self, name: str, mbid: int):
        if name not in self.tensors:
            return False
        return mbid in self.tensors[name]

    def clear(self):
        self.tensors = defaultdict(dict)


def send_grad(grad: torch.Tensor, dst_stage: int):
    p2p.send(grad, dst_stage)


def recv_grad(buf: torch.Tensor, src_stage: int):
    p2p.recv(buf, src_stage)
    return buf
