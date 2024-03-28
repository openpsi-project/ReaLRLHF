import base.constants
import base.namedarray


def random_sample(bs, seq_len, vocab_size):
    from flash_attn.bert_padding import unpad_input
    import torch

    input_ids = torch.randint(0, vocab_size, (bs, seq_len), dtype=torch.long)
    attention_mask = torch.ones_like(input_ids)
    packed_input_ids, _, cu_seqlens, max_seqlen = unpad_input(input_ids, attention_mask)
    prompt_mask = torch.zeros_like(packed_input_ids)
    data = base.namedarray.NamedArray(
        packed_input_ids=packed_input_ids,
        cu_seqlens=cu_seqlens,
        prompts=input_ids,
        prompt_mask=prompt_mask.bool(),
        prompt_att_mask=attention_mask,
    )
    return data


def find_factors(n):
    factors = []
    for i in range(1, n + 1):
        if n % i == 0:
            factors.append(i)
    return factors


def make_stats_key(rpc_name, bs, seq_len):
    return f"{rpc_name}|{bs}|{seq_len}"


def parse_stats_key(key):
    rpc_name, bs, seq_len = key.split("|")
    return rpc_name, int(bs), int(seq_len)