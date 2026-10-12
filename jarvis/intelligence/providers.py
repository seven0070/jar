"""Model providers: interchangeable local and cloud intelligence.

Every provider speaks the same tiny interface (:meth:`Provider.complete`), so the
executive never knows whether a plan came from a local Ollama model, a cloud API
or the built-in deterministic fallback. The deterministic provider is what makes
JARVIS boot and operate with no keys, no network and no GPU - and it is what the
test-suite exercises end to end.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from jarvis.errors import ModelUnavailable

TASK_CLASSES = ("planning", "reasoning", "coding", "summarising", "vision", "cheap")


@dataclass
class ModelSpec:
    id: str
    name: str
    kind: str                       # local | cloud | deterministic
    task_classes: List[str] = field(default_factory=lambda: list(TASK_CLASSES))
    cost: float = 0.0               # relative cost per call, 0 = free
    latency: float = 0.0            # relative latency, lower is better
    context: int = 8192
    enabled: bool = True
    options: Dict[str, Any] = field(default_factory=dict)

    def supports(self, task_class: str) -> bool:
        return task_class in self.task_classes or "any" in self.task_classes


@dataclass
class Completion:
    text: str
    model: str
    latency: float = 0.0
    usage: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""

    def json(self, default: Any = None) -> Any:
        return extract_json(self.text, default)


@dataclass
class CompletionRequest:
    prompt: str
    system: str = ""
    task_class: str = "reasoning"
    max_tokens: int = 1024
    temperature: float = 0.2
    json_mode: bool = False


class Provider:
    """Base class for model providers."""

    name = "provider"

    def __init__(self, spec: ModelSpec):
        self.spec = spec
        self.last_error = ""

    def available(self) -> bool:
        return self.spec.enabled

    def supports(self, task_class: str) -> bool:
        return self.spec.supports(task_class)

    def complete(self, request: CompletionRequest) -> Completion:  # pragma: no cover - interface
        raise NotImplementedError


# --------------------------------------------------------------------------- #
# Deterministic offline provider
# --------------------------------------------------------------------------- #
class DeterministicProvider(Provider):
    """Rule-based provider that always answers.

    It exists so that planning, verification and the operating cycle are exercisable
    without external services, and so a machine with no model installed still gets a
    coherent (if basic) plan instead of a crash.
    """

    name = "deterministic"

    def __init__(self, spec: Optional[ModelSpec] = None):
        super().__init__(
            spec or ModelSpec(
                id="jarvis-deterministic", name="JARVIS Deterministic Rules",
                kind="deterministic", cost=0.0, latency=0.0,
            )
        )
        self.plans: Dict[str, Dict[str, Any]] = {}

    def register_plan(self, key: str, plan: Dict[str, Any]) -> None:
        self.plans[key] = plan

    def available(self) -> bool:
        return True

    def complete(self, request: CompletionRequest) -> Completion:
        started = time.time()
        text = self._respond(request)
        return Completion(
            text=text, model=self.spec.id, latency=time.time() - started,
            usage={"source": "deterministic"}, provider=self.name,
        )

    def _respond(self, request: CompletionRequest) -> str:
        prompt = request.prompt or ""
        if request.json_mode:
            return json.dumps(self._json_for(prompt), indent=2)
        return self._text_for(prompt)

    def _json_for(self, prompt: str) -> Dict[str, Any]:
        for key, plan in self.plans.items():
            if key and key.lower() in prompt.lower():
                return plan
        lowered = prompt.lower()
        intent = _find_intent(lowered)
        return default_plan_for(intent, prompt)

    def _text_for(self, prompt: str) -> str:
        head = prompt.strip().splitlines()[0] if prompt.strip() else ""
        return (
            f"Deterministic analysis of: {head[:160]}\n"
            "No model was reachable, so this is a rule-based response. "
            "Configure a local or cloud model for richer reasoning."
        )


# --------------------------------------------------------------------------- #
# HTTP providers (local Ollama, cloud OpenAI-compatible)
# --------------------------------------------------------------------------- #
class HttpJsonProvider(Provider):
    """Shared plumbing for JSON-over-HTTP providers."""

    def __init__(self, spec: ModelSpec, endpoint: str, api_key: str = "", timeout: float = 60.0,
                 headers: Optional[Dict[str, str]] = None):
        super().__init__(spec)
        self.endpoint = endpoint
        self.api_key = api_key
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._probe: Optional[bool] = None

    def available(self) -> bool:
        if not self.spec.enabled:
            return False
        if self._probe is not None:
            return self._probe
        try:
            self._post(self._probe_payload(), timeout=min(self.timeout, 5.0))
            self._probe = True
        except Exception as exc:
            self.last_error = str(exc)
            self._probe = False
        return self._probe

    def reset_probe(self) -> None:
        self._probe = None

    def _post(self, payload: Dict[str, Any], timeout: Optional[float] = None) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        headers.update(self.headers)
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.endpoint, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.URLError as exc:
            raise ModelUnavailable(f"{self.name} unreachable: {exc}") from exc

    def _probe_payload(self) -> Dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError


class OllamaProvider(HttpJsonProvider):
    """Local models served by Ollama (http://127.0.0.1:11434)."""

    name = "ollama"

    def __init__(self, spec: ModelSpec, host: str = "http://127.0.0.1:11434", timeout: float = 120.0):
        super().__init__(spec, f"{host.rstrip('/')}/api/generate", timeout=timeout)
        self.host = host

    def _probe_payload(self) -> Dict[str, Any]:
        return {"model": self.spec.id, "prompt": "ping", "stream": False, "options": {"num_predict": 1}}

    def complete(self, request: CompletionRequest) -> Completion:
        started = time.time()
        payload = {
            "model": self.spec.id,
            "prompt": request.prompt,
            "system": request.system,
            "stream": False,
            "format": "json" if request.json_mode else None,
            "options": {"temperature": request.temperature, "num_predict": request.max_tokens},
        }
        payload = {k: v for k, v in payload.items() if v is not None}
        data = self._post(payload)
        return Completion(
            text=data.get("response", ""), model=self.spec.id,
            latency=time.time() - started, usage={"eval_count": data.get("eval_count")},
            provider=self.name,
        )


class OpenAICompatibleProvider(HttpJsonProvider):
    """Any OpenAI-compatible chat completions endpoint (cloud or self-hosted)."""

    name = "openai-compatible"

    def __init__(self, spec: ModelSpec, endpoint: str = "https://api.openai.com/v1/chat/completions",
                 api_key: str = "", timeout: float = 90.0):
        super().__init__(spec, endpoint, api_key=api_key, timeout=timeout)

    def _probe_payload(self) -> Dict[str, Any]:
        return {"model": self.spec.id, "messages": [{"role": "user", "content": "ping"}],
                "max_tokens": 1}

    def complete(self, request: CompletionRequest) -> Completion:
        started = time.time()
        messages = []
        if request.system:
            messages.append({"role": "system", "content": request.system})
        messages.append({"role": "user", "content": request.prompt})
        payload: Dict[str, Any] = {
            "model": self.spec.id, "messages": messages,
            "max_tokens": request.max_tokens, "temperature": request.temperature,
        }
        if request.json_mode:
            payload["response_format"] = {"type": "json_object"}
        data = self._post(payload)
        try:
            text = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ModelUnavailable(f"unexpected response shape: {exc}") from exc
        return Completion(text=text or "", model=self.spec.id, latency=time.time() - started,
                          usage=data.get("usage") or {}, provider=self.name)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_JSON_BLOCK = re.compile(r"\{.*\}|\[.*\]", re.S)


def extract_json(text: str, default: Any = None) -> Any:
    """Pull the first JSON object/array out of a model response."""
    if not text:
        return default
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.S)
    try:
        return json.loads(text)
    except ValueError:
        pass
    match = _JSON_BLOCK.search(text)
    if match:
        try:
            return json.loads(match.group(0))
        except ValueError:
            return default
    return default


def _find_intent(lowered: str) -> str:
    for keyword, intent in INTENT_KEYWORDS:
        if keyword in lowered:
            return intent
    return "assist"


INTENT_KEYWORDS = (
    ("fix", "fix"), ("bug", "fix"), ("broken", "fix"), ("failing test", "fix"),
    ("run the tests", "tests"), ("run tests", "tests"), ("test suite", "tests"),
    ("research", "research"), ("investigate", "research"), ("find out", "research"),
    ("monitor", "monitor"), ("watch", "monitor"), ("notify", "monitor"),
    ("release", "release"), ("ship", "release"),
    ("refactor", "code"), ("implement", "code"), ("write code", "code"),
    ("read", "inspect"), ("list", "inspect"), ("show me", "inspect"),
)


def default_plan_for(intent: str, prompt: str = "") -> Dict[str, Any]:
    """The rule-based plan templates used when no model is available."""
    if intent == "tests":
        return {
            "objective": "Run the project test suite and report the verified result",
            "steps": [
                {"tool": "code.run_tests", "args": {}, "title": "Run the test suite",
                 "produces": "tests", "requires": ["code.run"]},
            ],
            "success_criteria": [
                {"kind": "result_field", "args": {"path": "passed"}, "expect": True,
                 "description": "the test suite exited cleanly"},
            ],
        }
    if intent == "fix":
        return {
            "objective": "Reproduce the failure, repair it, and prove the repair with tests",
            "steps": [
                {"tool": "code.run_tests", "args": {}, "title": "Reproduce the failure",
                 "produces": "before", "requires": ["code.run"]},
                {"tool": "fs.search", "args": {"needle": "TODO"}, "title": "Locate the defect",
                 "produces": "location", "requires": ["fs.read"], "optional": True},
                {"tool": "code.run_tests", "args": {}, "title": "Confirm the repair",
                 "produces": "tests", "requires": ["code.run"]},
            ],
            "success_criteria": [
                {"kind": "result_field", "args": {"path": "passed"}, "expect": True,
                 "description": "tests pass after the repair"},
            ],
        }
    if intent == "research":
        return {
            "objective": "Gather evidence on the question and record a sourced summary",
            "steps": [
                {"tool": "research.collect", "args": {"question": prompt[:400]},
                 "title": "Collect evidence", "produces": "evidence",
                 "requires": ["network.fetch"], "optional": True},
                {"tool": "assistant.answer", "args": {"question": prompt[:400]},
                 "title": "Write the summary", "produces": "summary", "requires": []},
            ],
            "success_criteria": [
                {"kind": "result_field", "args": {"path": "answered"}, "expect": True,
                 "description": "a written answer was produced"},
            ],
        }
    if intent == "inspect":
        return {
            "objective": "Inspect the workspace and report what was found",
            "steps": [
                {"tool": "fs.list", "args": {}, "title": "List the workspace",
                 "produces": "listing", "requires": ["fs.read"]},
                {"tool": "assistant.answer", "args": {"question": prompt[:400]},
                 "title": "Summarise", "produces": "summary", "requires": []},
            ],
            "success_criteria": [
                {"kind": "result_field", "args": {"path": "answered"}, "expect": True,
                 "description": "a summary was produced"},
            ],
        }
    return {
        "objective": "Answer the request using memory and the available tools",
        "steps": [
            {"tool": "assistant.answer", "args": {"question": prompt[:400]},
             "title": "Compose the answer", "produces": "summary", "requires": []},
        ],
        "success_criteria": [
            {"kind": "result_field", "args": {"path": "answered"}, "expect": True,
             "description": "an answer was produced"},
        ],
    }


def providers_from_env() -> List[Provider]:
    """Build the provider list from the environment.

    ``JARVIS_OLLAMA`` (e.g. ``llama3.1`` or ``host=...,model=...``) adds a local model.
    ``JARVIS_OPENAI_API_KEY`` plus optional ``JARVIS_OPENAI_MODEL`` /
    ``JARVIS_OPENAI_BASE_URL`` add a cloud model. The deterministic provider is
    always last, so the system degrades instead of failing.
    """
    providers: List[Provider] = []

    ollama = os.environ.get("JARVIS_OLLAMA", "").strip()
    if ollama:
        host = "http://127.0.0.1:11434"
        model = ollama
        if "=" in ollama:
            for part in ollama.split(","):
                key, _, value = part.partition("=")
                if key.strip() == "host":
                    host = value.strip()
                elif key.strip() == "model":
                    model = value.strip()
        spec = ModelSpec(id=model, name=f"Ollama {model}", kind="local",
                         cost=0.0, latency=1.0, context=8192)
        providers.append(OllamaProvider(spec, host=host))

    key = os.environ.get("JARVIS_OPENAI_API_KEY", "").strip()
    if key:
        model = os.environ.get("JARVIS_OPENAI_MODEL", "gpt-4o-mini")
        base = os.environ.get("JARVIS_OPENAI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
        spec = ModelSpec(id=model, name=f"Cloud {model}", kind="cloud", cost=1.0, latency=2.0,
                         context=128_000)
        providers.append(OpenAICompatibleProvider(spec, endpoint=f"{base}/chat/completions",
                                                 api_key=key))

    providers.append(DeterministicProvider())
    return providers
