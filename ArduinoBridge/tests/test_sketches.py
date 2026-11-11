"""Controlli sugli sketch: il protocollo LoRa è lo stesso per il master e per i nodi sui tetti."""
import unittest
from pathlib import Path

SKETCHES = Path(__file__).resolve().parent.parent


class LoRaProtocolTests(unittest.TestCase):
    def test_master_and_roof_node_use_the_same_protocol(self):
        # Arduino IDE compila solo i file nella cartella dello sketch: il protocollo è copiato in entrambi
        master = (SKETCHES / "MasterNode" / "ProtocolloLora.h").read_bytes()
        roof = (SKETCHES / "RoofNode" / "ProtocolloLora.h").read_bytes()
        self.assertEqual(master, roof, "MasterNode/ProtocolloLora.h e RoofNode/ProtocolloLora.h sono diversi: copia "
                                       "quello modificato nell'altro sketch")


if __name__ == "__main__":
    unittest.main()
