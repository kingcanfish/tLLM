import random
from typing import Dict, List, Set, Tuple

from fastapi import WebSocket

from tllm.models.file_helper import parse_model_size, split_model_layers
from tllm.network.helper import find_continuous_path, tcp_ping_test
from tllm.schemas import ClientData, InitModelRequest, InitModelResponse, RegisterClientRequest, RegisterClientResponse


class WebsocketManager:
    def __init__(self, total_layers: int, model_name: str, skip_parse: bool = False):
        self.total_layers = total_layers
        self.model_name = model_name
        self.clients: Dict[str, ClientData] = {}  # 连接的客户端, client_id -> ClientData
        self.monitor_websockets: Set[WebSocket] = set()  # 前端页面的websocket连接

        self.connect_clients = []
        if skip_parse:
            self.client_size, self.layer_info = split_model_layers(1, total_layers)
        else:
            self.client_size, self.layer_info = split_model_layers(parse_model_size(model_name), total_layers)
        self.client_info = [[start_idx, end_idx, 0] for start_idx, end_idx in self.layer_info]  # 统计连接情况

    def get_free_layer(self) -> Tuple[int, int, int]:
        # 返回一个未被注册的start idx 和 end idx，如果所有层都被注册了，则随机返回一个
        if self.has_full_model:
            pp_rank = random.choice(range(self.client_size))
            return pp_rank, *self.layer_info[pp_rank]
        else:
            for pp_rank, (start_idx, end_idx, count) in enumerate(self.client_info):
                if count == 0:
                    return pp_rank, start_idx, end_idx
        raise ValueError("No free layer")

    async def register_client(self, request: RegisterClientRequest, model_path: str) -> RegisterClientResponse:
        ip, delay = tcp_ping_test(request.host, request.port)
        if ip is None:
            return RegisterClientResponse(msg="ping failed", pp_rank=-1, start_idx=-1, end_idx=-1)
        host = f"{ip}:{request.port}"
        print(f"ping {host} delay: {delay:.2f}ms")
        if request.pp_rank == -1:
            self.clients[request.client_id] = ClientData(client_id=request.client_id, host=host)

            pp_rank, start_idx, end_idx = self.get_free_layer()
            return RegisterClientResponse(
                pp_rank=pp_rank,
                start_idx=start_idx,
                end_idx=end_idx,
                model=model_path,
                msg="success",
            )
        else:
            # 二次连接
            self.clients[request.client_id] = ClientData(
                client_id=request.client_id,
                host=host,
                pp_rank=request.pp_rank,
                start_idx=request.start_idx,
                end_idx=request.end_idx,
            )
            self.client_info[request.pp_rank][-1] += 1
            return RegisterClientResponse(
                pp_rank=request.pp_rank,
                start_idx=request.start_idx,
                end_idx=request.end_idx,
                msg="success",
            )

    async def init_client(self, request: InitModelRequest) -> InitModelResponse:
        if request.client_id not in self.clients:
            return InitModelResponse(msg="client not found", status=499)
        self.clients[request.client_id].start_idx = request.start_idx
        self.clients[request.client_id].end_idx = request.end_idx
        self.clients[request.client_id].pp_rank = request.pp_rank

        self.client_info[request.pp_rank][-1] += 1
        return InitModelResponse(msg="success", status=200)

    def unregister_client(self, client_id: str):
        if client_id not in self.clients:
            return
        data = self.clients.pop(client_id)
        if data.pp_rank is not None and data.pp_rank != -1:
            self.client_info[data.pp_rank][-1] -= 1

    @property
    def has_full_model(self) -> bool:
        return len(self.connect_clients) == self.client_size

    def get_state(self) -> dict:
        """与前端同步的数据"""
        return {
            "model_name": self.model_name,
            "total_layers": self.total_layers,
            "client_info": self.client_info,
            "has_full_model": self.has_full_model,
            "connected_clients": len(self.clients),
        }

    def set_connect_clients(self) -> List[str]:
        x = find_continuous_path(self.clients, self.total_layers)
        self.connect_clients: List[ClientData] = x if x else []

        if len(self.connect_clients) == 0:
            return []
        self.print_host_list()
        return [x.host for x in self.connect_clients]

    def unset_connect_clients(self, idx_list: List[int]):
        for idx in idx_list:
            self.unregister_client(self.connect_clients[idx].client_id)
        self.connect_clients = []

    def print_host_list(self):
        print("route path: ", "->".join([f"[{x.host}]" for x in self.connect_clients]))

    def find_connect_clients(self, client_id) -> bool:
        for client in self.clients.values():
            if client.client_id == client_id:
                return True
        return False