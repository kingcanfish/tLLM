import math
from typing import List, Tuple

import mlx.core as mx
import mlx.nn as nn

from tllm.models.cache import AttentionData, CacheManager, RequestsCache
from tllm.schemas import SeqInput


def greedy_decode(logits: mx.array) -> List[int]:
    # logits shape: [seq_len, vocab_size]
    x = mx.argmax(logits, axis=-1)
    return x.tolist()  # TODO: first requests is too slow


def build_mlx_mask(seq_len_list: List[Tuple[int, int]], total_L: int, total_S: int) -> mx.array:
    mask_list = [
        mx.tril(mx.ones((L, S), dtype=mx.bool_), k=0) if L > 1 else mx.ones((L, S), dtype=mx.bool_)
        for (L, S) in seq_len_list
    ]

    combined_mask = mx.zeros((total_L, total_S), dtype=mx.bool_)

    l_index, r_index = 0, 0
    for mask in mask_list:
        combined_mask[l_index : l_index + mask.shape[0], r_index : r_index + mask.shape[1]] = mask
        l_index += mask.shape[0]
        r_index += mask.shape[1]

    final_mask = mx.where(combined_mask, 0, -math.inf)
    return final_mask


def build_forward_cache(seq_input: SeqInput, cache_manager: CacheManager, num_layers: int) -> AttentionData:
    request_cache = RequestsCache(num_layers)
    actual_seq_len_list = []
    L, S = 0, 0
    for uuid, q_len in zip(seq_input.uuid_list, seq_input.seq_len_list):
        if uuid in cache_manager.cache_dict:
            layer_cache_list, cache_seq_len = cache_manager.get(uuid)
            actual_seq_len_list.append([q_len, cache_seq_len + q_len])
            L += q_len
            S += cache_seq_len + q_len
        else:
            layer_cache_list = None
            actual_seq_len_list.append([q_len, q_len])
            L += q_len
            S += q_len
        request_cache.add(uuid, q_len, layer_cache_list)
    return AttentionData(
        request_cache=request_cache,
        attn_mask=build_mlx_mask(actual_seq_len_list, L, S),
        uuid_list=seq_input.uuid_list,
    )


def quantization_func(config, model, state_dict):
    if getattr(config, "quantization", None) is not None:
        # Handle legacy models which may not have everything quantized
        def class_predicate(p, m):
            if not hasattr(m, "to_quantized"):
                return False
            return f"{p}.scales" in state_dict

        nn.quantize(
            model,
            **config.quantization,
            class_predicate=class_predicate,
        )
    else:
        model.set_dtype(mx.bfloat16)

    return model


def empty_func(h, mask, cache):
    # TODO
    return h
