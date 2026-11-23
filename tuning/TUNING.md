# Abstimmung, 23.11.2026

Daten: 47 liquide US-Aktien (tuning/universe.txt), 60 Handelstage 5-Min-Kerzen (28.08.–20.11.2026, Yahoo).
Realistisch gerechnet: Einstieg zum Open der Kerze NACH dem Signal (Market-Order nach Kerzenschluss),
Stop/Ziel wie trader-exit sie setzt, Ausstieg spätestens 15:55. Ergebnis in R (1R = Risiko pro Aktie).

## 1. Die Regel allein (backtest.py, 384 Varianten, In-Sample bis 09.10. / Out-of-Sample ab 12.10.)

| Variante | In-Sample | Out-of-Sample |
| --- | --- | --- |
| Paket-Standard (OR 15 min, Stop Range-Mitte, Ziel 1R, Einstieg bis 12:25) | 617 Trades, Ø −0,069 R | 649 Trades, Ø −0,106 R |
| bestes In-Sample (OR 30, 3R, Volumen ≥ 1×) | 62 Trades, Ø −0,009 R | 44 Trades, Ø +0,270 R |

**Keine Variante war In-Sample positiv.** Ein reiner Opening-Range-Breakout hatte in diesen 60 Tagen
keinen Vorteil, egal wie Stop, Ziel, Zeitfenster oder Filter gesetzt waren.

## 2. Time Machine als Filter (gate_eval.py, walk-forward, nur Daten VOR dem Handelstag)

| Variante | ohne Filter | mit Time-Machine-Filter (Edge ≥ 0, Ø R ≥ 0) |
| --- | --- | --- |
| A Paket-Standard 1R | 848 Trades, Ø −0,104 R | (Edge ≥ 5) 157, Ø −0,070 R |
| B 1,5R, bis 11:25, ≤ 0,75 % über Range | 762, Ø −0,089 R | 216, Ø −0,073 R |
| C 2R, bis 11:25, ≤ 0,75 % | 762, Ø −0,114 R | 117, Ø +0,008 R |
| **E = C + Gap-Filter (Eröffnung ≥ Vortagesschluss)** | 371, Ø −0,031 R | **49, Ø +0,282 R** |
| D OR 30, 2R, Volumen ≥ 1× | 66, Ø +0,155 R | (Historie ≥ 0) 27, Ø +0,586 R |

Der Filter „Historie der Regel in den letzten 60 Tagen“ hat bei A–C geschadet, daher ist er abgeschaltet
(`tm_hist_min_avg_R: null`). Die Time-Machine-Edge hat in jeder Variante geholfen.

## Gewählt: Variante E

OR 15 min, Stop Range-Mitte, Ziel 2R, Signale bis 11:30, höchstens 0,75 % über der Range, nur an Tagen
mit Eröffnung ≥ Vortagesschluss, Time Machine Edge ≥ 0 Punkte und Ø R ≥ 0, max. 3 Trades und 2 offene
Positionen am Tag, $50 Risiko pro Trade, Stopp neuer Einstiege nach −$150 realisiert.

E statt D, weil D nur 66 Signale in 60 Tagen hatte (≈1 pro Tag über 47 Aktien) und das Volumenkriterium
dort fast alles entscheidet; E ist näher am Paket und hat mehr Signale.

## Ehrlich

49 gefilterte Trades sind eine kleine Stichprobe (Standardfehler grob ±0,2 R). +0,28 R kann Glück sein.
Erwartung: ungefähr Nullsumme bis leicht positiv, mit Gebühren/Slippage eher weniger. Genau dafür ist
Paper da: mindestens 1–2 Monate laufen lassen, dann `python3 tuning/*.py` erneut ausführen und
mit den echten Paper-Trades im Dashboard vergleichen.
