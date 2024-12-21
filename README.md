## together-LLM

跨机推理 LLM 框架

### QuickStart

1. install dependencies

- for mlx (macos arm):   `pip install -e ".[mlx]"`
- for nvidia: `pip install -e ".[torch]"`

2. run server

   2.1 (no communication)

   ```bash
   tllm.server --model_path mlx-community/Llama-3.2-1B-Instruct-4bit --is_local
   ```

   2.2 (with communication)

   ```bash
   # first in one terminal
   tllm.server --model_path mlx-community/Llama-3.2-1B-Instruct-4bit --hostname $YOUR_IP

   # in another terminal
   tllm.client --hostname $YOUR_IP
   ```
3. testing

```bash
python3 benchmarks/run_async_requests.py
```

### More Details

In `examples/config.json`

```json
// 客户端的数量会决定模型拆分的数量
{
    "server": {
        "grpc_port": 25001,         // server 的 grpc 端口，用于每个 client 发送状态数据以及最后一个 client 发送计算后的结果
        "http_port": 8022,          // server 的 http 端口，API 接口 以及 WebSocket 服务
        "hostname": "mac-mini"      // server 的 hostname，可以用 ip 代替，如 192.168.1.10，需要确保 client 能够访问
    },
    "client": [
        {
            "grpc_port": 25002,     // 第一个 client 的 grpc 端口
            "hostname": "m3pro"     // 第一个 client 的 hostname，需要确保 server 和 其他 client 能够访问
        },
        {
            "grpc_port": 25003,     // 第二个 client 的 grpc 端口
            "hostname": "m3"        // 第二个 client 的 hostname，需要确保 server 和 其他 client 能够访问
        }
    ]
}
```

### Features

- [X] Support Multi-Requests
- [X] Engine
  - [X] mlx
  - [X] torch
  - [ ] tinygrad
    - [ ] Multi-Request
    - [ ] Jit
    - [ ] Pipeline
- [X] Communication
  - [X] grpc
  - [X] Auto Find Node
    - [X] Simple Get Ip
    - [X] Test Ping
- [X] Attention
  - [X] xformers
  - [X] flash-attn
  - [ ] PageAttention

### Performance

In Mac Mini M4

|                      | `mlx-community/Llama-3.2-1B-Instruct-4bit` | `mlx-community/Llama-3.2-1B-Instruct` | `mlx-community/Meta-Llama-3.1-8B-Instruct-4bit` |
| -------------------- | -------------------------------------------- | --------------------------------------- | ------------------------------------------------- |
| Mac Mini M4          | 98.10 tok/s                                 | 35.45 tok/s                             | 20.68 tok/s                                       |
| Mac Mini M4 + M3 Pro |                                              |                                         |                                                   |

For `mlx-community/Llama-3.2-1B-Instruct-4bit`,

![1734779816425](image/README/1734779816425.png)

For `mlx-community/Llama-3.2-1B-Instruct`,

![1734779931105](image/README/1734779931105.png)

For `mlx-community/Meta-Llama-3.1-8B-Instruct-4bit`,

![1734779890405](image/README/1734779890405.png)

old version

For `mlx-community/Llama-3.2-1B-Instruct`

- mac mini m2
  ![alt text](asserts/image.png)
- m3 pro
  ![alt text](asserts/image-1.png)

for 8b

- m3 pro (layer=8) + mac mini m2 (layer=24)
  ![alt text](asserts/image-2.png)
