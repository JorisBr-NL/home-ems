# Home EMS

Eigen energiemanagementsysteem voor Home Assistant: stuurt (straks) de Quatt-warmtepomp via tado, de Quatt Chill, witgoed via slimme stekkers, de Alfen-laadpaal en later een thuisbatterij, op basis van zonne-opwek en dynamische prijzen.

Het ontwerp staat in Obsidian (`Projects/Energie/EMS ontwerp.md`); de analyse waarop de standaardwaarden zijn gebaseerd in `Projects/Energie/Analyse runs en flowmeter.md`.

## Status: versie 0.1 – observatiemodus

Home EMS leest sensoren uit en laat zien wat het **zou** doen. Het schrijft niets naar apparaten; je bestaande automatiseringen blijven leidend.

| Entiteit | Betekenis |
|---|---|
| `sensor.home_ems_advice` | `run`, `keep_running`, `running`, `wait` of `unknown`, met de reden als attribuut |
| `binary_sensor.home_ems_heat_pump_running` | warmtepomp draait (vermogen > 100 W) |
| `binary_sensor.home_ems_peak_window` | binnen het avondvenster 17:30–20:00 |
| `sensor.home_ems_floor_margin` | flowmetertemperatuur min woonkamertemperatuur (K) |
| `sensor.home_ems_pv_surplus` | teruglevering op dit moment (W) |
| `sensor.home_ems_runs_today` / `short_runs_today` | aantal runs en runs korter dan 30 min vandaag |

Advieslogica (v0.1): buiten het avondvenster een run als de vloermarge onder 0,5 K komt, of bij ≥ 700 W PV-overschot zolang de vloermarge onder 3 K ligt; een lopende run korter dan 30 min hoort door te lopen.

## Installeren via HACS

1. HACS → Integraties → menu → *Custom repositories* → URL van deze repo, categorie *Integration*.
2. *Home EMS* installeren en Home Assistant herstarten.
3. Instellingen → Apparaten & diensten → *Integratie toevoegen* → *Home EMS*, en de entiteiten kiezen.

## Ontwikkelen

```bash
python -m pip install -r requirements_test.txt
python -m pytest
```

De rekenlogica staat in `custom_components/home_ems/model.py` en heeft geen Home Assistant nodig, zodat die los te testen is.
