"""以真實終端機與系統訊號驗證退出流程。"""

import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import tempfile
import time
import unittest

if sys.platform.startswith("linux"):
    import fcntl
    import pty
    import termios


@unittest.skipUnless(sys.platform.startswith("linux"), "uses Linux PTY and process signals")
class CliExitTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="sysmon-cli-test-")
        self.addCleanup(directory.cleanup)
        self.environment = {**os.environ, "XDG_CONFIG_HOME": directory.name, "TERM": "xterm-256color"}
        self.environment.pop("TEXTUAL_ALLOW_SIGNALS", None)
        self.command = [sys.executable, "-m", "sysmon"]
        self.directory = Path(__file__).resolve().parents[1]

    @staticmethod
    def stop_child(process):
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)

    def read_until(self, descriptor, marker, timeout=8):
        output = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if select.select([descriptor], [], [], 0.05)[0]:
                output.extend(os.read(descriptor, 65536))
                if marker in output:
                    return bytes(output)
        self.fail(f"Terminal did not show {marker!r}; last output: {bytes(output[-500:])!r}")

    def test_ctrl_c_quits_tui_and_focused_search_and_restores_terminal(self):
        for search in (False, True):
            with self.subTest(search_focused=search):
                master, slave = pty.openpty()
                try:
                    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 120, 0, 0))
                    initial_attributes = termios.tcgetattr(slave)
                    process = subprocess.Popen(
                        self.command, cwd=self.directory, env=self.environment,
                        stdin=slave, stdout=slave, stderr=slave, start_new_session=True,
                    )
                    try:
                        output = self.read_until(master, b"CPU")
                        if search:
                            os.write(master, b"/")
                            time.sleep(0.15)
                            os.write(master, b"\x1b[200~quit-probe\x1b[201~")
                            output += self.read_until(master, b"quit-probe")
                            self.assertIsNone(process.poll())
                        os.write(master, b"\x03")
                        output += self.read_until(master, b"\x1b[?1049l", timeout=5)
                        self.assertEqual(process.wait(timeout=5), 0)
                        while select.select([master], [], [], 0)[0]:
                            output += os.read(master, 65536)
                        self.assertNotIn(b"Traceback", output)
                        self.assertNotIn(b"Do you want to quit?", output)
                        self.assertEqual(termios.tcgetattr(slave), initial_attributes)
                    finally:
                        self.stop_child(process)
                finally:
                    os.close(master)
                    os.close(slave)

    def test_sigint_stops_json_sampling_and_long_single_sample_without_traceback(self):
        for args in (("--json", "-n", "1000", "-i", "0.2"), ("--once", "-i", "10")):
            with self.subTest(args=args):
                process = subprocess.Popen(
                    [*self.command, *args], cwd=self.directory, env=self.environment,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                try:
                    if "--json" in args:
                        self.assertTrue(select.select([process.stdout], [], [], 8)[0])
                        self.assertTrue(process.stdout.readline().startswith(b'{"timestamp":'))
                    else:
                        deadline = time.monotonic() + 8
                        while time.monotonic() < deadline and process.poll() is None:
                            if "nanosleep" in Path(f"/proc/{process.pid}/wchan").read_text():
                                break
                            time.sleep(0.02)
                        else:
                            self.fail("Single-sample command did not reach the sampling wait")
                    process.send_signal(signal.SIGINT)
                    output, errors = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 130, errors.decode(errors="replace"))
                    self.assertNotIn(b"Traceback", output + errors)
                    self.assertEqual(errors, b"")
                finally:
                    self.stop_child(process)
                    process.stdout.close()
                    process.stderr.close()


if __name__ == "__main__":
    unittest.main()
