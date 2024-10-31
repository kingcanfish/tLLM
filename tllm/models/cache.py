from dataclasses import dataclass, field
import time
from typing import *

import torch
from transformers.cache_utils import DynamicCache

try:
    import mlx.core as mx
    from mlx_lm.models.cache import KVCache

    HAS_MLX = True
    CACHE_CLASS = KVCache
    cat_func = mx.concat
    split_func = mx.split
except:
    CACHE_CLASS = DynamicCache
    cat_func = torch.cat
    split_func = torch.split
    HAS_MLX = False


class SeqDynamicCache:
    def __init__(self) -> None:
        self.cache_dict: Dict[Any] = {}

    def add(self, uuid_str: str, seq_len: int, cache: Optional[DynamicCache] = None):
        self.cache_dict.update({uuid_str: {"cache": CACHE_CLASS() if cache is None else cache, "seq_len": seq_len}})

    def get_cache(self, uuid_str: str) -> Union[DynamicCache, "KVCache"]:
        return self.cache_dict[uuid_str]["cache"]

    def get_seq_len(self, uuid_str: str) -> int:
        return self.cache_dict[uuid_str]["seq_len"]

    def update(
        self,
        key_states: torch.Tensor,
        value_states: torch.Tensor,
        layer_idx: int,
        cache_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Updates the cache with the new `key_states` and `value_states` for the layer `layer_idx`.

        Parameters:
            key_states (`torch.Tensor`):
                The new key states to cache.
            value_states (`torch.Tensor`):
                The new value states to cache.
            layer_idx (`int`):
                The index of the layer to cache the states for.
            cache_kwargs (`Dict[str, Any]`, `optional`):
                Additional arguments for the cache subclass. No additional arguments are used in `DynamicCache`.

        Return:
            A tuple containing the updated key and value states.
        """
        # key_states: bsz x num_heads x seq_len x head_dim
        uuid_str_list = cache_kwargs.get("uuid_str_list", None)
        seq_len_list = [self.get_seq_len(uuid_str) for uuid_str in uuid_str_list]
        seq_key_states = torch.split(key_states, seq_len_list, dim=-2)
        seq_value_states = torch.split(value_states, seq_len_list, dim=-2)

        key_states_list, value_states_list = [], []
        for uuid_str, key_state, value_state in zip(uuid_str_list, seq_key_states, seq_value_states):
            key, value = self.get_cache(uuid_str).update(key_state, value_state, layer_idx)
            key_states_list.append(key)
            value_states_list.append(value)

        cat_key_states, cat_value_states = cat_func(key_states_list, dim=-2), cat_func(value_states_list, dim=-2)
        return cat_key_states, cat_value_states


class SeqMLXDynamicCache(SeqDynamicCache):
    def add(self, uuid_str: str, seq_len: int, cache: Optional["KVCache"] = None):
        cache = CACHE_CLASS() if cache is None else cache
        offset = cache.offset
        self.cache_dict.update({uuid_str: {"cache": cache, "seq_len": seq_len, "offset": offset}})

    @property
    def offset_list(self) -> List[int]:
        return [self.get_cache(uuid_str).offset for uuid_str in self.cache_dict.keys()]

    @property
    def index_list(self) -> List[int]:
        seq_len_list = [self.get_seq_len(uuid_str) for uuid_str in self.cache_dict.keys()]
        index_list, idx = [], 0
        for seq_len in seq_len_list[:-1]:
            idx += seq_len
            index_list.append(idx)
        return index_list

    def update_and_fetch(
        self,
        seq_key_states: List["mx.array"],
        value_states: "mx.array",
        cache_kwargs: Optional[Dict[str, Any]] = None,
    ) -> Tuple["mx.array", "mx.array"]:
        uuid_str_list = cache_kwargs.get("uuid_str_list", None)
        seq_value_states = split_func(value_states, self.index_list, axis=-2)
        key_states_list, value_states_list = [], []
        for uuid_str, key_state, value_state in zip(uuid_str_list, seq_key_states, seq_value_states):
            key, value = self.get_cache(uuid_str).update_and_fetch(key_state, value_state)
            key_states_list.append(key)
            value_states_list.append(value)

        cat_key_states, cat_value_states = cat_func(key_states_list, axis=-2), cat_func(value_states_list, axis=-2)
        return cat_key_states, cat_value_states


MIX_TENSOR = Union[torch.Tensor, "mx.array"]


@dataclass
class AttentionCache:
    uuid_str_list: List[str]
    past_key_value: Union[SeqDynamicCache, SeqMLXDynamicCache]
    attn_mask: Union[torch.Tensor, "mx.array"]
    position_ids: Optional[torch.Tensor] = field(default=None, repr=False)


class NextLayerCache:
    def __init__(self) -> None:
        self.cache_dict: Dict[Any] = {}
        self.key_cache: Union[torch.Tensor, "mx.array"] = None
        self.value_cache: Union[torch.Tensor, "mx.array"] = None

    def get_cache_seq_length(self, layer_idx: Optional[int] = 0) -> int:
        """Returns the sequence length of the cached states. A layer index can be optionally passed."""
        return 0 if self.key_cache is None else self.key_cache.shape[-2]

    @property
    def seq_len_list(self) -> List[int]:
        return [self.get_seq_len(uuid_str) for uuid_str in self.cache_dict.keys()]

    @property
    def index_list(self) -> List[int]:
        index_list, idx = [], 0
        for seq_len in self.seq_len_list[:-1]:
            idx += seq_len
            index_list.append(idx)
        return index_list

    def add(self, uuid_str: str, seq_len: int, cache: Optional[Tuple[MIX_TENSOR, MIX_TENSOR]] = None):
        cache = (self.key_cache, self.value_cache) if cache is None else cache
        self.cache_dict.update({uuid_str: {"cache": cache, "seq_len": seq_len}})

    def get_cache(self, uuid_str: str) -> Union[DynamicCache, "KVCache"]:
        return self.cache_dict[uuid_str]["cache"]

    def get_seq_len(self, uuid_str: str) -> int:
        return self.cache_dict[uuid_str]["seq_len"]

    def _update_uuid_cache(self, key_states: MIX_TENSOR, value_states: MIX_TENSOR) -> Tuple[MIX_TENSOR, MIX_TENSOR]:
        if self.key_cache is None:
            self.key_cache = key_states
            self.value_cache = value_states
        else:
            if HAS_MLX:
                self.key_cache = cat_func([self.key_cache, key_states], axis=2)
                self.value_cache = cat_func([self.value_cache, value_states], axis=2)
            else:
                self.key_cache = cat_func([self.key_cache, key_states], dim=-2)
                self.value_cache = cat_func([self.value_cache, value_states], dim=-2)
        return self.key_cache, self.value_cache

    def update(
        self, key_states: Union[torch.Tensor, List["mx.array"]], value_states: MIX_TENSOR, **cache_kwargs
    ) -> Tuple[MIX_TENSOR, MIX_TENSOR]:
        uuid_str_list = cache_kwargs.get("uuid_str_list", None)
        if HAS_MLX:
            seq_key_states = key_states  # 已经在外部 split 过了
            seq_value_states = split_func(value_states, self.index_list, axis=-2)
        else:
            seq_len_list = [self.get_seq_len(uuid_str) for uuid_str in uuid_str_list]
            seq_key_states = split_func(key_states, seq_len_list, dim=-2)
            seq_value_states = split_func(value_states, seq_len_list, dim=-2)

        key_states_list, value_states_list = [], []
        for key_state, value_state in zip(uuid_str_list, seq_key_states, seq_value_states):
            key, value = self._update_uuid_cache(key_state, value_state)
            key_states_list.append(key)
            value_states_list.append(value)

        if HAS_MLX:
            return cat_func(key_states_list, axis=2), cat_func(value_states_list, axis=2)
        else:
            return cat_func(key_states_list, dim=-2), cat_func(value_states_list, dim=-2)


@dataclass
class NextAttentionCache:
    uuid_str_list: List[str]
    cache: List[NextLayerCache]  # 每层模型都有一个 NextDynamicCache
    attn_mask: Union[torch.Tensor, "mx.array"]
    position_ids: Optional[torch.Tensor] = field(default=None, repr=False)  # 只在 torch 下游泳


class CacheManager:
    # 管理每个 client 的 past_key_values，即 kv_cache
    # max_alive_time: 超过多久没有访问就删除，单位秒
    def __init__(self, max_alive_time=60):
        self.max_alive_time = max_alive_time
        self.cache_dict = {}

    def get(self, key) -> Any:
        return self.cache_dict.get(key)["past_key_values"]

    def set(self, key, value: Any) -> None:
        self.cache_dict[key] = {"past_key_values": value, "ts": time.time()}

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
