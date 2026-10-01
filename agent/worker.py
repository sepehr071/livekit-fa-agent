"""راهنما — Shia-Islamic RAG voice agent (gpt-realtime-2, speech-to-speech).

Every religious answer is GROUNDED in a knowledge base (Persian Shia corpus)
via the search_knowledge retrieval tool; the agent answers ONLY from retrieved
text. This is the voice port of a text RAG agent — its control lines
([[ANSWER_LENGTH]]/[[EXTRA]]), source-map citations, and answer-length tags are
text-app features and are intentionally dropped for a voice UX (no spoken citations).

DPI wiring (verified against livekit-agents 1.6.3):
  * Realtime model traffic rides AI_PROXY (local HTTP proxy to a foreign exit) via http_session=.
  * The RAG endpoint (RAG_URL) is reachable DIRECT from the box
    -> a SEPARATE proxy-free aiohttp session; the foreign proxy is never used for it.
  * The SFU link stays DIRECT: WorkerOptions(http_proxy=None) + LIVEKIT_URL=ws://127.0.0.1:7880
    (worker registration would otherwise be shoved through the proxy; the Rust media SDK
     ignores Python proxy env regardless).

Bilingual + transcript-sidecar notes (verified against installed openai SDK 2.43.0 + plugin 1.6.3):
  * gpt-realtime-2 hears raw audio; bilingual SPEECH is driven by the PROMPT.
    input_audio_transcription (the user-bubble sidecar) is REMOVED: on long RAG waits it
    hallucinated its own bias prompt as a phantom user turn and garbled short utterances
    (Azeri/Turkic flips). Voice comprehension is unaffected (model hears raw audio).
  * Realtime: temperature is DEPRECATED/ignored in v1 -> consistency lever is reasoning(effort=).
    semantic_vad/eagerness="low" stops it clipping Farsi pauses. noise_reduction accepts the
    bare string (plugin's to_noise_reduction() converts it -> object, dodging bug #3466).

Robustness (lossy Iran net): resume_false_interruption + metrics + idle-close (stops realtime
billing on abandoned tabs) — all carried over unchanged.
"""
import asyncio
import logging
import os
from dataclasses import dataclass

import aiohttp

from openai.types.beta.realtime.session import TurnDetection
from openai.types.realtime.realtime_reasoning import RealtimeReasoning

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    MetricsCollectedEvent,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)
from livekit.plugins import openai

logger = logging.getLogger("rahnama-agent")

AI_PROXY = os.environ.get("AI_PROXY", "http://127.0.0.1:8118")
# user "away" (silence) -> wait this long for them to return, then close to stop realtime billing.
IDLE_GRACE_SECONDS = float(os.environ.get("IDLE_GRACE_SECONDS", "45"))

# --- RAG endpoint (DIRECT from the box, NOT through AI_PROXY) ---------------------------
RAG_URL = os.environ["RAG_URL"]  # e.g. https://rag.example.com/query
RAG_API_KEY = os.environ.get("RAG_API_KEY", "")
RAG_TIMEOUT = float(os.environ.get("RAG_TIMEOUT", "10"))  # seconds
RAG_TOP_K = int(os.environ.get("RAG_TOP_K", "5"))          # passages kept per query
RAG_CHAR_CAP = int(os.environ.get("RAG_CHAR_CAP", "3000")) # total chars of grounding context

# Tight, labeled persona — gpt-realtime-2 under-follows long prompts, so the Dify prompt is
# distilled to the rules that survive a voice UX.
INSTRUCTIONS = (
    "تو «راهنما» هستی، یک دستیار صوتیِ دینی بر پایهٔ منابع معتبر اسلامی (شیعه). "
    "گرم، محترم و محاوره‌ای صحبت کن و پاسخ‌ها را کوتاه و طبیعی نگه دار.\n\n"
    "# قاعدهٔ اصلی\n"
    "- برای هر پرسش دینی/اسلامی، اول ابزار search_knowledge را صدا بزن و فقط بر اساس "
    "متنِ بازیابی‌شده پاسخ بده. از دانش یا نظر شخصیِ خودت چیزی اضافه نکن.\n"
    "- کوئریِ جست‌وجو را از کلمات کلیدیِ دینیِ همان سؤال بساز (به فارسی).\n"
    "- متنِ بازیابی‌شده را کلمه‌به‌کلمه نخوان؛ آن را بفهم و خلاصه‌وار، کوتاه و به زبان خودت پاسخ بده.\n"
    "- اگر ابزار «(no relevant passages found)» برگرداند، بگو: «در منابع موجود، پاسخ مستقیمی "
    "برای این سؤال پیدا نشد. می‌تونی سؤالت را کمی روشن‌تر یا متفاوت بپرسی تا بهتر کمکت کنم؟»\n"
    "- اگر «(knowledge base unavailable)» برگرداند، خیلی کوتاه عذرخواهی کن و بخواه کمی بعد دوباره بپرسد.\n\n"
    "# موضوع\n"
    "- فقط به پرسش‌های دینی/اسلامی پاسخ بده. برای سؤال کاملاً نامرتبط، کوتاه و دوستانه برگردان: "
    "«این سؤال کمی از مسیر دینی دور شد! 😊 اگر سؤالی دربارهٔ اسلام یا احکام داری، خوشحال می‌شم کمک کنم.» "
    "اگر سؤال کمی هم به دین مربوط بود، پاسخ دادن اشکالی ندارد.\n\n"
    "# زبان\n"
    "- پیش‌فرض فارسی. به همان زبانی پاسخ بده که کاربر در آخرین نوبت صحبت کرد. به‌خاطر لهجه، نام، "
    "یا یک کلمهٔ قرضی زبان را عوض نکن.\n\n"
    "# سبک صدا\n"
    "- محاوره‌ای و روان؛ بدون مارک‌داون، بدون فهرست، و بدون ذکر نام فایل، منبع، کتاب یا هر جزئیاتِ ابزار.\n\n"
    "# Language (English)\n"
    "- You are راهنما, a Shia-Islamic voice assistant grounded ONLY in the retrieved corpus.\n"
    "- For ANY religious question, ALWAYS call search_knowledge first, then answer only from "
    "its returned text — no outside knowledge or personal opinion. Build the search query from the "
    "question's core religious keywords (in Persian). Summarize the retrieved text in your own words; "
    "never read it verbatim.\n"
    "- Reply in whichever language the user last spoke (default Persian). Speak concisely; no markdown, "
    "no lists, and never mention sources, books, file names, or any tool detail.\n"
    "- Only Islamic/religious topics; politely redirect clearly off-topic questions."
)

# ---------------------------------------------------------------------------
# Session state: one proxy-free HTTP session, reused across a conversation's RAG calls.
# ---------------------------------------------------------------------------
@dataclass
class SessionData:
    http: aiohttp.ClientSession


@function_tool
async def search_knowledge(context: RunContext, query: str) -> str:
    """Search the Islamic knowledge base — a Shia Persian religious corpus covering the
    Quran, hadith, fiqh (احکام), the history and sayings of the Ahl al-Bayt, and Islamic theology.
    Call this BEFORE answering any Islamic/religious question, then answer ONLY from the passages
    it returns. `query` must be the core religious keywords of the user's question, in Persian
    (e.g. "نظر امام علی درباره حجاب"). Returns the most relevant passages, most relevant first."""
    q = (query or "").strip()
    if not q:
        return "(no relevant passages found)"
    try:
        async with context.userdata.http.post(
            RAG_URL,
            json={"query": q[:2000]},
            headers={"X-API-Key": RAG_API_KEY},
            timeout=aiohttp.ClientTimeout(total=RAG_TIMEOUT),
        ) as resp:
            if resp.status != 200:
                logger.warning("rag http %s for %r", resp.status, q[:80])
                return "(knowledge base unavailable)"
            data = await resp.json()
    except Exception as e:  # noqa: BLE001 — best-effort grounding; never crash the turn
        logger.warning("rag call failed: %s", e)
        return "(knowledge base unavailable)"

    nodes = data.get("nodes") or []  # already ordered by relevance desc
    parts: list[str] = []
    total = 0
    for n in nodes[:RAG_TOP_K]:
        text = (n.get("text") or "").strip()
        if not text:
            continue
        if total + len(text) > RAG_CHAR_CAP:
            text = text[: max(0, RAG_CHAR_CAP - total)]
        if not text:
            break
        parts.append(text)
        total += len(text)
        if total >= RAG_CHAR_CAP:
            break
    if not parts:
        return "(no relevant passages found)"
    logger.info("rag: %d passages, %d chars for %r", len(parts), total, q[:80])
    return "\n\n---\n\n".join(parts)


def _load_by_active_jobs(worker) -> float:
    """Worker load = active-job count, NOT CPU.

    The default CPU-based load_fnc mis-reports on this VM: it oscillates around the prod
    load_threshold (0.7) even when the box is idle (load avg ~0.25), so the worker flaps
    "at full capacity -> unavailable" and the SFU gets "no servers available (received 1
    responses)" -> the agent is never dispatched and the UI hangs on "در حال اتصال".
    Counting active jobs is CPU-noise-immune: 0 jobs -> 0.0 (always available); the worker
    only goes unavailable near ~7 concurrent jobs (default 0.7 threshold). See livekit/agents#2130.
    """
    try:
        return min(len(worker.active_jobs) / 10.0, 1.0)
    except Exception:  # noqa: BLE001 — never let load calc wedge the worker; stay available
        return 0.0


async def entrypoint(ctx: JobContext):
    await ctx.connect()

    # DIRECT session (no proxy) — the RAG endpoint is domestic-reachable from the box.
    rag_http = aiohttp.ClientSession()

    session = AgentSession[SessionData](
        userdata=SessionData(http=rag_http),
        llm=openai.realtime.RealtimeModel(
            model="gpt-realtime-2",
            voice="ash",  # male voice (clear, mid-range)
            # reasoning-capable model: low effort = production-voice latency sweet spot.
            reasoning=RealtimeReasoning(effort="low"),
            # content-aware endpointing; eagerness="low" waits longest -> least Farsi cutoff.
            # interrupt_response=False: do NOT let detected user audio truncate the agent's reply.
            # On the lossy Iran net + long RAG waits, filler-echo/noise was firing OpenAI's VAD and
            # cancelling the answer ("speech not done in time after interruption"). Q&A bot ->
            # reliability over barge-in.
            turn_detection=TurnDetection(
                type="semantic_vad",
                eagerness="low",
                create_response=True,
                interrupt_response=False,
            ),
            # input_audio_transcription removed: the user-bubble sidecar hallucinated its bias
            # prompt as a phantom user turn during long waits and garbled short utterances.
            # Voice comprehension is unaffected (model hears raw audio).
            input_audio_noise_reduction="near_field",
            # realtime model -> Frankfurt via the local proxy (the only border crossing).
            http_session=aiohttp.ClientSession(proxy=AI_PROXY),
        ),
        # Interruption is owned SERVER-SIDE by the realtime model (semantic_vad turn detection),
        # so it's controlled via interrupt_response=False above — NOT by livekit. Setting
        # allow_interruptions=False / turn_handling interruption.enabled=False here is REJECTED by
        # livekit when the RealtimeModel does server-side turn detection (raises ValueError ->
        # job crashes at session.start). interrupt_response=False is the only lever needed.
        # (Deprecated resume_false_interruption / false_interruption_timeout / min_interruption_*
        # dropped; they had the same intent.)
    )

    async def _close_http():
        await rag_http.close()

    ctx.add_shutdown_callback(_close_http)

    # --- metrics: per-turn TTFT/latency + per-session token cost into pm2 logs ---
    usage = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev: MetricsCollectedEvent):
        metrics.log_metrics(ev.metrics)
        usage.collect(ev.metrics)

    async def _log_usage():
        logger.info("session usage: %s", usage.get_summary())

    ctx.add_shutdown_callback(_log_usage)

    # --- idle close: when the user goes "away" (silence), close after a grace period so
    #     gpt-realtime-2 stops billing on abandoned/flaky-disconnected tabs ---
    idle = {"task": None}

    async def _close_when_idle():
        await asyncio.sleep(IDLE_GRACE_SECONDS)
        logger.info("idle %ss after user went away — closing session", IDLE_GRACE_SECONDS)
        ctx.shutdown(reason="idle")

    @session.on("user_state_changed")
    def _on_user_state(ev):
        if ev.new_state == "away":
            if idle["task"] is None or idle["task"].done():
                idle["task"] = asyncio.create_task(_close_when_idle())
        elif idle["task"] is not None and not idle["task"].done():
            idle["task"].cancel()
            idle["task"] = None

    await session.start(
        room=ctx.room,
        agent=Agent(instructions=INSTRUCTIONS, tools=[search_knowledge]),
    )
    await session.generate_reply(
        instructions="خیلی کوتاه (یک جمله) خودت را به‌عنوان «راهنما»، دستیار دینی، معرفی کن "
        "و بپرس چه سؤال دینی‌ای داری."
    )


if __name__ == "__main__":
    # http_proxy=None => the worker's SFU registration stays direct (NOT through the proxy)
    # load_fnc=_load_by_active_jobs => availability by job count, not the flaky CPU default
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            http_proxy=None,
            load_fnc=_load_by_active_jobs,
        )
    )
