"""Test della ricerca della porta di Arduino. Da ArduinoBridge: python -m unittest discover -s tests -t ."""
import os
import select
import tempfile
import threading
import unittest
from types import SimpleNamespace

import protocol
import serial_ports
from serial_ports import ARDUINO, OTHER, USB_SERIAL, VIRTUAL, NodeNotFound, Port, SerialNode


def info(device, description="n/a", hwid="n/a", vid=None):
    """Una porta come la elenca pyserial (serial.tools.list_ports.comports)."""
    return SimpleNamespace(device=device, description=description, hwid=hwid, vid=vid)


class FakeClock:
    """Avanza di 0,1 secondi a ogni lettura, come readline con il timeout della seriale."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class FakeNode:
    """Dispositivo sulla porta: risponde a ID con `answer` dopo `delay` secondi (il riavvio di una scheda USB)."""

    def __init__(self, clock, answer=None, delay=0.0, noise=()):
        self.clock, self.answer, self.delay = clock, answer, delay
        self.noise = list(noise)
        self.queries = 0
        self.pending = []
        self.closed = False

    def write_line(self, line):
        if line == "ID" and self.answer and self.clock() >= self.delay:
            self.pending.append(self.answer)
        self.queries += line == "ID"

    def readline(self):
        self.clock.now += 0.1
        if self.noise:
            return self.noise.pop(0)
        return self.pending.pop(0) if self.pending else None

    def close(self):
        self.closed = True


class BootloaderNode(FakeNode):
    """Una scheda USB appena riavviata dall'apertura della porta, come Arduino Mega: se riceve dati mentre è
    attivo il bootloader resta in attesa di essere programmata e lo sketch non parte più."""

    def __init__(self, clock, boot_time=1.0, announces=True):
        super().__init__(clock, protocol.identity_line())
        self.boot_time, self.announces = boot_time, announces
        self.stuck = self.booted = False

    def write_line(self, line):
        if self.clock() < self.boot_time:
            self.stuck = True
        elif not self.stuck:
            super().write_line(line)

    def readline(self):
        self.clock.now += 0.1
        if self.stuck:
            return None
        if not self.booted and self.clock() >= self.boot_time:
            # Lo sketch parte e si presenta (o, se non lo fa, chiede la configurazione)
            self.booted = True
            return protocol.identity_line() if self.announces else "READY"
        return self.pending.pop(0) if self.pending else None


class ListPortsTests(unittest.TestCase):
    def test_macos_tries_only_usb_boards_and_virtual_ports(self):
        with tempfile.TemporaryDirectory() as tmp:
            virtual = os.path.join(tmp, "solarnode-bridge")
            open(virtual, "w").close()
            ports = serial_ports.list_ports([
                info("/dev/cu.debug-console"),
                info("/dev/cu.Bluetooth-Incoming-Port"),
                info("/dev/cu.STEREO"),  # cuffie Bluetooth: nessun produttore USB
                info("/dev/tty.wchusbserial1410", "USB Serial", vid=0x1A86),
                info("/dev/cu.usbmodem1101", "Arduino Uno", vid=0x2341),
            ], virtual_ports=[virtual, os.path.join(tmp, "missing")], platform="darwin")

        self.assertEqual([(port.device, port.kind) for port in serial_ports.candidates(ports)], [
            ("/dev/cu.usbmodem1101", ARDUINO),
            # /dev/tty.* diventa /dev/cu.*
            ("/dev/cu.wchusbserial1410", USB_SERIAL),
            (virtual, VIRTUAL),
        ])
        self.assertEqual({port.kind for port in ports[3:]}, {OTHER})
        self.assertEqual(ports[0].description, "Arduino Uno")
        self.assertEqual(ports[-1].description, "")

    def test_windows_com0com_pair_in_numeric_order(self):
        ports = serial_ports.list_ports([
            info("COM10", "com0com - serial port emulator (COM10)", "COM0COM\\PORT\\CNCB1"),
            info("COM3", "Standard Serial over Bluetooth link (COM3)", "BTHENUM\\{00001101}"),
            info("COM2", "com0com - serial port emulator (COM2)", "COM0COM\\PORT\\CNCB0"),
            info("COM1", "com0com - serial port emulator (COM1)", "COM0COM\\PORT\\CNCA0"),
        ], virtual_ports=["/tmp/solarnode-bridge"], platform="win32")

        self.assertEqual([port.device for port in serial_ports.candidates(ports)], ["COM1", "COM2", "COM10"])
        self.assertEqual(ports[-1], Port("COM3", OTHER, "Standard Serial over Bluetooth link (COM3)"))

    def test_normalize(self):
        self.assertEqual(serial_ports.normalize("/dev/tty.usbmodem1101", "darwin"), "/dev/cu.usbmodem1101")
        self.assertEqual(serial_ports.normalize("/dev/ttyACM0", "linux"), "/dev/ttyACM0")
        self.assertEqual(serial_ports.normalize("COM2", "win32"), "COM2")


class IdentifyTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()

    def test_asks_again_until_the_board_has_restarted(self):
        node = FakeNode(self.clock, protocol.identity_line(), delay=1.2, noise=["\x00\xff", "READY"])

        identity, _ = serial_ports.identify(node, timeout=3, clock=self.clock)

        self.assertEqual(identity, protocol.Identity("SolarNode", 1))
        self.assertGreaterEqual(node.queries, 3)

    def test_nothing_is_written_while_the_board_restarts(self):
        node = BootloaderNode(self.clock)

        identity, _ = serial_ports.identify(node, timeout=3, clock=self.clock)

        self.assertEqual(identity, protocol.Identity("SolarNode", 1))
        self.assertEqual(node.queries, 0)

    def test_board_that_does_not_introduce_itself_is_asked_once_started(self):
        node = BootloaderNode(self.clock, announces=False)

        identity, _ = serial_ports.identify(node, timeout=3, clock=self.clock)

        self.assertEqual(identity, protocol.Identity("SolarNode", 1))
        self.assertFalse(node.stuck)
        # Ha chiesto appena lo sketch ha inviato READY, senza aspettare la fine di BOOT_WAIT
        self.assertLess(self.clock.now, serial_ports.BOOT_WAIT)

    def test_writing_during_the_bootloader_blocks_the_board(self):
        # Il comportamento di prima: ID subito dopo l'apertura della porta
        node = BootloaderNode(self.clock)

        identity, _ = serial_ports.identify(node, timeout=3, clock=self.clock, boot_wait=0)

        self.assertIsNone(identity)
        self.assertTrue(node.stuck)

    def test_silent_port(self):
        identity, last_line = serial_ports.identify(FakeNode(self.clock), timeout=3, clock=self.clock)

        self.assertEqual((identity, last_line), (None, None))
        self.assertGreaterEqual(self.clock.now, 3)

    def probe(self, node):
        return serial_ports.probe(Port("COM2", VIRTUAL, ""), lambda device: node, timeout=1, clock=self.clock)

    def test_probe_outcomes(self):
        compatible = FakeNode(self.clock, protocol.identity_line())
        node, outcome = self.probe(compatible)
        self.assertIs(node, compatible)
        self.assertFalse(compatible.closed)
        self.assertEqual(outcome, "SolarNode, protocollo 1")

        cases = [
            (FakeNode(self.clock), "nessuna risposta"),
            # Sketch vecchio, senza ID: invia solo le misure
            (FakeNode(self.clock, noise=["23.40|812.0|3420.5"]), "non è un SolarNode aggiornato"),
            (FakeNode(self.clock, "ID|Termostato|3"), "altro dispositivo (Termostato)"),
            (FakeNode(self.clock, "ID|SolarNode|2"), "protocollo 2, mentre il bridge usa il 1"),
        ]
        for fake, expected in cases:
            with self.subTest(expected=expected):
                node, outcome = self.probe(fake)
                self.assertIsNone(node)
                self.assertIn(expected, outcome)
                self.assertTrue(fake.closed)

    def test_busy_port(self):
        windows = Exception("could not open port 'COM1': PermissionError(13, 'Accesso negato.', None, 5)")
        posix = Exception("[Errno 35] Could not exclusively lock port /dev/cu.usbmodem1101")

        self.assertIn("occupata", serial_ports.open_error(windows, "win32"))
        self.assertIn("occupata", serial_ports.open_error(posix, "darwin"))
        # Su Linux PermissionError vuol dire che mancano i permessi, non che la porta è occupata
        self.assertIn("non si può aprire", serial_ports.open_error(PermissionError(13, "Permission denied"), "linux"))

    def test_port_that_cannot_be_opened(self):
        def busy(device):
            raise OSError("Accesso negato")

        node, outcome = serial_ports.probe(Port("COM1", VIRTUAL, ""), busy, timeout=1, clock=self.clock)

        self.assertIsNone(node)
        self.assertIn("non si può aprire (Accesso negato)", outcome)


class FindNodeTests(unittest.TestCase):
    def test_uses_the_first_port_where_a_solar_node_answers(self):
        clock = FakeClock()
        solar_node = FakeNode(clock, protocol.identity_line())
        nodes = {"COM2": FakeNode(clock), "COM3": solar_node, "COM4": FakeNode(clock, protocol.identity_line())}

        def open_node(device):
            if device == "COM1":
                raise OSError("Accesso negato")  # la apre già SimulIDE
            return nodes[device]

        log = []
        node, port = serial_ports.find_node(
            [Port(device, VIRTUAL, "") for device in ("COM1", "COM2", "COM3", "COM4")],
            open_node, timeout=1, clock=clock, log=log.append,
        )

        self.assertIs(node, solar_node)
        self.assertEqual(port.device, "COM3")
        self.assertTrue(nodes["COM2"].closed)
        self.assertEqual(nodes["COM4"].queries, 0)
        self.assertEqual(len(log), 3)

    def test_explains_what_was_tried(self):
        clock = FakeClock()
        with self.assertRaises(NodeNotFound) as error:
            serial_ports.find_node([Port("COM2", VIRTUAL, "")], lambda device: FakeNode(clock), timeout=1,
                                   clock=clock, log=lambda message: None)
        self.assertIn("COM2: nessuna risposta", str(error.exception))
        self.assertIn("SimulIDE", str(error.exception))

        with self.assertRaisesRegex(NodeNotFound, "Nessuna porta seriale"):
            serial_ports.find_node([], log=lambda message: None)


@unittest.skipUnless(hasattr(os, "openpty"), "servono le pseudo-porte (pty) di macOS e Linux")
class RealSerialPortTests(unittest.TestCase):
    """Una vera porta seriale virtuale: dall'altra parte risponde un finto SolarNode, come farebbe SimulIDE."""

    def setUp(self):
        import tty  # solo su macOS e Linux

        master, slave = os.openpty()
        tty.setraw(slave)
        self.path = os.ttyname(slave)
        self.stop = threading.Event()
        thread = threading.Thread(target=self.solar_node, args=(master,), daemon=True)
        thread.start()

        def cleanup():
            self.stop.set()
            thread.join(timeout=1)
            os.close(master)
            os.close(slave)

        self.addCleanup(cleanup)

    def solar_node(self, master):
        buffer = b""
        while not self.stop.is_set():
            if not select.select([master], [], [], 0.05)[0]:
                continue
            buffer += os.read(master, 100)
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                if line.strip() == b"ID":
                    os.write(master, (protocol.identity_line() + "\r\n").encode())

    def test_finds_the_node_and_keeps_the_port_for_itself(self):
        node, port = serial_ports.find_node([Port(self.path, VIRTUAL, "")], timeout=2, log=lambda message: None,
                                            boot_wait=0.2)
        self.addCleanup(node.close)

        self.assertEqual(port.device, self.path)
        # Un secondo bridge non può aprire la stessa porta e rubare i dati al primo
        other, outcome = serial_ports.probe(Port(self.path, VIRTUAL, ""), timeout=1, boot_wait=0.2)
        self.assertIsNone(other)
        self.assertIn("occupata da un altro programma", outcome)

    def test_explicit_port(self):
        node, port = serial_ports.connect(self.path, timeout=2, boot_wait=0.2)
        node.close()
        self.assertEqual(port.device, self.path)

        with self.assertRaisesRegex(NodeNotFound, "usare la porta /dev/does-not-exist: non si può aprire"):
            serial_ports.connect("/dev/does-not-exist", timeout=1, boot_wait=0.2)


class SerialNodeTests(unittest.TestCase):
    def test_lines_arriving_in_pieces(self):
        chunks = [b"D|20.0|8", b"00.0|1000.0\r\n", b"", b"READY\n"]
        node = SerialNode(SimpleNamespace(readline=lambda: chunks.pop(0)))

        self.assertIsNone(node.readline())
        self.assertEqual(node.readline(), "D|20.0|800.0|1000.0")
        self.assertIsNone(node.readline())
        self.assertEqual(node.readline(), "READY")


if __name__ == "__main__":
    unittest.main()
