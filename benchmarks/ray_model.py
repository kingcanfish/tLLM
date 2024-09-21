from typing import Mapping
from transformers import AutoTokenizer, AutoConfig
from transformers.models.llama.modeling_llama import LlamaRMSNorm, LlamaSdpaAttention, LlamaConfig, LlamaRotaryEmbedding, apply_rotary_pos_emb, repeat_kv
from typing import *
import time
import torch
import torch.nn as nn
import ray
import time
from transformers.activations import GELUActivation
from transformers.modeling_outputs import BaseModelOutputWithPast
from transformers.cache_utils import Cache, DynamicCache

# 使用 ray 实现 张量并行，通信时通信仅通信输入

ray.init(ignore_reinit_error=True, num_cpus=4)


@ray.remote
class ParallelLinear(nn.Module):
    def __init__(self, row_size: int, col_size: int) -> None:
        super().__init__()
        self.layer = nn.Linear(row_size, col_size, bias=False)

    def load_state_dict(self, state_dict: Dict) -> None:
        self.layer.load_state_dict(state_dict)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.layer(x)


class MyLlamaMLP(nn.Module):
    def __init__(self, config, layer_idx: int):
        super().__init__()
        assert config.intermediate_size % config.tp == 0
        self.tp = config.tp
        self.slice_size = config.intermediate_size // config.tp
        self.layer_idx = layer_idx
        self.config = config
        self.hidden_size = config.hidden_size
        self.intermediate_size = config.intermediate_size
        # self.mlp_layer = [MLPParallelLinear.remote(self.hidden_size, self.slice_size) for _ in range(config.tp)]
        self.gate_proj = [ParallelLinear.remote(self.hidden_size, self.slice_size) for _ in range(config.tp)]
        self.up_proj = [ParallelLinear.remote(self.hidden_size, self.slice_size) for _ in range(config.tp)]
        self.down_proj = [ParallelLinear.remote(self.slice_size, self.hidden_size) for _ in range(config.tp)]
        self.act_fn = GELUActivation()

    def load_state_dict(self, state_dict: Dict) -> None:
        for key in ["gate_proj", "up_proj", "down_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.mlp.{key}.weight"
            if key == "down_proj":
                w_list = state_dict[layer_name].chunk(self.tp, dim=1)
            else:
                w_list = state_dict[layer_name].chunk(self.tp, dim=0)
            for i in range(self.tp):
                getattr(self, key)[i].load_state_dict.remote({"weight": w_list[i]})

    def forward(self, x):
        gate_futures = [self.gate_proj[i].forward.remote(x) for i in range(self.tp)]
        up_futures = [self.up_proj[i].forward.remote(x) for i in range(self.tp)]

        gate_results = torch.cat(ray.get(gate_futures), dim=-1)
        gate_out = self.act_fn(gate_results)

        up_out = torch.cat(ray.get(up_futures), dim=-1)
        intermediate_states = up_out * gate_out

        split_x_list = torch.chunk(intermediate_states, self.tp, dim=-1)
        down_futures = [self.down_proj[i].forward.remote(split_x_list[i]) for i in range(self.tp)]
        results = ray.get(down_futures)
        return torch.sum(torch.stack(results), dim=0)

class MyLlamaSdpaAttention(nn.Module):
    def __init__(self, config: LlamaConfig, layer_idx: Optional[int] = None):
        super().__init__()
        self.config = config
        self.tp = config.tp
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
        assert (self.num_heads * self.head_dim) % self.tp == 0
        assert (self.num_key_value_heads * self.head_dim) % self.tp == 0

        self.query_out_slices = self.num_heads * self.head_dim // self.tp
        self.key_value_slicing = self.num_key_value_heads * self.head_dim // self.tp

        self.q_proj = [ParallelLinear.remote(self.hidden_size, self.query_out_slices) for _ in range(config.tp)]
        self.k_proj = [ParallelLinear.remote(self.hidden_size, self.key_value_slicing) for _ in range(config.tp)]
        self.v_proj = [ParallelLinear.remote(self.hidden_size, self.key_value_slicing) for _ in range(config.tp)]
        self.o_proj = [ParallelLinear.remote(self.query_out_slices, self.hidden_size) for _ in range(config.tp)]
        self._init_rope()

    def _init_rope(self):
        self.rotary_emb = LlamaRotaryEmbedding(
            self.head_dim,
            max_position_embeddings=self.max_position_embeddings,
            base=self.rope_theta,
        )
    
    def load_state_dict(self, state_dict: Dict) -> None:
        for key in ["q_proj", "k_proj", "v_proj", "o_proj"]:
            layer_name = f"model.layers.{self.layer_idx}.self_attn.{key}.weight"
            if key[0] in "qkv":
                w_list = state_dict[layer_name].chunk(self.tp, dim=0)
            else:            
                w_list = state_dict[layer_name].chunk(self.tp, dim=1)
            for i in range(self.tp):
                getattr(self, key)[i].load_state_dict.remote({"weight": w_list[i]})

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_value: Optional["Cache"] = None,
    ) -> Tuple[torch.Tensor, Optional[torch.Tensor], Optional[Tuple[torch.Tensor]]]:
        bsz, q_len, _ = hidden_states.size()

        q_futures = [self.q_proj[i].forward.remote(hidden_states) for i in range(self.tp)]
        k_futures = [self.k_proj[i].forward.remote(hidden_states) for i in range(self.tp)]
        v_futures = [self.v_proj[i].forward.remote(hidden_states) for i in range(self.tp)]

        query_states = torch.cat(ray.get(q_futures), dim=-1)
        key_states = torch.cat(ray.get(k_futures), dim=-1)
        value_states = torch.cat(ray.get(v_futures), dim=-1)

        query_states = query_states.view(bsz, q_len, self.num_heads, self.head_dim).transpose(1, 2)
        key_states = key_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)
        value_states = value_states.view(bsz, q_len, self.num_key_value_heads, self.head_dim).transpose(1, 2)

        kv_seq_len = key_states.shape[-2]
        if past_key_value is not None:
            kv_seq_len += past_key_value.get_usable_length(kv_seq_len, self.layer_idx)
        cos, sin = self.rotary_emb(value_states, seq_len=kv_seq_len)

        query_states, key_states = apply_rotary_pos_emb(query_states, key_states, cos, sin, position_ids)

        if past_key_value is not None:
            cache_kwargs = {"sin": sin, "cos": cos}  # Specific to RoPE models
            key_states, value_states = past_key_value.update(key_states, value_states, self.layer_idx, cache_kwargs)

        key_states = repeat_kv(key_states, self.num_key_value_groups)
        value_states = repeat_kv(value_states, self.num_key_value_groups)

        # TODO: speed up the following line
        attn_output = torch.nn.functional.scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=None,
            # The q_len > 1 is necessary to match with AttentionMaskConverter.to_causal_4d that does not create a causal mask in case q_len == 1.
            is_causal=self.is_causal,
        )

        attn_output = attn_output.transpose(1, 2).contiguous()
        attn_output = attn_output.reshape(bsz, q_len, self.hidden_size)

        split_x_list = torch.chunk(attn_output, self.tp, dim=-1)
        futures = [self.o_proj[i].forward.remote(split_x_list[i]) for i in range(self.tp)]
        results = ray.get(futures)
        attn_output = torch.sum(torch.stack(results), dim=0)

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
        # print("keys", state_dict.keys())
        self.input_layernorm.load_state_dict({"weight": state_dict.pop(f"model.layers.{self.layer_idx}.input_layernorm.weight")})
        self.post_attention_layernorm.load_state_dict({"weight": state_dict.pop(f"model.layers.{self.layer_idx}.post_attention_layernorm.weight")})

        self.self_attn.load_state_dict(state_dict)
        self.mlp.load_state_dict(state_dict)

    def forward(self, hidden_states: torch.Tensor, position_ids: Optional[torch.LongTensor] = None, past_key_value: Optional["Cache"] = None) -> Tuple[torch.Tensor, Optional["Cache"]]:
        residual = hidden_states

        hidden_states = self.input_layernorm(hidden_states)

        # Self Attention
        hidden_states, _, past_key_value = self.self_attn(
            hidden_states=hidden_states,
            position_ids=position_ids,
            past_key_value=past_key_value,
        )
        hidden_states = residual + hidden_states

        # Fully Connected
        residual = hidden_states
        hidden_states = self.post_attention_layernorm(hidden_states)
        hidden_states = self.mlp(hidden_states)
        hidden_states = residual + hidden_states

        return hidden_states, past_key_value

class MyLlamaModel(nn.Module):
    def __init__(self, config):
        super().__init__()
        self.padding_idx = config.pad_token_id
        self.vocab_size = config.vocab_size

        self.decoder = nn.ModuleList(
            [MyLlamaDecoderLayer(config, layer_idx) for layer_idx in range(config.num_hidden_layers)]
        )
        self._use_sdpa = config._attn_implementation == "sdpa"
        self._use_flash_attention_2 = config._attn_implementation == "flash_attention_2"

    def load_state_dict(self, state_dict: Dict) -> None:
        for layer in self.decoder:
            layer.load_state_dict(state_dict)

    def forward(
        self,
        hidden_states: torch.Tensor,
        position_ids: Optional[torch.LongTensor] = None,
        past_key_values: Optional["Cache"] = None,
    ):
        next_decoder_cache = None
        for layer in self.decoder:
            layer_outputs = layer(
                hidden_states,
                position_ids=position_ids,
                past_key_value=past_key_values,
            )
            hidden_states = layer_outputs[0]

            # 所有层的 kv cache 放到一起了，所以这里只需要取最后一层的 kv cache
            next_decoder_cache = layer_outputs[1]
        next_cache = next_decoder_cache
        return BaseModelOutputWithPast(
            last_hidden_state=hidden_states, past_key_values=next_cache
        )

class MyLlamaForCausalLM(nn.Module):
    def __init__(self, config):
        # config.tp = 2
        super().__init__()
        self.model = MyLlamaModel(config)
        self.vocab_size = config.vocab_size

        self.embed_tokens = nn.Embedding(config.vocab_size, config.hidden_size)
        self.lm_head = nn.Linear(config.hidden_size, config.vocab_size, bias=False)
        self.norm = LlamaRMSNorm(config.hidden_size, eps=config.rms_norm_eps)

    @classmethod
    def from_pretrained(cls, model_path: str, **kwargs):
        config = AutoConfig.from_pretrained(model_path, trust_remote_code=True)
        config.tp = 2
        # config.num_hidden_layers = 1

        model = cls(config)
        from transformers import LlamaForCausalLM
        state_dict = LlamaForCausalLM.from_pretrained(
            model_path, trust_remote_code=True, device_map="cpu"
        ).state_dict()
        model.embed_tokens.load_state_dict({"weight": state_dict.pop("model.embed_tokens.weight")})
        model.norm.load_state_dict({"weight": state_dict.pop("model.norm.weight")})
        model.lm_head.load_state_dict({"weight": state_dict.pop("lm_head.weight")})

        model.model.load_state_dict(state_dict)

        model.eval()
        return model

    def forward(self, input_ids: List[int], position_ids, past_key_values):
        hidden_states = self.embed_tokens(input_ids)
        output = self.model(hidden_states, position_ids, past_key_values)
        hidden_states = self.norm(output.last_hidden_state)
        logits = self.lm_head(hidden_states)
        return logits, output.past_key_values


    @torch.no_grad()
    def generate(self, input_ids: torch.Tensor, **kwargs) -> Tuple[torch.Tensor, Optional[torch.Tensor]]:
        # input_ids: bs x seq_len
        max_new_tokens = kwargs.get("max_new_tokens", 16)
        bs, seq_len = input_ids.size() # bs == 1
        past_key_values = None
        position_ids = None
        cnt = 0
        while True:

            if past_key_values is None:
                past_key_values = DynamicCache()
                position_ids = torch.arange(seq_len, dtype=torch.long).unsqueeze(0)
            else:
                kv_cache_seq_len = past_key_values.key_cache[0].shape[-2]
                # request_seq_len = hidden_states.size(1) - 1
                # assert kv_cache_seq_len == request_seq_len, "seq_len not match"
                position_ids = torch.tensor([kv_cache_seq_len], dtype=torch.long).unsqueeze(
                    0
                )

            logits, past_key_values = self.forward(input_ids, position_ids, past_key_values)
            return logits, None
            cnt += 1
            if cnt > max_new_tokens:
                break
        return logits, None


def load_model_and_tokenizer(model_path: str) -> Tuple[MyLlamaForCausalLM, AutoTokenizer]:
    model = MyLlamaForCausalLM.from_pretrained(
        model_path, trust_remote_code=True, device_map="cpu"
    )
    tok = AutoTokenizer.from_pretrained(
        model_path, use_fast=True, trust_remote_code=True
    )
    return model, tok


formatted_prompt = "### Human: {}### Assistant:"


def tokenize_message(tok: AutoTokenizer, messages: List[Dict[str, str]]) -> List[int]:
    inputs = formatted_prompt.format(messages[0]["content"])
    # inputs = "Hello, how are you?"
    # inputs = tok.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    input_ids = tok.encode(inputs, add_special_tokens=True)
    while input_ids[0] == input_ids[1] == tok.bos_token_id:
        # input_ids = input_ids[1:]
        input_ids.pop(0)
    return input_ids


if __name__ == "__main__":
    model_path = "/Users/lujianghu/Documents/TinyLlama-1.1B-Chat-v1.0"
    model, tok = load_model_and_tokenizer(model_path)

    model.eval()

    messages = [{"role": "user", "content": "Hello, how are you?"}]
    input_id_list = tokenize_message(tok, messages)
    input_ids = torch.tensor(input_id_list).unsqueeze(0)
    print("input_ids: ", input_ids)
    # output = model.generate(input_ids, max_new_tokens=50, tokenizer=tok, eos_token_id=[0, tok.eos_token_id])
    # print(tok.decode(output[0][input_ids.shape[1]:], skip_special_tokens=True))

    with torch.no_grad():
        output = model.generate(input_ids, max_new_tokens=20, do_sample=False)
    print(tok.decode(output[0][input_ids.shape[1] :], skip_special_tokens=True))

    for _ in range(0):
        s1 = time.time()
        with torch.no_grad():
            output = model.generate(input_ids, max_new_tokens=20, do_sample=False)
        print(f"Time taken: {time.time() - s1}")
        print(tok.decode(output[0][input_ids.shape[1] :], skip_special_tokens=True))

    # 2.6-3.0s