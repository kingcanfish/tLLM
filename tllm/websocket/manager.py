import random
from typing import Dict, Set, Tuple

from fastapi import WebSocket

from tllm.rpc.manager import ClientRPCManager
from tllm.schemas import ClientData, InitModelRequest, InitModelResponse, RegisterClientRequest, RegisterClientResponse
from tllm.websocket.utils import find_continuous_path, parse_model_size, split_model_layers


class WebsocketManager:
    def __init__(self, total_layers: int, model_name: str):
        self.total_layers = total_layers
        self.model_name = model_name
        self.clients: Dict[str, ClientData] = {}
        self.monitor_websockets: Set[WebSocket] = set()  # 前端页面的websocket连接

        self.connect_clients = []
        self.client_size, self.layer_info = split_model_layers(parse_model_size(model_name), total_layers)
        self.client_info = [[start_idx, end_idx, 0] for start_idx, end_idx in self.layer_info]
        self.client_manager = ClientRPCManager(self.client_size)

    def get_free_layer(self) -> Tuple[int, int, int]:
        # 返回一个未被注册的start idx 和 end idx，如果所有层都被注册了，则随机返回一个
        if self.has_full_model:
            pp_rank = random.choice(len(self.layer_info))
            return self.layer_info[pp_rank]
        else:
            for pp_rank, (start_idx, end_idx, count) in enumerate(self.client_info):
                if count == 0:
                    return pp_rank, start_idx, end_idx

    async def register_client(self, request: RegisterClientRequest, model_path: str) -> RegisterClientResponse:
        if request.pp_rank == -1:
            self.clients[request.client_id] = ClientData(client_id=request.client_id, host=request.host)

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
                host=request.host,
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

    async def unregister_client(self, client_id: str):
        if client_id not in self.clients:
            return
        data = self.clients.pop(client_id)
        if data.pp_rank and data.pp_rank != -1:
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

    async def broadcast_state(self):
        """向所有监控页面广播状态更新"""
        state = self.get_state()
        disconnected = set()

        for ws in self.monitor_websockets:
            try:
                await ws.send_json(state)
            except:
                disconnected.add(ws)

        self.monitor_websockets -= disconnected

    def set_connect_clients(self):
        x = find_continuous_path(self.clients, self.total_layers)
        self.connect_clients = x if x else []
        print(self.connect_clients)

        self.client_manager.update_url([x.host for x in self.connect_clients])

    async def send_config(self, master_url):
        for i, client in enumerate(self.connect_clients):
            url = master_url if i == len(self.connect_clients) - 1 else self.connect_clients[i + 1].host
            await self.client_manager.set_config(i, {"forward_url": url, "master_url": master_url, "pp_rank": i})

    def find_connect_clients(self, client_id) -> bool:
        for client in self.clients.values():
            if client.client_id == client_id:
                return True
        return False
