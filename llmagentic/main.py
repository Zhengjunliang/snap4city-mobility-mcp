# AGENTIC MAIN

import json
import os
import requests
from token_manager import TokenManager
import time


def _pp(obj):
    # Pretty-print JSON with indentation, preserving unicode characters
    print(json.dumps(obj, indent=2, ensure_ascii=False))


def _pretty_print_tool_calls(tool_calls):
    if not tool_calls:
        return
    print("\n[MAIN] --- Tool calls ---\n")
    for i, tc in enumerate(tool_calls, 1):
        f = tc.get("function", {}) if isinstance(tc, dict) else {}
        name = f.get("name")
        args = f.get("arguments")
        # If arguments are JSON string, try to parse it to "pretty" print
        parsed_args = None
        if isinstance(args, str):
            try:
                parsed_args = json.loads(args)
            except Exception:
                parsed_args = args  # lascio la stringa com’è
        else:
            parsed_args = args

        print(f"[{i}] id={tc.get('id')}  type={tc.get('type')}  function={name}")
        if parsed_args is not None:
            try:
                print(json.dumps(parsed_args, indent=2, ensure_ascii=False))
            except Exception:
                print(parsed_args)


def _print_openai_agentic_response(resp: dict, full_dump: bool):
    """
    Print OpenAI output:
    - full_dump=True  -> print content (text), tool_calls and the whole response object (prettyfied).
    - full_dump=False -> print ONLY the text answer (message.content).
    """
    try:
        choice0 = (resp.get("choices") or [{}])[0]
        message = choice0.get("message", {}) if isinstance(choice0, dict) else {}
        content = message.get("content")
        tool_calls = message.get("tool_calls") or []

        if full_dump:
            print("\n[MAIN] --- Assistant content ---\n")
            if isinstance(content, str) and content.strip():
                print(content)
            else:
                print("(no textual content)")
            _pretty_print_tool_calls(tool_calls)
            print("\n[MAIN] --- OpenAI Agentic Complete Response ---\n")
            _pp(resp)
        else:
            # print ONLY text answer (content)
            print("\n[MAIN] --- Assistant answer ---\n")
            if isinstance(content, str) and content.strip():
                print(content)
            else:
                print("(no textual content in this turn; the model may have emitted tool_calls)")

    except Exception as e:
        print(f"[MAIN] - Pretty print failed: {e}")
        try:
            choice0 = (resp.get("choices") or [{}])[0]
            message = choice0.get("message", {}) if isinstance(choice0, dict) else {}
            content = message.get("content")
            if isinstance(content, str) and content.strip():
                print(content)
            else:
                print(resp)
        except Exception:
            print(resp)


def load_json(path, required_keys):
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    missing = [k for k in required_keys if k not in data]
    if missing:
        raise KeyError(f"Missing keys in '{path}': {missing}")
    return data


def validate_messages(messages):
    if not isinstance(messages, list):
        raise ValueError("messages must be a list of objects")
    for i, msg in enumerate(messages):
        if not isinstance(msg, dict):
            raise ValueError(f"messages[{i}] must be an object")
        if "role" not in msg or not isinstance(msg["role"], str) or not msg["role"].strip():
            raise ValueError(f"messages[{i}].role must be a non-empty string")
        if "content" not in msg or not isinstance(msg["content"], list):
            raise ValueError(f"messages[{i}].content must be a list")
        for j, part in enumerate(msg["content"]):
            if not isinstance(part, dict):
                raise ValueError(f"messages[{i}].content[{j}] must be an object")
            t = part.get("type")
            # [AGENTIC] accepts 'text', 'image' (legacy) and 'image_url' (OpenAI compliant)
            if t not in ("text", "image", "image_url"):
                raise ValueError(f"messages[{i}].content[{j}].type must be 'text' or 'image' or 'image_url'")
            if t == "text":
                if "text" not in part or not isinstance(part["text"], str):
                    raise ValueError(f"messages[{i}].content[{j}].text must be a string")
            if t == "image":
                # legacy: {"type":"image","url":"..."}
                if "url" not in part or not isinstance(part["url"], str) or not part["url"].strip():
                    raise ValueError(f"messages[{i}].content[{j}].url must be a non-empty string")
            if t == "image_url":
                # OpenAI style: {"type":"image_url","image_url":{"url":"..."}}
                iu = part.get("image_url")
                if not isinstance(iu, dict) or "url" not in iu or not isinstance(iu["url"], str) or not iu[
                    "url"].strip():
                    raise ValueError(f"messages[{i}].content[{j}].image_url.url must be a non-empty string")


def read_text_file(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def read_messages_file(path):
    # File must contain a valid JSON array
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    try:
        data = json.loads(raw)
    except Exception as e:
        raise ValueError(f"Failed to parse JSON from messages file '{path}': {e}")
    validate_messages(data)
    return data


def parse_messages_from_obj(value):
    """value must be a Python list already deserialized (messages array)."""
    if not isinstance(value, list):
        raise ValueError("prompt_multimodal_obj must be a JSON array (list) already deserialized")
    validate_messages(value)
    return value


def resolve_input(cfg):
    """
    Supported modes (highest -> lowest priority for autodetect):
      - 'prompt_multimodal_file'        -> cfg['prompt_multimodal_file'] (file with JSON array of messages)
      - 'prompt_multimodal_obj'         -> cfg['prompt_multimodal_obj'] (already-deserialized array)
      - 'prompt_file'                   -> cfg['prompt_file'] (plain text)
      - 'prompt_string'                 -> cfg['prompt_string'] (plain text)

    If cfg['input_type'] is present, it overrides autodetect even if others exist.
    Accepted values for input_type:
      'prompt_multimodal_file' | 'prompt_multimodal_obj' | 'prompt_file' | 'prompt_string'
    """
    # explicit override
    input_type = cfg.get("input_type")
    if input_type:
        mode = input_type.strip()
    else:
        # autodetect with requested priority
        if "prompt_multimodal_file" in cfg and str(cfg["prompt_multimodal_file"]).strip():
            mode = "prompt_multimodal_file"
        elif "prompt_multimodal_obj" in cfg and cfg["prompt_multimodal_obj"] not in (None, ""):
            mode = "prompt_multimodal_obj"
        elif "prompt_file" in cfg and str(cfg["prompt_file"]).strip():
            mode = "prompt_file"
        elif "prompt_string" in cfg and str(cfg["prompt_string"]).strip():
            mode = "prompt_string"
        else:
            raise KeyError(
                "No input source provided. Add one of: prompt_multimodal_file | prompt_multimodal_obj | "
                "prompt_file | prompt_string"
            )

    # resolve by mode
    if mode == "prompt_multimodal_file":
        path = str(cfg.get("prompt_multimodal_file", "")).strip()
        if not path:
            raise KeyError("prompt_multimodal_file is selected but not provided")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Messages file not found: '{path}'")
        messages = read_messages_file(path)
        return ("messages", messages, mode)

    if mode == "prompt_multimodal_obj":
        value = cfg.get("prompt_multimodal_obj")
        messages = parse_messages_from_obj(value)
        return ("messages", messages, mode)

    if mode == "prompt_file":
        path = str(cfg.get("prompt_file", "")).strip()
        if not path:
            raise KeyError("prompt_file is selected but not provided")
        if not os.path.exists(path):
            raise FileNotFoundError(f"Prompt file not found: '{path}'")
        prompt = read_text_file(path)
        return ("prompt", prompt, mode)

    if mode == "prompt_string":
        prompt = cfg.get("prompt_string")
        if not isinstance(prompt, str) or not prompt.strip():
            raise KeyError("prompt_string is selected but empty or not a string")
        return ("prompt", prompt, mode)

    raise ValueError(f"Unsupported input_type '{mode}'")


if __name__ == "__main__":
    # === Load credentials ===
    creds = load_json(
        "user_credentials.json",
        required_keys=["username", "password"]
    )
    username = creds["username"]
    password = creds["password"]

    # === Load ClearML config ===
    cfg = load_json(
        "clearml_config.json",
        required_keys=["clearml_ondemand_api_base_url", "clearml_llm_endpoint"]
    )
    clearml_ondemand_api_base_url = cfg["clearml_ondemand_api_base_url"]
    clearml_llm_endpoint = cfg["clearml_llm_endpoint"]

    # [AGENTIC_DEBUG] Flag to print the complete response in OpenAI agentic format
    #   - Enable via config:  "agentic_debug_full_dump": true
    agentic_debug_full_dump = bool(
        cfg.get("agentic_debug_full_dump", False)
    )

    # === Resolve input with override or autodetect ===
    input_kind, payload, mode_used = resolve_input(cfg)

    print("[MAIN] - Starting Main Script...")
    print(f"[MAIN] - Input mode: {mode_used}")
    print()

    tm = TokenManager(username, password)
    access_token = tm.get_token()

    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    if input_kind == "messages":
        params = {"messages": payload}
        # [AGENTIC] optional: enable agentic modality even without tool execution
        if "tools" in cfg:
            params["tools"] = cfg["tools"]
        if "tool_choice" in cfg:
            params["tool_choice"] = cfg["tool_choice"]
        if "temperature" in cfg:
            params["temperature"] = cfg["temperature"]
        print(f"[MAIN] - Sending multimodal messages to endpoint '{clearml_llm_endpoint}'")
        try:
            preview = json.dumps(payload[:1], ensure_ascii=False) if isinstance(payload, list) else "<object>"
            print(f"[MAIN] - Messages preview: {preview}")
        except Exception:
            pass
    else:
        params = {"prompt": payload}
        # [AGENTIC] optional: force OpenAI format also for chat-only
        if "tools" in cfg:
            params["tools"] = cfg["tools"]
        if "tool_choice" in cfg:
            params["tool_choice"] = cfg["tool_choice"]
        if "temperature" in cfg:
            params["temperature"] = cfg["temperature"]
        print(f"[MAIN] - Sending prompt to endpoint '{clearml_llm_endpoint}':")
        print(f"\"{payload}\"")

    body = {
        "access_token": access_token,  # [AGENTIC] None in questo esempio
        "endpoint": clearml_llm_endpoint,
        "params": params
    }

    print()
    print(f"[MAIN] - Calling API: '{clearml_ondemand_api_base_url}':")
    print(f"[MAIN] - Endpoint: '{clearml_llm_endpoint}':\n")

    t0 = time.perf_counter()
    response = requests.post(clearml_ondemand_api_base_url, data=json.dumps(body), headers=headers)
    t1 = time.perf_counter()
    print(f"API Response time:  {t1 - t0:.2f} s")

    try:
        parsed = response.json()
        if isinstance(parsed, dict) and "choices" in parsed:
            full_dump = bool(cfg.get("agentic_debug_full_dump", False))
            _print_openai_agentic_response(parsed, full_dump)
        # LEGACY
        elif "answer" in parsed:
            print("\n[MAIN] --- Answer ---\n")
            print(parsed["answer"])
        elif "message" in parsed:
            print("\n[MAIN] --- Error in Calling API: ", parsed["message"])
        elif "detail" in parsed:
            print("\n[MAIN] --- Error in Calling API: ", parsed["detail"])
        else:
            try:
                _pp(parsed)
            except Exception:
                print("\n[MAIN] --- ", response.text)

    except Exception as e:
        print(f"[MAIN] - Response parsing failed: {e}")
        print(response.text)

    print()
    print('[MAIN] - End Main Script.')
