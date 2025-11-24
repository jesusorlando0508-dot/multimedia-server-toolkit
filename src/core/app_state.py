import queue
import threading

# Central small state module to expose the UI queue for all modules
ui_queue = queue.Queue()


class GenerationControl:
	"""Shared control object for long-running generation tasks.

	- pause_event: when set, generation should pause until cleared.
	- stop_event: when set, generation should abort as soon as possible.
	"""
	def __init__(self):
		self.pause_event = threading.Event()
		self.stop_event = threading.Event()

	def pause(self):
		self.pause_event.set()

	def resume(self):
		self.pause_event.clear()

	def stop(self):
		self.stop_event.set()

	def is_paused(self):
		return self.pause_event.is_set()

	def stop_requested(self):
		return self.stop_event.is_set()

	def wait_if_paused(self, check_interval=0.5):
		# Block while paused, return early if stop requested
		while self.is_paused():
			if self.stop_requested():
				break
			threading.Event().wait(check_interval)


# Single shared instance used across modules
gen_control = GenerationControl()
