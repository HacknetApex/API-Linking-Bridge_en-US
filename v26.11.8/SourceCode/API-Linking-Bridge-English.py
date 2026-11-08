#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
API Linking Bridge - Codex Nexus
Single-file config: APIsetting.json (a list; each link has its own port)
"""

import os
import sys
import json
import time
import shutil
import socket
import subprocess
import webbrowser
import codecs
import uuid
import threading
import queue
import logging
import traceback
import platform
import requests
import tkinter as tk
from tkinter import ttk, messagebox
from flask import Flask, request, Response, jsonify

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

# ==================== Version info ====================
VERSION = "26.11.8"
UPDATE_API = "https://apicheckupdate.ctxcode.cn/APILinkingBridge"
HOMEPAGE = "https://alb.ctxcode.cn"
ICON_FILE = "Logo-192x192.ico"   # Window/taskbar icon, kept next to this script

# ==================== Environment & config ====================
def resource_path(filename):
    """Locate static files (icons etc.) that live next to this script.

    - Source run: relative to this .py's directory, not the current working dir;
    - PyInstaller: prefer the unpacked sys._MEIPASS, then the exe's directory.
    """
    candidates = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", "")
        if meipass:
            candidates.append(meipass)
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for base in candidates:
        path = os.path.join(base, filename)
        if os.path.exists(path):
            return path
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), filename)

def init_environment():
    admin_path = r"C:\Users\Administrator"
    if os.path.exists(admin_path):
        base_dir = os.path.join(admin_path, "APILinkingBridge")
    else:
        base_dir = os.path.join(os.environ.get('USERPROFILE', os.path.expanduser("~")), "APILinkingBridge")
    os.makedirs(base_dir, exist_ok=True)
    return os.path.join(base_dir, "APIsetting.json")

API_FILE = init_environment()


# ==================== Crash logging ====================
BASE_DIR = os.path.dirname(API_FILE)
LOG_DIR = os.path.join(BASE_DIR, "logs")
try:
    os.makedirs(LOG_DIR, exist_ok=True)
except Exception:
    LOG_DIR = BASE_DIR

_LAST_CRASH = {"key": "", "time": 0.0}
_DIALOG_COUNT = {"n": 0}
MAX_CRASH_DIALOGS = 3          # At most 3 crash dialogs per run, to avoid spam
_LOG_SINK = None               # Bound to the UI log by install_crash_handlers


def ui_log(text, level="info"):
    """Send a message to the UI log (thread-safe); silently ignored before the UI exists."""
    sink = _LOG_SINK
    if sink is None:
        return
    try:
        sink(text, level)
    except Exception:
        pass


def _write_crash_log(exc_type, exc, tb, context="GUI", recent=None):
    """Write an exception into the logs dir and return its absolute path. Never raises."""
    path = ""
    try:
        now = time.localtime()
        name = "crash-%s-%03d-%d.log" % (
            time.strftime("%Y%m%d-%H%M%S", now),
            int((time.time() % 1) * 1000),
            os.getpid(),
        )
        path = os.path.join(LOG_DIR, name)
        lines = [
            "=" * 78,
            " API Linking Bridge Crash Report",
            "=" * 78,
            "Time      : %s" % time.strftime("%Y-%m-%d %H:%M:%S", now),
            "Version   : %s" % VERSION,
            "Context   : %s" % (context or "unknown"),
            "Python    : %s (%s)" % (sys.version.split()[0], platform.machine()),
            "System    : %s" % platform.platform(),
            "PID       : %s" % os.getpid(),
            "CWD       : %s" % os.getcwd(),
            "Config dir: %s" % BASE_DIR,
            "=" * 78,
            "",
            "------------- Traceback -------------",
            "".join(traceback.format_exception(exc_type, exc, tb)).rstrip(),
            "",
        ]
        if recent:
            lines.append("------------- Recent Logs -------------")
            lines.extend(str(x) for x in recent)
            lines.append("")
        with open(path, "w", encoding="utf-8", errors="replace") as f:
            f.write("\n".join(lines) + "\n")
        # Keep only the 50 most recent crash logs
        try:
            olds = sorted(
                (os.path.join(LOG_DIR, f) for f in os.listdir(LOG_DIR)
                 if f.startswith("crash-") and f.endswith(".log")),
                key=os.path.getmtime)
            for old in olds[:-50]:
                if old != path:
                    try:
                        os.remove(old)
                    except Exception:
                        pass
        except Exception:
            pass
    except Exception:
        path = path or "(failed to write log)"
    return path


def install_crash_handlers(root=None, app=None):
    """Handle uncaught exceptions: log them, show a dialog with the path, never exit the window."""
    global _LOG_SINK
    _LOG_SINK = lambda text, level="info": UI_QUEUE.put(
        ("log_err" if level == "err" else "log", text))

    def _recent():
        # Tk widgets are main-thread only; worker threads skip the UI log
        if threading.current_thread() is not threading.main_thread():
            return []
        try:
            if app is not None and getattr(app, "log_text", None) is not None:
                return app.log_text.get("1.0", "end").splitlines()[-60:]
        except Exception:
            return []
        return []

    def _notify(text, path):
        try:
            messagebox.showerror(
                "An error occurred (the program will keep running)",
                text + "\n\nCrash log saved to:\n" + path +
                "\n\nYou can send this file to the developer for analysis.",
                parent=root,
            )
        except Exception:
            pass

    def handle(exc_type, exc, tb, context, show_dialog=True):
        if exc_type in (KeyboardInterrupt, SystemExit):
            return ""
        path = _write_crash_log(exc_type, exc, tb, context, _recent())
        msg = "%s: %s" % (exc_type.__name__, exc)
        try:
            if app is not None:
                if threading.current_thread() is threading.main_thread():
                    app.log("Error: %s" % msg, is_err=True)
                    app.log("Crash log saved: %s" % path, is_err=True)
                else:
                    # Worker threads must not touch Tk; route through the queue
                    UI_QUEUE.put(("log_err", "Error: %s" % msg))
                    UI_QUEUE.put(("log_err", "Crash log saved: %s" % path))
        except Exception:
            pass
        # Show the same error at most once per 30s
        now = time.time()
        key = "%s|%s" % (exc_type.__name__, str(exc))
        if (show_dialog and _DIALOG_COUNT["n"] < MAX_CRASH_DIALOGS and
                not (key == _LAST_CRASH["key"] and
                     now - _LAST_CRASH["time"] < 30)):
            _LAST_CRASH["key"] = key
            _LAST_CRASH["time"] = now
            _DIALOG_COUNT["n"] += 1
            _notify("An error was caught and the program kept running.\n\n" + msg, path)
        return path

    def hook(exc_type, exc, tb):
        handle(exc_type, exc, tb, "GUI main thread")

    sys.excepthook = hook

    if hasattr(threading, "excepthook"):
        def thook(args):
            if args.exc_type in (KeyboardInterrupt, SystemExit):
                return
            name = args.thread.name if args.thread else "worker thread"
            handle(args.exc_type, args.exc_value, args.exc_traceback,
                   "worker thread %s" % name, show_dialog=False)
        threading.excepthook = thook

    if root is not None:
        def tk_hook(exc_type, exc, tb):
            handle(exc_type, exc, tb, "GUI callback (tkinter)")
        root.report_callback_exception = tk_hook

    return handle


# Base URL is concatenated as f"{apiURL}{path}", so:
#   1) it must include /v1 (or the platform prefix, e.g. /v2, /api/v3);
#   2) it must not end with a slash (that yields //chat/completions).
# Direct mode (ExChangeModel=direct) calls /responses; only upstreams that natively
# support the Responses API (OpenAI, Azure OpenAI, LiteLLM, ...) can use it. Otherwise pick translation.
PROVIDERS = {
    "Custom": "",

    # ---------- Built-in ----------
    "NVIDIA": "https://integrate.api.nvidia.com/v1",
    "DeepSeek": "https://api.deepseek.com/v1",
    "OpenAI": "https://api.openai.com/v1",
    "Moonshot (Kimi)": "https://api.moonshot.cn/v1",
    "Aliyun (Qwen)": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "Zhipu (GLM)": "https://open.bigmodel.cn/api/paas/v4",
    "Groq": "https://api.groq.com/openai/v1",

    # ---------- China providers ----------
    "[CN] Moonshot (Kimi) International": "https://api.moonshot.ai/v1",
    "[CN] Zhipu (GLM) International": "https://api.z.ai/api/paas/v4",
    "[CN] Aliyun (Qwen) International": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
    "[CN] Aliyun (Qwen) US": "https://dashscope-us.aliyuncs.com/compatible-mode/v1",
    "[CN] SiliconFlow": "https://api.siliconflow.cn/v1",
    "[CN] Volcengine Ark": "https://ark.cn-beijing.volces.com/api/v3",
    "[CN] Baidu Qianfan": "https://qianfan.baidubce.com/v2",
    "[CN] Tencent Hunyuan": "https://api.hunyuan.cloud.tencent.com/v1",
    "[CN] iFlytek Spark": "https://spark-api-open.xf-yun.com/v1",
    "[CN] MiniMax": "https://api.minimaxi.com/v1",
    "[CN] MiniMax International": "https://api.minimax.io/v1",
    "[CN] StepFun": "https://api.stepfun.com/v1",
    "[CN] 01.AI": "https://api.lingyiwanwu.com/v1",
    "[CN] Baichuan": "https://api.baichuan-ai.com/v1",
    "[CN] SenseNova": "https://api.sensenova.cn/compatible-mode/v1",
    "[CN] Infini": "https://cloud.infini-ai.com/maas/v1",

    # ---------- Global providers ----------
    "[Overseas] xAI (Grok)": "https://api.x.ai/v1",
    "[Overseas] Mistral": "https://api.mistral.ai/v1",
    "[Overseas] Together AI": "https://api.together.xyz/v1",
    "[Overseas] Fireworks AI": "https://api.fireworks.ai/inference/v1",
    "[Overseas] DeepInfra": "https://api.deepinfra.com/v1/openai",
    # Perplexity has no /v1 prefix and no GET /models; Test Connection fails but chat works
    "[Overseas] Perplexity": "https://api.perplexity.ai",
    "[Overseas] Cerebras": "https://api.cerebras.ai/v1",
    "[Overseas] SambaNova": "https://api.sambanova.ai/v1",
    "[Overseas] Nebius Token Factory": "https://api.tokenfactory.nebius.com/v1",
    "[Overseas] Hyperbolic": "https://api.hyperbolic.xyz/v1",
    "[Overseas] Novita AI": "https://api.novita.ai/openai/v1",
    "[Overseas] Lambda Inference": "https://api.lambda.ai/v1",
    "[Overseas] Google Gemini": "https://generativelanguage.googleapis.com/v1beta/openai",
    "[Overseas] Cohere": "https://api.cohere.ai/compatibility/v1",

    # ---------- Aggregators / cloud ----------
    "[Aggregator] OpenRouter": "https://openrouter.ai/api/v1",
    "[Aggregator] AIML API": "https://api.aimlapi.com/v1",
    "[Aggregator] Requesty": "https://router.requesty.ai/v1",
    "[Aggregator] GitHub Models": "https://models.inference.ai.azure.com",
    # Replace {ACCOUNT_ID} with your Cloudflare account ID
    "[Aggregator] Cloudflare Workers AI": "https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1",
    # This tool sends only Authorization: Bearer; Azure needs an api-key header + ?api-version= and would 401/404
    "[Aggregator] Azure OpenAI (needs api-key header; unsupported here)": "https://{resource}.openai.azure.com/openai/deployments/{deployment}",

    # ---------- Local / self-hosted ----------
    "[Local] Ollama": "http://localhost:11434/v1",
    "[Local] LM Studio": "http://localhost:1234/v1",
    "[Local] vLLM": "http://localhost:8000/v1",
    "[Local] llama.cpp": "http://localhost:8080/v1",
    "[Local] LocalAI": "http://localhost:8080/v1",
    "[Local] Xinference": "http://localhost:9997/v1",
    "[Local] LiteLLM Proxy": "http://localhost:4000/v1",
    "[Local] One API / New API": "http://localhost:3000/v1",
}

def load_json(path, default_data):
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_data, f, indent=4, ensure_ascii=False)
        return default_data
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, list) else default_data
    except Exception:
        return default_data

def migrate_legacy_config(data, path):
    """Legacy APIsetting.json is a {url, key, mode, disable_tools, links} dict; convert it to a list."""
    links = []
    legacy = data.get("links") if isinstance(data, dict) else None
    if isinstance(legacy, dict):
        for i, (name, v) in enumerate(legacy.items(), 1):
            if not isinstance(v, dict):
                continue
            links.append({
                "name": name or f"Link{i}",
                "apiURL": v.get("url", v.get("apiURL", "")),
                "apiKey": v.get("key", v.get("apiKey", "")),
                "model": v.get("model", ""),
                "ListenPort": int(v.get("ListenPort", 15720 + i)),
                "disable_tools": bool(v.get("disable_tools", False)),
                "ExChangeModel": "direct" if v.get("mode") == "direct" else "translation",
            })
    if not links and (data.get("url") or data.get("key")):
        # Do not inherit the model name from the old AppearanceSetting.json: it belongs to a
        # previous provider and would 404 here. Leave it empty for the user to fill in.
        default_model = data.get("model") or ""
        links.append({
            "name": "Default Link",
            "apiURL": data.get("url", ""),
            "apiKey": data.get("key", ""),
            "model": default_model,
            "ListenPort": 15721,
            "disable_tools": bool(data.get("disable_tools", False)),
            "ExChangeModel": "direct" if data.get("mode") == "direct" else "translation",
        })
    return links

def save_links(links):
    with open(API_FILE, "w", encoding="utf-8") as f:
        json.dump(links, f, indent=4, ensure_ascii=False)

def load_links():
    """Load the link list; migrate legacy dict configs and back up the original file."""
    if not os.path.exists(API_FILE):
        return []
    try:
        with open(API_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    if isinstance(data, list):
        return data
    migrated = migrate_legacy_config(data, API_FILE)
    try:
        with open(API_FILE + ".bak", "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        save_links(migrated)
    except Exception:
        pass
    return migrated

# ==================== Proxy core state ====================
PROXY_APP = Flask(__name__)
UI_QUEUE = queue.Queue()
GLOBAL_STATS = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0,
                "requests": 0, "missing": 0}

PROXY_STATE = {"active_link": None, "config": {}, "server": None, "thread": None}


@PROXY_APP.errorhandler(Exception)
def _proxy_internal_error(e):
    """Exception during a request: log it, keep serving, and echo the log path to the client."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        return e
    path = _write_crash_log(type(e), e, e.__traceback__, "HTTP request handling",
                            ["%s %s" % (request.method, request.path)])
    ui_log("Request handling error: %s: %s" % (type(e).__name__, e), "err")
    ui_log("Crash log saved: %s" % path, "err")
    return jsonify({
        "error": {
            "message": "Internal server error; see the log file: %s" % path,
            "type": "internal_error",
        }
    }), 500


def get_active_config():
    return PROXY_STATE.get("config", {})

# ---------- Protocol conversion ----------
def convert_tools_for_chat(tools):
    if not tools:
        return []
    chat_tools = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        t_type = t.get("type", "")
        nested = t.get("function") if isinstance(t.get("function"), dict) else {}
        name = (t.get("name") or nested.get("name") or "").strip()
        description = t.get("description") or nested.get("description") or ""
        parameters = t.get("parameters") or nested.get("parameters")
        if t_type == "function":
            if not parameters:
                parameters = {"type": "object", "properties": {}}
            if not name:
                continue
            chat_tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": parameters}})
        elif t_type == "custom":
            if not name:
                continue
            chat_tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": parameters or {"type": "object", "properties": {"input": {"type": "string"}}}}})
        elif t_type == "web_search":
            chat_tools.append({"type": "function", "function": {"name": "web_search", "description": "Search the web", "parameters": {"type": "object", "properties": {"query": {"type": "string"}}}}})
        else:
            if name:
                chat_tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": parameters or {"type": "object", "properties": {}}}})
    return chat_tools

def responses_to_chat(payload, default_model="gpt-4o", disable_tools=False, model_override=""):
    model = model_override or payload.get("model") or default_model
    messages = []
    if payload.get("instructions"):
        messages.append({"role": "system", "content": payload["instructions"]})
    raw_input = payload.get("input")
    if isinstance(raw_input, str):
        messages.append({"role": "user", "content": raw_input})
    elif isinstance(raw_input, list):
        for item in raw_input:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type in (None, "message"):
                role = item.get("role", "user")
                content = item.get("content", "")
                if isinstance(content, list):
                    parts = []
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") in ("input_text", "output_text", "text"):
                                parts.append({"type": "text", "text": c.get("text", "")})
                            elif c.get("type") in ("input_image", "image_url"):
                                url = c.get("image_url")
                                if isinstance(url, dict):
                                    url = url.get("url")
                                if url:
                                    parts.append({"type": "image_url", "image_url": {"url": url}})
                    content = parts if parts else ""
                messages.append({"role": role, "content": content})
            elif item_type == "function_call":
                # Tool calls started by the assistant must become assistant.tool_calls,
                # otherwise the next tool message matches no call and strict upstreams return 400
                call = {
                    "id": item.get("call_id") or item.get("id") or ("call_" + uuid.uuid4().hex),
                    "type": "function",
                    "function": {
                        "name": item.get("name", ""),
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    messages[-1].setdefault("tool_calls", []).append(call)
                    if messages[-1].get("content") == "":
                        messages[-1]["content"] = None
                else:
                    messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
            elif item_type == "function_call_output":
                output = item.get("output", "")
                if not isinstance(output, str):
                    output = json.dumps(output, ensure_ascii=False)
                messages.append({"role": "tool", "tool_call_id": item.get("call_id", ""), "content": output})
            # Ignore reasoning and other item types

    chat = {"model": model, "messages": messages, "stream": bool(payload.get("stream", False))}
    if payload.get("temperature") is not None:
        chat["temperature"] = payload["temperature"]
    if payload.get("top_p") is not None:
        chat["top_p"] = payload["top_p"]
    if payload.get("max_output_tokens"):
        chat["max_tokens"] = payload["max_output_tokens"]

    if not disable_tools:
        chat_tools = convert_tools_for_chat(payload.get("tools"))
        if chat_tools:
            chat["tools"] = chat_tools
    return chat

def sse(event_type, data):
    return f"event: {event_type}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"

def stream_chat_to_responses(upstream, model, response_id):
    created_at = int(time.time())
    yield sse("response.created", {"type": "response.created", "response": {"id": response_id, "object": "response", "created_at": created_at, "status": "in_progress", "model": model, "output": []}})

    output_index = 0
    output_items = []
    usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}
    reasoning_started, reasoning_text, reasoning_id = False, "", "rs_" + uuid.uuid4().hex
    text_started, content_text, text_id = False, "", "msg_" + uuid.uuid4().hex
    tool_buffers = {}
    last_data_time = time.time()
    # Incremental decoder + line buffer: split UTF-8 chars and half-line JSON are preserved
    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    finished = False
    usage_seen = False

    for raw_bytes in upstream.iter_content(chunk_size=1024):
        if not raw_bytes:
            continue
        if time.time() - last_data_time > 3:
            yield ": keep-alive\n\n"
            last_data_time = time.time()
        buffer += decoder.decode(raw_bytes)
        if "\n" not in buffer:
            continue
        lines = buffer.split("\n")
        buffer = lines.pop()  # the last segment may be a partial line; keep it for the next chunk
        for line in lines:
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(data_str)
            except Exception:
                continue
            last_data_time = time.time()

            if chunk.get("usage"):
                u = chunk["usage"]
                usage = {"input_tokens": u.get("prompt_tokens", 0) or 0,
                         "output_tokens": u.get("completion_tokens", 0) or 0,
                         "total_tokens": u.get("total_tokens", 0) or 0}
                usage_seen = True
                UI_QUEUE.put(("stats", {"input": usage["input_tokens"], "output": usage["output_tokens"], "total": usage["total_tokens"]}))

            for choice in chunk.get("choices") or []:
                delta = choice.get("delta") or {}

                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if reasoning and not text_started:
                    if not reasoning_started:
                        reasoning_started = True
                        yield sse("response.output_item.added", {"type": "response.output_item.added", "output_index": output_index, "item": {"type": "reasoning", "id": reasoning_id, "summary": []}})
                        yield sse("response.reasoning_summary_part.added", {"type": "response.reasoning_summary_part.added", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "part": {"type": "summary_text", "text": ""}})
                    reasoning_text += reasoning
                    yield sse("response.reasoning_summary_text.delta", {"type": "response.reasoning_summary_text.delta", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "delta": reasoning})

                content = delta.get("content")
                if content:
                    if reasoning_started and not text_started:
                        reasoning_item = {"type": "reasoning", "id": reasoning_id, "summary": [{"type": "summary_text", "text": reasoning_text}]}
                        yield sse("response.reasoning_summary_text.done", {"type": "response.reasoning_summary_text.done", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "text": reasoning_text})
                        yield sse("response.reasoning_summary_part.done", {"type": "response.reasoning_summary_part.done", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "part": {"type": "summary_text", "text": reasoning_text}})
                        yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": reasoning_item})
                        output_items.append(reasoning_item)
                        output_index += 1
                        reasoning_started = False

                    if not text_started:
                        text_started = True
                        yield sse("response.output_item.added", {"type": "response.output_item.added", "output_index": output_index, "item": {"type": "message", "id": text_id, "role": "assistant", "status": "in_progress", "phase": "final_answer", "content": []}})
                        yield sse("response.content_part.added", {"type": "response.content_part.added", "item_id": text_id, "output_index": output_index, "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []}})

                    content_text += content
                    yield sse("response.output_text.delta", {"type": "response.output_text.delta", "item_id": text_id, "output_index": output_index, "content_index": 0, "delta": content})

                tcs = delta.get("tool_calls")
                if tcs and isinstance(tcs, list):
                    for tc in tcs:
                        idx = tc.get("index", 0)
                        buf = tool_buffers.setdefault(idx, {"id": None, "name": "", "arguments": ""})
                        if tc.get("id"):
                            buf["id"] = tc["id"]
                        fn = tc.get("function") or {}
                        if fn.get("name") and not buf["name"]:
                            buf["name"] = fn["name"]
                        if fn.get("arguments"):
                            buf["arguments"] += fn["arguments"]
        if finished:
            break

    if reasoning_started:
        reasoning_item = {"type": "reasoning", "id": reasoning_id, "summary": [{"type": "summary_text", "text": reasoning_text}]}
        yield sse("response.reasoning_summary_text.done", {"type": "response.reasoning_summary_text.done", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "text": reasoning_text})
        yield sse("response.reasoning_summary_part.done", {"type": "response.reasoning_summary_part.done", "item_id": reasoning_id, "output_index": output_index, "summary_index": 0, "part": {"type": "summary_text", "text": reasoning_text}})
        yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": reasoning_item})
        output_items.append(reasoning_item)
        output_index += 1

    if text_started:
        message_item = {"type": "message", "id": text_id, "role": "assistant", "status": "completed", "phase": "final_answer", "content": [{"type": "output_text", "text": content_text, "annotations": []}]}
        yield sse("response.output_text.done", {"type": "response.output_text.done", "item_id": text_id, "output_index": output_index, "content_index": 0, "text": content_text})
        yield sse("response.content_part.done", {"type": "response.content_part.done", "item_id": text_id, "output_index": output_index, "content_index": 0, "part": {"type": "output_text", "text": content_text, "annotations": []}})
        yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": message_item})
        output_items.append(message_item)
        output_index += 1

    for idx in sorted(tool_buffers.keys()):
        buf = tool_buffers[idx]
        if not buf["name"]:
            continue
        cid = buf["id"] or "call_" + uuid.uuid4().hex
        fid = "fc_" + uuid.uuid4().hex
        fc_item = {"type": "function_call", "id": fid, "call_id": cid, "name": buf["name"], "arguments": buf["arguments"] or "{}", "status": "completed"}
        yield sse("response.output_item.added", {"type": "response.output_item.added", "output_index": output_index, "item": {"type": "function_call", "id": fid, "call_id": cid, "name": buf["name"], "arguments": "", "status": "in_progress"}})
        yield sse("response.function_call_arguments.delta", {"type": "response.function_call_arguments.delta", "item_id": fid, "output_index": output_index, "delta": buf["arguments"]})
        yield sse("response.function_call_arguments.done", {"type": "response.function_call_arguments.done", "item_id": fid, "output_index": output_index, "arguments": buf["arguments"]})
        yield sse("response.output_item.done", {"type": "response.output_item.done", "output_index": output_index, "item": fc_item})
        output_items.append(fc_item)
        output_index += 1

    if not usage_seen:
        UI_QUEUE.put(("stats", None))
    yield sse("response.completed", {"type": "response.completed", "response": {"id": response_id, "object": "response", "created_at": created_at, "status": "completed", "model": model, "output": output_items, "usage": usage}})

def chat_to_responses(chat, model, response_id):
    """Convert a non-streaming chat.completion into a Responses API object."""
    created_at = int(time.time())
    output = []
    choices = chat.get("choices") or [{}]
    msg = (choices[0] or {}).get("message") or {}

    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if reasoning:
        output.append({"type": "reasoning", "id": "rs_" + uuid.uuid4().hex,
                       "summary": [{"type": "summary_text", "text": reasoning}]})

    content = msg.get("content")
    if content:
        output.append({"type": "message", "id": "msg_" + uuid.uuid4().hex, "role": "assistant",
                       "status": "completed",
                       "content": [{"type": "output_text", "text": content, "annotations": []}]})

    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        output.append({"type": "function_call", "id": "fc_" + uuid.uuid4().hex,
                       "call_id": tc.get("id") or ("call_" + uuid.uuid4().hex),
                       "name": fn.get("name", ""),
                       "arguments": fn.get("arguments") or "{}",
                       "status": "completed"})

    u = chat.get("usage") or {}
    return {"id": response_id, "object": "response", "created_at": created_at,
            "status": "completed", "model": model, "output": output,
            "usage": {"input_tokens": u.get("prompt_tokens", 0) or 0,
                      "output_tokens": u.get("completion_tokens", 0) or 0,
                      "total_tokens": u.get("total_tokens", 0) or 0}}

def api_base(api):
    """Return the Base URL without a trailing slash.

    Paths are concatenated as f"{apiURL}{path}"; typing "https://x/v1/" produces
    "https://x/v1//chat/completions", which most upstreams 404, so normalize here.
    """
    return (api.get("apiURL") or "").strip().rstrip("/")

def _post_upstream(api, path, body, stream):
    """Send a request upstream.

    Streaming requests set stream_options.include_usage so the provider reports
    real token usage in the final chunk. Some providers reject it with a 400,
    in which case it is dropped and the request retried once.
    """
    headers = {"Authorization": f"Bearer {api['apiKey']}", "Content-Type": "application/json"}
    body = dict(body)
    if stream:
        opts = dict(body.get("stream_options") or {})
        opts["include_usage"] = True
        body["stream_options"] = opts
    upstream = requests.post(f"{api_base(api)}{path}", headers=headers, json=body, stream=stream, timeout=600)
    if stream and upstream.status_code != 200 and "stream_options" in body:
        upstream.close()
        body.pop("stream_options", None)
        UI_QUEUE.put(("log", "Upstream rejected stream_options; using plain streaming (token usage may be unavailable)"))
        upstream = requests.post(f"{api_base(api)}{path}", headers=headers, json=body, stream=stream, timeout=600)
    return upstream

def _push_stats(data):
    u = (data or {}).get("usage") or {}
    if not u:
        UI_QUEUE.put(("stats", None))
        return
    inp = u.get("prompt_tokens", u.get("input_tokens", 0)) or 0
    out = u.get("completion_tokens", u.get("output_tokens", 0)) or 0
    tot = u.get("total_tokens", 0) or 0
    if not tot:
        tot = inp + out
    UI_QUEUE.put(("stats", {"input": inp, "output": out, "total": tot}))

@PROXY_APP.route("/v1/responses", methods=["POST"])
def handle_responses():
    api = get_active_config()
    if not api:
        return jsonify({"error": "Enable a link on the Dashboard first"}), 403

    payload = request.get_json(force=True, silent=True) or {}
    UI_QUEUE.put(("anim_start", None))

    model_override = (api.get("model") or "").strip()
    stream = bool(payload.get("stream", False))

    if api.get("ExChangeModel") == "direct":
        if model_override:
            payload["model"] = model_override
        try:
            upstream = _post_upstream(api, "/responses", payload, stream)
        except Exception as e:
            UI_QUEUE.put(("anim_end", None))
            return jsonify({"error": f"Network error: {e}"}), 502

        if upstream.status_code != 200:
            UI_QUEUE.put(("anim_end", None))
            UI_QUEUE.put(("log_err", f"Upstream rejected direct request {upstream.status_code}: {upstream.text[:300]}"))
            return jsonify({"error": upstream.text}), upstream.status_code

        if not stream:
            UI_QUEUE.put(("anim_end", None))
            try:
                data = upstream.json()
            except Exception:
                data = {}
            upstream.close()
            _push_stats(data)
            return jsonify(data)

        def direct_gen():
            decoder = codecs.getincrementaldecoder("utf-8")("replace")
            buffer = ""
            usage_seen = False
            try:
                for c in upstream.iter_content(chunk_size=1024):
                    if not c:
                        continue
                    yield c
                    buffer += decoder.decode(c)
                    if "\n" not in buffer:
                        continue
                    lines = buffer.split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        line = line.strip()
                        if not line.startswith("data:"):
                            continue
                        try:
                            obj = json.loads(line[5:].strip())
                        except Exception:
                            continue
                        resp = obj.get("response") if isinstance(obj, dict) else None
                        if isinstance(resp, dict) and resp.get("usage"):
                            usage_seen = True
                            _push_stats(resp)
                if not usage_seen:
                    UI_QUEUE.put(("stats", None))
            finally:
                upstream.close()
                UI_QUEUE.put(("anim_end", None))

        return Response(direct_gen(), status=200, mimetype="text/event-stream")

    try:
        chat_req = responses_to_chat(payload, disable_tools=api.get("disable_tools", False), model_override=model_override)
    except Exception as e:
        UI_QUEUE.put(("anim_end", None))
        return jsonify({"error": f"Request conversion failed: {e}"}), 400

    stream = chat_req.get("stream", False)

    try:
        upstream = _post_upstream(api, "/chat/completions", chat_req, stream)
    except Exception as e:
        UI_QUEUE.put(("anim_end", None))
        return jsonify({"error": f"Network error: {e}"}), 502

    if upstream.status_code != 200:
        UI_QUEUE.put(("anim_end", None))
        err_text = upstream.text
        UI_QUEUE.put(("log_err", f"Upstream error {upstream.status_code}: {err_text[:300]}"))
        return jsonify({"error": err_text}), upstream.status_code

    response_id = "resp_" + uuid.uuid4().hex

    if not stream:
        try:
            chat_resp = upstream.json()
        except Exception:
            chat_resp = {}
        upstream.close()
        UI_QUEUE.put(("anim_end", None))
        _push_stats(chat_resp)
        return jsonify(chat_to_responses(chat_resp, chat_req["model"], response_id))

    def generate():
        try:
            for chunk in stream_chat_to_responses(upstream, chat_req["model"], response_id):
                yield chunk
        finally:
            upstream.close()
            UI_QUEUE.put(("anim_end", None))

    return Response(generate(), status=200, mimetype="text/event-stream")

@PROXY_APP.route("/v1/chat/completions", methods=["POST"])
def handle_chat_completions():
    """Pass-through endpoint for plain OpenAI-style clients."""
    api = get_active_config()
    if not api:
        return jsonify({"error": "Enable a link on the Dashboard first"}), 403

    payload = request.get_json(force=True, silent=True) or {}
    UI_QUEUE.put(("anim_start", None))

    model_override = (api.get("model") or "").strip()
    if model_override:
        payload["model"] = model_override

    stream = bool(payload.get("stream", False))

    try:
        upstream = _post_upstream(api, "/chat/completions", payload, stream)
    except Exception as e:
        UI_QUEUE.put(("anim_end", None))
        return jsonify({"error": f"Network error: {e}"}), 502

    if upstream.status_code != 200:
        UI_QUEUE.put(("anim_end", None))
        UI_QUEUE.put(("log_err", f"Upstream error {upstream.status_code}: {upstream.text[:300]}"))
        return jsonify({"error": upstream.text}), upstream.status_code

    if not stream:
        try:
            data = upstream.json()
        except Exception:
            data = {}
        upstream.close()
        UI_QUEUE.put(("anim_end", None))
        _push_stats(data)
        return jsonify(data)

    def generate():
        # Forward untouched while scanning for the trailing usage chunk (token stats)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        buffer = ""
        usage_seen = False
        try:
            for c in upstream.iter_content(chunk_size=1024):
                if not c:
                    continue
                yield c
                buffer += decoder.decode(c)
                if "\n" not in buffer:
                    continue
                lines = buffer.split("\n")
                buffer = lines.pop()
                for line in lines:
                    line = line.strip()
                    if not line.startswith("data:"):
                        continue
                    ds = line[5:].strip()
                    if not ds or ds == "[DONE]":
                        continue
                    try:
                        obj = json.loads(ds)
                    except Exception:
                        continue
                    if obj.get("usage"):
                        usage_seen = True
                        _push_stats(obj)
            if not usage_seen:
                UI_QUEUE.put(("stats", None))
        finally:
            upstream.close()
            UI_QUEUE.put(("anim_end", None))

    return Response(generate(), status=200, mimetype="text/event-stream")

@PROXY_APP.route("/v1/models", methods=["GET"])
def list_models():
    api = get_active_config()
    if not api or not api.get("apiURL") or not api.get("apiKey"):
        return jsonify({"data": []})
    try:
        resp = requests.get(f"{api_base(api)}/models", headers={"Authorization": f"Bearer {api['apiKey']}"}, timeout=10)
        if resp.status_code == 200:
            return jsonify(resp.json())
    except Exception:
        pass
    return jsonify({"data": []})

# ==================== Port occupancy check ====================
def _find_port_pid(port):
    """On Windows, use netstat/tasklist to find the process listening on a port.
    Returns (None, "") when it cannot be determined."""
    if os.name != "nt":
        return None, ""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        raw = subprocess.check_output(["netstat", "-ano", "-p", "TCP"],
                                      creationflags=flags, timeout=6)
        text = raw.decode("mbcs", errors="replace")
    except Exception:
        return None, ""
    pid = None
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        local, state, pid_text = parts[1], parts[3], parts[4]
        if not local.endswith(":" + str(port)):
            continue
        if state.upper() != "LISTENING":
            continue
        try:
            pid = int(pid_text)
        except ValueError:
            continue
        break
    if pid is None:
        return None, ""
    name = ""
    try:
        raw = subprocess.check_output(
            ["tasklist", "/FI", "PID eq %d" % pid, "/FO", "CSV", "/NH"],
            creationflags=flags, timeout=6)
        lines = raw.decode("mbcs", errors="replace").strip().splitlines()
        if lines:
            name = lines[0].split(",")[0].strip('"')
    except Exception:
        pass
    return pid, name


def _port_owner(port):
    """Return (is_listening, pid, process_name). Used for warnings only."""
    try:
        port = int(port)
    except Exception:
        return False, None, ""
    sock = None
    listening = False
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.4)
        listening = (sock.connect_ex(("127.0.0.1", port)) == 0)
    except Exception:
        listening = False
    finally:
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
    if not listening:
        return False, None, ""
    pid, name = _find_port_pid(port)
    return True, pid, name


def describe_port(port, ours=False):
    """Turn the port state into a human-readable sentence."""
    if ours:
        return "● Local relay running at  http://127.0.0.1:%s" % port
    in_use, pid, name = _port_owner(port)
    if not in_use:
        return "○ Port %s is free" % port
    who = name or "unknown process"
    if pid is not None:
        who += " (PID %s)" % pid
    extra = ""
    if pid is not None and pid == os.getpid():
        extra = " (this process)"
    elif name and name.lower().startswith(("python", "api-linking-bridge", "api linking bridge")):
        extra = ", possibly another instance of this app"
    return "⚠ Port %s is already used by another program: %s%s" % (port, who, extra)


# ==================== Cache cleanup ====================
# Only explicit cache files are removed; login/config files such as
# auth.json and config.toml are never touched.
CACHE_CATEGORIES = [
    ("models", "Model list cache", "Fixes Codex / client model list not refreshing"),
    ("chat", "Chat / session cache", "Codex session history and this app's chat history"),
    ("logs", "Log cache", "Crash logs and Codex runtime logs"),
    ("temp", "Temporary files", "Temporary files under the cache directories"),
]
CACHE_TITLES = {key: title for key, title, _ in CACHE_CATEGORIES}
_CACHE_PROTECTED = {"auth.json", "config.toml", "config.json", "settings.json",
                    "AGENTS.md", "instructions.md"}


def codex_home():
    """Locate the Codex config/cache directory: CODEX_HOME first, then ~/.codex."""
    env_path = (os.environ.get("CODEX_HOME") or "").strip()
    if env_path:
        return os.path.expandvars(os.path.expanduser(env_path))
    return os.path.join(os.path.expanduser("~"), ".codex")


def _path_size(path):
    try:
        if os.path.isfile(path):
            return os.path.getsize(path)
        total = 0
        for root, _, files in os.walk(path):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    pass
        return total
    except OSError:
        return 0


def fmt_size(size):
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return ("%d %s" % (value, unit)) if unit == "B" else ("%.1f %s" % (value, unit))
        value /= 1024.0
    return "%.1f GB" % value


def _cache_targets(categories=None):
    """Collect cache paths to clean. Each path appears once; risky files are skipped."""
    cats = set(categories or [key for key, _, _ in CACHE_CATEGORIES])
    targets, seen = [], set()

    def add(cat, source, path):
        if cat not in cats:
            return
        try:
            full = os.path.abspath(path)
        except Exception:
            return
        base = os.path.basename(full).lower()
        if base in _CACHE_PROTECTED or base.endswith((".toml", ".key", ".pem")):
            return
        if full in seen or not os.path.exists(full):
            return
        seen.add(full)
        targets.append({"cat": cat, "source": source, "path": full,
                        "is_dir": os.path.isdir(full), "size": _path_size(full)})

    home = codex_home()
    if os.path.isdir(home):
        # Model list cache: Codex stores model-catalog.json / cc-switch-model-catalog.json
        try:
            for name in os.listdir(home):
                low = name.lower()
                if "model" in low and ("cache" in low or "catalog" in low):
                    add("models", "Codex", os.path.join(home, name))
        except Exception:
            pass
        for name in ("sessions", "archived_sessions", "session_index.jsonl",
                     "history.jsonl", "history.json", "transcription-history.jsonl"):
            add("chat", "Codex", os.path.join(home, name))
        for name in ("log", "logs"):
            add("logs", "Codex", os.path.join(home, name))
        for name in ("tmp", "temp", "cache", ".tmp"):
            add("temp", "Codex", os.path.join(home, name))
        try:
            for name in os.listdir(home):
                if name.lower().endswith((".tmp", ".temp")):
                    add("temp", "Codex", os.path.join(home, name))
        except Exception:
            pass

    # This app's own cache
    add("temp", "This app", os.path.join(BASE_DIR, "cache"))
    add("chat", "This app", CHAT_HISTORY_FILE)
    add("models", "This app", os.path.join(BASE_DIR, "model_cache.json"))
    if "logs" in cats:
        try:
            for name in os.listdir(LOG_DIR):
                if name.startswith("crash-") and name.endswith(".log"):
                    add("logs", "This app", os.path.join(LOG_DIR, name))
        except Exception:
            pass

    targets.sort(key=lambda item: (item["cat"], item["path"].lower()))
    return targets


def remove_cache_targets(targets):
    """Delete cache targets. Returns (removed, [(target, error), ...])."""
    removed, failed = [], []
    for target in targets:
        try:
            if target["is_dir"]:
                shutil.rmtree(target["path"])
            else:
                os.remove(target["path"])
            removed.append(target)
        except Exception as exc:
            failed.append((target, str(exc)))
    return removed, failed


# ==================== Built-in chat ====================
CHAT_HISTORY_FILE = os.path.join(BASE_DIR, "chat_history.json")


def load_chat_history():
    try:
        with open(CHAT_HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, list):
            return [m for m in data
                    if isinstance(m, dict)
                    and m.get("role") in ("user", "assistant")
                    and isinstance(m.get("content"), str)]
    except Exception:
        pass
    return []


def save_chat_history(messages):
    try:
        os.makedirs(BASE_DIR, exist_ok=True)
        with open(CHAT_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(messages[-200:], f, indent=2, ensure_ascii=False)
    except Exception:
        pass


def fetch_upstream_models(base, api_key, timeout=15):
    """Fetch the upstream model list (GET /models); returns (sorted ids, error)."""
    try:
        url = (base or "").rstrip("/") + "/models"
        headers = {"Authorization": "Bearer %s" % (api_key or "")}
        resp = requests.get(url, headers=headers, timeout=timeout)
        if resp.status_code != 200:
            return [], "HTTP %s: %s" % (resp.status_code, resp.text[:200])
        payload = resp.json()
        items = None
        if isinstance(payload, dict):
            items = payload.get("data")
            if items is None:
                items = payload.get("models")
        if not isinstance(items, list):
            return [], "Unexpected response format"
        out = []
        for item in items:
            if isinstance(item, dict):
                mid = item.get("id") or item.get("name")
            else:
                mid = item
            if mid and str(mid) not in out:
                out.append(str(mid))
        return sorted(out), ""
    except Exception as exc:
        return [], str(exc)

def iter_chat_stream(base, api_key, model, messages, stop_event=None):
    """Call the upstream /chat/completions stream and yield
    ("content" | "reasoning" | "usage", value)."""
    url = base.rstrip("/") + "/chat/completions"
    headers = {"Authorization": "Bearer %s" % api_key,
               "Content-Type": "application/json"}
    body = {"model": model, "messages": messages, "stream": True,
            "stream_options": {"include_usage": True}}
    resp = None
    try:
        resp = requests.post(url, headers=headers, json=body, stream=True, timeout=600)
        if resp.status_code != 200:
            resp.close()
            # A few providers reject stream_options; retry as plain streaming once.
            body.pop("stream_options", None)
            resp = requests.post(url, headers=headers, json=body, stream=True, timeout=600)
            if resp.status_code != 200:
                detail = resp.text[:500]
                resp.close()
                raise RuntimeError("Upstream returned %s: %s" % (resp.status_code, detail))
    except Exception:
        if resp is not None:
            try:
                resp.close()
            except Exception:
                pass
        raise

    decoder = codecs.getincrementaldecoder("utf-8")("replace")
    buffer = ""
    try:
        for raw in resp.iter_content(chunk_size=1024):
            if stop_event is not None and stop_event.is_set():
                break
            if not raw:
                continue
            buffer += decoder.decode(raw)
            if "\n" not in buffer:
                continue
            lines = buffer.split("\n")
            buffer = lines.pop()
            for line in lines:
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload or payload == "[DONE]":
                    continue
                try:
                    chunk = json.loads(payload)
                except Exception:
                    continue
                usage = chunk.get("usage")
                if usage:
                    yield ("usage", usage)
                for choice in chunk.get("choices") or []:
                    delta = choice.get("delta") or {}
                    reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                    if reasoning:
                        yield ("reasoning", reasoning)
                    content = delta.get("content")
                    if isinstance(content, str) and content:
                        yield ("content", content)
    finally:
        try:
            resp.close()
        except Exception:
            pass


# ==================== Tkinter UI ====================
class APIBridgeApp:
    def __init__(self, root):
        self.root = root
        self.root.title("API Linking Bridge - Made by Apex - Licensed - Details at https://ctxcode.cn")
        self.root.geometry("960x720")
        self.root.configure(bg="#1e1e1e")

        self.api_data = load_links()
        self.current_selected_index = -1
        self.is_new_link = False
        self.is_running = False
        self.particles = []

        self._updating = False
        self.apply_icon()
        self.setup_ui()
        self.root.after(100, self.process_queue)
        # Version check runs in a worker thread so a slow server cannot block startup
        threading.Thread(target=self.check_update, daemon=True).start()

    def apply_icon(self):
        """Use Logo-192x192.ico next to the script as the title-bar/taskbar icon.

        Windows uses iconbitmap (native .ico, scaled by the system per DPI);
        if the .ico is missing or corrupt, fall back to Logo.png (Tk 8.6 reads PNG natively).
        """
        ico = resource_path(ICON_FILE)
        if os.path.exists(ico):
            try:
                self.root.iconbitmap(ico)           # apply immediately to the current window
                self.root.iconbitmap(default=ico)   # later Toplevels inherit it too
                self._icon_path = ico
                return
            except Exception as e:
                UI_QUEUE.put(("log_err", f"Failed to load the ICO icon, falling back to PNG: {e}"))

        path = resource_path("Logo.png")
        if not os.path.exists(path):
            UI_QUEUE.put(("log_err", f"Icon not found: {ICON_FILE} / Logo.png; expected in: {os.path.dirname(ico)}"))
            return
        try:
            base = tk.PhotoImage(file=path)
            icons = [base]
            for factor in (4, 8, 16):
                try:
                    icons.append(base.subsample(factor, factor))
                except tk.TclError:
                    pass
            self.root.iconphoto(True, *icons)
            self._icon_images = icons  # keep a reference or the icon disappears after GC
        except Exception as e:
            UI_QUEUE.put(("log_err", f"Failed to load Logo.png (must be PNG): {e}"))

    def setup_ui(self):
        top = tk.Frame(self.root, bg="#1e1e1e")
        top.pack(fill=tk.X, pady=10, padx=20)
        tk.Label(top, text="◈ API Linking Bridge", font=("Consolas", 18, "bold"), fg="#00ffcc", bg="#1e1e1e").pack(side=tk.LEFT)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)
        self.tab_dash = tk.Frame(self.notebook, bg="#1e1e1e")
        self.tab_chat = tk.Frame(self.notebook, bg="#1e1e1e")
        self.tab_conf = tk.Frame(self.notebook, bg="#1e1e1e")
        self.tab_cache = tk.Frame(self.notebook, bg="#1e1e1e")
        self.tab_upd = tk.Frame(self.notebook, bg="#1e1e1e")
        self.notebook.add(self.tab_dash, text=" Dashboard ")
        self.notebook.add(self.tab_chat, text=" Chat ")
        self.notebook.add(self.tab_conf, text=" Links ")
        self.notebook.add(self.tab_cache, text=" Cache Cleaner ")
        self.notebook.add(self.tab_upd, text=" Updates ")

        self.setup_dashboard()
        self.setup_chat()
        self.setup_config()
        self.setup_cache()
        self.setup_update_tab()
        self.refresh_link_list()

    def setup_dashboard(self):
        ctrl = tk.Frame(self.tab_dash, bg="#1e1e1e")
        ctrl.pack(fill=tk.X, pady=5, padx=20)

        self.status_label = tk.Label(ctrl, text="● Not listening", font=("Consolas", 11), fg="#888", bg="#1e1e1e")
        self.status_label.pack(side=tk.LEFT)

        tk.Label(ctrl, text="Link:", font=("Consolas", 11), fg="#ccc", bg="#1e1e1e").pack(side=tk.LEFT, padx=(20, 5))
        self.cb_dash_links = ttk.Combobox(ctrl, font=("Consolas", 11), state="readonly", width=15)
        self.cb_dash_links.pack(side=tk.LEFT)

        self.btn_toggle = tk.Button(ctrl, text="⚡ Start Link", bg="#333", fg="#00ffcc", font=("Consolas", 11, "bold"), relief=tk.FLAT, command=self.toggle_link)
        self.btn_toggle.pack(side=tk.LEFT, padx=10, ipady=2)

        self.canvas = tk.Canvas(self.tab_dash, height=220, bg="#111", highlightthickness=0)
        self.canvas.pack(fill=tk.X, padx=20, pady=10)

        self.nodes = {
            "client": {"x": 150, "y": 110, "label": "Codex client"},
            "proxy": {"x": 480, "y": 110, "label": "Local relay"},
            "upstream": {"x": 810, "y": 110, "label": "API server"}
        }
        self.canvas.create_line(150, 110, 480, 110, width=12, fill="#222", capstyle=tk.ROUND)
        self.canvas.create_line(150, 110, 480, 110, width=6, fill="#333", capstyle=tk.ROUND)
        self.canvas.create_line(480, 110, 810, 110, width=12, fill="#222", capstyle=tk.ROUND)
        self.canvas.create_line(480, 110, 810, 110, width=6, fill="#333", capstyle=tk.ROUND)
        for k, n in self.nodes.items():
            self.canvas.create_oval(n["x"]-25, n["y"]-25, n["x"]+25, n["y"]+25, fill="#1e1e1e", outline="#00ffcc", width=2)
            self.canvas.create_text(n["x"], n["y"]+45, text=n["label"], fill="#ccc", font=("Consolas", 10))
            self.canvas.create_text(n["x"], n["y"], text=k[0].upper(), fill="#00ffcc", font=("Consolas", 14, "bold"))

        self.tick()

        bottom = tk.Frame(self.tab_dash, bg="#1e1e1e")
        bottom.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        stats = tk.LabelFrame(bottom, text=" Token Usage ", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 10))
        stats.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 10))
        self.lbl_in = tk.Label(stats, text="Input: 0", fg="#fff", bg="#1e1e1e", font=("Consolas", 11)); self.lbl_in.pack(anchor="w", padx=10, pady=2)
        self.lbl_out = tk.Label(stats, text="Output: 0", fg="#fff", bg="#1e1e1e", font=("Consolas", 11)); self.lbl_out.pack(anchor="w", padx=10, pady=2)
        self.lbl_total = tk.Label(stats, text="Total: 0", fg="#fff", bg="#1e1e1e", font=("Consolas", 11)); self.lbl_total.pack(anchor="w", padx=10, pady=2)
        self.lbl_req = tk.Label(stats, text="Requests: 0", fg="#888", bg="#1e1e1e", font=("Consolas", 9)); self.lbl_req.pack(anchor="w", padx=10, pady=(6, 0))
        tk.Button(stats, text="Reset Stats", bg="#333", fg="#00ffcc", font=("Consolas", 9), relief=tk.FLAT,
                  command=self.reset_stats).pack(anchor="w", padx=10, pady=6, fill=tk.X)

        logf = tk.LabelFrame(bottom, text=" Run Log ", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 10))
        logf.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)
        self.log_text = tk.Text(logf, bg="#111", fg="#aaa", font=("Consolas", 9), state=tk.DISABLED)
        self.log_text.tag_config("err", foreground="#ff4444")
        self.log_text.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

    # ---------- Built-in chat ----------
    def setup_chat(self):
        self._chat_models_cache = {}
        wrap = tk.Frame(self.tab_chat, bg="#1e1e1e")
        wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        bar = tk.Frame(wrap, bg="#1e1e1e")
        bar.pack(fill=tk.X, pady=(0, 4))

        tk.Label(bar, text="Link:", fg="#00ffcc", bg="#1e1e1e",
                 font=("Consolas", 10)).pack(side=tk.LEFT)
        self.cb_chat_links = ttk.Combobox(bar, state="readonly", width=16,
                                          font=("Consolas", 10))
        self.cb_chat_links.pack(side=tk.LEFT, padx=(4, 12))
        self.cb_chat_links.bind("<<ComboboxSelected>>", self.on_chat_link_select)

        tk.Label(bar, text="Model:", fg="#00ffcc", bg="#1e1e1e",
                 font=("Consolas", 10)).pack(side=tk.LEFT)
        self.ent_chat_model = ttk.Combobox(bar, width=24, font=("Consolas", 10),
                                           values=[])
        self.ent_chat_model.pack(side=tk.LEFT, padx=(4, 4), ipady=1)
        self.btn_chat_models = tk.Button(bar, text="Fetch models", bg="#333", fg="#00ffcc",
                                         font=("Consolas", 9), relief=tk.FLAT,
                                         command=self.fetch_chat_models)
        self.btn_chat_models.pack(side=tk.LEFT, padx=(0, 12), ipady=1)

        tk.Label(bar, text="Route:", fg="#00ffcc", bg="#1e1e1e",
                 font=("Consolas", 10)).pack(side=tk.LEFT)
        self.cb_chat_route = ttk.Combobox(
            bar, state="readonly", width=22, font=("Consolas", 10),
            values=["Auto (prefer local relay)", "Direct upstream", "Local relay"])
        self.cb_chat_route.set("Auto (prefer local relay)")
        self.cb_chat_route.pack(side=tk.LEFT, padx=(4, 8))
        self.cb_chat_route.bind("<<ComboboxSelected>>", self.on_chat_route_select)

        tk.Button(bar, text="Check port", bg="#333", fg="#00ffcc",
                  font=("Consolas", 9), relief=tk.FLAT,
                  command=self.update_chat_port_status).pack(side=tk.LEFT, padx=4, ipady=1)

        self.lbl_chat_port = tk.Label(wrap, text="○ No link selected", fg="#888",
                                      bg="#1e1e1e", font=("Consolas", 9),
                                      anchor="w", justify=tk.LEFT)
        self.lbl_chat_port.pack(fill=tk.X, pady=(0, 6))

        body = tk.Frame(wrap, bg="#1e1e1e")
        body.pack(fill=tk.BOTH, expand=True)
        self.chat_text = tk.Text(body, bg="#111", fg="#ddd", font=("Consolas", 10),
                                 wrap=tk.WORD, state=tk.DISABLED,
                                 insertbackground="white", relief=tk.FLAT)
        chat_sb = tk.Scrollbar(body, command=self.chat_text.yview)
        self.chat_text.config(yscrollcommand=chat_sb.set)
        self.chat_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        chat_sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.chat_text.tag_config("user", foreground="#00ffcc", font=("Consolas", 10, "bold"))
        self.chat_text.tag_config("assistant", foreground="#eeeeee")
        self.chat_text.tag_config("reasoning", foreground="#777777", font=("Consolas", 9, "italic"))
        self.chat_text.tag_config("system", foreground="#888888", font=("Consolas", 9))
        self.chat_text.tag_config("err", foreground="#ff4444")

        inputf = tk.Frame(wrap, bg="#1e1e1e")
        inputf.pack(fill=tk.X, pady=(8, 0))
        self.chat_input = tk.Text(inputf, height=3, bg="#111", fg="#fff",
                                  insertbackground="white", font=("Consolas", 10),
                                  wrap=tk.WORD, relief=tk.FLAT)
        self.chat_input.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.chat_input.bind("<Return>", self.on_chat_enter)

        btns = tk.Frame(inputf, bg="#1e1e1e")
        btns.pack(side=tk.RIGHT, fill=tk.Y, padx=(8, 0))
        self.btn_chat_send = tk.Button(btns, text="Send", bg="#00ffcc", fg="#111",
                                       font=("Consolas", 10, "bold"), relief=tk.FLAT,
                                       command=self.send_chat_message, width=10)
        self.btn_chat_send.pack(fill=tk.X, pady=2)
        self.btn_chat_stop = tk.Button(btns, text="Stop", bg="#333", fg="#ff6666",
                                       font=("Consolas", 10), relief=tk.FLAT,
                                       command=self.stop_chat, width=10, state=tk.DISABLED)
        self.btn_chat_stop.pack(fill=tk.X, pady=2)
        tk.Button(btns, text="New chat", bg="#333", fg="#ccc", font=("Consolas", 10),
                  relief=tk.FLAT, command=self.new_chat, width=10).pack(fill=tk.X, pady=2)
        tk.Button(btns, text="Clear history", bg="#333", fg="#ff4444", font=("Consolas", 10),
                  relief=tk.FLAT, command=self.clear_chat_history, width=10).pack(fill=tk.X, pady=2)

        tk.Label(wrap, text="Plain chat mode · Agent (tool calling / autonomous execution) "
                            "is not implemented yet — contributions on GitHub are welcome",
                 fg="#666", bg="#1e1e1e", font=("Consolas", 9)).pack(anchor="w", pady=(6, 0))

        self.chat_messages = load_chat_history()
        self.chat_streaming = False
        self._chat_stop = None
        self._chat_content_buf = ""
        self._chat_reasoning_buf = ""
        self._chat_shown_reasoning = False
        self._chat_shown_ai = False
        if self.chat_messages:
            self._append_chat("(Loaded previous conversation, %d messages)\n\n"
                              % len(self.chat_messages), "system")
            for item in self.chat_messages[-20:]:
                role = "You" if item.get("role") == "user" else "AI"
                text = item.get("content", "")
                if len(text) > 4000:
                    text = text[:4000] + "…"
                self._append_chat("%s: %s\n\n" % (role, text),
                                  "user" if role == "You" else "assistant")

    def _append_chat(self, text, tag=""):
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.insert(tk.END, text, tag)
        self.chat_text.see(tk.END)
        self.chat_text.config(state=tk.DISABLED)

    def on_chat_link_select(self, event=None):
        link = self.selected_chat_link()
        if link:
            self.ent_chat_model.delete(0, tk.END)
            self.ent_chat_model.insert(0, link.get("model", ""))
            self.ent_chat_model["values"] = self._chat_models_cache.get(link.get("name"), [])
            self.fetch_chat_models(auto=True)
        self.update_chat_port_status()

    def fetch_chat_models(self, auto=False):
        link = self.selected_chat_link()
        if not link or not link.get("apiURL") or not link.get("apiKey"):
            if not auto:
                messagebox.showwarning("Notice", "Select a link with a Base URL and API Key first")
            return
        name = link.get("name") or "Unnamed"
        if name in self._chat_models_cache:
            self.ent_chat_model["values"] = self._chat_models_cache[name]
            if auto:
                return
        self.log("Fetching the model list for %s…" % name)
        threading.Thread(target=self._fetch_chat_models_worker,
                         args=(link,), daemon=True).start()

    def _fetch_chat_models_worker(self, link):
        models, err = fetch_upstream_models(api_base(link), link.get("apiKey", ""))
        UI_QUEUE.put(("chat_models", (link.get("name") or "Unnamed", models, err)))

    def on_chat_route_select(self, event=None):
        self.update_chat_port_status()

    def selected_chat_link(self):
        name = self.cb_chat_links.get()
        if not name:
            return None
        for item in self.api_data:
            if item.get("name") == name:
                return item
        return None

    def chat_route_mode(self):
        text = self.cb_chat_route.get()
        if "Auto" in text:
            return "auto"
        if "Direct" in text:
            return "direct"
        return "relay"

    def update_chat_port_status(self):
        link = self.selected_chat_link()
        if not link:
            try:
                self.lbl_chat_port.config(text="○ No link selected", fg="#888")
            except Exception:
                pass
            return
        port = int(link.get("ListenPort", 15721))
        ours = (PROXY_STATE.get("server") is not None
                and PROXY_STATE.get("port") == port
                and PROXY_STATE.get("active_link") == link.get("name"))
        text = describe_port(port, ours)
        color = "#00ff00" if ours else "#888"
        if not ours:
            in_use, _, _ = _port_owner(port)
            if in_use:
                color = "#ffaa44"
                if self.chat_route_mode() == "relay":
                    text += " (local relay route unavailable: change the port or stop the other program)"
                else:
                    text += " (auto mode will use a direct upstream connection)"
        try:
            self.lbl_chat_port.config(text=text, fg=color)
        except Exception:
            pass

    def _ensure_relay(self, link):
        """Make sure the local relay is running on the target port.
        Returns (ok, detail)."""
        port = int(link.get("ListenPort", 15721))
        if (PROXY_STATE.get("server") is not None
                and PROXY_STATE.get("port") == port
                and PROXY_STATE.get("active_link") == link.get("name")):
            return True, ""
        in_use, _, _ = _port_owner(port)
        if in_use:
            return False, describe_port(port)
        if not self.start_server(link):
            return False, "Failed to start the local relay; see the run log"
        self.is_running = True
        self.btn_toggle.config(text="🛑 Stop link", bg="#ff4444", fg="#fff")
        self.status_label.config(
            text="● Listening: http://127.0.0.1:%s [ %s ]" % (port, link.get("name")), fg="#00ff00")
        UI_QUEUE.put(("log", "Chat needs the local relay; started link: %s (port %s)"
                      % (link.get("name"), port)))
        return True, ""

    def resolve_chat_route(self, link, mode):
        """Return (route, base, warning, error); route is relay / direct."""
        port = int(link.get("ListenPort", 15721))
        ours = (PROXY_STATE.get("server") is not None
                and PROXY_STATE.get("port") == port
                and PROXY_STATE.get("active_link") == link.get("name"))
        if mode == "relay":
            ok, detail = self._ensure_relay(link)
            if not ok:
                return None, None, None, "Cannot use the local relay: " + detail
            return "relay", "http://127.0.0.1:%s/v1" % port, None, None
        if mode == "auto" and ours:
            return "relay", "http://127.0.0.1:%s/v1" % port, None, None
        if mode == "auto":
            in_use, _, _ = _port_owner(port)
            if in_use:
                return ("direct", api_base(link),
                        describe_port(port) + "; this chat will use a direct upstream connection", None)
        return "direct", api_base(link), None, None

    def on_chat_enter(self, event):
        if event.state & 0x0001:      # Shift+Enter inserts a newline
            self.chat_input.insert(tk.INSERT, "\n")
            return "break"
        self.send_chat_message()
        return "break"

    def send_chat_message(self):
        if self.chat_streaming:
            return
        link = self.selected_chat_link()
        if not link:
            return messagebox.showwarning("Notice", "Select a link on the Links tab first")
        if not link.get("apiURL") or not link.get("apiKey"):
            return messagebox.showwarning("Notice", "This link is incomplete (Base URL / API Key)")
        model = self.ent_chat_model.get().strip() or (link.get("model") or "").strip()
        if not model:
            return messagebox.showwarning("Notice", "Enter a model name (upstream model)")
        text = self.chat_input.get("1.0", tk.END).strip()
        if not text:
            return

        route, base, warning, error = self.resolve_chat_route(link, self.chat_route_mode())
        if error:
            return messagebox.showerror("Port in use", error)
        if warning:
            messagebox.showwarning("Port in use", warning)
            UI_QUEUE.put(("log_err", warning))

        self.chat_messages.append({"role": "user", "content": text})
        self._append_chat("\nYou: %s\n\n" % text, "user")
        self.chat_input.delete("1.0", tk.END)
        self._chat_content_buf = ""
        self._chat_reasoning_buf = ""
        self._chat_shown_reasoning = False
        self._chat_shown_ai = False
        self.chat_streaming = True
        self._chat_stop = threading.Event()
        self.btn_chat_send.config(state=tk.DISABLED, text="Sending…")
        self.btn_chat_stop.config(state=tk.NORMAL)
        UI_QUEUE.put(("log", "Chat request → %s (%s)" % (
            "local relay" if route == "relay" else "direct upstream", base)))

        threading.Thread(
            target=self._chat_worker,
            args=(base, link.get("apiKey", ""), model, list(self.chat_messages), self._chat_stop),
            daemon=True).start()

    def _chat_worker(self, base, api_key, model, messages, stop_event):
        content = ""
        try:
            for kind, value in iter_chat_stream(base, api_key, model, messages, stop_event):
                if kind == "content":
                    content += value
                    UI_QUEUE.put(("chat_delta", ("content", value)))
                elif kind == "reasoning":
                    UI_QUEUE.put(("chat_delta", ("reasoning", value)))
                else:
                    UI_QUEUE.put(("chat_usage", value))
            if stop_event.is_set():
                UI_QUEUE.put(("chat_stopped", content))
            else:
                UI_QUEUE.put(("chat_done", content))
        except Exception as exc:
            UI_QUEUE.put(("chat_err", str(exc)))
        finally:
            UI_QUEUE.put(("chat_end", None))

    def stop_chat(self):
        if self._chat_stop is not None:
            self._chat_stop.set()
            self.btn_chat_stop.config(state=tk.DISABLED)
            UI_QUEUE.put(("log", "Stopping the chat response…"))

    def new_chat(self):
        if self.chat_streaming:
            return messagebox.showwarning("Notice", "A response is still streaming; stop it first")
        self.chat_messages = []
        self._chat_content_buf = ""
        self._chat_reasoning_buf = ""
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.config(state=tk.DISABLED)
        save_chat_history([])
        UI_QUEUE.put(("log", "Started a new chat"))

    def clear_chat_history(self):
        if self.chat_streaming:
            return messagebox.showwarning("Notice", "A response is still streaming; stop it first")
        if not messagebox.askyesno("Confirm", "Clear the whole conversation history? (cannot be undone)"):
            return
        self.chat_messages = []
        self.chat_text.config(state=tk.NORMAL)
        self.chat_text.delete("1.0", tk.END)
        self.chat_text.config(state=tk.DISABLED)
        try:
            if os.path.exists(CHAT_HISTORY_FILE):
                os.remove(CHAT_HISTORY_FILE)
        except Exception as exc:
            UI_QUEUE.put(("log_err", "Failed to delete chat history: %s" % exc))
        UI_QUEUE.put(("log", "Chat history cleared"))

    # ---------- Cache cleanup ----------
    def setup_cache(self):
        wrap = tk.Frame(self.tab_cache, bg="#1e1e1e")
        wrap.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        tk.Label(wrap, text="◈ Cache Cleaner", font=("Consolas", 16, "bold"),
                 fg="#00ffcc", bg="#1e1e1e").pack(anchor="w")
        tk.Label(wrap, text="Codex caches model lists, sessions and more on this machine. "
                            "If the model list will not refresh, clearing the "
                            "\"Model list cache\" usually fixes it; chat cache is off by default.",
                 fg="#aaa", bg="#1e1e1e", font=("Consolas", 10),
                 wraplength=820, justify=tk.LEFT).pack(anchor="w", pady=(8, 4))
        tk.Label(wrap, text="Codex cache directory: %s" % codex_home(),
                 fg="#666", bg="#1e1e1e", font=("Consolas", 9)).pack(anchor="w", pady=(0, 10))

        cats = tk.LabelFrame(wrap, text=" Select cache categories to clear ", fg="#00ffcc",
                             bg="#1e1e1e", font=("Consolas", 10))
        cats.pack(fill=tk.X, pady=(0, 10))
        self.var_cache_models = tk.BooleanVar(value=True)
        self.var_cache_chat = tk.BooleanVar(value=False)
        self.var_cache_logs = tk.BooleanVar(value=False)
        self.var_cache_temp = tk.BooleanVar(value=False)
        for var, title, desc in (
                (self.var_cache_models, "Model list cache", "Fix the Codex model list not refreshing"),
                (self.var_cache_chat, "Chat / session cache", "Codex sessions and this app's chat history (cannot be undone)"),
                (self.var_cache_logs, "Log cache", "Crash logs and Codex runtime logs"),
                (self.var_cache_temp, "Temporary files", "Temporary files under the cache directories")):
            row = tk.Frame(cats, bg="#1e1e1e")
            row.pack(fill=tk.X, padx=8, pady=2)
            tk.Checkbutton(row, text=title, variable=var, bg="#1e1e1e", fg="#fff",
                           selectcolor="#111", activebackground="#1e1e1e",
                           activeforeground="#00ffcc",
                           font=("Consolas", 10)).pack(side=tk.LEFT)
            tk.Label(row, text="— " + desc, fg="#777", bg="#1e1e1e",
                     font=("Consolas", 9)).pack(side=tk.LEFT, padx=(6, 0))

        btns = tk.Frame(wrap, bg="#1e1e1e")
        btns.pack(fill=tk.X, pady=(0, 8))
        tk.Button(btns, text="🔍 Scan cache", bg="#333", fg="#00ffcc", font=("Consolas", 10),
                  relief=tk.FLAT, command=self.scan_cache).pack(side=tk.LEFT, padx=(0, 8), ipady=2)
        tk.Button(btns, text="🧹 Clear selected", bg="#ff4444", fg="#fff",
                  font=("Consolas", 10, "bold"), relief=tk.FLAT,
                  command=self.clear_cache_selected).pack(side=tk.LEFT, padx=(0, 8), ipady=2)
        tk.Button(btns, text="⚡ Clear model cache only", bg="#00ffcc", fg="#111",
                  font=("Consolas", 10, "bold"), relief=tk.FLAT,
                  command=self.quick_clear_models).pack(side=tk.LEFT, padx=(0, 8), ipady=2)
        tk.Button(btns, text="📂 Open Codex folder", bg="#333", fg="#00ffcc",
                  font=("Consolas", 9), relief=tk.FLAT,
                  command=self.open_codex_dir).pack(side=tk.LEFT, ipady=2)

        self.lbl_cache_summary = tk.Label(wrap, text="Click \"Scan cache\" to preview files to delete",
                                          fg="#888", bg="#1e1e1e",
                                          font=("Consolas", 9), anchor="w")
        self.lbl_cache_summary.pack(fill=tk.X, pady=(0, 4))
        self.cache_text = tk.Text(wrap, bg="#111", fg="#bbb", font=("Consolas", 9),
                                  relief=tk.FLAT, state=tk.DISABLED)
        self.cache_text.pack(fill=tk.BOTH, expand=True)
        self.cache_targets = []

    def selected_cache_cats(self):
        cats = []
        if self.var_cache_models.get():
            cats.append("models")
        if self.var_cache_chat.get():
            cats.append("chat")
        if self.var_cache_logs.get():
            cats.append("logs")
        if self.var_cache_temp.get():
            cats.append("temp")
        return cats

    def scan_cache(self):
        cats = self.selected_cache_cats()
        if not cats:
            self.cache_text.config(state=tk.NORMAL)
            self.cache_text.delete("1.0", tk.END)
            self.cache_text.insert(tk.END, "No category selected.\n")
            self.cache_text.config(state=tk.DISABLED)
            return
        self.lbl_cache_summary.config(text="Scanning cache…")
        UI_QUEUE.put(("log", "Scanning cache…"))

        def work():
            try:
                UI_QUEUE.put(("cache_scan", _cache_targets(cats)))
            except Exception as exc:
                UI_QUEUE.put(("cache_scan_err", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def render_cache_scan(self, targets):
        self.cache_targets = targets
        self.cache_text.config(state=tk.NORMAL)
        self.cache_text.delete("1.0", tk.END)
        if not targets:
            self.cache_text.insert(tk.END, "No cache files found (already clean).\n")
            self.lbl_cache_summary.config(text="No cache found. Codex directory: %s" % codex_home())
        else:
            total = 0
            current = None
            for target in targets:
                total += target["size"]
                if target["cat"] != current:
                    current = target["cat"]
                    self.cache_text.insert(tk.END, "\n[%s]\n" % CACHE_TITLES.get(current, current))
                self.cache_text.insert(
                    tk.END, "  · %s  %s  (%s)\n" % (
                        target["source"], target["path"], fmt_size(target["size"])))
            self.lbl_cache_summary.config(
                text="%d item(s), %s total. Login/config files such as auth.json / config.toml are never deleted."
                     % (len(targets), fmt_size(total)))
        self.cache_text.config(state=tk.DISABLED)

    def clear_cache_selected(self):
        self._clear_cache(self.selected_cache_cats())

    def quick_clear_models(self):
        self.var_cache_models.set(True)
        self.var_cache_chat.set(False)
        self.var_cache_logs.set(False)
        self.var_cache_temp.set(False)
        self._clear_cache(["models"])

    def _clear_cache(self, cats):
        if not cats:
            return messagebox.showwarning("Notice", "Select at least one cache category")
        try:
            targets = _cache_targets(cats)
        except Exception as exc:
            return messagebox.showerror("Error", "Failed to scan cache: %s" % exc)
        if not targets:
            self.render_cache_scan([])
            return messagebox.showinfo("Notice", "Nothing to clean")
        total = sum(t["size"] for t in targets)
        preview = "\n".join("  · [%s] %s" % (CACHE_TITLES.get(t["cat"], t["cat"]), t["path"])
                            for t in targets[:12])
        if len(targets) > 12:
            preview += "\n  … and %d more" % (len(targets) - 12)
        warn = ""
        if "chat" in cats:
            warn = "\n\n⚠ Includes chat / session cache — this cannot be undone!"
        msg = ("About to clean %d cache item(s), %s in total.%s\n\n%s\n\nContinue?"
               % (len(targets), fmt_size(total), warn, preview))
        if not messagebox.askyesno("Confirm cache cleanup", msg):
            return

        self.cache_text.config(state=tk.NORMAL)
        self.cache_text.delete("1.0", tk.END)
        self.cache_text.insert(tk.END, "Cleaning…\n")
        self.cache_text.config(state=tk.DISABLED)
        self.lbl_cache_summary.config(text="Cleaning…")

        def work():
            try:
                removed, failed = remove_cache_targets(targets)
                UI_QUEUE.put(("cache_clear_done", (removed, failed)))
            except Exception as exc:
                UI_QUEUE.put(("cache_scan_err", str(exc)))

        threading.Thread(target=work, daemon=True).start()

    def on_cache_clear_done(self, removed, failed):
        freed = sum(t["size"] for t in removed)
        for target in removed:
            UI_QUEUE.put(("log", "Removed cache: %s" % target["path"]))
        for target, err in failed:
            UI_QUEUE.put(("log_err", "Delete failed: %s (%s)" % (target["path"], err)))
        self.scan_cache()
        summary = "Removed %d item(s), freed %s" % (len(removed), fmt_size(freed))
        if failed:
            summary += "; %d failed, see the log" % len(failed)
            messagebox.showwarning("Cleanup finished", summary)
        else:
            messagebox.showinfo("Cleanup finished", summary)

    def open_codex_dir(self):
        home = codex_home()
        if not os.path.isdir(home):
            return messagebox.showinfo("Notice", "Codex cache directory not found:\n%s" % home)
        try:
            os.startfile(home)
        except Exception as exc:
            messagebox.showerror("Error", "Cannot open the folder: %s" % exc)

    def setup_config(self):
        main = tk.Frame(self.tab_conf, bg="#1e1e1e")
        main.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        left = tk.Frame(main, bg="#1e1e1e", width=200)
        left.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 15))
        tk.Label(left, text="Saved links", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w")
        self.listbox_links = tk.Listbox(left, bg="#111", fg="#fff", font=("Consolas", 11), selectbackground="#005544", relief=tk.FLAT)
        self.listbox_links.pack(fill=tk.BOTH, expand=True, pady=5)
        self.listbox_links.bind("<<ListboxSelect>>", self.on_link_select)

        btnf = tk.Frame(left, bg="#1e1e1e")
        btnf.pack(fill=tk.X, pady=5)
        tk.Button(btnf, text="+ New", bg="#333", fg="#00ffcc", command=self.add_new_link).pack(side=tk.LEFT, expand=True, fill=tk.X, padx=2)
        tk.Button(btnf, text="- Delete", bg="#333", fg="#ff4444", command=self.delete_link).pack(side=tk.RIGHT, expand=True, fill=tk.X, padx=2)

        right = tk.Frame(main, bg="#1e1e1e")
        right.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True)

        tk.Label(right, text="Link name (unique ID):", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        self.ent_name = tk.Entry(right, bg="#2a2a2a", fg="#fff", insertbackground="white", font=("Consolas", 11), borderwidth=0)
        self.ent_name.pack(fill=tk.X, ipady=5, pady=(0, 15))

        tk.Label(right, text="Base URL:", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        self.cb_provider = ttk.Combobox(right, values=list(PROVIDERS.keys()), font=("Consolas", 11))
        self.cb_provider.pack(fill=tk.X, pady=(0, 5))
        self.cb_provider.bind("<<ComboboxSelected>>", self.on_provider_select)
        self.ent_url = tk.Entry(right, bg="#2a2a2a", fg="#fff", insertbackground="white", font=("Consolas", 11), borderwidth=0)
        self.ent_url.pack(fill=tk.X, ipady=5, pady=(0, 15))

        tk.Label(right, text="Upstream model (empty = pass through client model):", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        self.ent_model = tk.Entry(right, bg="#2a2a2a", fg="#fff", insertbackground="white", font=("Consolas", 11), borderwidth=0)
        self.ent_model.pack(fill=tk.X, ipady=5, pady=(0, 15))

        tk.Label(right, text="API Key:", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        keyf = tk.Frame(right, bg="#1e1e1e")
        keyf.pack(fill=tk.X, pady=(0, 15))
        self.ent_key = tk.Entry(keyf, bg="#2a2a2a", fg="#fff", insertbackground="white", font=("Consolas", 11), borderwidth=0, show="*")
        self.ent_key.pack(side=tk.LEFT, fill=tk.X, expand=True, ipady=5)
        self.btn_test = tk.Button(keyf, text="Test Connection", bg="#333", fg="#00ffcc", font=("Consolas", 10), relief=tk.FLAT, command=self.test_connection)
        self.btn_test.pack(side=tk.RIGHT, padx=(10, 0), ipady=2)

        tk.Label(right, text="Local listen port (ListenPort):", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        self.ent_port = tk.Entry(right, bg="#2a2a2a", fg="#fff", insertbackground="white", font=("Consolas", 11), borderwidth=0)
        self.ent_port.pack(fill=tk.X, ipady=5, pady=(0, 15))

        tk.Label(right, text="Conversion mode (ExChangeModel):", fg="#00ffcc", bg="#1e1e1e", font=("Consolas", 11)).pack(anchor="w", pady=(0, 5))
        self.cb_mode = ttk.Combobox(right, values=["translation (Chat translation - recommended)", "direct (native passthrough)"], font=("Consolas", 11), state="readonly")
        self.cb_mode.pack(fill=tk.X, pady=(0, 15))
        self.cb_mode.set("translation (Chat translation - recommended)")

        self.var_disable_tools = tk.BooleanVar()
        tk.Checkbutton(right, text="Disable tool calls (fixes 400 from some providers)", variable=self.var_disable_tools, bg="#1e1e1e", fg="#00ffcc", selectcolor="#111", font=("Consolas", 10)).pack(anchor="w", pady=(0, 15))

        tk.Button(right, text="💾 Save Link Config", bg="#00ffcc", fg="#111", font=("Consolas", 12, "bold"), relief=tk.FLAT, command=self.save_current_link).pack(fill=tk.X, ipady=5, pady=(10, 20))

        self.ent_name.config(state='readonly')

    # ---------- Updates ----------
    def setup_update_tab(self):
        wrap = tk.Frame(self.tab_upd, bg="#1e1e1e")
        wrap.pack(fill=tk.BOTH, expand=True, padx=40, pady=30)

        tk.Label(wrap, text="◈ Updates", font=("Consolas", 16, "bold"),
                 fg="#00ffcc", bg="#1e1e1e").pack(anchor="w")
        tk.Label(wrap, text=f"Current version: v{VERSION}", font=("Consolas", 12),
                 fg="#fff", bg="#1e1e1e").pack(anchor="w", pady=(14, 6))

        self.lbl_upd_status = tk.Label(wrap, text="Checking for updates…", font=("Consolas", 12),
                                       fg="#888", bg="#1e1e1e")
        self.lbl_upd_status.pack(anchor="w", pady=(4, 10))

        self.lbl_upd_msg = tk.Label(wrap, text="", font=("Consolas", 12, "bold"),
                                    fg="#ffaa44", bg="#1e1e1e", justify=tk.LEFT)
        self.lbl_upd_msg.pack(anchor="w", pady=(0, 14))

        tk.Button(wrap, text="🌐 Open Download Page", bg="#00ffcc", fg="#111",
                  font=("Consolas", 12, "bold"), relief=tk.FLAT,
                  command=self.open_homepage).pack(anchor="w", ipady=4, ipadx=12, pady=(0, 12))
        tk.Button(wrap, text="🔄 Check Again", bg="#333", fg="#00ffcc",
                  font=("Consolas", 10), relief=tk.FLAT,
                  command=self.recheck_update).pack(anchor="w", ipady=2, ipadx=10)

        tk.Label(wrap, text=f"Crash log directory: {LOG_DIR}", font=("Consolas", 9),
                 fg="#666", bg="#1e1e1e").pack(anchor="w", pady=(16, 4))
        tk.Button(wrap, text="🗂 Open Log Directory", bg="#333", fg="#00ffcc",
                  font=("Consolas", 10), relief=tk.FLAT,
                  command=self.open_log_dir).pack(anchor="w", ipady=2, ipadx=10)

        self._badge = self._make_dot()
        self.notebook.bind("<<NotebookTabChanged>>", self.on_tab_changed)

    def _make_dot(self, size=12, color="#ff3b30"):
        """Draw a tiny red dot as the unread badge (a ttk tab image is the reliable way)."""
        img = tk.PhotoImage(width=size, height=size)
        r = size / 2.0
        c = (size - 1) / 2.0
        for y in range(size):
            dy = y - c
            if abs(dy) > r:
                continue
            half = (r * r - dy * dy) ** 0.5
            x0 = max(0, int(round(c - half)))
            x1 = min(size, int(round(c + half)) + 1)
            if x1 > x0:
                img.put(color, to=(x0, y, x1, y + 1))
        return img

    def set_badge(self, on):
        """Toggle the red badge on the Updates tab."""
        try:
            if on:
                self.notebook.tab(self.tab_upd, image=self._badge, compound="right")
            else:
                self.notebook.tab(self.tab_upd, image="", compound="none")
        except Exception:
            pass

    def on_tab_changed(self, event=None):
        # Opening the Updates tab counts as reading it; clear the badge
        try:
            if self.notebook.select() == str(self.tab_upd):
                self.set_badge(False)
        except Exception:
            pass

    def _fetch_update_info(self):
        """Query the update API, retrying once on failure (servers hiccup)."""
        last = None
        for attempt in range(2):
            try:
                resp = requests.get(UPDATE_API, timeout=10,
                                    headers={"User-Agent": f"APILinkingBridge/{VERSION}"})
                resp.raise_for_status()
                return resp.json()
            except Exception as e:
                last = e
                if attempt == 0:
                    time.sleep(1.5)
        raise last

    @staticmethod
    def _friendly_update_error(e):
        """Turn urllib3's ConnectionPool/MaxRetry noise into a readable message."""
        if isinstance(e, requests.exceptions.Timeout):
            return "Timed out connecting to the update server"
        if isinstance(e, requests.exceptions.ConnectionError):
            return "Cannot reach the update server; check your network"
        resp = getattr(e, "response", None)
        if isinstance(e, requests.exceptions.HTTPError) and resp is not None:
            return f"Update server returned HTTP {resp.status_code}"
        if isinstance(e, ValueError):
            return "Update server returned invalid data"
        return "Network error"

    def check_update(self):
        """Worker thread: GET the update API and return {"Version": "x.y.z"}."""
        if self._updating:
            return
        self._updating = True
        try:
            data = self._fetch_update_info()
        except Exception as e:
            UI_QUEUE.put(("update_status", ("error", self._friendly_update_error(e))))
            return
        finally:
            self._updating = False

        remote = ""
        if isinstance(data, dict):
            remote = str(data.get("Version") or data.get("version") or "").strip()
        if not remote:
            UI_QUEUE.put(("update_status", ("error", "The server returned no valid version")))
        elif remote == VERSION:
            UI_QUEUE.put(("update_status", ("ok", remote)))
        else:
            UI_QUEUE.put(("update_status", ("new", remote)))

    def recheck_update(self):
        self.lbl_upd_status.config(text="Checking for updates…", fg="#888")
        self.lbl_upd_msg.config(text="")
        threading.Thread(target=self.check_update, daemon=True).start()

    def render_update_state(self, state, info):
        if state == "ok":
            self.lbl_upd_status.config(text=f"✅ Up to date (v{info})", fg="#00ff00")
            self.lbl_upd_msg.config(text="")
            self.set_badge(False)
        elif state == "new":
            self.lbl_upd_status.config(text="🔔 New version available", fg="#ffaa44")
            self.lbl_upd_msg.config(text=f"Download it from the website  Version: {info}")
            self.set_badge(True)
        else:
            self.lbl_upd_status.config(
                text=f"⚠ {info}. Click Check Again later.", fg="#ff6666")
            self.lbl_upd_msg.config(text="")

    def open_homepage(self):
        try:
            ok = webbrowser.open(HOMEPAGE)
        except Exception:
            ok = False
        if ok:
            UI_QUEUE.put(("log", "Opened the download page in your browser"))
        else:
            UI_QUEUE.put(("log_err", "Could not open a browser; visit the website manually"))

    def open_log_dir(self):
        """Open the crash log directory so users can send .log files to the developer."""
        try:
            os.makedirs(LOG_DIR, exist_ok=True)
        except Exception:
            pass
        try:
            os.startfile(LOG_DIR)
            UI_QUEUE.put(("log", f"Opened log directory: {LOG_DIR}"))
        except Exception:
            UI_QUEUE.put(("log_err", f"Could not open the folder; enter it manually: {LOG_DIR}"))

    # ---------- List / form ----------
    def refresh_link_list(self):
        self.listbox_links.delete(0, tk.END)
        names = [x.get("name", "Unnamed") for x in self.api_data]
        for n in names:
            self.listbox_links.insert(tk.END, n)
        self.cb_dash_links["values"] = names

        # Keep the chat tab's link dropdown in sync
        try:
            if hasattr(self, "cb_chat_links"):
                self.cb_chat_links["values"] = names
                if names:
                    if self.cb_chat_links.get() not in names:
                        self.cb_chat_links.current(0)
                    self.on_chat_link_select()
                else:
                    self.cb_chat_links.set("")
                    self.ent_chat_model.delete(0, tk.END)
                    self.update_chat_port_status()
        except Exception:
            pass

        if names:
            if self.current_selected_index < 0 or self.current_selected_index >= len(names):
                self.current_selected_index = 0
            self.cb_dash_links.current(self.current_selected_index)
            self.listbox_links.selection_clear(0, tk.END)
            self.listbox_links.selection_set(self.current_selected_index)
            self.on_link_select(None)
        else:
            self.cb_dash_links.set('')
            self.current_selected_index = -1
            self.ent_name.config(state='normal')
            self.ent_name.delete(0, tk.END)
            self.ent_name.config(state='readonly')
            self.ent_url.delete(0, tk.END)
            self.ent_model.delete(0, tk.END)
            self.ent_key.delete(0, tk.END)
            self.ent_port.delete(0, tk.END)

    def on_link_select(self, event):
        sel = self.listbox_links.curselection()
        if not sel:
            return
        idx = sel[0]
        self.current_selected_index = idx
        self.is_new_link = False
        d = self.api_data[idx]

        self.ent_name.config(state='normal')
        self.ent_name.delete(0, tk.END)
        self.ent_name.insert(0, d.get("name", ""))
        self.ent_name.config(state='readonly')

        self.ent_url.delete(0, tk.END)
        self.ent_url.insert(0, d.get("apiURL", ""))
        self.ent_model.delete(0, tk.END)
        self.ent_model.insert(0, d.get("model", ""))
        self.ent_key.delete(0, tk.END)
        self.ent_key.insert(0, d.get("apiKey", ""))
        self.ent_port.delete(0, tk.END)
        self.ent_port.insert(0, str(d.get("ListenPort", 15721)))

        self.cb_mode.set("direct (native passthrough)" if d.get("ExChangeModel") == "direct" else "translation (Chat translation - recommended)")
        self.var_disable_tools.set(d.get("disable_tools", False))

    def add_new_link(self):
        self.listbox_links.selection_clear(0, tk.END)
        self.current_selected_index = -1
        self.is_new_link = True

        self.ent_name.config(state='normal')
        self.ent_name.delete(0, tk.END)
        self.ent_name.insert(0, f"Link{len(self.api_data) + 1}")

        self.cb_provider.set("Custom")
        self.ent_url.delete(0, tk.END)
        self.ent_model.delete(0, tk.END)
        self.ent_key.delete(0, tk.END)

        # Auto-assign a port from 15721, skipping used ones
        used_ports = {x.get("ListenPort", 15721) for x in self.api_data}
        port = 15721
        while port in used_ports:
            port += 1
        self.ent_port.delete(0, tk.END)
        self.ent_port.insert(0, str(port))

        self.cb_mode.set("translation (Chat translation - recommended)")
        self.var_disable_tools.set(False)

    def delete_link(self):
        if self.current_selected_index < 0 or self.current_selected_index >= len(self.api_data):
            return
        link = self.api_data[self.current_selected_index]
        name = link.get("name", "Unnamed")

        if not messagebox.askyesno("Confirm", f"Delete link {name}?"):
            return

        # If the active link is being deleted, stop the server first
        if PROXY_STATE.get("active_link") == name:
            self.stop_server()
            self.is_running = False
            self.btn_toggle.config(text="⚡ Start Link", bg="#333", fg="#00ffcc")
            self.status_label.config(text="● Not listening", fg="#888")

        del self.api_data[self.current_selected_index]
        self.save_json_data()
        self.current_selected_index = -1
        self.refresh_link_list()
        UI_QUEUE.put(("log", f"Deleted link: {name}"))

    def save_current_link(self):
        new_name = self.ent_name.get().strip()
        if not new_name:
            return messagebox.showwarning("Notice", "Link name cannot be empty")

        # Unique-name check (for new links; exclude self on rename)
        for i, x in enumerate(self.api_data):
            if x.get("name") == new_name and i != self.current_selected_index:
                return messagebox.showerror("Error", "That name already exists; choose another")

        try:
            port = int(self.ent_port.get().strip())
            if port < 1 or port > 65535:
                raise ValueError
        except Exception:
            return messagebox.showerror("Error", "Port must be a number between 1 and 65535")

        data = {
            "name": new_name,
            "apiURL": self.ent_url.get().strip(),
            "apiKey": self.ent_key.get().strip(),
            "model": self.ent_model.get().strip(),
            "ListenPort": port,
            "disable_tools": self.var_disable_tools.get(),
            "ExChangeModel": "direct" if "direct" in self.cb_mode.get() else "translation",
        }

        if self.is_new_link or self.current_selected_index < 0:
            self.api_data.append(data)
            self.current_selected_index = len(self.api_data) - 1
            self.is_new_link = False
        else:
            old_name = self.api_data[self.current_selected_index].get("name")
            self.api_data[self.current_selected_index] = data
            # If this link is active, hot-update its config (suggest restart on port change)
            if PROXY_STATE.get("active_link") == old_name:
                PROXY_STATE["active_link"] = new_name
                PROXY_STATE["config"] = data
                UI_QUEUE.put(("log", f"Link [{new_name}] config hot-updated"))
                if PROXY_STATE.get("port") != port:
                    UI_QUEUE.put(("log_err", "Port changed; stop and restart the link for it to take effect"))

        self.save_json_data()
        self.refresh_link_list()
        messagebox.showinfo("Success", f"Link [{new_name}] saved!")

    def save_json_data(self):
        try:
            with open(API_FILE, "w", encoding="utf-8") as f:
                json.dump(self.api_data, f, indent=4, ensure_ascii=False)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save config: {e}")

    def on_provider_select(self, event):
        p = self.cb_provider.get()
        if p in PROVIDERS and PROVIDERS[p]:
            self.ent_url.delete(0, tk.END)
            self.ent_url.insert(0, PROVIDERS[p])

    def test_connection(self):
        url = self.ent_url.get().strip().rstrip("/")
        key = self.ent_key.get().strip()
        if not url or not key:
            return messagebox.showwarning("Notice", "Fill in Base URL and API Key first")
        self.btn_test.config(text="Testing...", state=tk.DISABLED)

        def _test():
            try:
                resp = requests.get(f"{url}/models", headers={"Authorization": f"Bearer {key}"}, timeout=10)
                if resp.status_code == 200:
                    models = [m.get("id") for m in resp.json().get("data", []) if m.get("id")]
                    UI_QUEUE.put(("log", f"✅ Connected, found {len(models)} models"))
                    self.root.after(0, lambda: messagebox.showinfo("Success", f"Connected!\nFound {len(models)} models"))
                else:
                    UI_QUEUE.put(("log_err", f"❌ Connection failed {resp.status_code}: {resp.text[:200]}"))
                    self.root.after(0, lambda: messagebox.showerror("Failed", f"HTTP {resp.status_code}\n{resp.text[:200]}"))
            except Exception as e:
                UI_QUEUE.put(("log_err", f"❌ Exception: {e}"))
                self.root.after(0, lambda: messagebox.showerror("Error", str(e)))
            finally:
                self.root.after(0, lambda: self.btn_test.config(text="Test Connection", state=tk.NORMAL))

        threading.Thread(target=_test, daemon=True).start()

    # ---------- Server control ----------
    def start_server(self, cfg):
        """Start Flask on the given port (check port occupancy first)"""
        port = int(cfg.get("ListenPort", 15721))
        host = "127.0.0.1"
        in_use, _, _ = _port_owner(port)
        if in_use:
            UI_QUEUE.put(("log_err", "Start failed: " + describe_port(port)))
            return False
        try:
            from werkzeug.serving import make_server
            server = make_server(host, port, PROXY_APP, threaded=True)
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            PROXY_STATE["server"] = server
            PROXY_STATE["thread"] = t
            PROXY_STATE["port"] = port
            PROXY_STATE["config"] = cfg
            PROXY_STATE["active_link"] = cfg.get("name")
            return True
        except Exception as e:
            UI_QUEUE.put(("log_err", f"Startup failed: {e}"))
            return False

    def stop_server(self):
        server = PROXY_STATE.get("server")
        if server:
            try:
                server.shutdown()
            except Exception:
                pass
        PROXY_STATE["server"] = None
        PROXY_STATE["thread"] = None
        PROXY_STATE["active_link"] = None
        PROXY_STATE["config"] = {}
        PROXY_STATE["port"] = None

    def toggle_link(self):
        if self.is_running:
            # stop
            self.stop_server()
            self.is_running = False
            self.btn_toggle.config(text="⚡ Start Link", bg="#333", fg="#00ffcc")
            self.status_label.config(text="● Not listening", fg="#888")
            UI_QUEUE.put(("log", "Link stopped"))
            self.update_chat_port_status()
        else:
            # start
            name = self.cb_dash_links.get()
            if not name:
                return messagebox.showwarning("Notice", "Select a link first")
            # find the matching config in the list
            cfg = None
            for x in self.api_data:
                if x.get("name") == name:
                    cfg = x
                    break
            if not cfg:
                return messagebox.showwarning("Notice", f"Link not found: {name}")
            if not cfg.get("apiURL") or not cfg.get("apiKey"):
                return messagebox.showwarning("Notice", f"Link {name} is incomplete; save it first")

            port = cfg.get("ListenPort", 15721)
            in_use, _, _ = _port_owner(port)
            if in_use:
                msg = describe_port(port)
                UI_QUEUE.put(("log_err", "Start failed: " + msg))
                return messagebox.showerror("Port in use", msg + "\n\nChange this link's listen port, or stop the program using it first.")

            if self.start_server(cfg):
                self.is_running = True
                self.btn_toggle.config(text="🛑 Stop Link", bg="#ff4444", fg="#fff")
                self.status_label.config(text=f"● Listening: http://127.0.0.1:{port} [ {name} ]", fg="#00ff00")
                UI_QUEUE.put(("log", f"Started link: {name} (port {port})"))
                self.update_chat_port_status()
                # also verify connectivity
                threading.Thread(target=self._probe_upstream, args=(cfg,), daemon=True).start()

    def _probe_upstream(self, cfg):
        try:
            r = requests.get(f"{api_base(cfg)}/models", headers={"Authorization": f"Bearer {cfg['apiKey']}"}, timeout=10)
            if r.status_code == 200:
                ids = [x.get("id") for x in r.json().get("data", []) if x.get("id")]
                UI_QUEUE.put(("log", f"Upstream ready, found {len(ids)} models"))
                if not cfg.get("model"):
                    sample = ", ".join(ids[:5]) if ids else "(none)"
                    UI_QUEUE.put(("log_err",
                                  f"No upstream model set; client model names are forwarded as-is and will likely 404. "
                                  f"Example models: {sample}"))
            else:
                UI_QUEUE.put(("log_err", f"Upstream returned {r.status_code}: {r.text[:150]}"))
        except Exception as e:
            UI_QUEUE.put(("log_err", f"Upstream connectivity check failed: {e}"))

    # ---------- Log / animation / queue ----------
    def render_stats(self):
        missing = GLOBAL_STATS["missing"]
        req = GLOBAL_STATS["requests"]
        self.lbl_in.config(text=f"Input: {GLOBAL_STATS['input_tokens']}")
        self.lbl_out.config(text=f"Output: {GLOBAL_STATS['output_tokens']}")
        self.lbl_total.config(text=f"Total: {GLOBAL_STATS['total_tokens']}")
        if missing:
            self.lbl_req.config(text=f"Requests: {req} ({missing} without usage)", fg="#ffaa44")
        else:
            self.lbl_req.config(text=f"Requests: {req}", fg="#888")

    def reset_stats(self):
        for k in GLOBAL_STATS:
            GLOBAL_STATS[k] = 0
        self.render_stats()
        UI_QUEUE.put(("log", "Token stats reset"))

    def log(self, msg, is_err=False):
        self.log_text.config(state=tk.NORMAL)
        self.log_text.insert(tk.END, f"[{time.strftime('%H:%M:%S')}] {msg}\n", "err" if is_err else "")
        self.log_text.see(tk.END)
        self.log_text.config(state=tk.DISABLED)

    def spawn_particle(self, start, end, color="#00ffcc"):
        p = self.canvas.create_oval(0, 0, 6, 6, fill=color, outline="")
        self.particles.append({
            "id": p,
            "sx": self.nodes[start]["x"], "sy": self.nodes[start]["y"],
            "ex": self.nodes[end]["x"], "ey": self.nodes[end]["y"],
            "p": 0.0
        })

    def tick(self):
        for p in list(self.particles):
            p["p"] += 0.04
            if p["p"] >= 1.0:
                self.canvas.delete(p["id"])
                self.particles.remove(p)
            else:
                cx = p["sx"] + (p["ex"] - p["sx"]) * p["p"]
                cy = p["sy"] + (p["ey"] - p["sy"]) * p["p"]
                self.canvas.coords(p["id"], cx - 3, cy - 3, cx + 3, cy + 3)
        self.root.after(30, self.tick)

    def process_queue(self):
        try:
            while True:
                msg, data = UI_QUEUE.get_nowait()
                if msg == "log":
                    self.log(data)
                elif msg == "log_err":
                    self.log(data, is_err=True)
                elif msg == "stats":
                    GLOBAL_STATS["requests"] += 1
                    if data is None:
                        # Upstream reported no usage; record that honestly
                        GLOBAL_STATS["missing"] += 1
                    else:
                        GLOBAL_STATS["input_tokens"] += data["input"]
                        GLOBAL_STATS["output_tokens"] += data["output"]
                        GLOBAL_STATS["total_tokens"] += data["total"]
                    self.render_stats()
                elif msg == "anim_start":
                    self.spawn_particle("client", "proxy", "#00ffcc")
                    self.root.after(300, lambda: self.spawn_particle("proxy", "upstream", "#ffcc00"))
                elif msg == "anim_end":
                    self.spawn_particle("upstream", "proxy", "#ffcc00")
                    self.root.after(300, lambda: self.spawn_particle("proxy", "client", "#00ffcc"))
                elif msg == "update_status":
                    self.render_update_state(data[0], data[1])
                elif msg == "chat_delta":
                    kind, text = data
                    if kind == "reasoning":
                        self._chat_reasoning_buf += text
                        if not self._chat_shown_reasoning:
                            self._chat_shown_reasoning = True
                            self._append_chat("(thinking) ", "reasoning")
                        self._append_chat(text, "reasoning")
                    else:
                        self._chat_content_buf += text
                        if not self._chat_shown_ai:
                            self._chat_shown_ai = True
                            if self._chat_shown_reasoning:
                                self._append_chat("\n\n", "assistant")
                            self._append_chat("AI: ", "assistant")
                        self._append_chat(text, "assistant")
                elif msg == "chat_usage":
                    inp = data.get("prompt_tokens", data.get("input_tokens", 0)) or 0
                    out = data.get("completion_tokens", data.get("output_tokens", 0)) or 0
                    self._append_chat("\n(tokens: input %s / output %s)" % (inp, out), "system")
                elif msg == "chat_stopped":
                    self._append_chat("\n\n[Generation stopped]\n", "system")
                    UI_QUEUE.put(("log", "Chat stopped by user"))
                elif msg == "chat_done":
                    if not self._chat_shown_ai and self._chat_shown_reasoning:
                        self._append_chat("\n\n", "assistant")
                    if self._chat_content_buf.strip():
                        self.chat_messages.append(
                            {"role": "assistant", "content": self._chat_content_buf})
                        save_chat_history(self.chat_messages)
                    self._append_chat("\n\n", "system")
                    GLOBAL_STATS["requests"] += 1
                    UI_QUEUE.put(("log", "Chat response complete"))
                elif msg == "chat_err":
                    self._append_chat("\n[Error] %s\n\n" % data, "err")
                    UI_QUEUE.put(("log_err", "Chat request failed: %s" % data))
                elif msg == "chat_end":
                    self.chat_streaming = False
                    self.btn_chat_send.config(state=tk.NORMAL, text="Send")
                    self.btn_chat_stop.config(state=tk.DISABLED)
                elif msg == "chat_models":
                    name, models, err = data
                    if models:
                        self._chat_models_cache[name] = models
                        if self.cb_chat_links.get() == name:
                            self.ent_chat_model["values"] = models
                            if not self.ent_chat_model.get().strip():
                                self.ent_chat_model.set(models[0])
                        self.log("Loaded %d model(s) for %s; pick one from the list" % (len(models), name))
                    elif err:
                        self.log("Failed to fetch the model list for %s: %s" % (name, err), is_err=True)
                elif msg == "cache_scan":
                    self.render_cache_scan(data if isinstance(data, list) else [])
                elif msg == "cache_scan_err":
                    self.lbl_cache_summary.config(text="Scan failed: %s" % data)
                    UI_QUEUE.put(("log_err", "Cache scan failed: %s" % data))
                elif msg == "cache_clear_done":
                    self.on_cache_clear_done(data[0], data[1])
        except queue.Empty:
            pass
        self.root.after(100, self.process_queue)


if __name__ == "__main__":
    try:
        root = tk.Tk()
    except Exception as exc:
        path = _write_crash_log(type(exc), exc, exc.__traceback__,
                                "GUI startup (creating window)")
        print("Could not create the window; crash log saved to: ", path)
        try:
            input("Press Enter to exit…")
        except Exception:
            pass
        raise SystemExit(1)

    install_crash_handlers(root)
    app = None
    startup_failed = False
    try:
        app = APIBridgeApp(root)
        install_crash_handlers(root, app)
    except Exception as exc:
        startup_failed = True
        path = _write_crash_log(type(exc), exc, exc.__traceback__,
                                "GUI startup (init)")
        try:
            root.title("API Linking Bridge - startup error (window kept)")
            messagebox.showerror(
                "Startup error",
                "An error occurred during initialization, but the window will stay open.\n\n"
                "Crash log saved to:\n%s\n\nPlease send this file to the developer." % path,
                parent=root,
            )
        except Exception:
            print("Startup error, log: ", path)

    if startup_failed:
        root.mainloop()          # keep the window open so the user can screenshot / collect logs
    else:
        try:
            root.mainloop()
        except Exception as exc:
            path = _write_crash_log(type(exc), exc, exc.__traceback__,
                                    "GUI main loop")
            try:
                messagebox.showerror(
                    "Program error",
                    "The main loop hit an error; the window will be kept if possible.\n\nLog:\n%s" % path,
                    parent=root,
                )
            except Exception:
                print("Program error, log: ", path)
            try:
                root.mainloop()      # keep going if possible instead of closing
            except Exception:
                try:
                    input("Press Enter to exit…")
                except Exception:
                    pass