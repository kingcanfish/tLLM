# coding: utf-8
from typing import *

import grpc
import torch

from tllm.commons.convert import deserialize_tensor, serialize_tensor
from tllm.models.protocol import SeqInput
from tllm.rpc import schemas_pb2, schemas_pb2_grpc


class RPCManager:
    def __init__(self, url_list: List[Optional[str]]):
        self.stub_list: List[schemas_pb2_grpc.RPCServiceStub] = []
        for url in url_list:
            if url is not None:
                channel = grpc.insecure_channel(url)
                self.stub_list.append(schemas_pb2_grpc.RPCServiceStub(channel))
            else:
                self.stub_list.append(None)

    def update_url(self, pp_idx: int, url: str) -> bool:
        if pp_idx >= len(self.stub_list):
            return False
        self.stub_list[pp_idx] = schemas_pb2_grpc.RPCServiceStub(grpc.insecure_channel(url))
        return True

    def remove_url(self, pp_idx: int) -> bool:
        if pp_idx >= len(self.stub_list):
            return False
        self.stub_list[pp_idx] = None
        return True

    def is_full_connected(self) -> bool:
        return all([stub is not None for stub in self.stub_list])

    def forward(
        self,
        url_idx: int,
        hidden_states: torch.Tensor,
        seq_input: SeqInput,
        is_first: bool = False,
        is_last: bool = False,
    ) -> Tuple[Union[torch.Tensor, bytes], float]:
        if is_first:
            hidden_states = serialize_tensor(hidden_states)
        forward_request = {
            "uuid": seq_input.uuid_list,
            "seq_len": seq_input.seq_len_list,
            "hidden_states": hidden_states,
        }

        request = schemas_pb2.ForwardRequest(**forward_request)
        response = self.stub_list[url_idx].Forward(request)
        if is_last:
            return deserialize_tensor(response.output, to_tensor=True), response.cost_time
        else:
            return response.output, response.cost_time

    def __len__(self):
        return len(self.stub_list)


if __name__ == "__main__":
    # for test
    server = RPCManager(["localhost:50051", "localhost:50052"])
    server.post_sync(0, "/forward", {"uuid": "123", "hidden_states": [[1.0, 2.0, 3.0]]})
