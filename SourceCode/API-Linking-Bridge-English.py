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
VERSION = "26.11.3"
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
        self.tab_conf = tk.Frame(self.notebook, bg="#1e1e1e")
        self.tab_upd = tk.Frame(self.notebook, bg="#1e1e1e")
        self.notebook.add(self.tab_dash, text=" Dashboard ")
        self.notebook.add(self.tab_conf, text=" Links ")
        self.notebook.add(self.tab_upd, text=" Updates ")

        self.setup_dashboard()
        self.setup_config()
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
        """Start Flask on the given port"""
        port = cfg.get("ListenPort", 15721)
        host = "127.0.0.1"
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

            if self.start_server(cfg):
                self.is_running = True
                port = cfg.get("ListenPort", 15721)
                self.btn_toggle.config(text="🛑 Stop Link", bg="#ff4444", fg="#fff")
                self.status_label.config(text=f"● Listening: http://127.0.0.1:{port} [ {name} ]", fg="#00ff00")
                UI_QUEUE.put(("log", f"Started link: {name} (port {port})"))
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