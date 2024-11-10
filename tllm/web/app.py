import json
import time
from typing import *

import gradio as gr
import requests

from tllm.static.gradio_data import GenerationConfig, custom_css


def process_response_chunk(chunk: bytes) -> Optional[Dict]:
    """处理响应的数据块"""
    try:
        response_text = chunk.decode("utf-8").split("data: ")[-1].strip()
        if response_text == "[DONE]":
            return None
        data_chunk = json.loads(response_text)
        if data_chunk["choices"][0]["finish_reason"] is not None:
            return None
        return data_chunk
    except:
        print("Error decoding chunk", chunk)
        return None


class ChatInterface:
    def __init__(self, chat_url: str):
        self.should_stop = False
        self.config = GenerationConfig()
        self.config.chat_url = chat_url
        self.metric_text = "Tokens Generated: {token_nums}\nSpeed: {speed:.2f} tokens/second"

    def _create_chat_column(self) -> Tuple[gr.Chatbot, gr.Textbox, gr.Button, gr.Button, gr.Button]:
        """创建聊天界面的主列"""
        chatbot = gr.Chatbot(height=600, show_label=False)

        with gr.Row():
            with gr.Column(scale=0.05):
                img = gr.Image(type="filepath", label="上传图片", container=True)
            with gr.Column(scale=13):
                with gr.Row():
                    msg = gr.Textbox(
                        show_label=False,
                        placeholder="输入消息...",
                        container=False,
                    )
                    submit_btn = gr.Button("发送", elem_classes="button-primary", scale=0.05)

                with gr.Row():
                    stop_btn = gr.Button("停止生成", elem_classes="button-secondary", scale=1)
                    clear_btn = gr.Button("清空对话", elem_classes="button-secondary", scale=1)

        return chatbot, img, msg, submit_btn, stop_btn, clear_btn

    def _create_config_column(self) -> List[gr.components.Component]:
        """创建配置界面的侧列"""
        gr.Markdown("### 模型参数设置")
        components = [
            gr.Textbox(label="url", value=self.config.chat_url, lines=2),
            gr.Textbox(label="System Prompt", value=self.config.system_prompt, lines=3),
            gr.Slider(minimum=0.0, maximum=2.0, value=self.config.temperature, step=0.1, label="Temperature"),
            gr.Slider(minimum=0.0, maximum=1.0, value=self.config.top_p, step=0.1, label="Top P"),
            gr.Slider(minimum=1, maximum=100, value=self.config.top_k, step=1, label="Top K"),
            gr.Slider(minimum=1, maximum=8192, value=self.config.max_tokens, step=64, label="Max Tokens"),
        ]

        return components

    def _setup_config_updates(self, components: List[gr.components.Component]) -> None:
        """设置配置更新的回调"""

        def update_config(url, sys_prompt, temp, tp, tk, max_tok):
            self.config.chat_url = url
            self.config.system_prompt = sys_prompt
            self.config.temperature = 1.0
            self.config.top_p = 1.0
            self.config.top_k = -1
            self.config.max_tokens = max_tok

        for component in components:
            component.change(update_config, inputs=components)

    def _format_chat_history(self, img_path, history: List[List[str]]) -> List[Dict[str, str]]:
        """将聊天历史转换为OpenAI格式"""
        formatted_history = []

        if self.config.system_prompt and len(self.config.system_prompt.strip()) > 0:
            formatted_history.append({"role": "system", "content": self.config.system_prompt})

        for message in history:
            user_input, assistant_response = message
            if img_path is None:
                formatted_history.append({"role": "user", "content": user_input})
            else:
                mm_content = [
                    {"type": "text", "text": user_input},
                    {"type": "image_url", "image_url": {"url": img_path}},
                ]
                formatted_history.append({"role": "user", "content": mm_content})
            if assistant_response is not None:
                formatted_history.append({"role": "assistant", "content": assistant_response})

        return formatted_history

    def _prepare_request_data(self, img_path: str, history: List[List[str]]) -> Dict[str, Any]:
        """准备请求数据"""
        return {
            "messages": self._format_chat_history(img_path, history),
            "model": "tt",
            "stream": True,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "top_k": self.config.top_k,
            "max_tokens": self.config.max_tokens,
        }

    def _handle_bot_response(self, img_path: str, history: List[List[str]]) -> Generator:
        """处理机器人的响应"""
        self.should_stop = False
        data = self._prepare_request_data(img_path, history)
        response = requests.post(self.config.chat_url, json=data, stream=True)

        tokens_generated = 0
        start_time = time.time()
        partial_message = ""

        for chunk in response.iter_content(chunk_size=1024):
            if self.should_stop:
                break

            if chunk:
                data_chunk = process_response_chunk(chunk)
                if data_chunk is None:
                    break

                if data_chunk["choices"][0]["delta"]["content"] is not None:
                    partial_message += data_chunk["choices"][0]["delta"]["content"]
                    history[-1][1] = partial_message

                    tokens_generated += 1
                    current_time = time.time()
                    time_elapsed = current_time - start_time
                    tokens_per_second = tokens_generated / time_elapsed

                    yield history, self.metric_text.format(token_nums=tokens_generated, speed=tokens_per_second)

    def _handle_user_input(self, user_message: str, history: List[List[str]]) -> Tuple[gr.update, List[List[str]]]:
        """处理用户输入"""
        return gr.update(value="", interactive=True), history + [[user_message, None]]

    def _handle_stop_generation(self) -> None:
        """处理停止生成"""
        self.should_stop = True

    def _handle_clear_history(self) -> Tuple[List[List[str]], str]:
        """处理清空历史"""
        self.should_stop = False
        return [], ""

    def create_interface(self) -> gr.Blocks:
        """创建完整的聊天界面"""
        with gr.Blocks(css=custom_css, title="tLLM Chat Demo") as demo:
            with gr.Row():
                with gr.Column(scale=4):
                    chatbot, img, msg, submit_btn, stop_btn, clear_btn = self._create_chat_column()

                with gr.Column(scale=1):
                    config_components = self._create_config_column()
                    metrics = gr.Markdown(value=self.metric_text.format(token_nums=0, speed=0))

            self._setup_config_updates(config_components)

            submit_btn.click(self._handle_user_input, inputs=[msg, chatbot], outputs=[msg, chatbot], queue=False).then(
                self._handle_bot_response, inputs=[img, chatbot], outputs=[chatbot, metrics]
            )

            msg.submit(self._handle_user_input, inputs=[msg, chatbot], outputs=[msg, chatbot], queue=False).then(
                self._handle_bot_response, inputs=[img, chatbot], outputs=[chatbot, metrics]
            )

            stop_btn.click(self._handle_stop_generation, queue=False)

            clear_btn.click(self._handle_clear_history, outputs=[chatbot, msg], queue=False)

        return demo


def parse_args():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--chat_url", type=str, default="localhost:8000")
    parser.add_argument("--port", type=int, default=7860)

    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    chat_interface = ChatInterface(args.chat_url)
    demo = chat_interface.create_interface()
    demo.queue()
    demo.launch(server_name="0.0.0.0", server_port=args.port, show_api=False, prevent_thread_lock=False, share=False)
