"""Arduino simulato: si comporta come SolarNode.ino, senza porta seriale né SimulIDE.

Serve a provare il bridge e il server con un impianto qualsiasi: misure plausibili per l'ora del giorno
(il sole sale dalle 6 alle 12 e scende fino alle 18), stessa formula della potenza e stessa pompa di lavaggio.
"""
import math
import random
import time
from collections import deque
from datetime import datetime

import protocol

READING_INTERVAL = 1.5  # secondi tra due misure, come il delay del DHT11 nello sketch
READY_INTERVAL = 2.0
CLEANING_SECONDS = 10

# Stessi parametri dello sketch
REFERENCE_TEMPERATURE = 25.0
TEMPERATURE_COEFFICIENT = -0.004  # -0,4% di potenza per °C sopra i 25 °C
MAX_LUX = 100000.0


class SimulatedNode:
    def __init__(self, clock=time.monotonic, local_time=datetime.now, rng=None, sleep=time.sleep):
        self.clock = clock
        self.sleep = sleep
        self.local_time = local_time
        self.rng = rng or random.Random()
        self.max_power_w = 0
        self.status = None
        self.pump_until = None
        self.outbox = deque()
        self.next_ready = self.next_reading = clock()

    @property
    def configured(self):
        return self.max_power_w > 0

    @property
    def pump_running(self):
        return self.pump_until is not None

    def write_line(self, line):
        """Comando dal bridge."""
        command = protocol.parse_command(line)
        if command is None:
            return
        kind, values = command
        if kind == protocol.IDENTIFY:
            self.outbox.append(protocol.identity_line())
        elif kind == protocol.CONFIG:
            self.max_power_w = values[0]
            self.outbox.append(f"{protocol.ACK}|{protocol.CONFIG}|{self.max_power_w}")
        elif kind == protocol.STATUS:
            self.status, new = values
            if self.status == "DRT" and new:
                self.start_pump()
            elif self.status != "DRT":
                self.stop_pump()

    def readline(self):
        """Prossima riga per il bridge, o None se per ora non c'è niente (aspetta un attimo, come la seriale)."""
        self.tick()
        if self.outbox:
            return self.outbox.popleft()
        self.sleep(0.05)
        return None

    def close(self):
        pass

    def tick(self):
        now = self.clock()
        if self.pump_running and now >= self.pump_until:
            self.stop_pump()
        if not self.configured:
            if now >= self.next_ready:
                self.outbox.append(protocol.READY)
                self.next_ready = now + READY_INTERVAL
        elif now >= self.next_reading:
            self.outbox.append(self.reading_line())
            self.next_reading = now + READING_INTERVAL

    def start_pump(self):
        if not self.pump_running:
            self.outbox.append(f"{protocol.PUMP}|ON")
        self.pump_until = self.clock() + CLEANING_SECONDS

    def stop_pump(self):
        if self.pump_running:
            self.pump_until = None
            self.outbox.append(f"{protocol.PUMP}|OFF")

    def reading_line(self):
        now = self.local_time()
        hours = now.hour + now.minute / 60 + now.second / 3600
        sun = max(0.0, math.sin(math.pi * (hours - 6) / 12))
        light = min(1.0, max(0.0, sun * self.rng.uniform(0.85, 0.95)))
        temperature = 12 + 14 * sun + self.rng.gauss(0, 0.3)
        lux = light * MAX_LUX
        power = light * self.max_power_w * (1 + (temperature - REFERENCE_TEMPERATURE) * TEMPERATURE_COEFFICIENT)
        return f"{protocol.DATA}|{temperature:.2f}|{lux:.1f}|{max(power, 0.0):.1f}"
