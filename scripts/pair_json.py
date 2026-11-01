"""JSON文件与标准输入统一按严格UTF-8读取，兼容UTF-8 BOM。"""
import json
from pathlib import Path
import sys


def read_json(filename=None):
    if filename is not None:
        data=Path(filename).read_bytes()
    elif hasattr(sys.stdin,'buffer'):
        data=sys.stdin.buffer.read()
    else:
        # StringIO等文本替身没有buffer；不允许surrogate被静默替换。
        data=sys.stdin.read().encode('utf-8')
    return json.loads(data.decode('utf-8-sig'))
