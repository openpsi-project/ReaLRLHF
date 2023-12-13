import argparse
import os
import shutil

from deepspeed.runtime import utils as ds_utils
import torch
import torch.nn as nn

from impl.model.nn.flash_mqat.flash_mqat_base import *
from impl.model.nn.flash_mqat.flash_mqat_parallel import *
from impl.model.utils.pipeline_module import LayerSpec
from impl.model.utils.save_load import save_to_disk
import base.constants

MODEL_CONFIG_FILES = [
    "config.json",
    "generation_config.json",
    "tokenizer_config.json",
    "vocab.json",
    "merges.txt",
    "special_tokens_map.json",
    "tokenizer.json",
]


def get_layer_specs(config: FlashMQATConfig, to_critic, is_mp):
    layer_specs = []
    # vocab pos embedding
    if not is_mp:
        embedding_layer = LayerSpec(VocabPositionEmbedding, config, dtype=None, device=None)
        layer_specs.append(embedding_layer)

        for i in range(config.n_layers):
            flash_mqat_block = LayerSpec(
                FlashMQATBlock,
                config,
                layer_index=i,
                output_layernorm=(i == config.n_layers - 1),
                ckpt_attn=(i > 0 and config.ckpt_attn),
                ckpt_mlp=(i > 0 and config.ckpt_mlp),
                dtype=None,
                device=None,
            )
            layer_specs.append(flash_mqat_block)
    else:
        embedding_layer = LayerSpec(ParallelVocabPositionEmbedding, config, dtype=None, device=None)
        layer_specs.append(embedding_layer)
        for i in range(config.n_layers):
            flash_mqat_block = LayerSpec(
                ParallelFlashMQATBlock,
                config,
                layer_index=i,
                output_layernorm=(i == config.n_layers - 1),
                ckpt_attn=(i > 0 and config.ckpt_attn),
                ckpt_mlp=(i > 0 and config.ckpt_mlp),
                dtype=None,
                device=None,
            )
            layer_specs.append(flash_mqat_block)

    head = LayerSpec(
        OutputHead,
        config.hidden_dim,
        config.vocab_size if not to_critic else 1,
        bias=False,
        device=None,
        dtype=None,
    )
    layer_specs.append(head)

    return layer_specs


def count_layer_params(layer_specs):
    param_counts = [0] * len(layer_specs)
    for idx, layer in enumerate(layer_specs):
        if isinstance(layer, LayerSpec):
            l = layer.build()
            params = filter(lambda p: p.requires_grad, l.parameters())
            param_counts[idx] = sum(p.numel() for p in params)
        elif isinstance(layer, nn.Module):
            params = filter(lambda p: p.requires_grad, layer.parameters())
            param_counts[idx] = sum(p.numel() for p in params)
        print(f"count_layer_params build layer {layer.typename.__name__}")
    return param_counts


def partition_layers(layer_specs, num_stages, method="uniform"):
    # Each stage gets a simple uniform number of layers.
    parts = None
    if method == "uniform":
        num_layers = len(layer_specs)
        parts = ds_utils.partition_uniform(num_items=num_layers, num_parts=num_stages)
    elif method == "parameters":
        param_counts = count_layer_params(layer_specs)
        parts = ds_utils.partition_balanced(weights=param_counts, num_parts=num_stages)
    else:
        raise NotImplementedError(f"Partitioning method {method} not implemented.")

    stage_to_layer_idx = {}
    for stage in range(num_stages):
        start = parts[stage]
        stop = parts[stage + 1]
        print(f"stage={stage} layers={stop - start}")
        for idx, layer in enumerate(layer_specs[start:stop]):
            name = str(layer)
            if isinstance(layer, LayerSpec):
                name = layer.typename.__name__
            if isinstance(layer, nn.Module):
                name = layer.__class__.__name__
            else:
                try:
                    name = layer.__name__
                except AttributeError:
                    pass
            print(f"    {idx+start:2d}: {name}")
        stage_to_layer_idx[stage] = (start, stop)
    return stage_to_layer_idx


def split_state_dict_by_stage(state_dict, stage_to_layer_idx):
    stage_to_state_dict = {}
    for stage, (start, stop) in stage_to_layer_idx.items():
        stage_state_dict = {}
        for k, v in state_dict.items():
            for i in range(start, stop):
                if k.startswith(f"{i}."):
                    stage_state_dict[k] = v
                    print(f"stage {stage} k={k}")
                    break
        stage_to_state_dict[stage] = stage_state_dict
    return stage_to_state_dict


def save_state_dict(state_dict, stage_index, mp_rank, shard_index, model_dir):
    os.makedirs(model_dir, exist_ok=True)
    output_fn = f"model-pp-{stage_index:02d}-mp-{mp_rank:02d}-s-{shard_index:02d}.safetensors"
    save_to_disk(state_dict, model_dir, output_fn=output_fn, save_type="st", n_shards=1, no_shard_suffix=True)
    print(
        f"saved {state_dict.keys()} to {model_dir}/model-pp-{stage_index:02d}-mp-00-s-{shard_index:02d}.safetensors"
    )
    print(f"saved {state_dict.keys()} to "
          f"{model_dir}/pytorch_model-pp-{stage_index:02d}-mp-{mp_rank:02d}-s-{shard_index:02d}.bin")


def fit_state_dict_to_critic(num_layers, state_dict):
    # modify last layer shape
    for k, v in state_dict.items():
        if k.startswith(f"{num_layers-1}."):
            print(f"last layer key {k} tensor shape {v.shape}")
            state_dict[k] = v[0].unsqueeze(0)
            print(f"critic head shape {state_dict[k].shape}")
    return state_dict


def copy_configs(src_model_dir, dst_model_dir):
    for file in MODEL_CONFIG_FILES:
        try:
            shutil.copy(os.path.join(src_model_dir, file), os.path.join(dst_model_dir, file))
            print(f"copied {file} from {src_model_dir} to {dst_model_dir}")
        except FileNotFoundError:
            print(f"{file} not exist in {src_model_dir} skipping.")


def split_state_dict_into_shards(state_dict, n_shards):
    if n_shards == 1:
        return [state_dict]

    keys = list(state_dict.keys())
    if len(keys) < n_shards:
        raise ValueError(f"state_dict has {len(keys)} keys, but n_shards={n_shards}")

    shard_size = len(keys) // n_shards
    extra = len(keys) % n_shards
    shard_size_list = [shard_size for _ in range(n_shards)]
    shard_size_list[-1] = shard_size + extra
    start, shards = 0, []
    for i, size in enumerate(shard_size_list):
        shard = {}
        for j in range(start, start + size):
            shard[keys[j]] = state_dict[keys[j]]
            # print(f"shard {i} key {keys[j]}")
        start += size
        shards.append(shard)
    return shards


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model_dir",
                        type=str,
                        default="/lustre/public/pretrained_model_weights/Llama-2-7b-hf")
    parser.add_argument("--model_type", type=str, default="llama")
    parser.add_argument("--num_pp", type=int, default=8)
    parser.add_argument("--num_mp", type=int, default=1)
    parser.add_argument("--num_shards", type=int, default=3)
    parser.add_argument("--output_dir", type=str, default=None)
    parser.add_argument(
        "--to_critic",
        action="store_true",
        help="transform actor model to critic model by changing the last layer, only for test purposes.")
    args = parser.parse_args()

    assert args.num_mp > 1 or args.num_pp > 1
    if args.output_dir is None:
        model_name = args.model_dir.split("/")[-1]
        if args.num_mp == 1:
            output_dir = f"{model_name}_{args.num_pp}pp_{args.num_shards}s"
        elif args.num_pp == 1:
            output_dir = f"{model_name}_{args.num_mp}mp_{args.num_shards}s"
        else:
            output_dir = f"{model_name}_{args.num_pp}pp_{args.num_mp}mp_{args.num_shards}s"
        default_save_root = "/lustre/public/pretrained_model_weights/sharded"
        output_dir = os.path.join(default_save_root, output_dir)
    else:
        output_dir = args.output_dir

    # TODO: load and process full statedict by shard for large model that can not fit into memory
    cfg = None
    base.constants.set_fake_mp_world_size(args.num_mp)
    base.constants.set_fake_mp_rank(0)
    if args.num_mp > 1:
        args.model_type = f"parallel_{args.model_type}"
        cfg, state_dict = getattr(FlashMQATModel, f"config_and_param_from_{args.model_type}")(
            model_path=args.model_dir, load_model_parallel_as_list=True)
        state_dict_list = [{k: v[mp_rank] for k, v in state_dict.items()} for mp_rank in range(args.num_mp)]
    else:
        cfg, state_dict = getattr(FlashMQATModel,
                                  f"config_and_param_from_{args.model_type}")(model_path=args.model_dir)
        state_dict_list = [state_dict]

    if args.num_pp > 1:
        layer_specs = get_layer_specs(cfg, args.to_critic, is_mp=args.num_mp > 1)
        state_dict_list = [FlashMQATModel.map_to_pipe_state_dict(cfg, sd) for sd in state_dict_list]
        if args.to_critic:
            state_dict_list = [fit_state_dict_to_critic(len(layer_specs), sd) for sd in state_dict_list]
        print("loaded full state_dict")
        stage_to_layer_idx = partition_layers(layer_specs, num_stages=args.num_pp, method="parameters")
        stage_to_state_dict_list = [
            split_state_dict_by_stage(sd, stage_to_layer_idx) for sd in state_dict_list
        ]
        for mp_rank, stage_to_state_dict in enumerate(stage_to_state_dict_list):
            for stage, state_dict in stage_to_state_dict.items():
                shards = split_state_dict_into_shards(state_dict, args.num_shards)
                # print(f"stage {stage} state_dict keys: {state_dict.keys()}")
                for shard_index, shard in enumerate(shards):
                    save_state_dict(shard, stage, mp_rank, shard_index, output_dir)
    elif args.num_pp == 1:
        for mp_rank, state_dict in enumerate(state_dict_list):
            shards = split_state_dict_into_shards(state_dict, args.num_shards)
            # print(f"state_dict keys: {state_dict.keys()}")
            for shard_index, shard in enumerate(shards):
                save_state_dict(shard, 0, mp_rank, shard_index, output_dir)

    copy_configs(args.model_dir, output_dir)


if __name__ == "__main__":
    main()
