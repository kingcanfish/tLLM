from typing import Dict, List, Optional, Tuple

from PIL import Image
from PIL.ImageFile import ImageFile

from tllm.img_helper import base64_to_pil_image
from tllm.schemas import MESSAGES, MultiModalContent, UrlItem

from .token_utils import TokenizerUtils


class MessageProcessor:
    def __init__(self, tok: TokenizerUtils):
        self.tok = tok
        self.role_set = {"user", "system", "assistant"}

    async def read_image(self, image: UrlItem) -> ImageFile:
        if image.base64 is not None:
            return Image.open(base64_to_pil_image(image.base64))
        if image.url is not None:
            print(image.url)
            raise NotImplementedError("url is not supported")
        if image.file_path is not None:
            return Image.open(image.file_path)
        raise ValueError("image must have url or file_path or base64")

    async def parse_mm_input(self, contents: List[MultiModalContent]) -> Tuple[str, Dict[str, ImageFile]]:
        text, mm_input = "", {}
        for content in contents:
            if content.type == "text":
                text = content.text
            elif content.type == "image_url":
                mm_input["image"] = await self.read_image(content.image_url)
        return text, mm_input

    async def parse_message(self, messages: MESSAGES) -> Tuple[List[Dict[str, str]], Dict[str, ImageFile]]:
        new_messages, mm_inputs = [], []
        for msg in messages:
            assert "role" in msg and "content" in msg, ValueError("role and content must be in message")
            if msg["role"] not in self.role_set:
                raise ValueError(f"role must be in {self.role_set}")
            if isinstance(msg["content"], list):
                text, mm_input = await self.parse_mm_input(msg["content"])
                mm_inputs.append(mm_input)
                new_messages.append({"role": msg["role"], "content": text})
            else:
                new_messages.append({"role": msg["role"], "content": msg["content"]})
        # 校验所有返回的 type 相同
        mm_input_dict, mm_type = {}, None
        for mm_input in mm_inputs:
            for key, value in mm_input.items():
                if key in mm_input_dict:
                    if mm_input_dict[key] != value:
                        raise ValueError(f"mm_input {key} must be the same")
                else:
                    mm_input_dict[key] = value
                if mm_type is None:
                    mm_type = key
                elif mm_type != key:
                    raise ValueError(f"mm_input must be the same type")
        return new_messages, mm_input_dict

    def preprocess(self, messages: List[Dict[str, str]]) -> List[int]:
        return self.tok.preprocess(messages=messages).input_ids

    def fetch_request_id(self, input_ids: List[int]) -> Tuple[Optional[str], int]:
        # max_index, max_id = -1, -1
        # for cache_input_ids, id_ in conversations_dict.items():
        #     index = list_common_prefix(input_ids, cache_input_ids)
        #     if index > max_index:
        #         max_id = id_
        #         max_index = index

        # if max_index == 0 or max_id == -1:
        #     return None, -1
        # return max_id, max_index
        return None, -1
