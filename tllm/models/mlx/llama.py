from typing import Any, Dict, Optional

import mlx.core as mx
import mlx.nn as nn
from mlx_lm.models.llama import ModelArgs
import numpy as np
from transformers import AutoConfig

from tllm.commons.cache import CacheManager
from tllm.models.mlx.helper import build_forward_cache, get_last_hidden_states, quantization_func
from tllm.models.mlx.layers import Decoder
from tllm.models.utils import read_eos_token_ids
from tllm.models.weight_helper import default_merge_attn_weight, default_merge_mlp_weight
from tllm.schemas import SeqInput


def get_inv_freq_mx(dim, base):
    return 1.0 / (base ** (mx.arange(0, dim, 2, dtype=mx.int32).astype(mx.float32) / dim))


class DynamicNTKScalingRoPE:
    def __init__(self, dims, max_position_embeddings, base, scale, rope_type, rope_scaling):
        self._freqs = get_inv_freq_mx(dims, base)

    def __call__(self, position_ids):
        inv_mx_freq_expanded = mx.expand_dims(self._freqs, (0, 2))[0]
        position_mx_ids_expanded = position_ids[None, :]

        freqs = (inv_mx_freq_expanded @ position_mx_ids_expanded).transpose(0, 1)
        emb_mx = mx.concatenate((freqs, freqs), axis=-1)
        return emb_mx.cos(), emb_mx.sin()


class MLXLlamaModel(nn.Module):
    def __init__(self, config: AutoConfig, is_merge: bool = True):
        super().__init__()
        args = ModelArgs.from_dict(config.to_dict())
        self.vocab_size = args.vocab_size
        self.cache_manager = CacheManager()
        self.config = config
        self.model = Decoder(args, config.decoder_start_layer_idx, config.decoder_end_layer_idx, is_merge)
        self.num_layers = config.decoder_end_layer_idx - config.decoder_start_layer_idx
        self.rotary_emb = DynamicNTKScalingRoPE(
            dims=args.head_dim or args.hidden_size // args.num_attention_heads,
            max_position_embeddings=args.max_position_embeddings,
            base=args.rope_theta,
            scale=1.0,
            rope_type="default",
            rope_scaling=1.0,
        )

    def __call__(self, hidden_states: mx.array, seq_input: SeqInput) -> mx.array:
        attention_data = build_forward_cache(seq_input, self.cache_manager, self.num_layers)

        cos, sin = self.rotary_emb(attention_data.position_ids)
        attention_data.cos, attention_data.sin = cos, sin

        mask = attention_data.attn_mask
        mask = mask if mask is None else mask.astype(hidden_states.dtype)
        output = self.model(hidden_states, mask=mask, cache=attention_data)

        # TODO 异步保存 cache
        for uuid, seq_len in zip(seq_input.uuid_list, seq_input.seq_len_list):
            self.cache_manager.set(uuid, attention_data.get_kv_cache_list(uuid), attention_data.get_cache_seq_len(uuid))
            self.cache_manager.check_alive()

        if self.config.decoder_end_layer_idx == self.config.num_hidden_layers:
            output = get_last_hidden_states(output, seq_input.seq_len_list)
        return output

    @classmethod
    def from_pretrained(cls, config: AutoConfig, state_dict: Dict[str, mx.array], is_merge: bool = True, **kwargs):
        if getattr(config, "quantization", None) is not None or state_dict is not None:
            is_merge = False

        model = cls(config, is_merge)
        state_dict = model.merge_weights(state_dict, is_merge)

        model = quantization_func(config, model, state_dict)
        model.load_weights(list(state_dict.items()), strict=False)
        mx.eval(model.parameters())
        model.eval()
        return model

    def merge_weights(self, state_dict: Dict[str, mx.array], is_merge: bool = True) -> Dict[str, mx.array]:
        if not is_merge:
            return state_dict
        state_dict = default_merge_attn_weight(state_dict)
        state_dict = default_merge_mlp_weight(state_dict)
        return state_dict


class MLXLlamaForCausalLM(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.vocab_size = config.vocab_size
        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.norm = nn.RMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    @classmethod
    def from_pretrained(cls, config, state_dict: Optional[Any], **kwargs):
        model = cls(config)

        cls.config = config
        cls.num_layers = config.num_hidden_layers
        cls.eos_token_ids = read_eos_token_ids(config)

        model = quantization_func(config, model, state_dict)
        model.load_weights(list(state_dict.items()))

        mx.eval(model.parameters())
        model.eval()
        return model

    def get_input_embeddings(self, x: np.ndarray) -> mx.array:
        return self.embed_tokens(mx.array(x))

    def get_logits(self, hidden_states: mx.array) -> mx.array:
        logits = self.lm_head(self.norm(hidden_states.astype(self.norm.weight.dtype)))
        return logits
