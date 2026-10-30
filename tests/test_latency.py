import json
import unittest
from unittest.mock import Mock, patch

from audio_buffer import AudioBuffer
import invoker_voice as invoker
import lina_voice as lina


class AudioTests(unittest.TestCase):
    def test_queue_is_bounded_and_keeps_latest(self):
        audio = AudioBuffer(lambda: True)
        for i in range(20):
            audio.callback(bytes([i]), 1, None, None)
        self.assertEqual(audio.queue.qsize(), 6)
        self.assertEqual(audio.get()[0], bytes([14]))

    def test_disabled_audio_cannot_replay_after_enable(self):
        enabled = [False]
        audio = AudioBuffer(lambda: enabled[0])
        audio.callback(b'old', 1, None, None)
        enabled[0] = True
        audio.callback(b'new', 1, None, None)
        self.assertIsNone(audio.get()[0])
        self.assertEqual(audio.get(), (b'new', True))

    def test_stale_audio_resets_recognition(self):
        audio = AudioBuffer(lambda: True)
        with patch('audio_buffer.time.monotonic', return_value=1):
            audio.callback(b'old', 1, None, None)
        with patch('audio_buffer.time.monotonic', return_value=2):
            self.assertEqual(audio.get(), (None, True))


class ListenerTests(unittest.TestCase):
    def test_invoker_partial_commands_execute_once(self):
        recognizer = Mock()
        recognizer.AcceptWaveform.return_value = False
        phrases = [invoker.SPELLS[n][1][0] for n in ('Tornado', 'Chaos Meteor')]
        words = ' '.join(phrases).split()
        recognizer.PartialResult.return_value = json.dumps({'partial': ' '.join(words)})
        listener = invoker.Listener(recognizer, None)
        with patch.object(invoker, 'execute') as execute:
            for _ in range(20):
                listener.feed(b'chunk')
            listener._final(json.dumps({'text': ' '.join(words), 'result': [
                {'word': w, 'conf': 1.0} for w in words]}))
            self.assertEqual([c.args[0] for c in execute.call_args_list], phrases)

    def test_reset_cancels_pending_commands(self):
        for module in (invoker, lina):
            recognizer = Mock()
            listener = module.Listener(recognizer, None) if module is invoker else module.Listener(recognizer)
            listener.partial, listener.stable = 'pending', 1.0
            listener.reset()
            recognizer.Reset.assert_called_once()
            recognizer.FinalResult.assert_not_called()
            self.assertEqual((listener.partial, listener.stable), ('', 0.0))


if __name__ == '__main__':
    unittest.main()
