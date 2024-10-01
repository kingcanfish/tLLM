from typing import *

import torch
import torch.nn as nn
from transformers.activations import ACT2FN
from transformers.cache_utils import Cache
from transformers.models.llama.modeling_llama import (
    LlamaConfig,
    LlamaRMSNorm,
    LlamaRotaryEmbedding,
    apply_rotary_pos_emb,
    repeat_kv,
)


class BaseParallelLayer(nn.Module):
    def __init__(self, world_size: int, rank: int) -> None:
        self.world_size = world_size
        self.rank = rank
        super().__init__()


class MergeParallelLayer(BaseParallelLayer):
    def __init__(self, row_size: int, col_size: int, dup_layer: int, world_size: int, rank: int) -> None:
        super().__init__(world_size, rank)
        assert col_size % self.world_size == 0
        self.row_size, self.col_size = row_size, col_size
        self.dup_layer = dup_layer
        self.layer = nn.Linear(row_size, col_size * self.dup_layer // self.world_size, bias=False)

    def load_weight(self, w_list: Optional[List[torch.Tensor]] = None):
        w = w_list[self.rank]
        self.load_state_dict({"layer.weight": w})

    @torch.no_grad()
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        node_output = self.layer(x)
        return torch.chunk(node_output, self.dup_layer, dim=-1)


class QKVParallelLayer(BaseParallelLayer):
    def __init__(self, row_size: int, col_size_list: List[int], world_size: int, rank: int) -> None:
        super().__init__(world_size, rank)
        for x in col_size_list:
            assert x % self.world_size == 0
        col_size = sum(col_size_list)
        assert col_size % self.world_size == 0

        self.row_size, self.col_size = row_size, col_size
        self.col_size_list = [x // self.world_size for x in col_size_list]
        self.layer = nn.Linear(row_size, col_size // self.world_size, bias=False)

    def load_weight(self, w_list: Optional[List[torch.Tensor]] = None):
        w = w_list[self.rank]
        self.load_state_dict({"layer.weight": w})

    @torch.no_grad()
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        node_output = self.layer(x)
        return torch.split(node_output, self.col_size_list, dim=-1)


class RowParallelLayer(BaseParallelLayer):
    def __init__(self, row_size: int, col_size: int, world_size: int, rank: int) -> None:
        super().__init__(world_size, rank)
        assert row_size % self.world_size == 0
        self.row_size, self.col_size = row_size, col_size
        self.layer = nn.Linear(row_size // self.world_size, col_size, bias=False)

    def load_weight(self, w: Optional[torch.Tensor] = None):
        if self.world_size > 1:
            w_list = w.chunk(self.world_size, dim=1)
            w = w_list[self.rank]
        self.load_state_dict({"layer.weight": w})

    @torch.no_grad()
    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layer(x)


class MyLlamaMLP(nn.Module):
    def __init__(self, config, layer_idx: int):
        super().__init__()
        self.layer_idx = layer_idx
        self.config = config
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        self.act_fn = ACT2FN[config.hidden_act]

        self.comm = config.comm
        self.rank = self.comm.rank
        self.world_size = self.comm.world_size

        self.gate_up_proj = MergeParallelLayer(self.hidden_size, self.intermediate_size, 2, self.world_size, self.rank)
        self.down_proj = RowParallelLayer(self.intermediate_size, self.hidden_size, self.world_size, self.rank)

    def load_state_dict(self, state_dict: Dict) -> None:
        for key in ["down_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.mlp.{key}.weight"
            getattr(self, key).load_weight(state_dict.get(layer_name, None))

        weight_chunks = []
        for key in ["gate_proj", "up_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.mlp.{key}.weight"
            weight = state_dict[layer_name]
            weight_chunks.append(torch.chunk(weight, self.world_size, dim=0))
        combined_weights = [torch.cat([chunk[i] for chunk in weight_chunks], dim=0) for i in range(self.world_size)]
        self.gate_up_proj.load_weight(combined_weights)

    def forward(self, x):
        gate_out, up_out = self.gate_up_proj(x)
        intermediate_states = self.act_fn(gate_out) * up_out
        return self.comm.all_reduce(self.down_proj(intermediate_states))


class MyLlamaSdpaAttention(nn.Module):
    def __init__(self, config: LlamaConfig, layer_idx: Optional[int] = None):
        super().__init__()
        self.config = config
        self.layer_idx = layer_idx

        self.attention_dropout = config.attention_dropout
        self.hidden_size = config.hidden_size
        self.num_heads = config.num_attention_heads
        self.head_dim = self.hidden_size // self.num_heads
        self.num_key_value_heads = config.num_key_value_heads
        self.num_key_value_groups = self.num_heads // self.num_key_value_heads
        self.max_position_embeddings = config.max_position_embeddings
        self.rope_theta = config.rope_theta
        self.is_causal = True

        if (self.head_dim * self.num_heads) != self.hidden_size:
            raise ValueError(
                f"hidden_size must be divisible by num_heads (got `hidden_size`: {self.hidden_size}"
                f" and `num_heads`: {self.num_heads})."
            )

        self.comm = config.comm
        self.rank = self.comm.rank
        self.world_size = self.comm.world_size

        self.qkv_proj = QKVParallelLayer(
            self.hidden_size,
            [
                self.num_heads * self.head_dim,
                self.num_key_value_heads * self.head_dim,
                self.num_key_value_heads * self.head_dim,
            ],
            self.world_size,
            self.rank,
        )
        self.o_proj = RowParallelLayer(self.num_heads * self.head_dim, self.hidden_size, self.world_size, self.rank)
        self._init_rope()

    def _init_rope(self):
        self.rotary_emb = LlamaRotaryEmbedding(
            self.head_dim,
            max_position_embeddings=self.max_position_embeddings,
            base=self.rope_theta,
        )

    def load_state_dict(self, state_dict: Dict) -> None:
        for key in ["o_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.self_attn.{key}.weight"
            getattr(self, key).load_weight(state_dict.get(layer_name, None))

        weight_chunks = []
        for key in ["q_proj", "k_proj", "v_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.self_attn.{key}.weight"
            weight = state_dict[layer_name]
            weight_chunks.append(torch.chunk(weight, self.world_size, dim=0))
        combined_weights = [torch.cat([chunk[i] for chunk in weight_chunks], dim=0) for i in range(self.world_size)]
        self.qkv_proj.load_weight(combined_weights)

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional["Cache"] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        bsz, q_len, _ = hidden_states.size()

        query_states, key_states, value_states = self.qkv_proj(hidden_states)

        query_states = query_states.view(bsz, q_len, -1, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, -1, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, -1, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx - self.config.offset)
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)

        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            cache_kwargs = {"sin": sin, "cos": cos}  # Specific to RoPE models
            key_states, value_states = past_key_value.update(
                key_states, value_states, self.layer_idx - self.config.offset, cache_kwargs
            )

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        # TODO: speed up the following line
        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            # The q_len > 1 is necessary to match with AttentionMaskConverter.to_causal_4d that does not create a causal mask in case q_len == 1.
            is_causal=self.is_causal and q_len > 1,
        )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(bsz, q_len, -1)

        attn_output = self.comm.all_reduce(self.o_proj(attn_output))
        return attn_output, None, past_key_value


class MyLlamaDecoderLayer(nn.Module):
    def __init__(self, config: LlamaConfig, layer_idx: int) -> None:
        super().__init__()
        self.hidden_size = config.hidden_size
        self.config = config
        self.layer_idx = layer_idx

        self.self_attn = MyLlamaSdpaAttention(config=config, layer_idx=layer_idx)

        self.mlp = MyLlamaMLP(config, layer_idx=layer_idx)
        self.input_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)
        self.post_attention_layernorm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    def load_state_dict(self, state_dict: Dict):
        self.input_layernorm.load_state_dict(
            {"weight": state_dict.pop(f"model.layers.{self.layer_idx}.input_layernorm.weight")}
        )
        self.post_attention_layernorm.load_state_dict(
            {"weight": state_dict.pop(f"model.layers.{self.layer_idx}.post_attention_layernorm.weight")}
        )

        self.self_attn.load_state_dict(state_dict)
        self.mlp.load_state_dict(state_dict)

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional["Cache"] = None,
    ) -> Tuple[torch.Tensor, Optional["Cache"]]:
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, _, past_key_value = self.self_attn(
            hidden_states=hidden_states,
            position_ids=position_ids,
            past_key_value=past_key_value,
        )
        hidden_states = residual + hidden_states

        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)

        # Fully Connected
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        return hidden_states, past_key_value