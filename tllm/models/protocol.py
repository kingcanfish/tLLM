import asyncio
from dataclasses import dataclass, field
from typing import *

import torch

from tllm.generate.sampler_utils import SamplerUtils
from tllm.generate.sampling_params import SamplingParams

finish_reason_type = Literal["length", "stop", None]


@dataclass
class SeqInput:
    uuid_list: List[str]
    seq_len_list: List[int]


@dataclass
class GenerateEnd:
    finish_reason: finish_reason_type
    is_end: bool


@dataclass
class CompletionOutput:
    index: int
    text: str
    token_ids: Tuple[int, ...]
    cumulative_logprob: Optional[float] = None
    logprobs: Optional[List[float]] = None
    finish_reason: Optional[str] = None
    stop_reason: Union[int, str, None] = None


@dataclass
class RequestOutput:
    # 转换为 HTTP 接口的数据结构
    def __init__(
        self,
        request_id: str,
        prompt: Optional[str],
        prompt_token_ids: List[int],
        outputs: List[CompletionOutput],
        finished: bool,
        prompt_logprobs: Optional[List[float]] = None,
    ) -> None:
        self.request_id = request_id
        self.prompt = prompt
        self.prompt_token_ids = prompt_token_ids
        self.prompt_logprobs = prompt_logprobs
        self.outputs = outputs
        self.finished = finished


@dataclass
class ForwardResult:
    logits: torch.Tensor
    comm_cost_time_list: Optional[List[float]] = None
    hidden_states: Optional[torch.Tensor] = None


@dataclass
class SequenceRequestData:
    # 每个请求在输入输出模型的数据
    request_id: str
    sampling_params: SamplingParams
    input_ids: Optional[List[int]] = None  # 输入的 token id
    finish_reason_list: Optional[List[str]] = None

    sampler: Optional[SamplerUtils] = None

    output_ids: Optional[List[int]] = None  # 最终生成的 token id
    output_text: Optional[str] = None  # 最终生成的 text

    generate_ids: Optional[List[int]] = None  # 每次生成的 token id
    generate_texts: Optional[List[str]] = None  # 每次生成的 text

    ttft_cost_time: Optional[List[float]] = None
    tpot_cost_time: Optional[List[float]] = None
    timeout: int = 100000  # 请求的总超时时间
    is_stop: bool = False

    condition: asyncio.Condition = field(default_factory=asyncio.Condition)

    def __post_init__(self):
        self.output_ids = []
        self.output_text = ""
        self.generate_ids = []
        self.generate_texts = []
        self.finish_reason_list = [None] * self.sampling_params.n

    def __repr__(self) -> str:
        return f"request_id={self.request_id}; output_ids={self.output_ids}"

    def to_request_output(self) -> RequestOutput:
        if not self.is_stop:
            return RequestOutput(
                self.request_id,
                None,
                self.input_ids.tolist(),
                [
                    CompletionOutput(
                        index=index,
                        text=self.generate_texts[index],
                        token_ids=self.generate_ids[index],
                        finish_reason=self.finish_reason_list[index],
                    )
                    for index in range(self.sampling_params.n)
                ],
                self.is_stop,
                None,
            )
        return RequestOutput(
            request_id=self.request_id,
            prompt=None,
            prompt_token_ids=self.input_ids.tolist(),
            outputs=[
                CompletionOutput(
                    index=index,
                    text=self.output_text,
                    token_ids=tuple(self.output_ids),
                    finish_reason=self.finish_reason_list[index],
                )
                for index in range(self.sampling_params.n)
            ],
            finished=True,
            prompt_logprobs=None,
        )