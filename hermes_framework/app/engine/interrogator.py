from __future__ import annotations

from typing import Optional

from app.api.sse import EventBus
from app.config import settings
from app.engine.containers import discover_containers
from app.engine.prompts import INTERROGATOR_SYSTEM, INTERROGATOR_TOOLS
from app.llm.anthropic_client import get_async_client
from app.models import Question
from app.state.store import Store


class InterrogationResult:
    def __init__(
        self,
        proceed: bool,
        questions: Optional[list[Question]] = None,
        resolved_container_id: Optional[str] = None,
        available_containers: Optional[list[str]] = None,
    ):
        self.proceed = proceed
        self.questions = questions or []
        self.resolved_container_id = resolved_container_id
        self.available_containers = available_containers or []


class Interrogator:
    """Implements Plan Mode — decide whether to ask clarifying questions.

    Forces a binary tool call (`proceed` or `ask_clarifications`). When the
    model asks for clarifications, the questions are persisted as Question
    rows and a `plan_mode.question` event is emitted per question. The
    orchestrator then pauses until `/answer` arrives for each pending one.
    """

    def __init__(self, store: Store, bus: EventBus) -> None:
        self.store = store
        self.bus = bus
        self.client = get_async_client()

    async def interrogate(
        self, session_id: str, user_msg: str, container_id: Optional[str]
    ) -> InterrogationResult:
        # Resolve container without nagging the user. If the request implies
        # cross-container intent ("all my X"), the planner sees the full list
        # via `available_containers` and can fan out via CODE_TRANSFORM. The
        # `container_id` we set is the "primary" — used for single-container
        # tools (aiagent, get_document_insights) when no fan-out is needed.
        resolved = container_id
        available = discover_containers()
        if not resolved:
            if len(available) == 1:
                # Unambiguous — auto-resolve without bothering the user.
                resolved = available[0]
                await self.bus.emit(
                    session_id,
                    "container.resolved",
                    {
                        "container_id": resolved,
                        "available": available,
                        "reason": "only_one_available",
                    },
                )
            # If multiple containers: do NOT auto-pick. Pass the full list to
            # the LLM so it can ask which container (for single-container
            # queries like RAG Q&A) or proceed for cross-container intents
            # ("translate all my documents", "dashboard from all containers").

        available_str = ", ".join(available) if available else "(none discovered)"
        user_content = (
            f"User request: {user_msg}\n"
            f"Available containers: [{available_str}]\n"
            f"User-specified container_id: {resolved or '(not specified — user did not provide one)'}\n\n"
            "Decide: proceed, or ask_clarifications. Call exactly one tool."
        )
        msg = await self.client.messages.create(
            model=settings.planner_model,
            max_tokens=2000,
            system=INTERROGATOR_SYSTEM,
            tools=INTERROGATOR_TOOLS,
            tool_choice={"type": "any"},
            messages=[{"role": "user", "content": user_content}],
        )

        for block in msg.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            name = getattr(block, "name", "")
            inp = block.input  # type: ignore[attr-defined]
            if name == "proceed":
                return InterrogationResult(
                    proceed=True,
                    resolved_container_id=resolved,
                    available_containers=available,
                )
            if name == "ask_clarifications":
                questions: list[Question] = []
                for q in inp.get("questions", []):
                    quest = Question(text=q["text"], options=q.get("options"))
                    await self.store.save_question(session_id, quest)
                    await self.bus.emit(
                        session_id,
                        "plan_mode.question",
                        {"question_id": quest.id, "text": quest.text, "options": quest.options},
                    )
                    questions.append(quest)
                return InterrogationResult(
                    proceed=False,
                    questions=questions,
                    resolved_container_id=resolved,
                    available_containers=available,
                )

        # Default to proceeding if the model failed to use a tool — better to
        # try than to hang.
        return InterrogationResult(
                    proceed=True,
                    resolved_container_id=resolved,
                    available_containers=available,
                )
