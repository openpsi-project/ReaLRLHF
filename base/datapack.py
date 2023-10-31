from typing import List, Tuple

import numba
import numpy as np


@numba.njit
def ffd_check(a: np.ndarray, c: int, n: int) -> bool:
    """Check if a[] could fit in n bins with capacity c.

    Adopted from https://github.com/imoneoi/openchat/blob/cd99f6366d243daf1a906b24f9475a811a90aaff/ochat/training_deepspeed/multipack_dataloader.py
     
    First-fit-decreasing bin packing.
    https://en.wikipedia.org/wiki/First-fit-decreasing_bin_packing
    
    Args:
        a (np.ndarray): _description_
        c (int): Capacity of bins.
        n (int): Number of bins.

    Returns:
        bool: .
    """

    a = np.sort(a)[::-1]
    bins = np.full((n,), c, dtype=a.dtype)
    for size in a:
        not_found = True
        for idx in range(n):
            if bins[idx] >= size:
                bins[idx] -= size
                not_found = False
                break

        if not_found:
            return False

    return True


@numba.njit
def ffd_with_result(a: np.ndarray, c: int) -> List[List[int]]:
    """First-fit-decreasing bin packing.

    Adopted from https://github.com/imoneoi/openchat/blob/cd99f6366d243daf1a906b24f9475a811a90aaff/ochat/training_deepspeed/multipack_dataloader.py
    
    Args:
        a (np.ndarray): Items to be packed.
        c (int): Capacity of each bin.

    Returns:
        List[List[int]]: Packed bins with item indices inside each bin.
    """

    indices = np.argsort(a)[::-1]
    a = a[indices]

    bins = []
    bins_result = []
    for a_id, size in enumerate(a):
        add_new = True
        for idx in range(len(bins)):
            if bins[idx] >= size:
                bins[idx] -= size
                bins_result[idx].append(indices[a_id])
                add_new = False
                break

        if add_new:
            bins.append(c - size)
            bins_result.append([indices[a_id]])

    return bins_result


@numba.njit
def ffd_with_result_unsorted(a: np.ndarray, c: int) -> List[List[int]]:
    """First-fit-decreasing bin packing.

    Args:
        a (np.ndarray): Items to be packed.
        c (int): Capacity of each bin.

    Returns:
        List[List[int]]: Packed bins with item indices inside each bin.
    """
    bins = []
    bins_result = []
    for a_id, size in enumerate(a):
        add_new = True
        for idx in range(len(bins)):
            if bins[idx] >= size:
                bins[idx] -= size
                bins_result[idx].append(a_id)
                add_new = False
                break

        if add_new:
            bins.append(c - size)
            bins_result.append([a_id])

    return bins_result


@numba.njit
def allocate(lengths: np.ndarray, lengths_cumsum: np.ndarray, c: int, rank: int,
             world_size: int) -> List[List[int]]:
    """Dynamic batch allocator, similar to Multifit, https://en.wikipedia.org/wiki/Multifit_algorithm.

    Adopted from https://github.com/imoneoi/openchat/blob/cd99f6366d243daf1a906b24f9475a811a90aaff/ochat/training_deepspeed/multipack_dataloader.py
    
    Args:
        lengths (np.ndarray): Sequence lengths.
        lengths_cumsum (np.ndarray): Cumulative summation of sequence length.
        c (int): Capacity of each bin, i.e., # tokens of each batch.
        rank (int): Data parallel rank.
        world_size (int): Data parallel world size.

    Returns:
        List[List[int]]: Packed bins with item indices inside each bin.
    """

    s = 0
    start_index = 0
    result = []

    while True:
        # binary search [l, r)
        l = 1
        r = 1 + np.searchsorted(lengths_cumsum[start_index:], s + c * world_size, "right")

        while r - l > 1:
            m = (l + r) // 2
            if ffd_check(lengths[start_index:start_index + m], c, world_size):
                l = m
            else:
                r = m

        # use length l
        batch = ffd_with_result(lengths[start_index:start_index + l], c)
        for i in range(len(batch)):
            batch[i] = [start_index + x for x in batch[i]]

        if len(batch) < world_size:
            break

        start_index += l
        s = lengths_cumsum[start_index - 1]

        # add local rank
        result.append(batch[rank])

    return result


@numba.njit
def min_abs_diff_partition(arr: np.ndarray, k: int) -> List[Tuple[int]]:
    """Partition an array into k subarrays with minimum absolute difference of sums.
    
    Dynamic programming solution generated by ChatGPT.

    Args:
        arr (np.ndarray): The array to be partitioned.
        k (int): Number of partitions.

    Returns:
        List[Tuple[int]]: Partition start/end indices. Length equals to k.
    """
    n = arr.shape[0]
    dp = np.full((n + 1, k + 1), dtype=np.int64, fill_value=np.iinfo(np.int64).max)
    prefix_sum = np.concatenate((np.zeros(1, dtype=np.int64), np.cumsum(arr)), axis=0)

    for i in range(n + 1):
        dp[i, 1] = prefix_sum[i]

    # Fill the table using dynamic programming
    for i in range(1, n + 1):
        for j in range(2, k + 1):
            for x in range(i):
                dp[i, j] = min(dp[i, j], max(dp[x, j - 1], prefix_sum[i] - prefix_sum[x]))

    # Backtrack to find the partition
    partitions = []
    i, j = n, k
    while j > 1:
        for x in range(i):
            if dp[i][j] == max(dp[x][j - 1], prefix_sum[i] - prefix_sum[x]):
                partitions.append((x, i))
                i = x
                j -= 1

    partitions.append((0, i))

    return partitions[::-1]