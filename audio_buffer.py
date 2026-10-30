"""Bounded microphone queue; never replay speech from an earlier listening session."""

import queue
import time


class AudioBuffer:
    def __init__(self, enabled, max_age=0.3):
        self.enabled = enabled
        self.max_age = max_age
        self.queue = queue.Queue(maxsize=6)
        self.generation = 0
        self.active = False
        self.last_generation = None

    def callback(self, indata, frames, time_info, status):
        active = bool(self.enabled())
        if active != self.active or status:
            self.generation += 1
        self.active = active
        item = (time.monotonic(), self.generation, active, bytes(indata))
        try:
            self.queue.put_nowait(item)
        except queue.Full:
            try:
                self.queue.get_nowait()
            except queue.Empty:
                pass
            self.queue.put_nowait(item)

    def get(self):
        stamp, generation, active, chunk = self.queue.get()
        reset = generation != self.last_generation
        self.last_generation = generation
        if time.monotonic() - stamp > self.max_age:
            return None, True
        if not active or not self.enabled():
            return None, True
        return chunk, reset
