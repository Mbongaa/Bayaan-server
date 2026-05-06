import asyncio
import unittest

from resource_management import ResourceManager, STTStreamManager


class FakeStream:
    def __init__(self):
        self.closed = False
        self.close_count = 0

    async def aclose(self):
        self.closed = True
        self.close_count += 1


class ResourceManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_context_entry_starts_and_shutdown_stops_heartbeat_monitor(self):
        manager = ResourceManager()

        await manager.__aenter__()
        monitor_task = manager.heartbeat_monitor._monitor_task

        self.assertIsNotNone(monitor_task)
        self.assertFalse(monitor_task.done())

        await manager.shutdown()

        self.assertTrue(monitor_task.done())
        self.assertEqual(manager.heartbeat_monitor.participants, {})


class STTStreamManagerTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_participant_stream_with_track_id_closes_only_matching_stream(self):
        manager = STTStreamManager()
        stream_one = FakeStream()
        stream_two = FakeStream()

        manager._streams.update({stream_one, stream_two})
        manager._participant_streams[("participant-a", "track-1")] = stream_one
        manager._participant_streams[("participant-a", "track-2")] = stream_two
        manager._stream_metadata[stream_one] = {"participant_id": "participant-a", "track_id": "track-1"}
        manager._stream_metadata[stream_two] = {"participant_id": "participant-a", "track_id": "track-2"}
        manager._stats.active_streams = 2

        await manager.close_participant_stream("participant-a", "track-1")

        self.assertTrue(stream_one.closed)
        self.assertFalse(stream_two.closed)
        self.assertNotIn(("participant-a", "track-1"), manager._participant_streams)
        self.assertIn(("participant-a", "track-2"), manager._participant_streams)
        self.assertEqual(manager.get_stats().active_streams, 1)

        await manager.close_participant_stream("participant-a")

        self.assertTrue(stream_two.closed)
        self.assertEqual(manager._participant_streams, {})
        self.assertEqual(manager.get_stats().active_streams, 0)


if __name__ == "__main__":
    unittest.main()
