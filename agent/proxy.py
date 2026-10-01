"""DPI bypass — the AI (foreign) leg.

Routes the agent's foreign AI calls (OpenAI STT / LLM / TTS or Realtime) through the
server-local forward proxy, while the LiveKit SFU link stays DIRECT (domestic).

Why it must be per-plugin and NOT a global HTTPS_PROXY (verified in livekit-agents src):
  * The LiveKit *worker* reads HTTPS_PROXY/HTTP_PROXY into WorkerOptions.http_proxy and
    applies it to its OWN SFU-registration WebSocket -- and it ignores NO_PROXY. A global
    proxy would wrongly tunnel the (domestic, localhost) SFU link through Frankfurt.
  * Room media + signaling run through the Rust livekit-rtc SDK, which ignores Python
    proxy env entirely -> stays direct for free.
  => Set WorkerOptions(http_proxy=None); inject the proxy ONLY into the AI clients below.

"""
import os

import aiohttp
import httpx

# Local HTTP CONNECT proxy with a foreign exit. Override via env.
AI_PROXY = os.environ.get("AI_PROXY", "http://127.0.0.1:8118")


def proxied_aiohttp_session() -> aiohttp.ClientSession:
    """aiohttp session whose every request + websocket rides the AI proxy.

    Hand to any plugin that accepts ``http_session=`` (openai realtime/stt-stream,
    deepgram, cartesia, elevenlabs, azure-tts). HTTP CONNECT covers wss:// too.
    You OWN this session -> close it on shutdown (ctx.add_shutdown_callback).
    """
    return aiohttp.ClientSession(proxy=AI_PROXY)


def proxied_httpx_client() -> httpx.AsyncClient:
    """httpx client through the AI proxy. Feed to the OpenAI SDK via http_client=,
    then pass that SDK client to the LiveKit plugin via client=.

        import openai
        from livekit.plugins import openai as lk_openai
        llm = lk_openai.LLM(model="<your-choice>",
                            client=openai.AsyncClient(http_client=proxied_httpx_client()))
    """
    return httpx.AsyncClient(proxy=AI_PROXY)


# Reminder for the worker entrypoint (model-agnostic; you pick STT/LLM/TTS or Realtime):
#   cli.run_app(WorkerOptions(entrypoint_fnc=..., http_proxy=None))  # SFU stays DIRECT
#   LIVEKIT_URL = ws://127.0.0.1:7880   (worker <-> SFU is localhost, never proxied)
