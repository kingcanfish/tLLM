import asyncio
from datetime import datetime
from typing import Optional

import aiohttp

from tllm.commons.manager import ModelManager
from tllm.schemas import InitModelRequest, InitModelResponse, RegisterClientRequest, RegisterClientResponse


class HTTPClient:
    def __init__(
        self,
        master_url: str,
        comm,
        logger,
        ping_interval: int = 30,
        max_retry_attempts: int = 100,
        retry_delay: int = 5,
    ):
        self.master_url = master_url
        self.is_running = False
        self.init_model_info = None
        self.last_ping_time: Optional[datetime] = None
        self.ping_interval = ping_interval
        self.max_retry_attempts = max_retry_attempts
        self.retry_delay = retry_delay
        self.model = None
        self.logger = logger
        self.comm = comm

    async def ping(self) -> bool:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(f"{self.master_url}/health") as response:
                    if response.status == 200:
                        self.last_ping_time = datetime.now()
                        return True
                    return False
        except Exception as e:
            self.logger.error(f"Ping failed")
            return False

    async def register_client(self, request_data: RegisterClientRequest):
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.master_url}/register_client", json=request_data.dict(), timeout=3
            ) as response:
                return RegisterClientResponse(**await response.json())

    async def init_model(self, request_data: InitModelRequest):
        async with aiohttp.ClientSession() as session:
            async with session.post(f"{self.master_url}/init_model", json=request_data.dict(), timeout=3) as response:
                return InitModelResponse(**await response.json())

    async def maintain_connection(self, client_id: str, ip_addr: str, port: int):
        """
        维护连接的协程，定期发送ping请求
        """
        while self.is_running:
            is_connected = await self.ping()

            if not is_connected:
                self.logger.warning("Connection lost, attempting to reconnect...")
                retry_count = 0

                while retry_count < self.max_retry_attempts and self.is_running:
                    try:
                        # 尝试重新注册
                        await self.connect(client_id, ip_addr, port)
                        if await self.ping():
                            self.logger.info("Reconnection successful")
                            break
                    except Exception as e:
                        self.logger.error(f"Reconnection attempt {retry_count + 1}")

                    retry_count += 1
                    if retry_count < self.max_retry_attempts:
                        await asyncio.sleep(self.retry_delay)

                if retry_count >= self.max_retry_attempts:
                    self.logger.error("Max retry attempts reached, connection lost")
                    # 可以在这里添加额外的错误处理逻辑

            await asyncio.sleep(self.ping_interval)

    async def load_model(self, model: str, start_idx: int, end_idx: int):
        model_manager = ModelManager(start_idx, end_idx)
        self.model = model_manager.load_model(self.comm, model)

    async def connect(self, client_id: str, ip_addr: str, port: int):
        """定期发送连接请求的协程"""
        try:
            if not self.init_model_info:
                register_request = RegisterClientRequest(client_id=client_id, host=f"{ip_addr}:{port}")
                response: RegisterClientResponse = await self.register_client(register_request)

                await self.load_model(response.model, response.start_idx, response.end_idx)

                self.init_model_info = {
                    "pp_rank": response.pp_rank,
                    "start_idx": response.start_idx,
                    "end_idx": response.end_idx,
                }
                init_request = InitModelRequest(client_id=client_id, **self.init_model_info)
                response = await self.init_model(init_request)
                self.logger.info(f"Connection successful")
            else:
                register_request = RegisterClientRequest(
                    client_id=client_id,
                    host=f"{ip_addr}:{port}",
                    pp_rank=self.init_model_info["pp_rank"],
                    start_idx=self.init_model_info["start_idx"],
                    end_idx=self.init_model_info["end_idx"],
                )
                response: RegisterClientResponse = await self.register_client(register_request)

        except Exception as e:
            self.logger.error(f"Connection failed")
            raise
