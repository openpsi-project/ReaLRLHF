from typing import Callable, Dict, List, Optional
import itertools
import json

import numpy as np
import torch
import torch.utils.data

from base.datapack import ffd_with_result_unsorted, min_abs_diff_partition
import api.data


class RewardModelingPackedPairedDataset(torch.utils.data.IterableDataset):

    def __init__(
        self,
        util: api.data.DatasetUtility,
        max_length: int,
        n_tokens_per_batch: int,
        max_pairs_per_prompt: Optional[int] = None,
        dataset_path: Optional[str] = None,
        dataset_builder: Optional[Callable[[], List[Dict]]] = None,
    ):
        """Dataset used for reward modeling. Each sample consists of a prompt, several positive answers, and several negative answers.

        Args:
            util (api.data.DatasetUtility): Dataset utility.
            max_length (int): The maximum sequence length. Sequences will be truncated to this length.
            dataset_path (Optional[str], optional): Path to the dataset json/jsonl file.
                The json/jsonl file should be a list of dictionary. Each element in the list should have
                the key "prompt", "pos_answers", and "neg_answers". Each "pos_answer" must correspond to
                one and only one "neg_answer" (i.e., they are one-to-one pairs). Defaults to None.
            dataset_builder (Optional[Callable[[], List[Dict]]], optional): Alternative to dataset_path.
                A callable that returns a list of dictionary. Defaults to None.
        """
        self.util = util
        tokenizer = self.util.tokenizer

        self.rng = np.random.RandomState(seed=util.seed)

        if dataset_path is not None:
            if dataset_path.endswith(".jsonl"):
                with open(dataset_path, 'r') as f:
                    data = [json.loads(ff) for ff in f]
            elif dataset_path.endswith(".json"):
                with open(dataset_path, 'r') as f:
                    data = json.load(f)
            else:
                raise NotImplementedError(f"Unkown dataset extension: {dataset_path}")
        else:
            assert dataset_builder is not None
            data = dataset_builder()

        shuffle_indices = api.data.get_shuffle_indices(util.seed, len(data))
        data = [data[i] for i in shuffle_indices]
        for x in data:
            for pa, na in zip(x['pos_answers'], x['neg_answers']):
                if pa.startswith(x['prompt']) or na.startswith(x['prompt']):
                    raise ValueError("Answers should not start with prompt.")

        prompts = [x['prompt'] for x in data]
        if max_pairs_per_prompt is not None:
            pos_answers = [[
                x['prompt'] + c + tokenizer.eos_token
                for c in self.rng.choice(x['pos_answers'], max_pairs_per_prompt)
            ] for x in data]
            neg_answers = [[
                x['prompt'] + c + tokenizer.eos_token
                for c in self.rng.choice(x['neg_answers'], max_pairs_per_prompt)
            ] for x in data]
        else:
            pos_answers = [[x['prompt'] + c + tokenizer.eos_token for c in x['pos_answers']] for x in data]
            neg_answers = [[x['prompt'] + c + tokenizer.eos_token for c in x['neg_answers']] for x in data]

        for a, b in zip(pos_answers, neg_answers):
            if len(a) != len(b):
                raise RuntimeError("pos_answers and neg_answers must be one-to-one pairs.")
            if len(a) == 0:
                raise RuntimeError("pos_answers and neg_answers must be non-empty.")

        group_sizes = [len(x) for x in pos_answers]

        _prompt_tokens = tokenizer(
            prompts,
            max_length=max_length,
            return_length=True,
            return_attention_mask=False,
            padding=False,
            truncation=True,
        )
        prompt_lens = _prompt_tokens['length']

        _answer_tokens = tokenizer(
            list(itertools.chain.from_iterable(pos_answers + neg_answers)),
            max_length=max_length,
            return_length=True,
            return_attention_mask=False,
            padding=False,
            truncation=True,
        )

        pos_answer_tokens = []
        neg_answer_tokens = []
        offset = 0
        for g in group_sizes:
            pos_answer_tokens.append({k: v[offset:offset + g] for k, v in _answer_tokens.items()})
            neg_answer_tokens.append({
                k: v[sum(group_sizes) + offset:sum(group_sizes) + offset + g]
                for k, v in _answer_tokens.items()
            })
            offset += g

        group_token_lengths = [
            sum(x['length']) + sum(y['length']) for x, y in zip(pos_answer_tokens, neg_answer_tokens)
        ]

        start, end = min_abs_diff_partition(group_token_lengths, util.world_size)[util.ddp_rank]

        self.pos_answer_tokens = pos_answer_tokens[start:end]
        self.neg_answer_tokens = neg_answer_tokens[start:end]
        self.prompt_lens = prompt_lens[start:end]
        self.group_sizes = group_sizes[start:end]
        self.group_posneg_seqlens = [
            sum(x['length']) + sum(y['length'])
            for x, y in zip(self.pos_answer_tokens, self.neg_answer_tokens)
        ]

        self.n_tokens_per_batch = n_tokens_per_batch
        self.shuffle_cnt = 0

        self._shuffle()
        assert all(seq <= self.n_tokens_per_batch
                   for seq in self.group_posneg_seqlens), max(self.group_posneg_seqlens)
        self.__batch_indices = ffd_with_result_unsorted(np.array(self.group_posneg_seqlens),
                                                        self.n_tokens_per_batch)
        self.rng.shuffle(self.__batch_indices)

    def _shuffle(self):
        shuffle_indices = api.data.get_shuffle_indices(
            self.util.seed + self.shuffle_cnt * 7 + self.util.ddp_rank * 3, len(self.group_posneg_seqlens))
        self.pos_answer_tokens = [self.pos_answer_tokens[i] for i in shuffle_indices]
        self.neg_answer_tokens = [self.neg_answer_tokens[i] for i in shuffle_indices]
        self.group_posneg_seqlens = [self.group_posneg_seqlens[i] for i in shuffle_indices]
        self.group_sizes = [self.group_sizes[i] for i in shuffle_indices]
        self.prompt_lens = [self.prompt_lens[i] for i in shuffle_indices]
        self.shuffle_cnt += 1

    def __len__(self):
        return len(self.__batch_indices)

    def __iter__(self):
        for indices in self.__batch_indices:
            prompt_lens = [self.prompt_lens[i] for i in indices]
            group_pos_answers = [self.pos_answer_tokens[i]['input_ids'] for i in indices]
            group_neg_answers = [self.neg_answer_tokens[i]['input_ids'] for i in indices]
            pos_answers = list(itertools.chain.from_iterable(group_pos_answers))
            neg_answers = list(itertools.chain.from_iterable(group_neg_answers))
            pair_seqlens = [len(x) for x in itertools.chain.from_iterable(zip(pos_answers, neg_answers))]
            seqlens = [len(x) + len(y) for x, y in zip(pos_answers, neg_answers)]
            group_sizes = [self.group_sizes[i] for i in indices]

            # sanity checks
            for j, s in enumerate(seqlens):
                assert s == pair_seqlens[2 * j] + pair_seqlens[2 * j + 1], (s, pair_seqlens[2 * j] +
                                                                            pair_seqlens[2 * j + 1])
            for pa, na, g in zip(group_pos_answers, group_neg_answers, group_sizes):
                assert len(pa) == len(na) == g, (len(pa), len(na), g)
            total_seqlen = sum(seqlens)
            assert total_seqlen <= self.n_tokens_per_batch, (total_seqlen, self.n_tokens_per_batch)

            packed_input_ids = torch.cat(
                [torch.tensor(p) for p in itertools.chain.from_iterable(zip(pos_answers, neg_answers))])
            group_factor = torch.tensor([1 / g for _ in range(g) for g in group_sizes], dtype=torch.float32)
            prompt_lens = torch.tensor([x for _ in range(g) for x, g in zip(prompt_lens, group_sizes)],
                                       dtype=torch.int32)

            assert prompt_lens.shape[0] == len(seqlens)
            assert packed_input_ids.shape[0] == sum(pair_seqlens) == sum(seqlens), (packed_input_ids.shape[0],
                                                                                    sum(pair_seqlens),
                                                                                    sum(seqlens))
            yield dict(
                packed_input_ids=packed_input_ids,
                input_lens=torch.tensor(seqlens, dtype=torch.int32),
                pair_input_lens=torch.tensor(pair_seqlens, dtype=torch.int32),
                group_factor=group_factor,
                prompt_lens=prompt_lens,
            )
        self._shuffle()
        self.__batch_indices = ffd_with_result_unsorted(np.array(self.group_posneg_seqlens),
                                                        self.n_tokens_per_batch)
        self.rng.shuffle(self.__batch_indices)


if __name__ != "__main__":
    api.data.register_dataset("packed_rw_pair", RewardModelingPackedPairedDataset)
else:
    import transformers

    from base.dataparallel import PackedParallelDataBroker
    from base.namedarray import from_dict

    def have_common_prefix_at_least(a, b, n):
        return (a[:n] == b[:n]).all()

    tokenizer = transformers.AutoTokenizer.from_pretrained("/lustre/fw/pretrained/gpt2-large")
    ddp_rank = 0
    world_size = 1
    seed = 1

    util = api.data.DatasetUtility(tokenizer=tokenizer, ddp_rank=ddp_rank, world_size=world_size, seed=seed)

    dataset = RewardModelingPackedPairedDataset(
        util,
        max_length=1024,
        n_tokens_per_batch=16384,
        dataset_path="/lustre/fw/datasets/imdb/rl/rm_paired-valid.jsonl",
        max_pairs_per_prompt=10,
    )
    dataloader = torch.utils.data.DataLoader(dataset, batch_size=None)
    for _ in range(5):
        print("dataset iteration")
        for x in dataloader:
            datas = PackedParallelDataBroker.scatter_to(from_dict(x), 8)
            for data in datas:
                assert len(
                    data['pair_input_lens']) == len(data['input_lens']) * 2 == len(data['group_factor']) * 2
                assert data['packed_input_ids'].shape[0] == sum(data['pair_input_lens']) == sum(
                    data['input_lens']), (data['packed_input_ids'].shape[0], sum(data['pair_input_lens']),
                                          sum(data['input_lens']))
                offset = 0
                for i in range(len(data['input_lens'])):
                    len1, len2 = data['pair_input_lens'][2 * i], data['pair_input_lens'][2 * i + 1]
                    a = data['packed_input_ids'][offset:offset + len1]
                    b = data['packed_input_ids'][offset + len1:offset + len1 + len2]
                    assert have_common_prefix_at_least(a, b, 8), (a, b)
                    offset += len1 + len2
            continue