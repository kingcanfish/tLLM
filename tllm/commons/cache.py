# coding: utf-8
import time
from typing import *

import torch

from tllm import HAS_MLX
from tllm.schemas import MIX_TENSOR

if HAS_MLX:
    import mlx.core as mx

    cat_func = lambda tensors: mx.concat(tensors, axis=0)
else:
    cat_func = lambda tensors: torch.cat(tensors, dim=0)


KV_CACHE_TYPE = Tuple[MIX_TENSOR, MIX_TENSOR]


class KVCache:
    def __init__(self, seq_len: Optional[int] = -1, num_key_value_heads: Optional[int] = -1, head_dim: Optional[int] = -1) -> None:
        # key_states/value_states: seq_len x num_heads x head_dim
        if seq_len == -1:
            self.key_states: Optional[MIX_TENSOR] = None
            self.value_states: Optional[MIX_TENSOR] = None
        else:
            self.key_states = torch.empty(seq_len, num_key_value_heads, head_dim)
            self.value_states = torch.empty(seq_len, num_key_value_heads, head_dim)

    def __len__(self) -> int:
        return 0 if self.key_states is None else self.key_states.shape[0]


class RequestsCache:
    def __init__(self, num_layers: int) -> None:
        self.cache_dict: Dict[str : Dict[str, Union[List[KVCache], int]]] = {}
        self.num_layers = num_layers

    def add(self, uuid: str, seq_len: int, layer_cache_list: Optional[List[KVCache]] = None):
        # 保存每个 uuid 请求所有层的 cache
        self.cache_dict[uuid] = {
            "cache": [KVCache() for _ in range(self.num_layers)] if layer_cache_list is None else layer_cache_list,
            "seq_len": seq_len,
        }

    def get_kv_cache(self, uuid: str) -> List[KVCache]:
        return self.cache_dict[uuid]["cache"]

    def get_layer_idx_kv_cache(self, uuid: str, layer_idx: int) -> KVCache:
        return self.get_kv_cache(uuid)[layer_idx]

    def get_seq_len(self, uuid: str) -> int:
        # 获取每个 uuid 请求的 key_states/value_states 的 seq_len
        return self.cache_dict[uuid]["seq_len"]

    def get_cache_seq_len(self, uuid: str, layer_idx: Optional[int] = 0) -> int:
        # 获取每个 uuid 请求的 kv cache 的 seq_len
        return len(self.get_kv_cache(uuid)[layer_idx])

    def get_offset_list(self, uuid_list: List[str], layer_idx: int) -> List[int]:
        # 获取每个 uuid 请求的 offset，用于 mlx framework 旋转位置编码
        return [self.get_cache_seq_len(uuid, layer_idx) for uuid in uuid_list]

    def update(
        self, key_states: MIX_TENSOR, value_states: MIX_TENSOR, uuid_list: List[str], layer_idx: int
    ) -> KV_CACHE_TYPE:
        # TODO Need Optimization
        key_lst, value_lst = [], []
        start = 0
        for uuid in uuid_list:
            kv_cache: KVCache = self.get_layer_idx_kv_cache(uuid, layer_idx)
            interval = self.get_seq_len(uuid)
            end = start + interval
            cur_key_states, cur_value_states = key_states[start:end], value_states[start:end]
            if kv_cache.key_states is None:
                kv_cache.key_states, kv_cache.value_states = cur_key_states, cur_value_states
            else:
                kv_cache.key_states = cat_func([kv_cache.key_states, cur_key_states])
                kv_cache.value_states = cat_func([kv_cache.value_states, cur_value_states])
            key_lst.append(kv_cache.key_states)
            value_lst.append(kv_cache.value_states)
            start = end
        return cat_func(key_lst), cat_func(value_lst)

        # start = 0
        # max_seq_len = 100

        # total_k = torch.empty(seq_len, num_heads, head_dim, dtype=key_states.dtype, device=key_states.device)
        # total_v = torch.empty(seq_len, num_heads, head_dim, dtype=key_states.dtype, device=key_states.device)
        # total_start = 0
        # for uuid in uuid_list:
        #     kv_cache: KVCache = self.get_layer_idx_kv_cache(uuid, layer_idx)
        #     interval = self.get_seq_len(uuid)
        #     end = start + interval
        #     req_start, req_end = kv_cache.act_len+start, kv_cache.act_len + end

        #     cur_key_states, cur_value_states = key_states[start:end], value_states[start:end]
        #     kv_cache.key_states[req_start:req_end], kv_cache.value_states[req_start:req_end] = cur_key_states, cur_value_states
        #     total_k[total_start:total_end] = kv_cache.key_states[:req_end]
        #     total_v[total_start:total_end] = kv_cache.value_states[:req_end]

        #     kv_cache.act_len = req_end
        #     total_end = total_start + kv_cache.act_len
        #     total_start += kv_cache.act_len
        #     start = end
        # return total_k[:total_end], kv_cache.value_states[:total_end]


class AttentionData:
    def __init__(
        self,
        uuid_list: List[str],
        request_cache: RequestsCache,
        attn_mask: MIX_TENSOR,
        position_ids: Optional[torch.Tensor] = None,
    ) -> None:
        self.uuid_list = uuid_list
        self.request_cache = request_cache
        self.attn_mask = attn_mask
        self.position_ids = position_ids  # 只在 torch 下有意义

    def get_kv_cache_list(self, uuid: str) -> List[KVCache]:
        return self.request_cache.get_kv_cache(uuid)

    def get_cache_seq_len(self, uuid: str) -> int:
        return self.request_cache.get_cache_seq_len(uuid)


class CacheManager:
    # 管理每个节点的 cache kv_cache
    # max_alive_time: 超过多久没有访问就删除，单位秒
    def __init__(self, max_alive_time=60):
        self.max_alive_time = max_alive_time
        self.cache_dict = {}

    def get(self, key) -> Tuple[AttentionData, int]:
        return self.cache_dict.get(key)["cache"], self.cache_dict.get(key)["seq_len"]

    def set(self, key, value: List[KV_CACHE_TYPE], seq_len: int) -> None:
        self.cache_dict[key] = {"cache": value, "ts": time.time(), "seq_len": seq_len}

    def delete(self, key):
        self.cache_dict.pop(key)

    def clear(self):
        self.cache_dict.clear()

    def check_alive(self):
        now = time.time()
        key_list = list(self.cache_dict.keys())
        for key in key_list:
            if now - self.cache_dict[key]["ts"] > self.max_alive_time:
                self.cache_dict.pop(key)
