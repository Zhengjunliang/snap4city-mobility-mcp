# Agentic Endpoint (OpenAI-compatible Tool Calling) — README

This document explains **how to use** the vLLM/ClearML-serving endpoint in **agentic** modality (OpenAI/MCP-compatible)

> **TL;DR**
>
> * To enable the OpenAI-MCP compliant response format (agentic modality), send **`tools`** *OR* **`tool_choice`** *OR* 
> a message with `role:"tool"`, and the system will return a complete **OpenAI object** (`choices`, `message`, 
> `tool_calls`, etc...).
> * If both `tools` or `tool_choice` are omitted in the request, the behavior is the same as in the **legacy** modality 
> (for backward compatibility), with response:
>
>   ```json
>   { "prompt": "...", "answer": "..." }
>   ```

---

## 0) Runtime VLLM prerequisites (BACKEND)

vLLM docker image has been started in the ClearML cluster with the following recommended options for Llama 4 + tool calling:

```bash
--enable-auto-tool-choice
--tool-call-parser llama4_pythonic
--chat-template /templates/tool_chat_template_llama.jinja
```

The tool call parser interprets the model output to extract tool calls in a proper structured format (e.g., JSON or Python).
**Recommended parser** for Llama4: `llama4_pythonic` (“pythonic” output for tool calling). 
See: https://docs.vllm.ai/en/v0.10.1/features/tool_calling.html#llama-models-llama3_json

tool_chat_template_llama.jinja has been used as the official chat template 
from: https://github.com/vllm-project/vllm/blob/main/examples/tool_chat_template_llama4_pythonic.jinja


## 1) Use case A: chat only (without tools)

To enable "agentic" mode even **without calling any tool**, include `tools: []` *OR* `tool_choice: "none"`.

**Request** (multimodal, OpenAI style):

```python
payload = {
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "<IMAGE_URL>"}},
                {"type": "text", "text": "Describe this image"}
            ]
        }
    ],
    "tools": [],
    "tool_choice": "none"
}
```

**Expected response example** (OpenAI/MCP compliant):

```json
{
    "id": "chatcmpl-XXXX",
    "object": "chat.completion",
    "created": "<TIMESTAMP_IN_SECONDS>",
    "model": "llama4-scout-fp8",
    "choices": [
      {
        "index": 0,
        "message": {
          "role": "assistant",
          "content": "This image contains...",
          "refusal": null,
          "annotations": null,
          "audio": null,
          "function_call": null,
          "tool_calls": [],
          "reasoning_content": null
        },
        "logprobs": null,
        "finish_reason": "stop",
        "stop_reason": null
      }
    ],
    "service_tier": null,
    "system_fingerprint": null,
    "usage": {
      "prompt_tokens": "<NUMBER_OF_PROMPT_TOKENS>",
      "total_tokens": "<NUMBER_OF_TOTAL_TOKENS>",
      "completion_tokens": "<NUMBER_OF_COMPLETION_TOKENS> = <NUMBER_OF_TOTAL_TOKENS> - <NUMBER_OF_PROMPT_TOKENS>",
      "prompt_tokens_details": null
    },
    "prompt_logprobs": null,
    "kv_transfer_params": null
}
```

The `choices` field contains the model’s actual reply payload: each item 
includes the assistant message (either plain text in content or pending tool_calls) and a finish_reason. In most agent 
loops, choices[0] is all you need to decide whether to execute a tool or display a final answer.

> If both `tools` and `tool_choice` are omitted, the endpoint behavior is the same as in the **legacy** modality 
> (for retro-compatibility), with response:
>
> ```json
> { "prompt": "...", "answer": "..." }
> ```

---

## 2) Use case B: native tool calling (with `tool_calls`)

To enable the LLM to **choose** and **execute** tools, the `tools` array must be included in the request. With 
`--enable-auto-tool-choice` the model decides **if** and **which** tool to use.

**Example of tool definition (OpenAI schema):**

```python
tools = [{
    "type": "function",
    "function": {
        "name": "find_services",
        "description": "Find the services within a radius (km) from lat/lon",
        "parameters": {
            "type": "object",
            "properties": {
                "lat": {"type": "number", "description": "Latitude"},
                "lon": {"type": "number", "description": "Longitude"},
                "radius_km": {"type": "number", "description": "Radius in km", "default": 1.0}
            },
            "required": ["lat", "lon", "radius_km"]
        }
    }
}]
```

**Request** (auto tool-calling):

```python

payload = {
    "messages": [
        {"role":"user","content":"Show me the services within 1km of Piazza Duomo, Firenze"}
    ],
    "tools": tools,
    "tool_choice": "auto"
}
```

**Response with  `tool_calls`:**

```json
{
  "choices": [
    {
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [
          {
            "id": "call_XXXXXX",
            "type": "function",
            "function": {
              "name": "find_services",
              "arguments": "{\"lat\":43.773, \"lon\":11.255, \"radius_km\":1.0}"
            }
          }
        ]
      },
      "finish_reason": "tool_calls"
    }
  ]
}
```

---

## 3) Use a specific tool

It is possible to force a specific tool:

```python
payload["tool_choice"] = {"type": "function", "function": {"name": "find_services"}}
```

---

## 4) Next turns: send back the **tool result/response**

After **executing** the tool (MCP server, orchestrator etc.), send **a new turn** with `role:"tool"`, 
**the same** `tool_call_id`, and the **result** of the tool execution:

```python
tool_call = response["choices"][0]["message"]["tool_calls"][0]
call_id   = tool_call["id"]

tool_result = {
    "services": [
        {"name":"Bank XXX","lat":43.7735,"lon":11.2561,"distance_km":0.2},
        {"name":"Restaurant YYY","lat":43.7742,"lon":11.2547,"distance_km":0.4}
    ]
}

followup = {
    "messages": [
        *payload["messages"],
        response["choices"][0]["message"],
        {
            "role": "tool",
            "tool_call_id": call_id,
            "content": json.dumps(tool_result)  # string
        }
    ],
    "tools": tools,
    "tool_choice": "none",   # in this way the LLM will generate the final text answer
}
```

**Final answer (text only):**

```json
{
    "choices": [
      {
        "message": {
          "role": "assistant",
          "content": "Within 1km I found: Bank XXX (200m), Restaurant YYY (400m)."
        },
        "finish_reason": "stop"
      }
    ]
}
```

---

## 5) Multimodal + tool calling

The **agentic modality** always use the OpenAI compliant format, also for images:

```json
{"type":"image_url","image_url":{"url":"https://..." }}
```

**Example:**

```python
payload = {
    "messages": [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "<IMAGE_URL>>"}},
                {"type": "text", "text": "Describe this image and use tools if needed..."}
            ]
        }
    ],
    "tools": tools,
    "tool_choice": "auto"
}
```