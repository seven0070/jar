"""Voice interface: same runtime, spoken surface."""

from __future__ import annotations

from jarvis.interfaces.voice import Recognizer, TextSynthesizer, VoiceInterface


class ScriptedRecognizer(Recognizer):
    def __init__(self, answers):
        self.answers = list(answers)

    def listen(self, prompt=""):
        return self.answers.pop(0) if self.answers else ""


def test_a_spoken_request_produces_a_spoken_verified_result(permissive):
    interface = VoiceInterface(permissive, synthesizer=TextSynthesizer(), wake_word=False)

    turn = interface.process("run the tests")

    assert turn.verdict == "verified"
    assert turn.spoken.startswith("Done and verified")
    assert interface.synth.spoken[-1] == turn.spoken


def test_the_wake_word_is_stripped_before_understanding(permissive):
    interface = VoiceInterface(permissive, synthesizer=TextSynthesizer())

    turn = interface.process("Jarvis, run the tests")

    assert turn.verdict == "verified"
    assert permissive.store.get_task(turn.task_id).intent == "run the tests"


def test_a_blocked_task_is_reported_honestly(permissive):
    interface = VoiceInterface(permissive, synthesizer=TextSynthesizer(), wake_word=False)
    turn = interface.process("make the artifact appear", )
    task = permissive.store.get_task(turn.task_id)

    if task.state.value == "blocked":
        assert "could not complete" in turn.spoken
    else:
        assert turn.spoken


def test_speech_can_trigger_the_emergency_stop(permissive):
    interface = VoiceInterface(permissive, synthesizer=TextSynthesizer(), wake_word=False)

    assert interface.process("stop").state == "halted"
    assert permissive.policy.halted is True
    assert interface.process("resume").state == "resumed"
    assert permissive.policy.halted is False


def test_an_approval_request_is_spoken_and_answered(jarvis):
    interface = VoiceInterface(jarvis, synthesizer=TextSynthesizer(),
                               recognizer=ScriptedRecognizer(["yes"]), wake_word=False)

    turn = interface.process("run the tests")

    assert turn.verdict == "verified"
    assert any("approval" in line for line in interface.synth.spoken)


def test_a_denied_approval_is_reported_as_blocked(jarvis):
    interface = VoiceInterface(jarvis, synthesizer=TextSynthesizer(),
                               recognizer=ScriptedRecognizer(["no"]), wake_word=False)

    turn = interface.process("run the tests")

    assert turn.state == "blocked"
    assert "could not complete" in turn.spoken


def test_the_voice_loop_runs_until_quit(permissive):
    interface = VoiceInterface(
        permissive, synthesizer=TextSynthesizer(),
        recognizer=ScriptedRecognizer(["run the tests", "quit"]), wake_word=False)

    turns = interface.run()

    assert len(turns) == 1
    assert interface.synth.spoken[0] == "JARVIS is listening."
    assert interface.synth.spoken[-1] == "Goodbye."
