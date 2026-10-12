"""Voice interface.

The voice layer is deliberately thin: it turns speech into the same request the
desktop and CLI send, and speaks back the verified outcome. Nothing about intent,
planning, permissions or verification changes because the input arrived by voice.

Backends, chosen automatically:

* **TTS** - Windows SAPI via PowerShell (no dependency), ``pyttsx3`` if installed,
  ``espeak`` on Linux, and a text-only fallback that prints what it would say.
* **STT** - ``SpeechRecognition`` with the default microphone when installed,
  otherwise typed input, so the interface is still usable headless.
"""

from __future__ import annotations

import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from jarvis.application import Jarvis
from jarvis.core.models import Task


# --------------------------------------------------------------------------- #
# speech out
# --------------------------------------------------------------------------- #
class Synthesizer:
    name = "null"

    def available(self) -> bool:
        return False

    def say(self, text: str) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class WindowsSapiSynthesizer(Synthesizer):
    """Native Windows speech, no third-party dependency."""

    name = "windows-sapi"

    def available(self) -> bool:
        return platform.system() == "Windows"

    def say(self, text: str) -> None:
        script = (
            "Add-Type -AssemblyName System.Speech; "
            "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer; "
            f"$s.Speak([Console]::In.ReadToEnd())"
        )
        try:
            subprocess.run(["powershell", "-NoProfile", "-Command", script],
                           input=text, text=True, timeout=60, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            print(f"[jarvis] {text}")


class Pyttsx3Synthesizer(Synthesizer):
    name = "pyttsx3"

    def __init__(self) -> None:
        self._engine = None

    def available(self) -> bool:
        try:
            import pyttsx3  # noqa: F401

            return True
        except ImportError:
            return False

    def say(self, text: str) -> None:
        import pyttsx3

        if self._engine is None:
            self._engine = pyttsx3.init()
        self._engine.say(text)
        self._engine.runAndWait()


class EspeakSynthesizer(Synthesizer):
    name = "espeak"

    def available(self) -> bool:
        return shutil.which("espeak") is not None or shutil.which("espeak-ng") is not None

    def say(self, text: str) -> None:
        binary = shutil.which("espeak-ng") or shutil.which("espeak")
        try:
            subprocess.run([binary, text], timeout=60, capture_output=True)
        except (OSError, subprocess.SubprocessError):
            print(f"[jarvis] {text}")


class TextSynthesizer(Synthesizer):
    """Prints instead of speaking - used headless and in tests."""

    name = "text"

    def __init__(self) -> None:
        self.spoken: List[str] = []

    def available(self) -> bool:
        return True

    def say(self, text: str) -> None:
        self.spoken.append(text)
        print(f"jarvis> {text}")


def default_synthesizer() -> Synthesizer:
    for candidate in (WindowsSapiSynthesizer(), Pyttsx3Synthesizer(), EspeakSynthesizer()):
        if candidate.available():
            return candidate
    return TextSynthesizer()


# --------------------------------------------------------------------------- #
# speech in
# --------------------------------------------------------------------------- #
class Recognizer:
    name = "text"

    def available(self) -> bool:
        return True

    def listen(self, prompt: str = "you> ") -> str:
        try:
            return input(prompt).strip()
        except (EOFError, KeyboardInterrupt):
            return ""


class MicrophoneRecognizer(Recognizer):
    """Uses the ``SpeechRecognition`` package with the default microphone."""

    name = "microphone"

    def __init__(self, timeout: float = 8.0, language: str = "en-US"):
        self.timeout = timeout
        self.language = language

    def available(self) -> bool:
        try:
            import speech_recognition  # noqa: F401

            return True
        except ImportError:
            return False

    def listen(self, prompt: str = "listening… ") -> str:
        import speech_recognition as sr

        recognizer = sr.Recognizer()
        print(prompt, end="", flush=True)
        try:
            with sr.Microphone() as source:
                audio = recognizer.listen(source, timeout=self.timeout)
            return recognizer.recognize_google(audio, language=self.language).strip()
        except Exception:
            return ""


def default_recognizer() -> Recognizer:
    candidate = MicrophoneRecognizer()
    return candidate if candidate.available() else Recognizer()


# --------------------------------------------------------------------------- #
# interface
# --------------------------------------------------------------------------- #
@dataclass
class VoiceTurn:
    transcript: str
    spoken: str
    task_id: str = ""
    state: str = ""
    verdict: str = ""


class VoiceInterface:
    """One assistant, spoken: the same executive runtime, a different surface."""

    WAKE_WORDS = ("jarvis",)

    def __init__(self, jarvis: Jarvis, synthesizer: Optional[Synthesizer] = None,
                 recognizer: Optional[Recognizer] = None, wake_word: bool = True):
        self.jarvis = jarvis
        self.synth = synthesizer or default_synthesizer()
        self.recognizer = recognizer or default_recognizer()
        self.wake_word = wake_word
        self.history: List[VoiceTurn] = []

    # -- speech helpers ----------------------------------------------------- #
    def speak(self, text: str) -> None:
        self.synth.say(text)

    @staticmethod
    def summarise(task: Task) -> str:
        if task.state.value == "awaiting_approval":
            approvals = ", ".join(a for a in task.approvals)
            return (f"I need your approval before I can continue. "
                    f"The request is waiting as {approvals}.")
        if task.verdict == "verified":
            detail = ""
            if isinstance(task.result, dict):
                answer = task.result.get("answer")
                if isinstance(answer, str) and answer:
                    detail = f" {answer[:240]}"
            return f"Done and verified.{detail}"
        if task.state.value == "blocked":
            return f"I could not complete that. {(task.error or 'no detail')[:240]}"
        return f"Task {task.id} is {task.state.value}, verdict {task.verdict or 'pending'}."

    # -- one turn ----------------------------------------------------------- #
    def process(self, transcript: str) -> VoiceTurn:
        text = (transcript or "").strip()
        if not text:
            return VoiceTurn("", "", state="empty")
        if self.wake_word and self.WAKE_WORDS:
            lowered = text.lower()
            if not any(word in lowered for word in self.WAKE_WORDS):
                # No wake word: still accept it, but say so once in a while.
                pass
            else:
                for word in self.WAKE_WORDS:
                    text = text.lower().replace(word, "", 1).strip(" ,.-")
        lowered = text.lower()
        if lowered in ("stop", "halt", "emergency stop"):
            self.jarvis.halt()
            self.speak("Emergency stop engaged. Nothing will run until you resume.")
            return VoiceTurn(transcript, "Emergency stop engaged.", state="halted")
        if lowered in ("resume", "continue"):
            self.jarvis.resume()
            self.speak("Resuming.")
            return VoiceTurn(transcript, "Resuming.", state="resumed")

        task = self.jarvis.request(text)
        if task.state.value == "awaiting_approval":
            pending = self.jarvis.approvals()
            if pending:
                request = pending[-1]
                question = (f"I need approval to use {request['scope']} "
                            f"({request['risk']} risk) for: {request['action']}. Approve?")
                self.speak(question)
                answer = (self.recognizer.listen("approve? [yes/no] ") or "").lower()
                self.jarvis.approve(request["id"], answer.startswith("y"), note="voice")
                task = self.jarvis.store.get_task(task.id) or task
        spoken = self.summarise(task)
        self.speak(spoken)
        turn = VoiceTurn(transcript=transcript, spoken=spoken, task_id=task.id,
                         state=task.state.value, verdict=task.verdict)
        self.history.append(turn)
        return turn

    # -- loop --------------------------------------------------------------- #
    def run(self, max_turns: Optional[int] = None,
            on_turn: Optional[Callable[[VoiceTurn], None]] = None) -> List[VoiceTurn]:
        self.speak("JARVIS is listening.")
        turns: List[VoiceTurn] = []
        while max_turns is None or len(turns) < max_turns:
            transcript = self.recognizer.listen()
            if not transcript:
                continue
            if transcript.lower() in ("quit", "exit", "goodbye"):
                self.speak("Goodbye.")
                break
            turn = self.process(transcript)
            turns.append(turn)
            if on_turn is not None:
                on_turn(turn)
        return turns


def main(argv: Optional[List[str]] = None) -> int:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(description="JARVIS voice interface")
    parser.add_argument("--text", action="store_true", help="force typed input and printed speech")
    parser.add_argument("--no-wake-word", action="store_true")
    args = parser.parse_args(argv)
    jarvis = Jarvis()
    synth = TextSynthesizer() if args.text else default_synthesizer()
    interface = VoiceInterface(jarvis, synthesizer=synth, wake_word=not args.no_wake_word)
    print(f"speech out: {interface.synth.name} | speech in: {interface.recognizer.name}")
    try:
        interface.run()
    finally:
        jarvis.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
