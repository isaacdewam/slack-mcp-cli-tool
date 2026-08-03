import unittest

import slack_claude_runner as runner


class RunnerHelpersTest(unittest.TestCase):
    def test_parse_json_lines_separates_json_and_noise(self) -> None:
        events, diagnostics = runner.parse_json_lines(
            '{"type":"message","ok":true}\n'
            "not json\n"
            '{"type":"result","subtype":"success"}\n'
        )

        self.assertEqual(len(events), 2)
        self.assertEqual(events[0]["type"], "message")
        self.assertEqual(events[1]["type"], "result")
        self.assertEqual(diagnostics, ["not json"])

    def test_timestamp_sort_key_orders_fractional_timestamps(self) -> None:
        values = ["100.9", "100.12", "99.5", "101"]

        self.assertEqual(sorted(values, key=runner.timestamp_sort_key), ["99.5", "100.9", "100.12", "101"])

    def test_trigger_task_trims_command_prefix(self) -> None:
        trigger = runner.Trigger(
            channel_id="D123",
            message_ts="1.0",
            thread_ts="1.0",
            text="  !run:   add tests  ",
        )

        self.assertEqual(trigger.task, "add tests")


if __name__ == "__main__":
    unittest.main()
