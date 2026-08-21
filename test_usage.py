import json
import unittest

from app import UsageTracker, _cache_hit_rate


class UsageTrackerTest(unittest.TestCase):
    def test_anthropic_sse_across_chunk_boundaries(self):
        tracker = UsageTracker("text/event-stream; charset=utf-8")
        events = (
            'event: message_start\n'
            'data: {"type":"message_start","message":{"usage":'
            '{"input_tokens":10,"cache_creation_input_tokens":20,'
            '"cache_read_input_tokens":70}}}\n\n'
            'event: message_delta\n'
            'data: {"type":"message_delta","usage":{"output_tokens":15}}\n\n'
        ).encode()
        for boundary in (events[:17], events[17:91], events[91:]):
            tracker.feed(boundary)

        usage = tracker.finish()
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 15)
        self.assertEqual(_cache_hit_rate(usage), 0.7)

    def test_openai_non_streaming_usage(self):
        tracker = UsageTracker("application/json")
        payload = {"usage": {"prompt_tokens": 100,
                             "prompt_tokens_details": {"cached_tokens": 80},
                             "completion_tokens": 12}}
        raw = json.dumps(payload).encode()
        tracker.feed(raw[:10])
        tracker.feed(raw[10:])

        usage = tracker.finish()
        self.assertEqual(usage["completion_tokens"], 12)
        self.assertEqual(_cache_hit_rate(usage), 0.8)


if __name__ == "__main__":
    unittest.main()
