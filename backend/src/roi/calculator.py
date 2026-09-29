"""Ritorno dell'investimento (ROI) di un impianto fotovoltaico, anno per anno lungo la sua vita utile."""
from dataclasses import dataclass


@dataclass(frozen=True)
class Assumptions:
    """Ipotesi della simulazione: hanno valori tipici, il consulente può modificarle."""

    # Quota della produzione consumata subito in casa: senza batterie è in genere tra il 30% e il 40%
    self_consumption_share: float = 0.35
    # €/kWh riconosciuti per l'energia immessa in rete (ritiro dedicato)
    export_price: float = 0.10
    # Perdita di produzione dei pannelli ogni anno
    annual_degradation: float = 0.005
    lifetime_years: int = 25

    @property
    def self_consumption_percent(self):
        return self.self_consumption_share * 100

    @property
    def degradation_percent(self):
        return self.annual_degradation * 100


@dataclass(frozen=True)
class YearResult:
    year: int
    production_kwh: float
    self_consumed_kwh: float
    exported_kwh: float
    savings: float  # € risparmiati in bolletta
    revenue: float  # € ricavati dall'energia immessa in rete
    cumulative_cash_flow: float  # € guadagnati fino a quest'anno, al netto del costo dell'impianto

    @property
    def benefit(self):
        return self.savings + self.revenue


@dataclass(frozen=True)
class RoiResult:
    system_cost: float
    annual_consumption_kwh: float
    years: list  # un YearResult per ogni anno di vita utile
    payback_years: float | None  # anni per rientrare del costo, None se non si rientra

    @property
    def first_year(self):
        return self.years[0]

    @property
    def consumption_coverage(self):
        """Quota dei consumi coperta dall'autoconsumo nel primo anno."""
        if not self.annual_consumption_kwh:
            return None
        return self.first_year.self_consumed_kwh / self.annual_consumption_kwh

    @property
    def lifetime_benefit(self):
        return sum(year.benefit for year in self.years)

    @property
    def net_gain(self):
        return self.lifetime_benefit - self.system_cost

    @property
    def roi(self):
        """Guadagno netto sulla vita utile in rapporto al costo dell'impianto."""
        return self.net_gain / self.system_cost


def calculate_roi(annual_production_kwh, annual_consumption_kwh, system_cost, energy_price, assumptions=Assumptions()):
    """Flussi di cassa anno per anno, tempo di rientro e ROI dell'impianto.

    annual_production_kwh è la produzione del primo anno; negli anni successivi cala del degrado annuo.
    """
    years = []
    cumulative = -system_cost
    payback = None
    for year in range(1, assumptions.lifetime_years + 1):
        production = annual_production_kwh * (1 - assumptions.annual_degradation) ** (year - 1)
        # In casa si consuma solo una parte di ciò che si produce, e mai più del proprio fabbisogno
        self_consumed = min(production * assumptions.self_consumption_share, annual_consumption_kwh)
        exported = production - self_consumed
        savings = self_consumed * energy_price
        revenue = exported * assumptions.export_price
        benefit = savings + revenue

        if payback is None and benefit > 0 and cumulative + benefit >= 0:
            # Rientro durante l'anno: la frazione d'anno necessaria a coprire il residuo
            payback = year - 1 + (-cumulative) / benefit
        cumulative += benefit
        years.append(YearResult(year, production, self_consumed, exported, savings, revenue, cumulative))

    return RoiResult(
        system_cost=system_cost, annual_consumption_kwh=annual_consumption_kwh, years=years, payback_years=payback
    )
